import asyncio
import math
import statistics
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import List, Optional

import pandas as pd

from elysian_core.core.market_data import OrderBook
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class AbstractDataFeed(ABC):
    """
    Base class for all exchange order-book feeds.

    Subclasses only need to implement:
      - create_new()       set _name / _interval
      - __call__()         open the WebSocket and drive process_event_to_data()

    Everything else — order-book delta application, slippage pricing,
    rolling volatility, periodic CSV logging — lives here once.
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        self._name: Optional[str] = None
        self._interval: Optional[str] = None
        self._data: Optional[OrderBook] = None
        self._vol: Optional[float] = None
        self._historical: deque = deque([], maxlen=60)       # mid prices, 1/s
        self._historical_ob: deque = deque([], maxlen=60)    # OrderBook snapshots, 1/s
        self._full_df: pd.DataFrame = pd.DataFrame()
        self.save_data: bool = save_data
        self.file_dir: Optional[str] = file_dir

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def name(self) -> Optional[str]:
        return self._name

    @property
    def interval(self) -> Optional[str]:
        return self._interval

    @property
    def data(self) -> Optional[OrderBook]:
        return self._data

    @property
    def volatility(self) -> Optional[float]:
        return self._vol

    @property
    def latest_price(self) -> float:
        return self._historical[-1]

    @property
    def latest_bid_price(self) -> float:
        return self._historical_ob[-1].best_bid_price

    @property
    def latest_ask_price(self) -> float:
        return self._historical_ob[-1].best_ask_price

    # ── Slippage pricing ──────────────────────────────────────────────────────

    def executed_buy_price(self, amount: float, amount_in_base: bool = True) -> float:
        """VWAP ask price for a buy of `amount` (base or quote)."""
        ob = self._data
        ask_orders = ob.ask_orders
        total_cost = 0.0

        if amount_in_base:
            remaining = amount
            for price, qty in ask_orders:
                if remaining <= qty:
                    total_cost += remaining * price
                    return total_cost / amount
                total_cost += qty * price
                remaining -= qty
        else:
            remaining_cost = amount
            total_base = 0.0
            for price, qty in ask_orders:
                available = qty * price
                if remaining_cost <= available:
                    total_base += remaining_cost / price
                    return amount / total_base
                total_cost += available
                total_base += qty
                remaining_cost -= available

        return ask_orders[-1][0]

    def executed_sell_price(self, amount: float, amount_in_base: bool = True) -> float:
        """VWAP bid price for a sell of `amount` (base or quote)."""
        ob = self._data
        bid_orders = ob.bid_orders
        total_proceeds = 0.0

        if amount_in_base:
            remaining = amount
            for price, qty in bid_orders:
                if remaining <= qty:
                    total_proceeds += remaining * price
                    return total_proceeds / amount
                total_proceeds += qty * price
                remaining -= qty
        else:
            remaining_cost = amount
            total_base = 0.0
            for price, qty in bid_orders:
                available = qty * price
                if remaining_cost <= available:
                    total_base += remaining_cost / price
                    return amount / total_base
                total_proceeds += available
                total_base += qty
                remaining_cost -= available

        return bid_orders[-1][0]

    # ── Order-book delta application ──────────────────────────────────────────

    @staticmethod
    def apply_orderbook_delta(
        orders: List[List[float]],
        deltas: list,
        descending: bool = True,
    ) -> List[List[float]]:
        """
        Apply a list of price-level deltas to an order-book side.

        Handles both 2-element [price, qty] deltas (Binance/Bybit)
        and 4-element [price, qty, _, _] deltas (OKX).
        """
        for delta in deltas:
            price = float(delta[0])
            qty = float(delta[1])
            if qty == 0:
                orders = [o for o in orders if float(o[0]) != price]
            else:
                for i, o in enumerate(orders):
                    if float(o[0]) == price:
                        orders[i] = [price, qty]
                        break
                else:
                    orders.append([price, qty])

        return sorted(orders, key=lambda x: -float(x[0]) if descending else float(x[0]))

    # ── Background stats loop ─────────────────────────────────────────────────

    async def update_current_stats(self):
        """
        Runs every second: append mid-price to rolling window and compute
        60-second realised volatility.
        """
        while True:
            try:
                if self._data:
                    mid = self._data.mid_price
                    self._historical.append(mid)
                    self._historical_ob.append(self._data)

                    if len(self._historical) == 60:
                        returns = [
                            (self._historical[i] - self._historical[i - 1]) / self._historical[i - 1]
                            for i in range(1, len(self._historical))
                        ]
                        self._vol = statistics.stdev(returns) * math.sqrt(0.8)

                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[{self._name}] update_current_stats error: {e}")
                break

    # ── Periodic CSV logging ──────────────────────────────────────────────────

    async def periodically_log_data(self, interval_secs: int = 60):
        while True:
            try:
                if not self._full_df.empty and self.file_dir:
                    snapshot = self._full_df.copy()
                    path = Path(self.file_dir) / f"{self._name}_ob.csv"
                    snapshot.to_csv(path)
                    logger.info(f"[{self._name}] OB snapshot saved to {path}")
                await asyncio.sleep(interval_secs)
            except Exception as e:
                logger.error(f"[{self._name}] periodically_log_data error: {e}")
                break

    async def _append_to_df(self, ob: OrderBook):
        row = ob.to_dataframe_row()
        self._full_df = pd.concat([self._full_df, row], ignore_index=True)

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def create_new(self, asset: str, interval: str = "1s"):
        """Configure the feed for a specific asset and interval."""
        ...

    @abstractmethod
    async def __call__(self):
        """Open the connection and run the feed until cancelled."""
        ...

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self._name})"
