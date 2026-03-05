import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import pandas as pd


@dataclass
class OrderBook:
    """
    Normalised order book snapshot used by all exchange connectors.
    Replaces the misnamed BinanceOrderBook that was used across Binance, Bybit and OKX feeds.
    """
    last_update_id: int
    last_timestamp: int
    ticker: str
    interval: Optional[str]
    best_bid_price: float
    best_bid_amount: float
    best_ask_price: float
    best_ask_amount: float
    bid_orders: List[List[float]]   # [[price, qty], ...]  sorted descending
    ask_orders: List[List[float]]   # [[price, qty], ...]  sorted ascending

    @property
    def mid_price(self) -> float:
        return (self.best_bid_price + self.best_ask_price) / 2

    @property
    def spread(self) -> float:
        return self.best_ask_price - self.best_bid_price

    @property
    def spread_bps(self) -> float:
        return self.spread / self.mid_price * 10_000

    def to_dataframe_row(self) -> pd.DataFrame:
        data = {
            "last_update_id": self.last_update_id,
            "ticker": self.ticker,
            "interval": self.interval,
            "best_bid_price": self.best_bid_price,
            "best_bid_amount": self.best_bid_amount,
            "best_ask_price": self.best_ask_price,
            "best_ask_amount": self.best_ask_amount,
            "bid_orders": [self.bid_orders],
            "ask_orders": [self.ask_orders],
        }
        return pd.DataFrame([data])

    def __str__(self) -> str:
        return (
            f"OrderBook({self.ticker} | "
            f"bid={self.best_bid_price} x {self.best_bid_amount} | "
            f"ask={self.best_ask_price} x {self.best_ask_amount} | "
            f"spread={self.spread:.4f})"
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


