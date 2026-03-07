import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from collections import OrderedDict
import pandas as pd
from sortedcontainers import SortedDict
import asyncio


@dataclass
class OrderBook:
    """
    Normalised order book snapshot used by all exchange connectors.
    bid_orders: OrderedDict keyed by price (descending) → quantity
    ask_orders: OrderedDict keyed by price (ascending)  → quantity
    """
    last_update_id: int
    last_timestamp: int
    ticker: str
    interval: Optional[str]
    bid_orders: SortedDict = field(default_factory=SortedDict)
    ask_orders: SortedDict = field(default_factory=SortedDict)


    @classmethod
    def from_lists(
        cls,
        last_update_id: int,
        last_timestamp: int,
        ticker: str,
        interval: Optional[str],
        bid_levels: list[list[float]],
        ask_levels: list[list[float]],
    ) -> "OrderBook":

        bids = SortedDict()
        asks = SortedDict()

        for price, qty in bid_levels:
            bids[float(price)] = float(qty)

        for price, qty in ask_levels:
            asks[float(price)] = float(qty)

        return cls(
            last_update_id=last_update_id,
            last_timestamp=last_timestamp,
            ticker=ticker,
            interval=interval,
            bid_orders=bids,
            ask_orders=asks,
        )

    # ------------------------------------------------------------------
    # Mutation helpers (apply incremental updates)
    # ------------------------------------------------------------------

    def apply_update(self, side: str, price: float, qty: float) -> None:

        book = self.bid_orders if side == "bid" else self.ask_orders
        price = float(price)
        qty = float(qty)

        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty

    # ------------------------------------------------------------------
    # Best bid / ask derived from the ordered dicts
    # ------------------------------------------------------------------

    @property
    def best_bid_price(self) -> float:
        if not self.bid_orders:
            return 0.0
        return self.bid_orders.peekitem(-1)[0]

    @property
    def best_bid_amount(self) -> float:
        if not self.bid_orders:
            return 0.0
        return self.bid_orders.peekitem(-1)[1]

    @property
    def best_ask_price(self) -> float:
        if not self.ask_orders:
            return 0.0
        return self.ask_orders.peekitem(0)[0]

    @property
    def best_ask_amount(self) -> float:
        if not self.ask_orders:
            return 0.0
        return self.ask_orders.peekitem(0)[1]

    @property
    def mid_price(self) -> float:
        return (self.best_bid_price + self.best_ask_price) / 2

    @property
    def spread(self) -> float:
        return self.best_ask_price - self.best_bid_price

    @property
    def spread_bps(self) -> float:
        if self.mid_price == 0:
            return 0.0
        return self.spread / self.mid_price * 10_000

    def __str__(self) -> str:
        return (
            f"OrderBook({self.ticker} | "
            f"bid={self.best_bid_price} x {self.best_bid_amount} | "
            f"ask={self.best_ask_price} x {self.best_ask_amount} | "
            f"spread={self.spread:.6f})"
        )

@dataclass
class Kline:
    """
    OHLCV candle, exchange-agnostic.
    """
    ticker: str
    interval: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None

    def __str__(self) -> str:
        return (
            f"Kline({self.ticker} {self.interval} | "
            f"O={self.open} H={self.high} L={self.low} C={self.close} V={self.volume})"
        )





######################################################################################################################
######################################### Exchange Specific Superclasses #############################################
######################################################################################################################



class AsterOrderBook(OrderBook):
    """
    Aster-specific order book with additional fields and methods.
    Inherits from the generic OrderBook dataclass.
    """
    
    async def apply_update_lists(self, side: str, price_levels: list) -> None:
        """
        Apply multiple level updates from a list of [price, qty] pairs.
        """
        side_to_update = self.bid_orders if side == "bid" else self.ask_orders
        for price, qty in price_levels:
            price, qty = float(price), float(qty)
            if qty == 0.0:
                side_to_update.pop(price, None)
            else:
                side_to_update[price] = qty
                

                
    
    async def apply_both_updates(
        self,
        bid_levels: Optional[list[list]],
        ask_levels: Optional[list[list]],
    ) -> None:
        """
        Apply both bid and ask updates concurrently.
        """
        await asyncio.gather(
            self.apply_update_lists("bid", bid_levels or []),
            self.apply_update_lists("ask", ask_levels or []),
        )
        
                
        
                