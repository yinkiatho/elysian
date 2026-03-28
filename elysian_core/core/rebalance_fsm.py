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
from typing import Any, Optional, TYPE_CHECKING
import elysian_core.utils.logger as log

from elysian_core.core.enums import RebalanceState
from elysian_core.core.fsm import BaseFSM
from elysian_core.core.events import EventType, RebalanceCycleEvent, RebalanceCompleteEvent
from elysian_core.core.signals import RebalanceResult, TargetWeights


if TYPE_CHECKING:
    from elysian_core.core.event_bus import EventBus
    from elysian_core.core.weight_aggregator import WeightAggregator
    from elysian_core.execution.engine import ExecutionEngine
    from elysian_core.risk.optimizer import PortfolioOptimizer

logger = log.setup_custom_logger("root")


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
        aggregator: Optional["WeightAggregator"] = None,
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
        self._aggregator = aggregator

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
            logger.debug(
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
        try:
            result = self._strategy.compute_weights(**ctx)
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

        # Tag context with strategy_id for downstream stages
        ctx["strategy_id"] = self._strategy.strategy_id

        if self._aggregator is not None:
            # Multi-strategy mode: store this strategy's weights, merge all
            await self._aggregator.update_weights(self._strategy.strategy_id, weights)
            merged = await self._aggregator.get_merged_weights()
            ctx["target_weights"] = merged
            ctx["strategy_weights"] = weights  # keep original for audit
            logger.info(
                "[%s] Weights computed: %d symbols (merged: %d symbols from aggregator)",
                self._name, len(weights), len(merged),
            )
        else:
            # Single-strategy mode (no aggregator)
            ctx["target_weights"] = weights
            logger.info("[%s] Weights computed: %d symbols", self._name, len(weights))

        await self.trigger("weights_ready", **ctx)

    async def _on_enter_validating(self, **ctx) -> None:
        """Run Stage 4 risk validation.

        Reads ``ctx["target_weights"]`` from the computing stage.
        Enriches ctx with ``validated_weights`` for the executing stage.
        """
        await self._publish_cycle_event("weights_ready", **ctx)

        weights = ctx.get("target_weights", {})
        target = TargetWeights(
            weights=weights,
            timestamp=int(time.time() * 1000),
            metadata={},
        )

        try:
            validated = self._optimizer.validate(target, **ctx)
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

        ctx["validated_weights"] = validated
        logger.info(
            "[%s] Stage 4 passed: %d symbols after risk adjustment",
            self._name, len(validated.weights),
        )
        await self.trigger("accepted", **ctx)

    async def _on_enter_executing(self, **ctx) -> None:
        """Run Stage 5 execution.

        Reads ``ctx["validated_weights"]`` from the validating stage.
        Enriches ctx with ``rebalance_result``.
        """
        await self._publish_cycle_event("accepted", **ctx)

        validated = ctx.get("validated_weights")
        try:
            result = await self._execution_engine.execute(validated, **ctx)
        except Exception as e:
            logger.error("[%s] execution_engine.execute error: %s", self._name, e, exc_info=True)
            await self.trigger("failed", error=e, **ctx)
            return

        # Lock shadow book for submitted limit orders (Fix B)
        book = getattr(self._strategy, '_shadow_book', None)
        if book is not None and result.submitted_orders:
            for order_id, intent in result.submitted_orders.items():
                try:
                    book.lock_for_order(order_id, intent)
                except Exception as e:
                    logger.error(
                        "[%s] shadow_book.lock_for_order error: %s", self._name, e, exc_info=True
                    )

        self._last_result = result
        ctx["rebalance_result"] = result
        logger.info(
            "[%s] Rebalance result: %d submitted, %d failed",
            self._name, result.submitted, result.failed,
        )

        # ── Fill attribution to shadow books ────────────────────────────
        mark_prices = ctx.get("_mark_prices", {})
        portfolio_total_value = ctx.get("_portfolio_total_value", 0.0)

        if self._aggregator is not None and portfolio_total_value > 0:
            # Multi-strategy: reconcile ALL shadow books via aggregator
            await self._aggregator.attribute_fills(mark_prices, portfolio_total_value)
            if result.failed > 0:
                logger.warning(
                    "[%s] %d orders failed — shadow books may drift from Portfolio. "
                    "Reconciliation recommended.",
                    self._name, result.failed,
                )

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
            loop = asyncio.get_event_loop()
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
            logger.error("[%s] cooldown_done error: %s", self._name, e, exc_info=True)

    async def _on_enter_error(self, error=None, **ctx) -> None:
        """Log the error. Auto-retry after cooldown period."""
        await self._publish_cycle_event("failed", **ctx)
        logger.error("[%s] Entered ERROR state: %s", self._name, error)

        # Auto-retry after cooldown
        if self._cooldown_s > 0:
            loop = asyncio.get_event_loop()
            loop.call_later(self._cooldown_s, self._fire_retry)
        else:
            await self.trigger("retry")

    def _fire_retry(self) -> None:
        asyncio.ensure_future(self._do_retry())

    async def _do_retry(self) -> None:
        try:
            await self.trigger("retry")
        except Exception as e:
            logger.error("[%s] retry error: %s", self._name, e, exc_info=True)

    async def _on_enter_suspended(self, **ctx) -> None:
        """Pause all rebalancing."""
        await self._publish_cycle_event("suspend", **ctx)
        logger.warning("[%s] SUSPENDED — rebalancing paused", self._name)

    async def _on_resume(self, **ctx) -> None:
        """Resume rebalancing from SUSPENDED."""
        await self._publish_cycle_event("resume", **ctx)
        logger.info("[%s] Resumed — back to IDLE", self._name)

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