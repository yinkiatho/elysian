"""
Event-driven strategy base class for SPOT trading.

Subclass :class:`SpotStrategy` and override the ``on_*`` hooks you care about.
All hooks are async and receive typed event dataclasses.  Hooks that are not
overridden are silently ignored (no-op).

Usage::

    class MyArb(SpotStrategy):
        async def on_kline(self, event: KlineEvent):
            price = event.kline.close
            ...

        async def on_orderbook_update(self, event: OrderBookUpdateEvent):
            spread = event.orderbook.spread
            ...

    strategy = MyArb(
        exchanges={Venue.BINANCE: exchange},
        event_bus=bus,
    )
    await strategy.start()
"""

import asyncio
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from elysian_core.execution.engine import ExecutionEngine
    from elysian_core.risk.optimizer import PortfolioOptimizer

from elysian_core.connectors.base import SpotExchangeConnector, AbstractDataFeed
from elysian_core.core.enums import Side, StrategyState, Venue
from elysian_core.core.event_bus import EventBus
from elysian_core.core.portfolio import Portfolio
from elysian_core.core.events import (
    EventType,
    KlineEvent,
    OrderBookUpdateEvent,
    OrderUpdateEvent,
    BalanceUpdateEvent,
    RebalanceCompleteEvent,
)
from elysian_core.core.signals import RebalanceResult, TargetWeights
from elysian_core.core.rebalance_fsm import RebalanceFSM
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

_STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})


