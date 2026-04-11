"""
Portfolio: lightweight NAV aggregator across per-strategy ShadowBooks.

In sub-account mode each strategy has its own ShadowBook as the authoritative
position ledger.  Portfolio's only role is to sum up NAV, cash, and positions
across all registered ShadowBooks for monitoring and reporting purposes.

No event subscriptions, no fill attribution, no position tracking of its own.
Call :meth:`register_shadow_book` once per strategy after each ShadowBook is
created, then read :meth:`aggregate_nav`, :meth:`snapshot`, etc. at any time.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from elysian_core.core.enums import Side, Venue
from elysian_core.config.app_config import AppConfig
from elysian_core.db.models import PortfolioSnapshot
from elysian_core.core.position import Position
import elysian_core.utils.logger as log
from datetime import datetime, timezone, timedelta

UTC8 = timezone(timedelta(hours=8))

if TYPE_CHECKING:
    from elysian_core.core.shadow_book import ShadowBook


# ── Fill record (audit trail) — imported by shadow_book.py ──────────────────

@dataclass(frozen=True)
class Fill:
    """Immutable record of a single fill applied to a shadow book."""

    symbol: str
    venue: Venue
    side: Side
    quantity: float             # always positive
    price: float
    commission: float = 0.0
    commission_asset: str = ""
    timestamp: int = 0          # epoch ms
    order_id: str = ""
    
    @property
    def timestamp_str(self) -> str:
        """Return UTC+8 datetime string: 'YYYY-MM-DD HH:MM:SS +0800'."""
        dt = datetime.fromtimestamp(self.timestamp / 1000, tz=timezone.utc).astimezone(UTC8)
        return dt.strftime("%Y-%m-%d %H:%M:%S %z")

# ── Portfolio ─────────────────────────────────────────────────────────────────

class Portfolio:
    """Thin NAV aggregator across registered per-strategy ShadowBooks.

    Parameters
    ----------
    venue:
        The exchange venue this portfolio monitors.
    cfg:
        Application config (used only for snapshot persistence).
    """

    def __init__(self, venue: Venue = Venue.BINANCE, cfg: Optional[AppConfig] = None):
        self.logger = log.setup_custom_logger("root")
        self.venue = venue
        self.cfg = cfg
        self._shadow_books: Dict[int, "ShadowBook"] = {}  # strategy_id -> ShadowBook
        self._peak_equity: float = 0.0
        self._max_drawdown: float = 0.0
        self._last_snapshot_time: int = 0
        self._nav = 0.0
        self._cash = 0.0
        self._positions: Dict[str, Position] = {}

    # ── Registration ─────────────────────────────────────────────────────────

    def register_shadow_book(self, shadow_book: "ShadowBook") -> None:
        """Register a ShadowBook for aggregation reporting."""
        self._shadow_books[shadow_book.strategy_id] = shadow_book
        self.logger.info(
            f"[Portfolio@{self.venue.value}] Registered ShadowBook "
            f"for strategy {shadow_book.strategy_id}"
        )

    # ── Aggregation reads ─────────────────────────────────────────────────────

    def aggregate_nav(self) -> float:
        """Sum of NAV across all registered shadow books."""
        return sum(sb.nav for sb in self._shadow_books.values())

    def aggregate_cash(self) -> float:
        """Sum of cash across all registered shadow books."""
        return sum(sb.cash for sb in self._shadow_books.values())

    def aggregate_positions(self) -> Dict[str, Any]:
        """Merged position dict across all registered shadow books.

        Where multiple strategies hold the same symbol, quantities are summed.
        """
        merged: Dict[str, Any] = {}
        for sb in self._shadow_books.values():
            for sym, pos in sb.positions.items():
                if sym not in merged:
                    merged[sym] = pos
                else:
                    existing = merged[sym]
                    total_qty = existing.quantity + pos.quantity
                    if total_qty == 0:
                        merged.pop(sym, None)
                    else:
                        # Weighted average entry price
                        avg_entry = (
                            (existing.avg_entry_price * existing.quantity
                             + pos.avg_entry_price * pos.quantity) / total_qty
                        )
                        merged[sym] = Position(
                            symbol=sym,
                            venue=existing.venue,
                            quantity=total_qty,
                            avg_entry_price=avg_entry,
                        )
        return merged

    def total_value(self) -> float:
        """Alias for aggregate_nav() — satisfies duck-type interfaces."""
        return self.aggregate_nav()

    @property
    def nav(self) -> float:
        return self.aggregate_nav()

    @property
    def cash(self) -> float:
        return self.aggregate_cash()
    
    @property
    def positions(self) -> Dict[str, Any]:
        return self.aggregate_positions()
    
    @property
    def unrealized_pnl(self) -> float:
        """Sum of unrealized PnL across all registered shadow books."""
        return sum(sb.unrealized_pnl() for sb in self._shadow_books.values())
    
    @property
    def realized_pnl(self) -> float:
        """Sum of realized PnL across all registered shadow books."""
        return sum(sb.realized_pnl for sb in self._shadow_books.values())

    @property
    def name(self) -> str:
        return f"Portfolio@{self.venue.value}"
    
    def gross_exposure(self) -> float:
        """Sum of absolute position weights across all registered shadow books."""
        return sum(sb.gross_exposure() for sb in self._shadow_books.values())
    
    def net_exposure(self) -> float:
        """Sum of position weights across all registered shadow books."""
        return sum(sb.net_exposure() for sb in self._shadow_books.values())
    
    def long_exposure(self) -> float:
        """Sum of long position weights across all registered shadow books."""
        return sum(sb.long_exposure() for sb in self._shadow_books.values())
    
    def short_exposure(self) -> float:
        """Sum of short position weights across all registered shadow books."""
        return sum(sb.short_exposure() for sb in self._shadow_books.values())

    # ── Snapshot / display ────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Current aggregate state across all shadow books."""
        positions = self.aggregate_positions()
        return {
            "venue": self.venue.value,
            "aggregate_nav": self.aggregate_nav(),
            "aggregate_cash": self.aggregate_cash(),
            "num_strategies": len(self._shadow_books),
            "strategy_ids": list(self._shadow_books.keys()),
            "positions": {sym: str(pos) for sym, pos in positions.items()},
            "shadow_book_navs": {
                sid: sb.nav for sid, sb in self._shadow_books.items()
            },
        }

    def save_snapshot(self):
        """Persist aggregate snapshot to the database."""
        self._nav = self.aggregate_nav()
        self._cash = self.aggregate_cash()
        self._positions = self.aggregate_positions()
        positions_json = {
            sym: {
                "qty": pos.quantity,
                "avg_entry": pos.avg_entry_price,
            }
            for sym, pos in self._positions.items()
        }
        # Update the peak equity and max drawdown
        if self._nav > self._peak_equity:
            self._peak_equity = self._nav
        drawdown = self._peak_equity - self._nav
        if drawdown > self._max_drawdown:
            self._max_drawdown = drawdown
        
        if self._peak_equity > 0:
            current_drawdown = max(0.0, (self._peak_equity - self._nav) / self._peak_equity)
        else:
            current_drawdown = 0.0
        
        try:
            PortfolioSnapshot.create(
                strategy_id=self.cfg.meta.strategy_id if self.cfg else 0,
                venue=self.venue,
                nav=self._nav,
                cash=self._cash,
                unrealized_pnl=self.unrealized_pnl,
                realized_pnl=self.realized_pnl,
                total_commission=0.0,
                peak_equity=self._peak_equity,
                max_drawdown=self._max_drawdown,
                current_drawdown=current_drawdown,
                gross_exposure=self.gross_exposure(),
                net_exposure=self.net_exposure(),
                positions=positions_json,
                weights={},
                mark_prices={},
                num_fills=0,
            )
            self.logger.info(
                f"[Portfolio@{self.venue.value}] Snapshot saved: "
                f"nav={self._nav:.2f} cash={self._cash:.2f} strategies={len(self._shadow_books)}"
            )
        except Exception as e:
            self.logger.error(f"[Portfolio@{self.venue.value}] Failed to save snapshot: {e}", exc_info=True)

    def __str__(self) -> str:
        return (
            f"Portfolio(venue={self.venue.value} "
            f"nav={self.aggregate_nav():.2f} "
            f"strategies={len(self._shadow_books)})"
        )
