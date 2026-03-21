"""
Portfolio: centralized position, cash, and risk state for a single account.

Responsibilities:
  - Position tracking with avg-entry / realized PnL bookkeeping
  - Weight vector computation (current allocation vs cash)
  - Risk metrics: peak equity, drawdown, running P&L
  - Fill-level audit trail (trade history)
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from elysian_core.core.enums import Side, Venue
from elysian_core.core.position import Position
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


# ── Fill record (audit trail) ────────────────────────────────────────────────

@dataclass(frozen=True)
class Fill:
    """Immutable record of a single fill applied to the portfolio."""

    symbol: str
    venue: Venue
    side: Side
    quantity: float             # always positive
    price: float
    commission: float = 0.0
    commission_asset: str = ""
    timestamp: int = 0          # epoch ms
    order_id: str = ""


# ── Portfolio ─────────────────────────────────────────────────────────────────

class Portfolio:
    """Tracks all positions, cash, risk metrics, and trade history.

    Parameters:
        initial_cash: Starting quote-currency balance (e.g. USDT).
        max_history:  Maximum number of fills retained in memory.
    """

    def __init__(self, initial_cash: float = 0.0, max_history: int = 10_000):
        self._positions: Dict[str, Position] = {}
        self._cash: float = initial_cash

        # ── Risk metrics ─────────────────────────────────────────────────
        self._peak_equity: float = initial_cash
        self._max_drawdown: float = 0.0
        self._total_realized_pnl: float = 0.0
        self._total_commission: float = 0.0

        # ── Trade history (ring buffer) ──────────────────────────────────
        self._fills: Deque[Fill] = deque(maxlen=max_history)

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Position & Cash
    # ══════════════════════════════════════════════════════════════════════════

    def position(self, symbol: str) -> Position:
        """Return the position for *symbol*, or a flat placeholder."""
        return self._positions.get(symbol, Position(symbol=symbol))

    @property
    def positions(self) -> Dict[str, Position]:
        """Shallow copy of all tracked positions."""
        return dict(self._positions)

    @property
    def active_positions(self) -> Dict[str, Position]:
        """Only non-flat positions."""
        return {s: p for s, p in self._positions.items() if not p.is_flat()}

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def fills(self) -> List[Fill]:
        """Full trade history (oldest first)."""
        return list(self._fills)

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Valuation
    # ══════════════════════════════════════════════════════════════════════════

    def total_value(self, mark_prices: Dict[str, float]) -> float:
        """NAV = cash + sum(position_notional)."""
        pos_value = sum(
            pos.quantity * mark_prices.get(pos.symbol, pos.avg_entry_price)
            for pos in self._positions.values()
        )
        return self._cash + pos_value

    def unrealized_pnl(self, mark_prices: Dict[str, float]) -> float:
        return sum(
            pos.unrealized_pnl(mark_prices[pos.symbol])
            for pos in self._positions.values()
            if pos.symbol in mark_prices
        )

    @property
    def total_realized_pnl(self) -> float:
        """Cumulative realized P&L across all positions."""
        return self._total_realized_pnl

    @property
    def total_commission(self) -> float:
        """Cumulative commissions paid."""
        return self._total_commission

    def net_pnl(self, mark_prices: Dict[str, float]) -> float:
        """Realized + unrealized - commissions."""
        return self._total_realized_pnl + self.unrealized_pnl(mark_prices) - self._total_commission

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Weight vector
    # ══════════════════════════════════════════════════════════════════════════

    def current_weights(self, mark_prices: Dict[str, float]) -> Dict[str, float]:
        """Compute the live portfolio weight vector.

        Returns a dict mapping symbol -> weight (notional / NAV).
        Cash weight is accessible via :meth:`cash_weight`.
        """
        nav = self.total_value(mark_prices)
        if nav <= 0:
            return {}
        return {
            sym: (pos.quantity * mark_prices.get(sym, pos.avg_entry_price)) / nav
            for sym, pos in self._positions.items()
            if not pos.is_flat() and sym in mark_prices
        }

    def cash_weight(self, mark_prices: Dict[str, float]) -> float:
        """Fraction of NAV held as cash."""
        nav = self.total_value(mark_prices)
        return self._cash / nav if nav > 0 else 1.0

    def gross_exposure(self, mark_prices: Dict[str, float]) -> float:
        """Sum of abs(weight) across all positions."""
        weights = self.current_weights(mark_prices)
        return sum(abs(w) for w in weights.values())

    def net_exposure(self, mark_prices: Dict[str, float]) -> float:
        """Sum of signed weights (long - short)."""
        weights = self.current_weights(mark_prices)
        return sum(weights.values())

    def long_exposure(self, mark_prices: Dict[str, float]) -> float:
        """Sum of positive weights."""
        weights = self.current_weights(mark_prices)
        return sum(w for w in weights.values() if w > 0)

    def short_exposure(self, mark_prices: Dict[str, float]) -> float:
        """Sum of absolute negative weights."""
        weights = self.current_weights(mark_prices)
        return sum(abs(w) for w in weights.values() if w < 0)

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Risk metrics
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def peak_equity(self) -> float:
        """Highest NAV observed (updated on each :meth:`mark_to_market` call)."""
        return self._peak_equity

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough decline observed, as a positive fraction."""
        return self._max_drawdown

    def current_drawdown(self, mark_prices: Dict[str, float]) -> float:
        """Current drawdown from peak, as a positive fraction."""
        nav = self.total_value(mark_prices)
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - nav) / self._peak_equity)

    def mark_to_market(self, mark_prices: Dict[str, float]):
        """Update peak-equity and max-drawdown from current mark prices.

        Call this periodically (e.g. every kline) to keep risk metrics fresh.
        """
        nav = self.total_value(mark_prices)
        if nav > self._peak_equity:
            self._peak_equity = nav
        if self._peak_equity > 0:
            dd = (self._peak_equity - nav) / self._peak_equity
            if dd > self._max_drawdown:
                self._max_drawdown = dd

    # ══════════════════════════════════════════════════════════════════════════
    #  WRITE — Position updates
    # ══════════════════════════════════════════════════════════════════════════

    def update_position(
        self,
        symbol: str,
        qty_delta: float,
        price: float,
        commission: float = 0.0,
        commission_asset: str = "",
        venue: Venue = Venue.BINANCE,
        order_id: str = "",
    ):
        """Apply a fill to the portfolio.

        ``qty_delta > 0`` → bought, ``< 0`` → sold.

        Updates:
          - Position avg entry price and realized PnL
          - Cash balance
          - Commission totals
          - Trade history
          - Portfolio-level realized PnL accumulator
        """
        pos = self._positions.get(symbol, Position(symbol=symbol, venue=venue))
        new_qty = pos.quantity + qty_delta

        same_direction = (pos.quantity >= 0) == (qty_delta >= 0) or pos.quantity == 0

        if same_direction:
            # Adding to position — update average entry
            total_cost = pos.avg_entry_price * pos.quantity + price * qty_delta
            pos.avg_entry_price = total_cost / new_qty if new_qty != 0 else 0.0
            pos.quantity = new_qty
        else:
            # Closing (partially or fully, possibly flipping)
            closing_qty = min(abs(qty_delta), abs(pos.quantity))
            sign = 1 if pos.quantity > 0 else -1
            pnl = (price - pos.avg_entry_price) * closing_qty * sign

            pos.realized_pnl += pnl
            self._total_realized_pnl += pnl

            pos.quantity = new_qty
            if new_qty == 0:
                pos.avg_entry_price = 0.0
            elif (new_qty > 0) != (pos.quantity - qty_delta > 0):
                # Flipped direction — new entry price
                pos.avg_entry_price = price

        # Commission bookkeeping
        pos.total_commission += commission
        self._total_commission += commission

        # Cash adjustment
        self._positions[symbol] = pos
        self._cash -= qty_delta * price
        self._cash -= commission  # commissions reduce cash

        # Audit trail
        side = Side.BUY if qty_delta > 0 else Side.SELL
        self._fills.append(Fill(
            symbol=symbol,
            venue=venue,
            side=side,
            quantity=abs(qty_delta),
            price=price,
            commission=commission,
            commission_asset=commission_asset,
            timestamp=int(time.time() * 1000),
            order_id=order_id,
        ))

    def set_cash(self, amount: float):
        self._cash = amount

    def adjust_cash(self, delta: float):
        self._cash += delta

    # ══════════════════════════════════════════════════════════════════════════
    #  Snapshot / display
    # ══════════════════════════════════════════════════════════════════════════

    def snapshot(self, mark_prices: Optional[Dict[str, float]] = None) -> dict:
        """Serializable snapshot of current portfolio state."""
        out = {
            "cash": self._cash,
            "total_realized_pnl": self._total_realized_pnl,
            "total_commission": self._total_commission,
            "peak_equity": self._peak_equity,
            "max_drawdown": self._max_drawdown,
            "num_fills": len(self._fills),
            "positions": {sym: str(pos) for sym, pos in self._positions.items()},
        }
        if mark_prices:
            out["nav"] = self.total_value(mark_prices)
            out["unrealized_pnl"] = self.unrealized_pnl(mark_prices)
            out["current_drawdown"] = self.current_drawdown(mark_prices)
            out["gross_exposure"] = self.gross_exposure(mark_prices)
            out["net_exposure"] = self.net_exposure(mark_prices)
        return out

    def __str__(self) -> str:
        active = [str(p) for p in self._positions.values() if not p.is_flat()]
        return (
            f"Portfolio(cash={self._cash:.2f} "
            f"realized_pnl={self._total_realized_pnl:.4f} "
            f"comm={self._total_commission:.4f} "
            f"dd={self._max_drawdown:.4%} | "
            f"{', '.join(active) or 'flat'})"
        )
