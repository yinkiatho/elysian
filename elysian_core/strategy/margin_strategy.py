"""
Multi-sub-account margin strategy base class.

Subclass :class:`MarginStrategy` to build strategies that trade across multiple
sub-account pipelines (e.g. a SPOT long-book + an isolated-MARGIN short-book).

Each pipeline owns its own ``(exchange, private_bus, shadow_book, optimizer, fsm,
execution_engine)`` stack.  Pipelines are built by :class:`MarginStrategyRunner`
and injected into ``strategy._pipelines`` before ``start()`` is called.

Usage::

    class EthLongBtcShort(MarginStrategy):
        async def on_kline(self, event: KlineEvent):
            await self.request_rebalance()

        def compute_weights(self, **ctx) -> Dict[str, float]:
            key = ctx.get('sub_account_key')
            if key and key.asset_type == AssetType.MARGIN:
                return {"BTCUSDT": -0.5}   # short BTC on isolated margin
            return {"ETHUSDT": 0.8}        # long ETH on spot

The runner calls ``compute_weights`` once per pipeline per rebalance cycle,
injecting ``ctx['sub_account_key']`` so the strategy can return the right slice.
"""

import asyncio
from typing import Any, Dict, FrozenSet, Optional, TYPE_CHECKING
from concurrent.futures import ProcessPoolExecutor

from elysian_core.core.enums import AssetType, StrategyState, Venue
from elysian_core.core.event_bus import EventBus
from elysian_core.core.events import (
    EventType,
    KlineEvent,
    OrderBookUpdateEvent,
    OrderUpdateEvent,
    BalanceUpdateEvent,
    RebalanceCompleteEvent,
)
from elysian_core.core.rebalance_fsm import RebalanceFSM
from elysian_core.core.sub_account_pipeline import SubAccountKey, SubAccountPipeline
from elysian_core.strategy.base_strategy import SpotStrategy
import elysian_core.utils.logger as log

if TYPE_CHECKING:
    from elysian_core.core.shadow_book import ShadowBook


