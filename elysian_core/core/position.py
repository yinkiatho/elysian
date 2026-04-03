"""
Single-asset position tracker.

Separated from :mod:`portfolio` so it can be imported independently
(e.g. by exchange connectors that only need position state).
"""

from dataclasses import dataclass
from elysian_core.core.enums import Venue


@dataclass
class Position:
    """Tracks a single open position in one asset.

    Attributes:
        symbol:          Trading pair (e.g. "ETHUSDT").
        venue:           Exchange this position lives on.
        quantity:        Signed quantity.  >0 → long, <0 → short.
        avg_entry_price: Volume-weighted average entry price.
        realized_pnl:    Cumulative realized P&L from partial/full closes.
        total_commission: Total fees paid across all fills for this position.
    """

    symbol: str
    venue: Venue = Venue.BINANCE
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    total_commission: float = 0.0
    commission_asset: str = "USDT"
    locked_quantity: float = 0.0  # reserved qty for pending SELL limit orders

    # ── Computed properties ──────────────────────────────────────────────────

    @property
    def free_quantity(self) -> float:
        """Quantity available for new sell orders (excludes locked LIMIT SELL reservations)."""
        return max(0.0, self.quantity - self.locked_quantity)


    def unrealized_pnl(self, mark_price: float) -> float:
        """Mark-to-market unrealized P&L."""
        return (mark_price - self.avg_entry_price) * self.quantity

    def total_pnl(self, mark_price: float) -> float:
        """Realized + unrealized, before commissions."""
        return self.realized_pnl + self.unrealized_pnl(mark_price)

    def net_pnl(self, mark_price: float) -> float:
        """Total P&L minus commissions paid."""
        return self.total_pnl(mark_price) - self.total_commission

    def notional(self, mark_price: float) -> float:
        """Absolute notional exposure at *mark_price*."""
        return abs(self.quantity) * mark_price

    def is_flat(self) -> bool:
        return self.quantity == 0.0

    def is_long(self) -> bool:
        return self.quantity > 0.0

    def is_short(self) -> bool:
        return self.quantity < 0.0

    def __str__(self) -> str:
        side = "LONG" if self.quantity > 0 else "SHORT" if self.quantity < 0 else "FLAT"
        return (
            f"Position({self.symbol} {side} qty={self.quantity:.6f} "
            f"avg_entry={self.avg_entry_price:.6f} "
            f"realized_pnl={self.realized_pnl:.4f} "
            f"comm={self.total_commission:.4f})"
        )
