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

from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent, RebalanceCompleteEvent
import elysian_core.utils.logger as log
from collections import deque
import statistics
import time

logger = log.setup_custom_logger("root")


class EventDrivenStrategy(SpotStrategy):
    """
    Event - Driven Strategy Momentum Long Only Strategy with weights as the difference from running returns
    - Work at the minute level and track short term momentum rolling statistics
    """

    def __init__(self, *args, rebalance_interval_s: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._rebalance_interval = rebalance_interval_s
        self._symbols: set = set(self.cfg.symbols.symbols_for("binance", "spot")) if self.cfg else set()
        

    async def on_start(self):
        '''
        Strategy specific on start logic to be triggered aside from the common event subscribing and fsm initialization
        '''
        logger.info(
            f"[EventDriven Momentum Weight Strategy] Started — rebalance every {self._rebalance_interval}s"
        )
        
        self._price_series = {symbol: deque([], maxlen=60*240) for symbol in self._symbols}
        self._returns_series = {symbol: deque([], maxlen=60*240) for symbol in self._symbols}
        self._rolling_returns_series = {symbol: deque([], maxlen=60*240) for symbol in self._symbols}
        self._last_marked_time = None
        
        self._symbol_availability_status = {symbol: False for symbol in self._symbols}
        
    async def on_kline(self, event: KlineEvent):
        '''
        on_kline hook, we update the price_series and _return_series first, check for status and then fire trigger
        '''
        if event.symbol not in self._symbols:
            self._symbols.add(event.symbol)
            self._price_series[event.symbol] = deque([], maxlen=60*240)
            self._returns_series[event.symbol] = deque([], maxlen=60*240)
            
        # Update new klines and returns 
        last_price = self._price_series[event.symbol][-1]
        self._price_series[event.symbol].append(event.kline.close)
        if len(self._returns_series[event.symbol]):
            self._returns_series[event.symbol].append((event.kline.close - last_price)/last_price)
            
            if len(self._returns_series) >= 30:
                self._rolling_returns_series[event.symbol].append(statistics.mean(self._returns_series[-30:]))
                self._symbol_availability_status[event.symbol] = True
                 
        # Check for full availability and past rebalance interval and trigger
        if all(self._symbol_availability_status.values()) and (self._last_marked_time is None or time.time() - self._last_marked_time > self._rebalance_interval):
            self._last_marked_time = time.time()
            await self.request_rebalance()
            
            
    async def run_forever(self):
        """Event-driven — no timer. Rebalance is triggered from on_kline().

        Blocks until stop() is called.
        """
        logger.info("Initiating EventDriven Strategy")

        if self._rebalance_fsm is None:
            logger.warning(
                "[EventDriven] No RebalanceFSM — optimizer/execution_engine not configured"
            )
            return

        self._rebalance_fsm._cooldown_s = self._rebalance_interval
        await self._stop_event.wait()


    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        r = event.result
        if r.errors:
            logger.warning(f"[EqualWeight] Rebalance had errors: {r.errors}")


    def compute_weights(self, **ctx) -> dict:
        """
        Allocate weights based on deviation of latest return from rolling mean return.

        Long-only:
        - Positive deviation → allocate weight
        - Negative deviation → ignore (weight = 0)

        Weights are normalized to sum to 0.90 (10% cash buffer).
        """

        signals = {}
        for sym in self._symbols:
            returns = self._returns_series.get(sym)
            rolling = self._rolling_returns_series.get(sym)

            # Need enough data
            if not returns or not rolling or len(returns) == 0 or len(rolling) == 0:
                continue

            current_return = returns[-1]
            rolling_mean = rolling[-1]

            signal = current_return - rolling_mean

            # Long-only: keep positive signals
            if signal > 0:
                signals[sym] = signal

        # If no positive signals → stay in cash
        if not signals:
            return {}

        # Normalize weights
        total_signal = sum(signals.values())

        if total_signal == 0:
            return {}

        weights = {
            sym: sig / total_signal
            for sym, sig in signals.items()
        }
        return weights
