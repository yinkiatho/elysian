"""
Example weight-vector strategy that demonstrates the Stage 3-5 pipeline.

Shows **two trigger modes** for the RebalanceFSM:

1. **Timer mode** — periodic rebalance on a clock (this example)
2. **Event mode** — reactive rebalance from ``on_kline`` / ``on_orderbook_update``

Both modes can be combined: timer fires periodic cycles, and event hooks
fire additional cycles via ``request_rebalance()``.  If the FSM is busy
when either fires, the request is silently skipped.

Usage::

    strategy = EqualWeightStrategy(exchanges={}, event_bus=bus)
    runner = StrategyRunner()
    await runner.run(strategy=strategy)
"""

import asyncio
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent, RebalanceCompleteEvent
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class EqualWeightStrategy(SpotStrategy):
    """Equal-weight across all tracked symbols, rebalanced on a timer.

    This is a reference implementation showing how to use the RebalanceFSM.

    **Where your strategy logic goes:**

    - ``on_kline()`` / ``on_orderbook_update()`` — accumulate state from
      market data (prices, signals, indicators, etc.)
    - ``compute_weights()`` — read that state and return target portfolio
      weights.  Called automatically by the FSM on each cycle.
    - ``request_rebalance()`` — call from any ``on_*`` hook to trigger
      a cycle immediately (instead of waiting for the timer).
    """

    def __init__(self, *args, rebalance_interval_s: float = 60.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._rebalance_interval = rebalance_interval_s
        self._symbols: set = set()

    async def on_start(self):
        '''
        Strategy specific on start logic to be triggered aside from the common event subscribing and fsm initialization
        '''
        logger.info(
            f"[EqualWeight] Started — rebalance every {self._rebalance_interval}s"
        )

    async def on_kline(self, event: KlineEvent):
        # Track symbols that have live data
        self._symbols.add(event.symbol)

        # ── EVENT-DRIVEN TRIGGER EXAMPLE ──
        # Ttrigger a rebalance whenever a new symbol appears:
        
        if event.symbol not in self._symbols:
            self._symbols.add(event.symbol)
            await self.request_rebalance()   # fires FSM if idle
        #

    async def run_forever(self):
        """Start the rebalance FSM timer after an initial warmup period.

        The FSM drives the compute -> validate -> execute -> cooldown cycle.
        No ``while True`` loop needed.

        **Timer mode** (this example)::

            self._rebalance_fsm.start_timer(interval_s=60)
            await self._rebalance_fsm.wait()

        **Event-only mode** (no timer, purely reactive)::

            # Don't call start_timer(). Instead, call
            # await self.request_rebalance() from on_kline / on_orderbook.
            await self._rebalance_fsm.wait()

        **Both** (timer + events)::

            self._rebalance_fsm.start_timer(interval_s=60)
            # AND call request_rebalance() from hooks.
            # Timer and events don't conflict — first one wins.
            await self._rebalance_fsm.wait()
        """

        logger.info("Initiating Example Weight Strategy")

        if self._rebalance_fsm is not None:
            self._rebalance_fsm._cooldown_s = self._rebalance_interval
            self._rebalance_fsm.start_timer(interval_s=self._rebalance_interval)
            await self._rebalance_fsm.wait()  # blocks until stop_timer() is called
        else:
            logger.warning(
                "[EqualWeight] No RebalanceFSM — optimizer/execution_engine not configured"
            )


    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        r = event.result
        if r.errors:
            logger.warning(f"[EqualWeight] Rebalance had errors: {r.errors}")


    def compute_weights(self, **ctx) -> dict:
        """Compute equal weights for all tracked symbols.

        Override this method to implement custom alpha logic.  Return a dict
        mapping symbol -> target weight (floats summing to <= 1.0).
        """
        if not self._symbols:
            return {}

        n = len(self._symbols)
        # Reserve 10% cash, distribute rest equally
        per_asset = 0.90 / n
        return {sym: per_asset for sym in sorted(self._symbols)}
