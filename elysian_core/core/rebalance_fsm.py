"""
Rebalance cycle state machine.

Replaces ``while True`` + ``asyncio.sleep()`` loops in strategies with an
explicit FSM that drives the **compute -> validate -> execute -> cooldown**
pipeline.  Each tick transitions through the states; the strategy only needs
to implement ``_compute_weights()`` and configure a timer interval.

Integrates with the EventBus to publish :class:`RebalanceCycleEvent` on
every state transition for observability.

Usage (inside a SpotStrategy subclass)::

    # In start():
    self._rebalance_fsm = RebalanceFSM(
        strategy=self,
        optimizer=self._optimizer,
        execution_engine=self._execution_engine,
        event_bus=self._event_bus,
        cooldown_s=60.0,
    )

    # In run_forever():
    await asyncio.sleep(20)          # initial warmup
    self._rebalance_fsm.start_timer(interval_s=60.0)
    await self._rebalance_fsm.wait()  # block until stopped
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING
import elysian_core.utils.logger as log

from elysian_core.core.enums import RebalanceState
from elysian_core.core.fsm import BaseFSM
from elysian_core.core.events import EventType, RebalanceCycleEvent, RebalanceCompleteEvent
from elysian_core.core.signals import RebalanceResult, TargetWeights

from elysian_core.core.event_bus import EventBus
from elysian_core.execution.engine import ExecutionEngine
from elysian_core.risk.optimizer import PortfolioOptimizer

logger = log.setup_custom_logger("root")


class RebalanceFSM(BaseFSM):
    """Drives the rebalance cycle: compute -> validate -> execute -> cooldown.

    Parameters
    ----------
    strategy:
        The strategy instance. Must implement ``_compute_weights() -> dict``.
    optimizer:
        Stage 4 risk optimizer.
    execution_engine:
        Stage 5 execution engine.
    event_bus:
        For publishing RebalanceCycleEvent notifications.
    cooldown_s:
        Seconds to wait after a rebalance before allowing the next one.
    name:
        Label for logging.
    """

    _TRANSITIONS = {
        # Normal cycle
        (RebalanceState.IDLE,       "timer_tick"):    (RebalanceState.COMPUTING,  "_on_enter_computing"),
        (RebalanceState.IDLE,       "signal"):        (RebalanceState.COMPUTING,  "_on_enter_computing"),
        (RebalanceState.COMPUTING,  "weights_ready"): (RebalanceState.VALIDATING, "_on_enter_validating"),
        (RebalanceState.COMPUTING,  "no_signal"):     (RebalanceState.IDLE,       None),
        (RebalanceState.VALIDATING, "accepted"):      (RebalanceState.EXECUTING,  "_on_enter_executing"),
        (RebalanceState.VALIDATING, "rejected"):      (RebalanceState.COOLDOWN,   "_on_enter_cooldown"),
        (RebalanceState.EXECUTING,  "complete"):      (RebalanceState.COOLDOWN,   "_on_enter_cooldown"),
        (RebalanceState.EXECUTING,  "failed"):        (RebalanceState.ERROR,      "_on_enter_error"),
        (RebalanceState.COOLDOWN,   "cooldown_done"): (RebalanceState.IDLE,       None),
        (RebalanceState.ERROR,      "retry"):         (RebalanceState.IDLE,       None),
        # Suspend / resume from any non-terminal state
        ("*",                       "suspend"):       (RebalanceState.SUSPENDED,  "_on_enter_suspended"),
        (RebalanceState.SUSPENDED,  "resume"):        (RebalanceState.IDLE,       "_on_resume"),
    }

    def __init__(
        self,
        strategy,
        optimizer: "PortfolioOptimizer",
        execution_engine: "ExecutionEngine",
        event_bus: Optional["EventBus"] = None,
        cooldown_s: float = 0.0,  # Default no cooldown
        name: str = "RebalanceFSM",
    ):
        super().__init__(
            initial_state=RebalanceState.IDLE,
            event_bus=event_bus,
            name=name,
        )
        self._strategy = strategy
        self._optimizer = optimizer
        self._execution_engine = execution_engine
        self._cooldown_s = cooldown_s

        # Internal bookkeeping
        self._timer_handle: Optional[asyncio.TimerHandle] = None
        self._timer_interval: Optional[float] = None
        self._stopped = asyncio.Event()
        self._last_result: Optional[RebalanceResult] = None

        # Temporary storage passed between transition callbacks
        self._pending_weights: Optional[Dict[str, float]] = None
        self._pending_validated = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def request(self) -> bool:
        """Request a rebalance cycle. Safe to call at any time.

        Returns ``True`` if the cycle was triggered (FSM was IDLE).
        Returns ``False`` if the FSM is busy (mid-cycle, cooldown,
        suspended, error) — the request is silently dropped.
        """
        if self._state != RebalanceState.IDLE:
            logger.debug(
                "[%s] Rebalance request skipped — currently in %s",
                self._name, self._state.name,
            )
            return False
        await self.trigger("signal")
        return True

    # ── Timer management ──────────────────────────────────────────────────────

    def start_timer(self, interval_s: float) -> None:
        """Begin periodic rebalance ticks at *interval_s* intervals."""
        self._timer_interval = interval_s
        self._schedule_tick()
        logger.info("[%s] Timer started (every %.1fs)", self._name, interval_s)

    def stop_timer(self) -> None:
        """Stop the periodic timer and signal ``wait()`` to return."""
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        self._timer_interval = None
        self._stopped.set()
        logger.info("[%s] Timer stopped", self._name)

    async def wait(self) -> None:
        """Block until :meth:`stop_timer` is called. Use in ``run_forever``."""
        await self._stopped.wait()

    def _schedule_tick(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_interval is None:
            return
        loop = asyncio.get_event_loop()
        self._timer_handle = loop.call_later(
            self._timer_interval, self._fire_tick,
        )

    def _fire_tick(self) -> None:
        """Callback from ``call_later`` — create a task to run the trigger."""
        if self._state == RebalanceState.IDLE:
            asyncio.ensure_future(self._tick())
        else:
            # Not idle (still executing a previous cycle) — skip this tick.
            logger.debug(
                "[%s] Skipping tick — still in %s", self._name, self._state.name,
            )
            self._schedule_tick()

    async def _tick(self) -> None:
        """Run one full rebalance cycle via trigger chain."""
        try:
            await self.trigger("timer_tick")
        except Exception as e:
            logger.error("[%s] Tick error: %s", self._name, e, exc_info=True)

    # ── Transition callbacks ──────────────────────────────────────────────────

    async def _on_enter_computing(self, **ctx) -> None:
        """Compute target weights by calling the strategy's compute_weights().

        The strategy must implement ``compute_weights()`` (public method).
        It can be sync or async:

        - **sync**: return a ``dict[str, float]`` of target weights
        - **async**: ``await`` feeds, exchanges, or heavy compute, then return weights

        Return an empty dict or ``None`` to skip this cycle (transitions to IDLE).
        """
        await self._publish_cycle_event("timer_tick")   #### why is this necessary?
        try:
            result = self._strategy.compute_weights(**ctx)   # Pass the **ctx so that the function has context to compute weights from? 
            if asyncio.iscoroutine(result):
                weights = await result
            else:
                weights = result
        except Exception as e:
            logger.error("[%s] compute_weights error: %s", self._name, e, exc_info=True)
            await self.trigger("no_signal", **ctx)
            return

        if not weights:
            logger.debug("[%s] No weights returned — returning to IDLE", self._name)
            await self.trigger("no_signal", **ctx)
            return

        self._pending_weights = weights
        logger.info("[%s] Weights computed: %d symbols", self._name, len(weights))
        await self.trigger("weights_ready", **ctx)

    async def _on_enter_validating(self, **ctx) -> None:
        """Run Stage 4 risk validation. Transitioned from on_enter_computing to validating the weights here"""
        
        await self._publish_cycle_event("weights_ready")

        target = TargetWeights(
            weights=self._pending_weights,
            timestamp=int(time.time() * 1000),
            metadata={},
        )

        try:
            validated = self._optimizer.validate(target, **ctx)   ### New updated context to be reutrned as well? 
        except Exception as e:
            logger.error("[%s] optimizer.validate error: %s", self._name, e, exc_info=True)
            await self.trigger("rejected", **ctx)
            return

        if validated.rejected:
            logger.warning(
                "[%s] Weights REJECTED: %s", self._name, validated.rejection_reason,
            )
            await self.trigger("rejected", **ctx)
            return

        self._pending_validated = validated
        logger.info(
            "[%s] Stage 4 passed: %d symbols after risk adjustment",
            self._name, len(validated.weights),
        )
        await self.trigger("accepted", **ctx)

    async def _on_enter_executing(self, **ctx) -> None:
        """Run Stage 5 execution."""
        await self._publish_cycle_event("accepted")

        try:
            result = await self._execution_engine.execute(self._pending_validated, **ctx)  # Return out fresh context as well here 
        except Exception as e:
            logger.error("[%s] execution_engine.execute error: %s", self._name, e, exc_info=True)
            await self.trigger("failed", error=e)
            return

        self._last_result = result
        logger.info(
            "[%s] Rebalance result: %d submitted, %d failed",
            self._name, result.submitted, result.failed,
        )

        # Publish the existing RebalanceCompleteEvent for backward compat
        if self._event_bus:
            await self._event_bus.publish(RebalanceCompleteEvent(
                result=result,
                validated_weights=self._pending_validated,
                timestamp=int(time.time() * 1000),
            ))

        await self.trigger("complete", **ctx)

    async def _on_enter_cooldown(self, **ctx) -> None:
        """Schedule a timer to exit cooldown after the configured duration."""
        await self._publish_cycle_event("cooldown")

        # Clean up temp state
        self._pending_weights = None
        self._pending_validated = None

        loop = asyncio.get_event_loop()
        loop.call_later(self._cooldown_s, self._fire_cooldown_done, **ctx)

    def _fire_cooldown_done(self, **ctx) -> None:
        """Exit cooldown and schedule the next tick."""
        asyncio.ensure_future(self._end_cooldown(**ctx))

    async def _end_cooldown(self, **ctx) -> None:
        try:
            await self.trigger("cooldown_done", **ctx)
        except Exception as e:
            logger.error("[%s] cooldown_done error: %s", self._name, e, exc_info=True)
        # Re-schedule the periodic tick now that we're back in IDLE
        self._schedule_tick()

    async def _on_enter_error(self, error=None, **ctx) -> None:
        """Log the error. Auto-retry after cooldown period."""
        await self._publish_cycle_event("failed")
        logger.error("[%s] Entered ERROR state: %s", self._name, error)

        self._pending_weights = None
        self._pending_validated = None

        # Auto-retry after cooldown
        loop = asyncio.get_event_loop()
        loop.call_later(self._cooldown_s, self._fire_retry)

    def _fire_retry(self) -> None:
        asyncio.ensure_future(self._do_retry())

    async def _do_retry(self) -> None:
        try:
            await self.trigger("retry")
        except Exception as e:
            logger.error("[%s] retry error: %s", self._name, e, exc_info=True)
        self._schedule_tick()

    async def _on_enter_suspended(self, **ctx) -> None:
        """Pause all rebalancing. Timer ticks stop."""
        await self._publish_cycle_event("suspend")
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        self._pending_weights = None
        self._pending_validated = None
        logger.warning("[%s] SUSPENDED — rebalancing paused", self._name)

    async def _on_resume(self, **ctx) -> None:
        """Resume rebalancing from SUSPENDED."""
        await self._publish_cycle_event("resume")
        logger.info("[%s] Resumed — back to IDLE", self._name)
        if self._timer_interval is not None:
            self._schedule_tick()

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def last_result(self) -> Optional[RebalanceResult]:
        """The most recent rebalance result, or None."""
        return self._last_result

    async def _publish_cycle_event(self, trigger_name: str) -> None:
        """Publish a RebalanceCycleEvent to the EventBus."""
        if self._event_bus is None:
            return
        # _state has already been updated by BaseFSM before the callback runs,
        # so we reconstruct old_state from the transition table.
        await self._event_bus.publish(RebalanceCycleEvent(
            old_state=self._state,  # technically the new state; good enough for observability
            new_state=self._state,
            trigger=trigger_name,
            timestamp=int(time.time() * 1000),
        ))
