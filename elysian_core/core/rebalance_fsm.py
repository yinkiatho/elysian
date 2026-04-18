"""
Rebalance cycle state machine.

Drives the **compute -> validate -> execute -> cooldown** pipeline as a pure
state machine.  The FSM does **not** own any timer or scheduling logic — the
strategy decides *when* to trigger a cycle (timer-based, event-driven, or
both) by calling :meth:`request`.

Context flows through each stage via ``**ctx``.  Each callback enriches
``ctx`` with its output so downstream stages have full pipeline context:

- ``_on_enter_computing``  → adds ``ctx["target_weights"]``
- ``_on_enter_validating`` → adds ``ctx["validated_weights"]``
- ``_on_enter_executing``  → adds ``ctx["rebalance_result"]``

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

    # Event-driven — call from on_kline / on_orderbook:
    await self._rebalance_fsm.request(**ctx)

    # Timer-driven — use PeriodicTask or asyncio loop in run_forever():
    async def run_forever(self):
        while not self._stop_event.is_set():
            await asyncio.sleep(60)
            await self.request_rebalance()
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, TYPE_CHECKING
import elysian_core.utils.logger as log

from elysian_core.core.enums import RebalanceState
from elysian_core.core.fsm import BaseFSM
from elysian_core.core.events import RebalanceCycleEvent, RebalanceCompleteEvent
from elysian_core.core.signals import RebalanceResult, TargetWeights


if TYPE_CHECKING:
    from elysian_core.core.event_bus import EventBus
    from elysian_core.execution.engine import ExecutionEngine
    from elysian_core.risk.optimizer import PortfolioOptimizer
    from elysian_core.core.sub_account_pipeline import SubAccountKey


class RebalanceFSM(BaseFSM):
    """Drives the rebalance cycle: compute -> validate -> execute -> cooldown.

    This is a **pure state machine** — it does not own any timer or scheduling.
    The strategy controls *when* to trigger cycles by calling :meth:`request`.

    Parameters
    ----------
    strategy:
        The strategy instance. Must implement ``compute_weights(**ctx) -> dict``.
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
        # Normal cycle — only "signal" enters COMPUTING from IDLE
        (RebalanceState.IDLE,       "signal"):        (RebalanceState.COMPUTING,  "_on_enter_computing"),
        (RebalanceState.COMPUTING,  "weights_ready"): (RebalanceState.VALIDATING, "_on_enter_validating"),
        (RebalanceState.COMPUTING,  "no_signal"):     (RebalanceState.IDLE,       None),
        (RebalanceState.VALIDATING, "accepted"):      (RebalanceState.EXECUTING,  "_on_enter_executing"),
        (RebalanceState.VALIDATING, "rejected"):      (RebalanceState.COOLDOWN,   "_on_enter_cooldown"),
        (RebalanceState.EXECUTING,  "complete"):      (RebalanceState.COOLDOWN,   "_on_enter_cooldown"),
        (RebalanceState.EXECUTING,  "failed"):        (RebalanceState.ERROR,      "_on_enter_error"),
        (RebalanceState.COOLDOWN,   "cooldown_done"): (RebalanceState.IDLE,       None),
        (RebalanceState.ERROR,      "retry"):         (RebalanceState.IDLE,       None),

        # Explicit COOLDOWN/ERROR → SUSPENDED so the pending timer handle is cancelled.
        # These override the wildcard below for these two states.
        (RebalanceState.COOLDOWN,   "suspend"):       (RebalanceState.SUSPENDED,  "_on_enter_suspended"),
        (RebalanceState.ERROR,      "suspend"):       (RebalanceState.SUSPENDED,  "_on_enter_suspended"),

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
        cooldown_s: float = 0.0,
        name: str = "RebalanceFSM",
        sub_account_key: Optional["SubAccountKey"] = None,
    ):
        super().__init__(
            initial_state=RebalanceState.IDLE,
            event_bus=event_bus,
            name=name,
        )
        self._strategy = strategy
        self.logger = log.setup_custom_logger(f"{strategy.strategy_name}_{strategy.strategy_id}")
        self._optimizer = optimizer
        self._execution_engine = execution_engine
        self._cooldown_s = cooldown_s
        self._sub_account_key = sub_account_key  # None for single-pipeline (SPOT) strategies

        # Most recent rebalance result for inspection
        self._last_result: Optional[RebalanceResult] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def request(self, **ctx) -> bool:
        """Request a rebalance cycle. Safe to call at any time.

        Any keyword arguments are forwarded as context through the entire
        pipeline (compute -> validate -> execute -> cooldown).

        Returns ``True`` if the cycle was triggered (FSM was IDLE).
        Returns ``False`` if the FSM is busy (mid-cycle, cooldown,
        suspended, error) — the request is silently dropped.
        """
        if self._state != RebalanceState.IDLE:
            self.logger.debug(
                "[%s] Rebalance request skipped — currently in %s",
                self._name, self._state.name,
            )
            return False
        await self.trigger("signal", **ctx)
        return True

    # ── Transition callbacks ──────────────────────────────────────────────────

    async def _on_enter_computing(self, **ctx) -> None:
        """Compute target weights by calling the strategy's compute_weights().

        The strategy must implement ``compute_weights(**ctx)`` (public method).
        It can be sync or async:

        - **sync**: return a ``dict[str, float]`` of target weights
        - **async**: ``await`` feeds, exchanges, or heavy compute, then return weights

        Return an empty dict or ``None`` to skip this cycle (transitions to IDLE).

        Enriches ctx with ``target_weights`` for downstream stages.
        """
        await self._publish_cycle_event("signal", **ctx)

        # Portfolio state snapshot before compute
        book = getattr(self._strategy, '_shadow_book', None)
        if book is not None:
            self.logger.info(
                f"[{self._name}] === STAGE 3 START (compute_weights) === "
                f"NAV={book.nav:.4f} cash={book.cash:.4f} "
                f"free_cash={book.free_cash:.4f} locked_cash={book._locked_cash:.4f} "
                f"positions={list(book.active_positions.keys())} "
                f"weights={book.weights} "
                f"active_orders={list(book.active_orders.keys())}"
            )

        # Inject sub_account_key so MarginStrategy can return the right weight slice
        if self._sub_account_key is not None:
            ctx['sub_account_key'] = self._sub_account_key

        try:
            result = self._strategy.compute_weights(**ctx)
            if asyncio.iscoroutine(result):
                weights = await result
            else:
                weights = result
        except Exception as e:
            self.logger.error("[%s] compute_weights error: %s", self._name, e, exc_info=True)
            await self.trigger("no_signal", **ctx)
            return

        if not weights:
            self.logger.debug("[%s] No weights returned — returning to IDLE", self._name)
            await self.trigger("no_signal", **ctx)
            return

        # Tag context with strategy_id for downstream stages
        ctx["strategy_id"] = self._strategy.strategy_id
        ctx["target_weights"] = weights
        self.logger.info(
            "[%s] Weights computed: %d symbols — %s",
            self._name, len(weights),
            {s: f"{w:.4f}" for s, w in weights.items()},
        )

        await self.trigger("weights_ready", **ctx)

    async def _on_enter_validating(self, **ctx) -> None:
        """Run Stage 4 risk validation.

        Reads ``ctx["target_weights"]`` from the computing stage.
        Enriches ctx with ``validated_weights`` for the executing stage.
        """
        await self._publish_cycle_event("weights_ready", **ctx)
        weights = ctx.get("target_weights", {})
        venue = ctx.get("venue", self._optimizer.venue)
        target = TargetWeights(
            weights=weights,
            timestamp=int(time.time() * 1000),
            metadata={},
            strategy_id=self._strategy.strategy_id,
            venue=venue,
            liquidate=bool(ctx.get("convert_all_base", False)),
        )

        try:
            validated = self._optimizer.validate(target, **ctx)
        except Exception as e:
            self.logger.error("[%s] optimizer.validate error: %s", self._name, e, exc_info=True)
            await self.trigger("rejected", **ctx)
            return

        if validated.rejected:
            self.logger.warning(
                "[%s] Weights REJECTED: %s", self._name, validated.rejection_reason,
            )
            await self.trigger("rejected", **ctx)
            return

        ctx["validated_weights"] = validated
        self.logger.info(
            "[%s] === STAGE 4 PASSED: %d symbols after risk adjustment === "
            "target=%s validated=%s clipped=%s",
            self._name, len(validated.weights),
            {s: f"{w:.4f}" for s, w in weights.items()},
            {s: f"{w:.4f}" for s, w in validated.weights.items()},
            validated.clipped or {},
        )
        await self.trigger("accepted", **ctx)

    async def _on_enter_executing(self, **ctx) -> None:
        """Run Stage 5 execution.

        Reads ``ctx["validated_weights"]`` from the validating stage.
        Enriches ctx with ``rebalance_result``.
        """
        await self._publish_cycle_event("accepted", **ctx)

        validated = ctx.get("validated_weights")

        # Portfolio state snapshot before execution
        book = getattr(self._strategy, '_shadow_book', None)
        if book is not None:
            self.logger.info(
                f"[{self._name}] === STAGE 5 START (execute) === "
                f"NAV={book.nav:.4f} cash={book.cash:.4f} "
                f"free_cash={book.free_cash:.4f} "
                f"positions={list(book.active_positions.keys())} "
                f"current_weights={book.weights} "
                f"target_weights={({s: f'{w:.4f}' for s, w in validated.weights.items()} if validated else 'None')}"
            )

        try:
            result = await self._execution_engine.execute(validated, **ctx)  # <== Receives the RebalanceResult from the ExecutionEngine
        except Exception as e:
            self.logger.error("[%s] execution_engine.execute error: %s", self._name, e, exc_info=True)
            await self.trigger("failed", error=e, **ctx)
            return

        # Lock shadow book for submitted limit orders (Fix B)
        book = getattr(self._strategy, '_shadow_book', None)
        if book is not None and result.submitted_orders:
            for order_id, intent in result.submitted_orders.items():
                try:
                    book.lock_for_order(order_id, intent)
                except Exception as e:
                    self.logger.error(
                        "[%s] shadow_book.lock_for_order error: %s", self._name, e, exc_info=True
                    )

        self._last_result = result
        ctx["rebalance_result"] = result
        self.logger.info(
            "[%s] === STAGE 5 COMPLETE: %d submitted, %d failed === %s",
            self._name, result.submitted, result.failed,
            {oid: f"{i.side.value} {i.symbol} qty={i.quantity:.6f}" for oid, i in (result.submitted_orders or {}).items()},
        )
        if result.errors:
            self.logger.warning("[%s] Execution errors: %s", self._name, result.errors)

        # Publish RebalanceCompleteEvent for strategy hooks
        if self._event_bus:
            await self._event_bus.publish(RebalanceCompleteEvent(
                result=result,
                validated_weights=validated,
                timestamp=int(time.time() * 1000),
                strategy_id=self._strategy.strategy_id,
            ))

        await self.trigger("complete", **ctx)

    async def _on_enter_cooldown(self, **ctx) -> None:
        """Wait for the configured cooldown duration then return to IDLE."""
        await self._publish_cycle_event("cooldown", **ctx)

        if self._cooldown_s > 0:
            loop = asyncio.get_running_loop()
            loop.call_later(self._cooldown_s, self._fire_cooldown_done)
        else:
            # No cooldown — immediately transition back to IDLE
            await self.trigger("cooldown_done", **ctx)

    def _fire_cooldown_done(self) -> None:
        """Callback from ``call_later`` — create a task to exit cooldown."""
        asyncio.ensure_future(self._end_cooldown())

    async def _end_cooldown(self) -> None:
        try:
            await self.trigger("cooldown_done")
        except Exception as e:
            self.logger.error("[%s] cooldown_done error: %s", self._name, e, exc_info=True)

    async def _on_enter_error(self, error=None, **ctx) -> None:
        """Log the error. Auto-retry after cooldown period."""
        await self._publish_cycle_event("failed", **ctx)
        self.logger.error("[%s] Entered ERROR state: %s", self._name, error)

        # Auto-retry after cooldown
        if self._cooldown_s > 0:
            loop = asyncio.get_running_loop()
            loop.call_later(self._cooldown_s, self._fire_retry)
        else:
            await self.trigger("retry")

    def _fire_retry(self) -> None:
        asyncio.ensure_future(self._do_retry())

    async def _do_retry(self) -> None:
        try:
            await self.trigger("retry")
        except Exception as e:
            self.logger.error("[%s] retry error: %s", self._name, e, exc_info=True)

    async def _on_enter_suspended(self, **ctx) -> None:
        """Pause all rebalancing."""
        await self._publish_cycle_event("suspend", **ctx)
        self.logger.warning("[%s] SUSPENDED — rebalancing paused", self._name)

    async def _on_resume(self, **ctx) -> None:
        """Resume rebalancing from SUSPENDED."""
        await self._publish_cycle_event("resume", **ctx)
        self.logger.info("[%s] Resumed — back to IDLE", self._name)

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def last_result(self) -> Optional[RebalanceResult]:
        """The most recent rebalance result, or None."""
        return self._last_result

    async def _publish_cycle_event(self, trigger_name: str, **ctx) -> None:
        """Publish a RebalanceCycleEvent to the EventBus.

        Uses ``ctx["_fsm_old_state"]`` (injected by BaseFSM) so that
        old_state and new_state are accurate.
        """
        if self._event_bus is None:
            return
        await self._event_bus.publish(RebalanceCycleEvent(
            old_state=ctx.get("_fsm_old_state", self._state),
            new_state=self._state,
            trigger=trigger_name,
            timestamp=int(time.time() * 1000),
        ))