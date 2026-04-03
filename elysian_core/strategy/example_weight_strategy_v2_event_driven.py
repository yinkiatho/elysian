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
import numpy as np

logger = log.setup_custom_logger("EventDrivenStrategy")


class EventDrivenStrategy(SpotStrategy):
    """
    Event - Driven Strategy Momentum Long Only Strategy with weights as the difference from running returns
    - Work at the minute level and track short term momentum rolling statistics
    """

    def __init__(self, *args, 
                 symbols: set = None, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Symbols can be passed explicitly or loaded from cfg in on_start
        self._symbols: set = set(symbols) if symbols else set()
        self._rebalance_interval = 60  # seconds; also set on FSM cooldown     
        
        
        # Strategy Params and State to write below:
        self._price_series = {symbol: deque([], maxlen=60*240) for symbol in self._symbols}
        self._returns_series = {symbol: deque([], maxlen=60*240) for symbol in self._symbols}
        self._rolling_returns_series = {symbol: deque([], maxlen=60*240) for symbol in self._symbols}
        self._last_marked_time = None
        self._symbol_availability_status = {symbol: False for symbol in self._symbols}

    async def on_start(self):
        '''
        Strategy specific on start logic to be triggered aside from the common event subscribing and fsm initialization.
        cfg is guaranteed to be set by StrategyRunner.setup_strategy() before start() is called.
        '''
        # Load symbols from cfg if not explicitly provided at construction
        if not self._symbols and self.cfg:
            self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))

        logger.info(
            f"[EventDriven Momentum Weight Strategy] Started — "
            f"rebalance every {self._rebalance_interval}s, "
            f"symbols={self._symbols}"
        )
        
    async def on_stop(self):
        '''
        Default on_stop behaviour for strategy is to convert all assets back to USDT + other functions to add 
        '''
        await self.request_rebalance(convert_all_base=True) # We convert all to base here 
        

    async def on_kline(self, event: KlineEvent):
        '''
        on_kline hook, we update the price_series and _return_series first, check for status and then fire trigger,
        
        For now we will receive kline events from ALL VENUES AND ASSET_TYPES take note here. 
        '''
        if event.symbol not in self._symbols:
            return

        # Update new klines and returns
        last_price = self._price_series[event.symbol][-1] if self._price_series[event.symbol] else None
        self._price_series[event.symbol].append(event.kline.close)
        if last_price is not None and last_price > 0:
            self._returns_series[event.symbol].append((event.kline.close - last_price) / last_price)

            if len(self._returns_series[event.symbol]) >= 30:
                self._rolling_returns_series[event.symbol].append(statistics.mean(list(self._returns_series[event.symbol])[-30:]))
                self._symbol_availability_status[event.symbol] = True
                 
        # Check for full availability and past rebalance interval and trigger
        if all(self._symbol_availability_status.values()) and (self._last_marked_time is None or time.monotonic() - self._last_marked_time > self._rebalance_interval):
            self._last_marked_time = time.monotonic()
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
        For each symbol:
            1. Compute the latest return (e.g., 1‑period return).
            2. Compute rolling mean and rolling standard deviation (lookback = 20).
            3. Calculate z‑score = (latest_return - rolling_mean) / rolling_std.
            4. If z‑score > threshold (0.5) → positive signal = z‑score.
            Else → signal = 0 (stay out).
            5. Scale each signal by 1 / rolling_std (inverse volatility).
            6. Normalise the resulting weights so that their sum = target_allocation (0.9).
        Returns a dict {symbol: weight} – missing symbols imply zero weight.
        """
        # --- Kill switch (go 100% cash) ---
        if ctx.get('convert_all_base', False):
            logger.info("Kill signal received → returning zero weights (100% cash)")
            return {sym: 0.0 for sym in self._symbols}

        # --- Parameters ---
        LOOKBACK = 20          # periods for rolling mean/std
        Z_THRESHOLD = 0.5      # minimum z‑score to enter a trade
        TARGET_ALLOC = 0.9     # sum of weights (10% cash buffer)

        signals = {}           # raw z‑scores (positive only)
        vol_estimates = {}     # rolling std for each symbol

        for sym in self._symbols:
            returns = self._returns_series.get(sym)
            if returns is None or len(returns) < LOOKBACK + 1:
                logger.debug(f"Insufficient return data for {sym}")
                continue

            current_return = returns[-1]
            rolling_mean = np.mean(returns[-LOOKBACK:])  
            rolling_std = np.std(returns[-LOOKBACK:])
            
            if rolling_std == 0:
                continue

            z_score = (current_return - rolling_mean) / rolling_std
            if abs(z_score) > Z_THRESHOLD:
                signals[sym] = z_score
                vol_estimates[sym] = rolling_std

        if not signals:
            logger.info("No positive signals above threshold → staying in cash")
            return {}

        # --- Inverse volatility scaling (risk parity) ---
        inv_vol = {sym: 1.0 / vol_estimates[sym] for sym in signals}
        total_inv_vol = sum(inv_vol.values())
        raw_weights = {sym: inv_vol[sym] / total_inv_vol for sym in signals}
        adjusted_weights = {sym: raw_weights[sym] * signals[sym] for sym in signals}
        total_adj = sum(adjusted_weights.values())
        
        if total_adj == 0:
            return {}
        
        final_weights = {sym: w / total_adj * TARGET_ALLOC for sym, w in adjusted_weights.items()}
        logger.info(f"Final weights: {final_weights} (sum = {sum(final_weights.values()):.2f})")
        
        return final_weights