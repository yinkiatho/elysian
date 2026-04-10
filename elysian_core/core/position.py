"""
Single-asset position tracker.

Separated from :mod:`portfolio` so it can be imported independently
(e.g. by exchange connectors that only need position state).
"""

from dataclasses import dataclass, field
from typing import Dict, Optional
from elysian_core.core.enums import Venue


@dataclass
class Position:
    """Tracks a single open position in one asset.

    Attributes:
        symbol:              Trading pair (e.g. "ETHUSDT").
        venue:               Exchange this position lives on.
        quantity:            Signed quantity.  >0 → long, <0 → short.
        avg_entry_price:     Volume-weighted average entry price.
        realized_pnl:        Cumulative realized P&L from partial/full closes.
        commission_by_asset: Fees paid per commission asset across all fills
                             (e.g. {"BNB": 3.15e-05, "USDT": 0.01}).
                             Non-stablecoin commissions are also deducted from
                             the corresponding position qty in ShadowBook.
    """

    symbol: str
    venue: Venue = Venue.BINANCE
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    commission_by_asset: Dict[str, float] = field(default_factory=dict)
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

    def net_pnl(self, mark_price: float, asset_prices: Optional[Dict[str, float]] = None) -> float:
        """Total P&L minus commissions converted to USDT.

        asset_prices maps raw asset names (e.g. 'BNB') to their USDT price.
        Stablecoins default to 1.0. Use ShadowBook.net_pnl() for the
        authoritative portfolio-level figure which has all mark prices.
        """
        prices = asset_prices or {}
        comm_usdt = sum(
            qty * prices.get(asset, 1.0)
            for asset, qty in self.commission_by_asset.items()
        )
        return self.total_pnl(mark_price) - comm_usdt

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
        comm_str = ", ".join(f"{qty:.6f} {asset}" for asset, qty in self.commission_by_asset.items()) or "none"
        return (
            f"Position: \n"
            f"{'Symbol':<10} {self.symbol:<10}\n"
            f"{'Side':<10} {side:<10}\n"
            f"{'Quantity':<10} {self.quantity:>10.6f}\n"
            f"{'Avg Entry':<10} {self.avg_entry_price:>10.6f}\n"
            f"{'Realized PnL':<10} {self.realized_pnl:>10.4f}\n"
            f"{'Commission':<10} {comm_str}"
        )