class MarginStrategy(SpotStrategy):
    """Base class for multi-sub-account strategies (SPOT + MARGIN pipelines).

    Overrides the single-pipeline lifecycle of :class:`SpotStrategy` with a
    ``_pipelines`` dict keyed by :class:`SubAccountKey`.  Each pipeline runs its
    own :class:`RebalanceFSM` on its own private EventBus.

    Event dispatch routes by ``event.asset_type`` + ``event.sub_account_id`` so
    fills from the MARGIN pipeline cannot corrupt the SPOT ShadowBook and vice
    versa.

    Parameters
    ----------
    (inherits all SpotStrategy constructor kwargs)

    Notes
    -----
    - ``_pipelines`` is populated by :class:`MarginStrategyRunner.setup_strategy`
      **before** ``start()`` is called.
    - ``compute_weights(**ctx)`` receives ``ctx['sub_account_key']`` for each
      pipeline invocation.  Return a flat ``Dict[str, float]`` for that pipeline.
    - Returning ``{}`` or ``None`` skips the cycle for that pipeline only.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Keyed by SubAccountKey; populated by MarginStrategyRunner before start()
        self._pipelines: Dict[SubAccountKey, SubAccountPipeline] = {}

    # ── Backward-compat _shadow_book property ────────────────────────────────

    @property
    def _shadow_book(self) -> Optional["ShadowBook"]:
        """Returns the first pipeline's ShadowBook.

        Used by RebalanceFSM logging and backward-compatible strategy code.
        For multi-pipeline strategies, use ``shadow_books`` or ``get_pipeline()``.
        """
        if not self._pipelines:
            return None
        return next(iter(self._pipelines.values())).shadow_book

    @_shadow_book.setter
    def _shadow_book(self, value) -> None:
        # MarginStrategyRunner does NOT set _shadow_book directly — books live in
        # _pipelines.  This no-op setter ensures SpotStrategy.__init__ and legacy
        # runner code that does ``strategy._shadow_book = sb`` don't break.
        pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create per-pipeline FSMs, subscribe to all buses, call on_start().

        Called by MarginStrategyRunner after all pipelines have been injected.
        """
        if self._state != StrategyState.CREATED:
            self.logger.warning(
                f"[MarginStrategy] start() called in state {self._state.name}, ignoring"
            )
            return

        self._state = StrategyState.STARTING
        self._loop = asyncio.get_running_loop()

        # Collect symbols union and venues from all pipelines for event filtering
        for key, pipeline in self._pipelines.items():
            self._symbols.update(pipeline.symbols)
            self.venues = frozenset(self.venues | {key.venue})

        # Subscribe to the shared (market data) bus
        self._shared_event_bus.subscribe(EventType.KLINE, self._dispatch_kline)
        self._shared_event_bus.subscribe(EventType.ORDERBOOK_UPDATE, self._dispatch_ob)

        # Per-pipeline: create RebalanceFSM and subscribe to private bus
        for key, pipeline in self._pipelines.items():
            pipeline.rebalance_fsm = RebalanceFSM(
                strategy=self,
                optimizer=pipeline.optimizer,
                execution_engine=pipeline.execution_engine,
                event_bus=pipeline.private_event_bus,
                sub_account_key=key,
                name=f"{self.__class__.__name__}.FSM[{key}]",
            )
            pipeline.private_event_bus.subscribe(
                EventType.ORDER_UPDATE, self._dispatch_order
            )
            pipeline.private_event_bus.subscribe(
                EventType.BALANCE_UPDATE, self._dispatch_balance
            )
            pipeline.private_event_bus.subscribe(
                EventType.REBALANCE_COMPLETE, self._dispatch_rebalance
            )

        await self.on_start()
        self._state = StrategyState.READY
        self.logger.info(
            f"[MarginStrategy] {self.__class__.__name__} started — "
            f"{len(self._pipelines)} pipeline(s), "
            f"symbols={self._symbols}"
        )

    async def stop(self) -> None:
        """Unsubscribe all buses, stop all shadow books, call on_stop()."""
        if self._state == StrategyState.STOPPED:
            return

        self._state = StrategyState.STOPPING
        await self.on_stop()
        self._stop_event.set()

        # Unsubscribe from shared bus
        self._shared_event_bus.unsubscribe(EventType.KLINE, self._dispatch_kline)
        self._shared_event_bus.unsubscribe(EventType.ORDERBOOK_UPDATE, self._dispatch_ob)

        # Unsubscribe from each pipeline's private bus and stop its shadow book
        for pipeline in self._pipelines.values():
            pipeline.private_event_bus.unsubscribe(
                EventType.ORDER_UPDATE, self._dispatch_order
            )
            pipeline.private_event_bus.unsubscribe(
                EventType.BALANCE_UPDATE, self._dispatch_balance
            )
            pipeline.private_event_bus.unsubscribe(
                EventType.REBALANCE_COMPLETE, self._dispatch_rebalance
            )
            pipeline.shadow_book.stop()

        self._executor.shutdown(wait=False)
        self._state = StrategyState.STOPPED
        self.logger.info(f"[MarginStrategy] {self.__class__.__name__} stopped")

    # ── Rebalance API ─────────────────────────────────────────────────────────

    async def request_rebalance(
        self,
        sub_account_key: Optional[SubAccountKey] = None,
        **ctx: Any,
    ) -> bool:
        """Trigger a rebalance cycle on one sub-account or all pipelines.

        Parameters
        ----------
        sub_account_key:
            Target a specific pipeline. If ``None``, all pipeline FSMs are
            triggered concurrently via ``asyncio.gather``.
        **ctx:
            Forwarded through the entire FSM pipeline to ``compute_weights``.

        Returns ``True`` if ALL requested FSMs were IDLE and triggered.
        Returns ``False`` if any FSM was busy or no pipelines are configured.
        """
        if not self._pipelines:
            self.logger.warning(
                "[MarginStrategy] request_rebalance called but no pipelines configured"
            )
            return False

        if sub_account_key is not None:
            pipeline = self._pipelines.get(sub_account_key)
            if pipeline is None or pipeline.rebalance_fsm is None:
                self.logger.warning(
                    f"[MarginStrategy] No pipeline for sub_account_key={sub_account_key}"
                )
                return False
            return await pipeline.rebalance_fsm.request(**ctx)

        # Trigger all pipelines concurrently
        results = await asyncio.gather(
            *(
                p.rebalance_fsm.request(**ctx)
                for p in self._pipelines.values()
                if p.rebalance_fsm is not None
            ),
            return_exceptions=True,
        )
        return all(r is True for r in results)

    async def suspend_rebalancing(self) -> None:
        """Suspend all pipeline FSMs."""
        for pipeline in self._pipelines.values():
            if pipeline.rebalance_fsm is not None:
                await pipeline.rebalance_fsm.trigger("suspend")

    async def resume_rebalancing(self) -> None:
        """Resume all suspended pipeline FSMs."""
        for pipeline in self._pipelines.values():
            if pipeline.rebalance_fsm is not None:
                await pipeline.rebalance_fsm.trigger("resume")

    # ── Internal event dispatch — route by sub-account key ───────────────────

    async def _dispatch_kline(self, event: KlineEvent) -> None:
        """Filter klines to this strategy's symbol union, then call on_kline hook.

        ShadowBooks and ExecutionEngines subscribe to the shared bus independently
        via ``ShadowBook.start()`` and ``ExecutionEngine.start()`` — no manual
        forwarding needed here.
        """
        if event.venue not in self.venues or event.symbol not in self._symbols:
            return
        self._mark_running()
        try:
            await self.on_kline(event)
        except Exception as e:
            self.logger.error(f"MarginStrategy.on_kline error: {e}", exc_info=True)

    async def _dispatch_order(self, event: OrderUpdateEvent) -> None:
        """Route ORDER_UPDATE to the correct pipeline's ShadowBook."""
        key = SubAccountKey(
            strategy_id=self.strategy_id,
            asset_type=event.asset_type,
            venue=event.venue,
            sub_account_id=event.sub_account_id,
        )
        pipeline = self._pipelines.get(key)
        if pipeline is None:
            self.logger.warning(
                f"[MarginStrategy] OrderUpdate for unknown sub-account {key}"
            )
            return
        try:
            await asyncio.gather(
                self.on_order_update(event),
                pipeline.shadow_book._on_order_update(event),
            )
        except Exception as e:
            self.logger.error(f"MarginStrategy._dispatch_order error: {e}", exc_info=True)

    async def _dispatch_balance(self, event: BalanceUpdateEvent) -> None:
        """Route BALANCE_UPDATE to the correct pipeline's ShadowBook."""
        key = SubAccountKey(
            strategy_id=self.strategy_id,
            asset_type=event.asset_type,
            venue=event.venue,
            sub_account_id=event.sub_account_id,
        )
        pipeline = self._pipelines.get(key)
        if pipeline is None:
            self.logger.warning(
                f"[MarginStrategy] BalanceUpdate for unknown sub-account {key}"
            )
            return
        try:
            await asyncio.gather(
                self.on_balance_update(event),
                pipeline.shadow_book._on_balance_update(event),
            )
        except Exception as e:
            self.logger.error(
                f"MarginStrategy._dispatch_balance error: {e}", exc_info=True
            )

    async def _dispatch_rebalance(self, event: RebalanceCompleteEvent) -> None:
        """Broadcast REBALANCE_COMPLETE to the strategy hook."""
        try:
            await self.on_rebalance_complete(event)
        except Exception as e:
            self.logger.error(
                f"MarginStrategy._dispatch_rebalance error: {e}", exc_info=True
            )

    # ── Pipeline helpers ──────────────────────────────────────────────────────

    def get_pipeline(self, key: SubAccountKey) -> Optional[SubAccountPipeline]:
        """Return the pipeline for the given sub-account key, or None."""
        return self._pipelines.get(key)

    @property
    def shadow_books(self) -> Dict[SubAccountKey, "ShadowBook"]:
        """Map of sub-account key → ShadowBook for all pipelines."""
        return {k: p.shadow_book for k, p in self._pipelines.items()}
