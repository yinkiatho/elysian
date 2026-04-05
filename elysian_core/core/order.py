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
    last_fill_price: float = 0.0          # last executed price (field "L"); for incremental fill
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
        Dispatches named on_enter_* hooks after the status is set.
        """
        validate_order_transition(self.status, new_status, order_id=self.id)
        self.status = new_status
        if new_status == OrderStatus.FILLED:
            self.on_enter_filled()
        elif new_status == OrderStatus.PARTIALLY_FILLED:
            self.on_enter_partially_filled()
        elif new_status == OrderStatus.CANCELLED:
            self.on_enter_cancelled()
        elif new_status == OrderStatus.REJECTED:
            self.on_enter_rejected()
        elif new_status == OrderStatus.EXPIRED:
            self.on_enter_expired()

    # ── State entry hooks (no-ops; override in subclasses) ────────────────

    def on_enter_filled(self) -> None:
        """Called when order enters FILLED state. Override in subclasses."""
        pass

    def on_enter_partially_filled(self) -> None:
        """Called when order enters PARTIALLY_FILLED state."""
        pass

    def on_enter_cancelled(self) -> None:
        """Called when order enters CANCELLED state."""
        pass

    def on_enter_rejected(self) -> None:
        """Called when order enters REJECTED state."""
        pass

    def on_enter_expired(self) -> None:
        """Called when order enters EXPIRED state."""
        pass

    def update_fill(self, filled_qty: float, fill_price: float):
        """Update fill quantities and weighted average price.

        Does NOT transition status — callers must call transition_to() with the
        authoritative exchange status after calling this method.
        """
        prev_filled = self.filled_qty
        if filled_qty <= prev_filled:
            return
        self.filled_qty = filled_qty
        if filled_qty > 0:
            self.avg_fill_price = (
                (prev_filled * self.avg_fill_price + (filled_qty - prev_filled) * fill_price)
                / filled_qty
            )
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
class ActiveOrder(Order):
    """Order being actively tracked — adds fill-delta bookkeeping via _prev_filled.

    Inherits all fields from Order.  The extra field has a default so it
    satisfies the dataclass inheritance rule (defaults after non-defaults).
    """
    _prev_filled: float = field(default=0.0, repr=False, compare=False)

    def sync_from_event(self, event_order: "Order") -> float:
        """Sync state from an incoming exchange event.  Returns incremental fill delta."""
        delta = max(0.0, event_order.filled_qty - self._prev_filled)
        if delta > 0:
            self.update_fill(
                event_order.filled_qty,
                event_order.last_fill_price
            )
            self._prev_filled = event_order.filled_qty
        
        # Transitions to next status if event_order is'ahead'
        self.transition_to(event_order.status)
        self.commission += event_order.commission  # accumulate per-execution commission (Binance field "n")
        self.commission_asset = event_order.commission_asset
        self.last_updated_timestamp = event_order.last_updated_timestamp
        return delta


@dataclass
class ActiveLimitOrder(ActiveOrder):
    """ActiveOrder with balance reservation for pending LIMIT orders.

    Tracks how much cash (BUY) or quantity (SELL) is reserved for this order.
    Reservation is initialized on placement and released incrementally as fills
    arrive, or drained entirely on terminal (cancel/expire).
    """
    reserved_cash: float = field(default=0.0, repr=False, compare=False)
    reserved_qty: float = field(default=0.0, repr=False, compare=False)

    def initialize_reservation(self) -> None:
        """Set initial reservation amounts based on order side."""
        if self.side == Side.BUY and self.price is not None:
            self.reserved_cash = self.quantity * self.price
            self.reserved_qty = 0.0
        else:  # SELL
            self.reserved_qty = self.quantity
            self.reserved_cash = 0.0

    def release_partial(self, delta_filled: float) -> tuple:
        """Release reservation proportional to delta_filled.
        Returns (cash_released, qty_released)."""
        if self.side == Side.BUY and self.price is not None:
            release = min(delta_filled * self.price, self.reserved_cash)
            self.reserved_cash = max(0.0, self.reserved_cash - release)
            return release, 0.0
        else:
            release = min(delta_filled, self.reserved_qty)
            self.reserved_qty = max(0.0, self.reserved_qty - release)
            return 0.0, release

    def release_all(self) -> tuple:
        """Drain all remaining reservation on terminal.  Returns (cash, qty)."""
        cash, qty = self.reserved_cash, self.reserved_qty
        self.reserved_cash = 0.0
        self.reserved_qty = 0.0
        return cash, qty


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


