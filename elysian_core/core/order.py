import datetime
from dataclasses import dataclass, field
from typing import Optional, Tuple

from elysian_core.core.enums import Side, OrderType, OrderStatus, RangeOrderType, Venue
from elysian_core.core.order_fsm import validate_order_transition, is_terminal
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


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
    use_quote_order_qty: bool = False
    avg_fill_price: float = 0.0
    filled_qty: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    timestamp: Optional[datetime.datetime] = None
    venue: Optional[Venue] = None
    commission: float = 0.0
    commission_asset: Optional[str] = None
    last_updated_timestamp: Optional[int] = None
    strategy_id: Optional[int] = None      # set by ExecutionEngine at placement time

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.quantity - self.filled_qty)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_active(self) -> bool:
        return self.status.is_active()

    @property
    def is_terminal(self) -> bool:
        """True if the order is in a terminal state (FILLED, CANCELLED, REJECTED)."""
        return is_terminal(self.status)

    def transition_to(self, new_status: OrderStatus) -> None:
        """Validate and apply an order status transition.

        Uses warn-mode: logs invalid transitions but always applies them
        because the exchange is the source of truth.
        """
        validate_order_transition(self.status, new_status, order_id=self.id)
        self.status = new_status

    def update_fill(self, filled_qty: float, fill_price: float):
        prev_filled = self.filled_qty
        if filled_qty <= prev_filled:
            return
        self.filled_qty = filled_qty
        # running weighted average fill price
        if filled_qty > 0:
            self.avg_fill_price = (
                (prev_filled * self.avg_fill_price + (filled_qty - prev_filled) * fill_price)
                / filled_qty
            )
        new_status = OrderStatus.FILLED if filled_qty >= self.quantity else OrderStatus.PARTIALLY_FILLED
        self.transition_to(new_status)
        self.last_updated_timestamp = int(datetime.datetime.now().timestamp() * 1000)
        
        
    def __str__(self) -> str:
        fill_pct = (self.filled_qty / self.quantity * 100) if self.quantity else 0
        return (
            f"┌─ Order {self.id}\n"
            f"│  Symbol   : {self.symbol:<10} │  Venue  : {self.venue.value if self.venue else 'N/A'}\n"
            f"│  Side     : {self.side.value:<10} │  Type   : {self.order_type.value}\n"
            f"│  Qty      : {self.quantity:<10} │  Filled : {self.filled_qty} ({fill_pct:.1f}%)  │  Remaining: {self.remaining_qty}\n"
            f"│  Price    : {str(self.price) if self.price is not None else 'MARKET':<10} │  Avg Fill: {self.avg_fill_price}\n"
            f"│  Status   : {self.status.value:<10} │  Commission: {self.commission} {self.commission_asset or ''}\n"
            f"└─ Updated  : {self.last_updated_timestamp}"
        )

@dataclass
class LimitOrder:
    """
    Generic LimitOrder Class
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


