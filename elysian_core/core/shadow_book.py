"""
Per-strategy virtual position ledger (shadow book).

Each strategy gets its own ShadowBook that tracks the slice of the shared
Portfolio attributable to it.  The real Portfolio is the exchange-level source
of truth; ShadowBooks are internal accounting for:

  - Per-strategy position tracking
  - Per-strategy PnL attribution (realized + unrealized)
  - Per-strategy weight computation (used by compute_weights)
  - Per-strategy risk metrics (drawdown, exposure)

**Invariant**: sum of all ShadowBook positions for a symbol ≈ Portfolio position
for that symbol.  Small rounding differences are expected from step-size
quantization; periodic reconciliation corrects drift.

Strategies read their own ShadowBook — NOT the aggregate Portfolio — to
decide target weights.  This is the primary interface between a strategy
and "what it owns".
"""

import time
from collections import deque
from typing import Deque, Dict, List, Optional

from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.events import EventType
from elysian_core.core.portfolio import Fill
from elysian_core.core.position import Position
from elysian_core.core.signals import OrderIntent
from elysian_core.db.models import PortfolioSnapshot
import elysian_core.utils.logger as log

_STABLECOINS = frozenset({"USDT", "USDC", "BUSD"})

logger = log.setup_custom_logger("root")


class ShadowBook:
    """Per-strategy virtual position and cash ledger.

    Parameters
    ----------
    strategy_id:
        Unique identifier for the owning strategy.
    allocation:
        Fraction of total portfolio capital allocated to this strategy [0, 1].
    venue:
        Default venue for positions created by fills.
    """

    def __init__(self, strategy_id: int, allocation: float,
                 venue: Venue = Venue.BINANCE):
        self._strategy_id = strategy_id
        self._allocation = allocation
        self._venue = venue

        # Position state
        self._positions: Dict[str, Position] = {}
        self._cash: float = 0.0

        # Mark prices (updated via strategy's kline dispatch)
        self._mark_prices: Dict[str, float] = {}

        # PnL tracking
        self._realized_pnl: float = 0.0
        self._total_commission: float = 0.0

        # Trade history (ring buffer, mirrors Portfolio._fills)
        self._fills: Deque[Fill] = deque(maxlen=10_000)

        # Risk metrics (updated on every _refresh_derived)
        self._peak_equity: float = 0.0
        self._max_drawdown: float = 0.0
        self._nav: float = 0.0
        self._weights: Dict[str, float] = {}

        # EventBus wiring
        self._event_bus = None

        # Fill tracking: order_id -> last known cumulative filled_qty (mirrors Portfolio)
        self._fill_tracker: Dict[str, float] = {}

        # Shared map from ExecutionEngine: order_id -> strategy_id for event routing
        # Passed in via start(). ShadowBook only processes orders belonging to its strategy.
        self._order_strategy_map: Optional[Dict[str, int]] = None

        # ── Locked balance (pending LIMIT orders only) ────────────────────
        self._locked_cash: float = 0.0
        self._locked_quantities: Dict[str, float] = {}
        self._locked_cash_per_order: Dict[str, float] = {}
        self._locked_qty_per_order: Dict[str, float] = {}
        self._pending_order_id_to_intent: Dict[str, OrderIntent] = {}


    # ── Initialization ────────────────────────────────────────────────────

    def init_from_portfolio_cash(self, portfolio_cash: float,
                                 mark_prices: Optional[Dict[str, float]] = None):
        """Initialize with proportional cash allocation (fresh start, no positions).

        This is the standard init path for a new deployment.  Each strategy
        starts with ``allocation * portfolio_cash`` and builds positions
        through rebalance cycles.
        """
        self._cash = portfolio_cash * self._allocation
        self._mark_prices = dict(mark_prices or {})
        self._refresh_derived()
        self._peak_equity = self._nav
        logger.info(
            f"[ShadowBook-{self._strategy_id}] Initialized: "
            f"cash={self._cash:.2f}, allocation={self._allocation:.2%}"
        )

    def init_from_existing(self, portfolio_cash: float,
                                 portfolio_positions: Dict[str, Position],
                                 mark_prices: Dict[str, float]):
        """Initialize by distributing existing Portfolio state proportionally.

        Used when restarting with pre-existing positions.  Distributes both
        cash and positions by allocation fraction.  This is a best-effort
        approximation — subsequent rebalance cycles will correct any
        mis-attribution.
        """
        self._cash = portfolio_cash * self._allocation
        self._mark_prices = dict(mark_prices)

        for sym, pos in portfolio_positions.items():
            self._positions[sym] = Position(
                symbol=sym,
                venue=pos.venue,
                quantity=pos.quantity * self._allocation,
                avg_entry_price=pos.avg_entry_price,
            )

        self._refresh_derived()
        self._peak_equity = self._nav
        logger.info(
            f"[ShadowBook-{self._strategy_id}] Initialized from existing: "
            f"cash={self._cash:.2f}, {len(self._positions)} positions, "
            f"allocation={self._allocation:.0%}"
        )

    # ── Lifecycle — EventBus wiring ──────────────────────────────────────

    def start(self, event_bus, order_strategy_map: Optional[Dict[str, int]] = None):
        """Subscribe to ORDER_UPDATE for event-driven fill attribution.

        Parameters
        ----------
        event_bus:
            Shared EventBus instance.
        order_strategy_map:
            Reference to ExecutionEngine._order_strategy_map — a shared dict
            mapping exchange order_id -> strategy_id.  Used to filter events
            to only fills placed by this strategy.
        """
        self._event_bus = event_bus
        #self._order_strategy_map = order_strategy_map
        event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)
        logger.info(
            f"[ShadowBook-{self._strategy_id}] started — subscribed to ORDER_UPDATE"
        )

    def stop(self):
        """Unsubscribe from EventBus."""
        if self._event_bus is None:
            return
        self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._on_order_update)
        self._event_bus = None
        logger.info(f"[ShadowBook-{self._strategy_id}] stopped")

    async def _on_order_update(self, event):
        """Route exchange fill events into apply_fill().

        Filters events to fills that belong to this strategy using
        _order_strategy_map (order_id -> strategy_id, maintained by ExecutionEngine).
        Computes the incremental fill delta to avoid double-counting partial fills.
        commission is already per-fill from Binance field "n".
        """
        order = event.order

        # Filter by strategy ownership
        if order.strategy_id is not None and order.strategy_id != self._strategy_id:
            return

        if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            if order.is_terminal:
                self._fill_tracker.pop(order.id, None)
                if self._order_strategy_map is not None:
                    self._order_strategy_map.pop(order.id, None)
                self.release_order_lock(order.id)
            return

        order_id = order.id
        prev_filled = self._fill_tracker.get(order_id, 0.0)
        delta_filled = order.filled_qty - prev_filled

        if delta_filled < 1e-10:
            if order.is_terminal:
                self._fill_tracker.pop(order_id, None)
                if self._order_strategy_map is not None:
                    self._order_strategy_map.pop(order_id, None)
                self.release_order_lock(order_id)
            return

        self._fill_tracker[order_id] = order.filled_qty

        # Release lock proportionally for this fill increment
        self._partial_release_lock(order_id, delta_filled)

        qty_delta = delta_filled if order.side == Side.BUY else -delta_filled

        self.apply_fill(
            symbol=order.symbol,
            qty_delta=qty_delta,
            price=order.avg_fill_price,
            commission=order.commission,    # per-fill from Binance field "n"
            venue=event.venue,
        )

        if order.is_terminal:
            self._fill_tracker.pop(order_id, None)
            if self._order_strategy_map is not None:
                self._order_strategy_map.pop(order_id, None)
            self.release_order_lock(order_id)

    # ── Read — Identity ───────────────────────────────────────────────────

    @property
    def strategy_id(self) -> int:
        return self._strategy_id

    @property
    def allocation(self) -> float:
        return self._allocation

    # ── Read — Positions & Cash ───────────────────────────────────────────

    def position(self, symbol: str) -> Position:
        """Return this strategy's position for *symbol*, or a flat placeholder."""
        return self._positions.get(symbol, Position(symbol=symbol, venue=self._venue))

    @property
    def positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    @property
    def active_positions(self) -> Dict[str, Position]:
        return {s: p for s, p in self._positions.items() if not p.is_flat()}

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def nav(self) -> float:
        """Cached NAV from last mark price update."""
        return self._nav

    @property
    def weights(self) -> Dict[str, float]:
        """Cached weight vector from last mark price update."""
        return dict(self._weights)

    @property
    def mark_prices(self) -> Dict[str, float]:
        return dict(self._mark_prices)


    # ── Read — Valuation ──────────────────────────────────────────────────
    
    def total_value(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        """NAV = cash + sum(position_notional)."""
        prices = mark_prices if mark_prices is not None else self._mark_prices
        pos_value = sum(
            pos.quantity * prices.get(pos.symbol, pos.avg_entry_price)
            for pos in self._positions.values()
        )
        return self._cash + pos_value

    def current_weights(self, mark_prices: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """Compute the live weight vector for this strategy's positions."""
        prices = mark_prices if mark_prices is not None else self._mark_prices
        nav = self.total_value(prices)
        if nav <= 0:
            return {}
        return {
            sym: (pos.quantity * prices.get(sym, pos.avg_entry_price)) / nav
            for sym, pos in self._positions.items()
            if not pos.is_flat() and sym in prices
        }

    def unrealized_pnl(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        prices = mark_prices if mark_prices is not None else self._mark_prices
        return sum(
            pos.unrealized_pnl(prices[pos.symbol])
            for pos in self._positions.values()
            if pos.symbol in prices
        )
        
    def gross_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(abs(w) for w in self.current_weights(mark_prices).values())

    def net_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(self.current_weights(mark_prices).values())

    def long_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(w for w in self.current_weights(mark_prices).values() if w > 0)

    def short_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(abs(w) for w in self.current_weights(mark_prices).values() if w < 0)

    def cash_weight(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        nav = self.total_value(mark_prices)
        return self._cash / nav if nav > 0 else 1.0

    def net_pnl(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return self._realized_pnl + self.unrealized_pnl(mark_prices) - self._total_commission

    @property
    def fills(self) -> List[Fill]:
        return list(self._fills)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def total_commission(self) -> float:
        return self._total_commission

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def max_drawdown(self) -> float:
        return self._max_drawdown

    def current_drawdown(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        nav = self.total_value(mark_prices)
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - nav) / self._peak_equity)

    # ── Write — Fill attribution ──────────────────────────────────────────

    def apply_fill(self, symbol: str, qty_delta: float, price: float,
                   commission: float = 0.0, venue: Optional[Venue] = None):
        """Attribute a fill to this shadow book.

        Mirrors Portfolio.update_position() logic but operates on this
        strategy's virtual positions only.  Called by the fill attribution
        system after execution completes.
        """
        if abs(qty_delta) < 1e-12:
            return

        venue = venue or self._venue
        side = Side.BUY if qty_delta > 0 else Side.SELL
        pos = self._positions.get(symbol, Position(symbol=symbol, venue=venue))
        old_qty = pos.quantity
        new_qty = pos.quantity + qty_delta

        same_direction = (pos.quantity >= 0) == (qty_delta >= 0) or pos.quantity == 0

        if same_direction:
            total_cost = pos.avg_entry_price * pos.quantity + price * qty_delta
            pos.avg_entry_price = total_cost / new_qty if new_qty != 0 else 0.0
            pos.quantity = new_qty
        else:
            closing_qty = min(abs(qty_delta), abs(pos.quantity))
            sign = 1 if pos.quantity > 0 else -1
            pnl = (price - pos.avg_entry_price) * closing_qty * sign

            pos.realized_pnl += pnl
            self._realized_pnl += pnl

            pos.quantity = new_qty
            if new_qty == 0:
                pos.avg_entry_price = 0.0
            elif (new_qty > 0) != (old_qty > 0):
                # Position flipped direction
                pos.avg_entry_price = price

        pos.total_commission += commission
        self._total_commission += commission
        self._positions[symbol] = pos
        self._cash -= qty_delta * price
        self._cash -= commission

        self._mark_prices[symbol] = price
        self._refresh_derived()

        # Audit trail
        self._fills.append(Fill(
            symbol=symbol, venue=venue, side=side,
            quantity=abs(qty_delta), price=price,
            commission=commission,
            timestamp=int(time.time() * 1000),
        ))

        logger.debug(
            f"[ShadowBook-{self._strategy_id}] Fill: {side.value} {symbol} "
            f"qty={abs(qty_delta):.6f} @ {price:.4f} "
            f"(pos {old_qty:.6f} -> {new_qty:.6f})"
        )

    def mark_to_market(self, mark_prices: Dict[str, float]):
        """Manual mark price update (mirrors Portfolio.mark_to_market)."""
        self._mark_prices.update(mark_prices)
        self._refresh_derived()

    def set_cash(self, amount: float):
        self._cash = amount

    def adjust_cash(self, delta: float):
        self._cash += delta

    def on_balance(self, event) -> None:
        """Mirror Portfolio._on_balance(), scaled by allocation.

        Called automatically by base_strategy._dispatch_balance() so that
        deposits and withdrawals are reflected in the shadow book.
        """
        if event.asset in _STABLECOINS:
            self._cash += event.delta * self._allocation
            self._refresh_derived()
        elif event.venue == self._venue:
            scaled_delta = event.delta * self._allocation
            curr = self._positions.get(event.asset)
            if curr is None:
                self._positions[event.asset] = Position(
                    symbol=event.asset,
                    venue=self._venue,
                    quantity=scaled_delta,
                    avg_entry_price=self._mark_prices.get(event.asset, 0.0),
                )
            else:
                self._positions[event.asset] = Position(
                    symbol=event.asset,
                    venue=curr.venue,
                    quantity=curr.quantity + scaled_delta,
                    avg_entry_price=curr.avg_entry_price,
                    realized_pnl=curr.realized_pnl,
                    total_commission=curr.total_commission,
                )
            self._refresh_derived()

    # ── Locked balance — LIMIT order reservations ─────────────────────────

    def lock_for_order(self, order_id: str, intent: OrderIntent) -> None:
        """Reserve balance for a submitted LIMIT order. No-op for MARKET orders."""
        if intent.order_type != OrderType.LIMIT:
            return
        if order_id in self._pending_order_id_to_intent:
            return  # idempotent
        self._pending_order_id_to_intent[order_id] = intent
        if intent.side == Side.BUY:
            notional = intent.quantity * intent.price
            self._locked_cash += notional
            self._locked_cash_per_order[order_id] = notional
        else:  # SELL
            self._locked_quantities[intent.symbol] = (
                self._locked_quantities.get(intent.symbol, 0.0) + intent.quantity
            )
            self._locked_qty_per_order[order_id] = intent.quantity

    def _partial_release_lock(self, order_id: str, delta_filled: float) -> None:
        """Release lock proportionally as a partial/full fill arrives."""
        intent = self._pending_order_id_to_intent.get(order_id)
        if intent is None or intent.order_type != OrderType.LIMIT:
            return
        if intent.side == Side.BUY:
            release = delta_filled * intent.price
            remaining = max(0.0, self._locked_cash_per_order.get(order_id, 0.0) - release)
            self._locked_cash_per_order[order_id] = remaining
            self._locked_cash = max(0.0, self._locked_cash - release)
        else:  # SELL
            remaining = max(0.0, self._locked_qty_per_order.get(order_id, 0.0) - delta_filled)
            self._locked_qty_per_order[order_id] = remaining
            sym_locked = max(0.0, self._locked_quantities.get(intent.symbol, 0.0) - delta_filled)
            if sym_locked == 0.0:
                self._locked_quantities.pop(intent.symbol, None)
            else:
                self._locked_quantities[intent.symbol] = sym_locked

    def release_order_lock(self, order_id: str) -> None:
        """Release any remaining lock for a terminal order."""
        intent = self._pending_order_id_to_intent.pop(order_id, None)
        if intent is None or intent.order_type != OrderType.LIMIT:
            self._locked_cash_per_order.pop(order_id, None)
            self._locked_qty_per_order.pop(order_id, None)
            return
        if intent.side == Side.BUY:
            remaining = self._locked_cash_per_order.pop(order_id, 0.0)
            self._locked_cash = max(0.0, self._locked_cash - remaining)
        else:  # SELL
            remaining = self._locked_qty_per_order.pop(order_id, 0.0)
            sym_locked = max(0.0, self._locked_quantities.get(intent.symbol, 0.0) - remaining)
            if sym_locked == 0.0:
                self._locked_quantities.pop(intent.symbol, None)
            else:
                self._locked_quantities[intent.symbol] = sym_locked

    @property
    def free_cash(self) -> float:
        """Cash available for new orders (excludes locked LIMIT BUY reservations)."""
        return max(0.0, self._cash - self._locked_cash)

    def free_quantity(self, symbol: str) -> float:
        """Base quantity available for new orders (excludes locked LIMIT SELL reservations)."""
        return max(0.0, self.position(symbol).quantity - self._locked_quantities.get(symbol, 0.0))

    def update_mark_prices(self, prices: Dict[str, float]):
        """Update mark prices and refresh derived metrics.

        Called by the strategy's kline dispatch to keep the shadow book
        in sync with live market data.
        """
        self._mark_prices.update(prices)
        self._refresh_derived()

    # ── Private ───────────────────────────────────────────────────────────

    def _refresh_derived(self):
        """Recompute NAV, weights, peak equity, and drawdown."""
        self._nav = self.total_value()
        if self._nav > 0:
            self._weights = {
                sym: (pos.quantity * self._mark_prices[sym]) / self._nav
                for sym, pos in self._positions.items()
                if not pos.is_flat() and sym in self._mark_prices  # BUG-7: skip stale prices
            }
        else:
            self._weights = {}

        if self._nav > self._peak_equity:
            self._peak_equity = self._nav
        if self._peak_equity > 0:
            dd = (self._peak_equity - self._nav) / self._peak_equity
            if dd > self._max_drawdown:
                self._max_drawdown = dd

    # ── Display ───────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "strategy_id": self._strategy_id,
            "allocation": self._allocation,
            "cash": self._cash,
            "nav": self._nav,
            "realized_pnl": self._realized_pnl,
            "total_commission": self._total_commission,
            "peak_equity": self._peak_equity,
            "max_drawdown": self._max_drawdown,
            "positions": {sym: str(pos) for sym, pos in self._positions.items()},
            "weights": dict(self._weights),
        }
        
    
    def save_snapshot(self):
        positions_json = {
            sym: {
                "qty": pos.quantity,
                "avg_entry": pos.avg_entry_price,
                "realized_pnl": pos.realized_pnl,
                "commission": pos.total_commission,
            }
            for sym, pos in self._positions.items()
            if not pos.is_flat()
        }

        try:
            PortfolioSnapshot.create(
                strategy_id=self._strategy_id,
                venue=self._venue or Venue.BINANCE,
                nav=self._nav,
                cash=self._cash,
                unrealized_pnl=self.unrealized_pnl(),
                realized_pnl=self._realized_pnl,
                total_commission=self._total_commission,
                peak_equity=self._peak_equity,
                max_drawdown=self._max_drawdown,
                current_drawdown=self.current_drawdown(),
                gross_exposure=self.gross_exposure(),
                net_exposure=self.net_exposure(),
                positions=positions_json,
                weights=dict(self._weights),
                mark_prices=dict(self._mark_prices),
                num_fills=len(self._fills),
            )
            logger.info(
                f"[ShadowBook-{self._strategy_id}] Snapshot saved: nav={self._nav:.2f} "
                f"cash={self._cash:.2f} dd={self._max_drawdown:.4%} "
                f"positions={len(positions_json)}"
            )
        except Exception as e:
            logger.error(
                f"[ShadowBook-{self._strategy_id}] Failed to save snapshot: {e}",
                exc_info=True,
            )

    def __repr__(self) -> str:
        active = len(self.active_positions)
        return (
            f"ShadowBook(strategy={self._strategy_id} alloc={self._allocation:.0%} "
            f"cash={self._cash:.2f} nav={self._nav:.2f} positions={active})"
        )


# ── Fill attribution helper ──────────────────────────────────────────────

def reconcile_shadow_books(
    shadow_books: Dict[int, "ShadowBook"],
    strategy_weights: Dict[int, Dict[str, float]],
    allocations: Dict[int, float],
    mark_prices: Dict[str, float],
    portfolio_total_value: float,
):
    """Periodic drift-correction pass for shadow books.

    DEPRECATED AS PRIMARY ACCOUNTING — ShadowBook.start() + _on_order_update()
    now handles real-time fill attribution at actual fill prices with correct
    commission attribution.  This function should only be called as a periodic
    reconciliation sweep to correct accumulated rounding drift, NOT as the
    primary mechanism for updating shadow book positions.

    Reconciles all shadow books to their *target* positions after a rebalance.
    Uses mark prices (not fill prices) — cost basis accuracy is limited.
    Commission is NOT attributed here.

    Parameters
    ----------
    shadow_books:
        strategy_id -> ShadowBook mapping.
    strategy_weights:
        strategy_id -> {symbol: raw_weight} — the latest weights each
        strategy computed (before allocation scaling).
    allocations:
        strategy_id -> allocation fraction [0, 1].
    mark_prices:
        symbol -> price used for fill attribution.
    portfolio_total_value:
        Total NAV of the aggregate Portfolio at time of execution.
    """
    for strat_id, book in shadow_books.items():
        alloc = allocations.get(strat_id, 0.0)
        weights = strategy_weights.get(strat_id, {})
        strat_target_value = alloc * portfolio_total_value

        # All symbols this strategy cares about (current + target)
        all_syms = set(weights.keys()) | set(book.active_positions.keys())

        for sym in all_syms:
            price = mark_prices.get(sym, 0.0)
            if price <= 0:
                continue

            target_w = weights.get(sym, 0.0)
            target_qty = target_w * strat_target_value / price
            current_qty = book.position(sym).quantity
            delta = target_qty - current_qty

            if abs(delta) < 1e-10:
                continue

            book.apply_fill(sym, delta, price)

        logger.debug(
            f"[ShadowBook-{strat_id}] Reconciled: "
            f"nav={book.nav:.2f} cash={book.cash:.2f} "
            f"{len(book.active_positions)} positions"
        )
