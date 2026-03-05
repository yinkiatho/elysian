from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class Position:
    """
    Tracks a single open position in one asset.
    quantity > 0 → long, < 0 → short.
    """
    symbol: str
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0

    def unrealized_pnl(self, mark_price: float) -> float:
        return (mark_price - self.avg_entry_price) * self.quantity

    def notional(self, mark_price: float) -> float:
        return abs(self.quantity) * mark_price

    def is_flat(self) -> bool:
        return self.quantity == 0.0

    def __str__(self) -> str:
        return (
            f"Position({self.symbol} | qty={self.quantity:.6f} "
            f"avg_entry={self.avg_entry_price:.6f} realized_pnl={self.realized_pnl:.4f})"
        )


class Portfolio:
    """
    Tracks all positions and cash balance for a single account.
    """

    def __init__(self, initial_cash: float = 0.0):
        self._positions: Dict[str, Position] = {}
        self._cash: float = initial_cash

    # ── read ──────────────────────────────────────────────────────────────────

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol=symbol))

    @property
    def positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    @property
    def cash(self) -> float:
        return self._cash

    def total_value(self, mark_prices: Dict[str, float]) -> float:
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

    # ── write ─────────────────────────────────────────────────────────────────

    def update_position(self, symbol: str, qty_delta: float, price: float):
        """
        Apply a fill to the portfolio.
        qty_delta > 0 → bought, < 0 → sold.
        Updates average entry and realized PnL on partial/full close.
        """
        pos = self._positions.get(symbol, Position(symbol=symbol))
        new_qty = pos.quantity + qty_delta

        same_direction = (pos.quantity >= 0) == (qty_delta >= 0) or pos.quantity == 0

        if same_direction:
            total_cost = pos.avg_entry_price * pos.quantity + price * qty_delta
            pos.avg_entry_price = total_cost / new_qty if new_qty != 0 else 0.0
            pos.quantity = new_qty
        else:
            closing_qty = min(abs(qty_delta), abs(pos.quantity))
            pos.realized_pnl += (price - pos.avg_entry_price) * closing_qty * (1 if pos.quantity > 0 else -1)
            pos.quantity = new_qty
            if new_qty == 0:
                pos.avg_entry_price = 0.0
            elif (new_qty > 0) != (pos.quantity - qty_delta > 0):
                pos.avg_entry_price = price

        self._positions[symbol] = pos
        self._cash -= qty_delta * price

    def set_cash(self, amount: float):
        self._cash = amount

    def adjust_cash(self, delta: float):
        self._cash += delta

    def snapshot(self) -> dict:
        return {
            "cash": self._cash,
            "positions": {sym: str(pos) for sym, pos in self._positions.items()},
        }

    def __str__(self) -> str:
        pos_str = ", ".join(str(p) for p in self._positions.values() if not p.is_flat())
        return f"Portfolio(cash={self._cash:.2f} | {pos_str or 'flat'})"
