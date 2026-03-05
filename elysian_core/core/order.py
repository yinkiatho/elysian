import datetime
from dataclasses import dataclass, field
from typing import Optional, Tuple

from elysian_core.core.enums import Side, OrderType, OrderStatus, RangeOrderType, Venue


@dataclass
class Order:
    """
    Generic exchange order — covers CEX limit and market orders.
    """
    id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    price: Optional[float] = None           # None for market orders
    filled_qty: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    timestamp: Optional[datetime.datetime] = None
    venue: Optional[Venue] = None
    client_order_id: Optional[str] = None
    fee: float = 0.0
    fee_currency: Optional[str] = None

    @property
    def remaining_qty(self) -> float:
        return self.quantity - self.filled_qty

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_active(self) -> bool:
        return self.status.is_active()

    def __str__(self) -> str:
        return (
            f"Order({self.id} | {self.symbol} {self.side.value} {self.order_type.value} | "
            f"qty={self.quantity} filled={self.filled_qty} price={self.price} | "
            f"status={self.status.value})"
        )


@dataclass
class LimitOrder:
    """
    LP limit order used by Chainflip-style AMMs.
    """
    id: int
    base_asset: str
    quote_asset: str
    side: Side
    price: float
    amount: float
    lp_account: Optional[str] = None
    timestamp: Optional[datetime.datetime] = None
    volatility: float = 0.0

    def __str__(self) -> str:
        return (
            f"LimitOrder({self.base_asset}/{self.quote_asset} {self.side.value} | "
            f"price={self.price} amount={self.amount} id={self.id} lp={self.lp_account})"
        )


@dataclass
class RangeOrder:
    """
    LP range order (concentrated liquidity) used by Chainflip-style AMMs.
    """
    id: int
    base_asset: str
    quote_asset: str
    lower_price: float
    upper_price: float
    order_type: RangeOrderType
    amount: Optional[float] = None
    min_amounts: Optional[Tuple[float, float]] = None
    max_amounts: Optional[Tuple[float, float]] = None
    lp_account: Optional[str] = None
    timestamp: Optional[datetime.datetime] = None

    def __str__(self) -> str:
        return (
            f"RangeOrder({self.base_asset}/{self.quote_asset} | "
            f"[{self.lower_price}, {self.upper_price}] amount={self.amount} id={self.id} lp={self.lp_account})"
        )