class SpotStrategy:
    """Base class for event-driven spot strategies.

    All ``on_*`` hooks have default no-op implementations — override only the
    ones you need.  For CPU-heavy calculations, use :meth:`run_heavy` to
    offload work to a :class:`ProcessPoolExecutor`.
    """

    def __init__(
        self,
        exchanges: Dict[Venue, SpotExchangeConnector],
        event_bus: EventBus,
        feeds: Optional[Dict[str, AbstractDataFeed]] = None,
        max_heavy_workers: int = 4,
        optimizer: Optional["PortfolioOptimizer"] = None,
        execution_engine: Optional["ExecutionEngine"] = None,
        args: Optional[Any] = None,
        config_json: Optional[Dict] = None,
    ):
        self._exchanges = exchanges
        self._event_bus = event_bus
        self._feeds = feeds or {}
        self._executor = ProcessPoolExecutor(max_workers=max_heavy_workers)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._optimizer = optimizer
        self._execution_engine = execution_engine
        self.args = args
        self.config_json = config_json or {}
        self.portfolio = Portfolio()

        # Lifecycle state
        self._state = StrategyState.CREATED

        # Rebalance FSM — initialized in start() if optimizer + engine are set
        self._rebalance_fsm: Optional[RebalanceFSM] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    @property
    def state(self) -> StrategyState:
        """Current lifecycle state."""
        return self._state

    @property
    def rebalance_fsm(self) -> Optional[RebalanceFSM]:
        """The rebalance cycle FSM, or None if not configured."""
        return self._rebalance_fsm

    async def start(self):
        """Register all hooks with the event bus and call :meth:`on_start`."""
        if self._state != StrategyState.CREATED:
            logger.warning(f"[Strategy] start() called in state {self._state.name}, ignoring")
            return

        self._state = StrategyState.STARTING
        self._loop = asyncio.get_running_loop()

        self._event_bus.subscribe(EventType.KLINE, self._dispatch_kline)
        self._event_bus.subscribe(EventType.ORDERBOOK_UPDATE, self._dispatch_ob)
        self._event_bus.subscribe(EventType.ORDER_UPDATE, self._dispatch_order)
        self._event_bus.subscribe(EventType.BALANCE_UPDATE, self._dispatch_balance)
        self._event_bus.subscribe(EventType.REBALANCE_COMPLETE, self._dispatch_rebalance)

        # Initialize rebalance FSM if the pipeline is fully configured
        if self._optimizer is not None and self._execution_engine is not None:
            self._rebalance_fsm = RebalanceFSM(
                strategy=self,
                optimizer=self._optimizer,
                execution_engine=self._execution_engine,
                event_bus=self._event_bus,
                name=f"{self.__class__.__name__}.RebalanceFSM",
            )
            logger.info(f"[Strategy] RebalanceFSM initialized for {self.__class__.__name__}")

        await self.on_start()
        self._state = StrategyState.READY
        logger.info(
            f"[Strategy] {self.__class__.__name__} started — "
            f"subscribed to 5 event types, "
            f"{len(self._exchanges)} exchanges, {len(self._feeds)} feeds"
        )

    async def stop(self):
        """Unsubscribe from bus, shut down executor, call :meth:`on_stop`."""
        if self._state == StrategyState.STOPPED:
            return

        self._state = StrategyState.STOPPING
        logger.info(f"[Strategy] {self.__class__.__name__} stopping...")

        # Stop rebalance FSM timer
        if self._rebalance_fsm is not None:
            self._rebalance_fsm.stop_timer()

        self._event_bus.unsubscribe(EventType.KLINE, self._dispatch_kline)
        self._event_bus.unsubscribe(EventType.ORDERBOOK_UPDATE, self._dispatch_ob)
        self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._dispatch_order)
        self._event_bus.unsubscribe(EventType.BALANCE_UPDATE, self._dispatch_balance)
        self._event_bus.unsubscribe(EventType.REBALANCE_COMPLETE, self._dispatch_rebalance)

        self._executor.shutdown(wait=False)
        await self.on_stop()
        self._state = StrategyState.STOPPED
        logger.info(f"[Strategy] {self.__class__.__name__} stopped")

    # ── Rebalance FSM controls ──────────────────────────────────────────────

    async def suspend_rebalancing(self) -> None:
        """Pause the rebalance FSM (e.g. drawdown breaker). No-op if no FSM."""
        if self._rebalance_fsm is not None:
            await self._rebalance_fsm.trigger("suspend")

    async def resume_rebalancing(self) -> None:
        """Resume the rebalance FSM after a suspension."""
        if self._rebalance_fsm is not None:
            await self._rebalance_fsm.trigger("resume")

    async def request_rebalance(self) -> bool:
        """Request a rebalance cycle from any ``on_*`` hook.

        Safe to call at any time — silently ignored if the FSM is busy
        (mid-cycle, cooldown, suspended, etc.).  Returns ``True`` if the
        cycle was triggered, ``False`` if skipped or no FSM configured.

        Use this from event hooks to trigger reactive rebalancing::

            async def on_kline(self, event: KlineEvent):
                if self._should_rebalance(event):
                    await self.request_rebalance()

        Can be combined with a timer: the timer fires periodic cycles,
        and ``request_rebalance()`` fires additional cycles on events.
        Whichever fires first wins; the other is silently skipped if
        the FSM is already busy.
        """
        if self._rebalance_fsm is None:
            logger.warning("[Strategy] request_rebalance called but no FSM configured")
            return False
        return await self._rebalance_fsm.request()

    # ── Internal dispatch (error isolation) ────────────────────────────────────

    def _mark_running(self) -> None:
        """Transition from READY to RUNNING on first event dispatch."""
        if self._state == StrategyState.READY:
            self._state = StrategyState.RUNNING

    async def _dispatch_kline(self, event: KlineEvent):
        self._mark_running()
        try:
            await self.on_kline(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_kline error: {e}", exc_info=True)

    async def _dispatch_ob(self, event: OrderBookUpdateEvent):
        try:
            await self.on_orderbook_update(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_orderbook_update error: {e}", exc_info=True)

    async def _dispatch_order(self, event: OrderUpdateEvent):
        try:
            await self.on_order_update(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_order_update error: {e}", exc_info=True)

    async def _dispatch_balance(self, event: BalanceUpdateEvent):
        try:
            await self.on_balance_update(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_balance_update error: {e}", exc_info=True)

    async def _dispatch_rebalance(self, event: RebalanceCompleteEvent):
        try:
            await self.on_rebalance_complete(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_rebalance_complete error: {e}", exc_info=True)

    # ── Hook functions (override in subclass) ──────────────────────────────────

    async def on_start(self):
        """Called once when the strategy starts. Override for init logic."""
        pass

    async def on_stop(self):
        """Called on shutdown. Override for cleanup."""
        pass

    async def run_forever(self):
        """Optional long-running coroutine for background strategy tasks.

        Override this if your strategy needs its own event loop (e.g. periodic
        rebalancing, timer-based signals).  The runner will ``asyncio.gather``
        this alongside the feed tasks so both run concurrently.

        Default implementation does nothing (returns immediately).  If you
        override this, it should block (e.g. ``while True: await asyncio.sleep(...)``).
        """
        pass
    
    async def on_kline(self, event: KlineEvent):
        """Called on each closed kline candle."""
        pass

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        """Called on each orderbook depth update (~100ms per symbol)."""
        pass

    async def on_order_update(self, event: OrderUpdateEvent):
        """Called when an order status changes (new, partial fill, fill, cancel)."""
        pass

    async def on_balance_update(self, event: BalanceUpdateEvent):
        """Called when account balance changes."""
        pass

    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        """Called after the execution engine completes a rebalance cycle.

        Override to inspect :attr:`event.result` for submitted/failed counts,
        or :attr:`event.validated_weights` to see what the optimizer changed.
        """
        pass

    def compute_weights(self) -> Optional[Dict[str, float]]:
        """**YOUR STRATEGY LOGIC GOES HERE.**

        Called by the RebalanceFSM on each tick (timer or signal-driven).
        Return a dict mapping symbol -> target weight, e.g.::

            {"ETHUSDT": 0.4, "BTCUSDT": 0.5}   # 10% implicit cash

        Return ``{}`` or ``None`` to skip this rebalance cycle.

        Can be sync or async — the FSM handles both::

            # Sync (simple)
            def compute_weights(self):
                return {"ETHUSDT": 0.5, "BTCUSDT": 0.4}

            # Async (if you need to await feeds, APIs, etc.)
            async def compute_weights(self):
                price = await self.get_current_price("ETHUSDT")
                ...
                return weights

        Everything downstream (risk validation, execution, cooldown) is
        handled automatically by the FSM. You only decide WHAT weights.
        """
        return {}

    # ── Weight submission pipeline ───────────────────────────────────────────

    async def submit_weights(
        self,
        weights: Dict[str, float],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[RebalanceResult]:
        """Submit target portfolio weights through the risk / execution pipeline.

        This is the primary output mechanism for weight-vector strategies.
        The flow is::

            strategy  ->  optimizer.validate()  ->  executor.execute()  ->  exchange

        Returns a :class:`RebalanceResult` on success, or ``None`` if the
        optimizer / executor are not configured or the signal was rejected.
        
        
        CURRENTLY DEPRECATED USING REBALANCE_FSM
        """
        if self._state not in (StrategyState.READY, StrategyState.RUNNING):
            logger.warning(
                f"[Strategy] submit_weights called in state {self._state.name} — "
                f"must be READY or RUNNING"
            )
            return None

        if self._optimizer is None or self._execution_engine is None:
            logger.warning(
                "[Strategy] submit_weights called but optimizer/execution_engine not configured. "
                "Use direct exchange access or configure the pipeline."
            )
            return None

        logger.info(
            f"[Strategy] submit_weights: {len(weights)} symbols, "
            f"sum={sum(weights.values()):.4f}"
        )

        target = TargetWeights(
            weights=weights,
            timestamp=int(time.time() * 1000),
            metadata=metadata or {},
        )

        # Stage 4: Risk validation
        logger.info("[Strategy] Stage 4 -> Optimizer.validate()")
        validated = self._optimizer.validate(target)

        if validated.rejected:
            logger.warning(f"[Strategy] Weights REJECTED by optimizer: {validated.rejection_reason}")
            return None

        logger.info(
            f"[Strategy] Stage 4 passed: {len(validated.weights)} symbols after risk adjustment"
        )

        # Stage 5: Execution
        logger.info("[Strategy] Stage 5 -> ExecutionEngine.execute()")
        result = await self._execution_engine.execute(validated)

        logger.info(
            f"[Strategy] Rebalance result: {result.submitted} submitted, "
            f"{result.failed} failed"
        )

        # Publish rebalance-complete event for observability
        if self._event_bus:
            await self._event_bus.publish(RebalanceCompleteEvent(
                result=result,
                validated_weights=validated,
                timestamp=int(time.time() * 1000),
            ))

        return result

    # ── Exchange access ────────────────────────────────────────────────────────

    def get_exchange(self, venue: Venue = Venue.BINANCE) -> SpotExchangeConnector:
        """Return the exchange connector for the given venue."""
        return self._exchanges[venue]

    # ── Feed access ────────────────────────────────────────────────────────────

    def get_feed(self, symbol: str) -> Optional[AbstractDataFeed]:
        return self._feeds.get(symbol)

    # ── Heavy compute offloading ───────────────────────────────────────────────

    async def run_heavy(self, fn, *args):
        """Run a CPU-bound function in a separate process.

        ``fn`` must be a top-level picklable function (not a lambda or method).
        Returns the function's result.
        """
        return await self._loop.run_in_executor(self._executor, fn, *args)

    


    def get_current_vol(self, symbol: str) -> Optional[float]:
        """Return annualised rolling volatility in bps for a feed, or None."""
        feed = self._feeds.get(symbol)
        if feed and feed.volatility is not None:
            return feed.volatility * 10_000
        return None
    

    def _split_pair(self, pair: str):
        """Split a symbol like 'ETHUSDT' into ('ETH', 'USDT')."""
        known_tokens: set = set()
        for sym in self._feeds:
            for stable in _STABLECOINS:
                if sym.endswith(stable):
                    known_tokens.add(sym[: -len(stable)])
                    known_tokens.add(stable)

        for token in known_tokens:
            if pair.startswith(token) and pair[len(token):] in known_tokens:
                return token, pair[len(token):]
            if pair.endswith(token) and pair[: -len(token)] in known_tokens:
                return pair[: -len(token)], token

        return None, None
