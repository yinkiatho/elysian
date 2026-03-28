"""
Portfolio: centralized position, cash, and risk state for a single account.

The Portfolio is **event-driven** — call :meth:`start` with an EventBus and
feeds dict to have it automatically:
  - Update mark prices and weight vector on every kline
  - Track drawdown and peak equity continuously
  - Sync cash from stablecoin balance updates

It can also be used standalone (without an EventBus) by calling
:meth:`mark_to_market` manually.
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Set
from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.position import Position
from elysian_core.core.signals import OrderIntent
import elysian_core.utils.logger as log

from elysian_core.core.events import EventType
from elysian_core.db.models import PortfolioSnapshot

from elysian_core.config.app_config import AppConfig

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
    
    _STABLECOINS = frozenset({"USDT", "USDC", "BUSD"})

    def __init__(self, exchange, max_history: int = 10_000, 
                        venue: Venue = Venue.BINANCE,cfg: AppConfig = None):
        self.cfg = cfg
        self._positions: Dict[str, Position] = {}
        self._cash: float = 0.0  # To Update as we sync from exchange
        
        
        # ── Per-asset cash breakdown (for multi-stablecoin / multi-venue) ──
        self._cash_dict: Dict[str, float] = {}  # { "USDT": 1000.0, "USDC": 500.0, ... }

        # ── Risk metrics ─────────────────────────────────────────────────
        self._peak_equity: float = 0.0 # To Update as we sync from exchange
        self._max_drawdown: float = 0.0
        self._total_realized_pnl: float = 0.0
        self._total_commission: float = 0.0

        # ── Trade history (ring buffer) ──────────────────────────────────
        self._fills: Deque[Fill] = deque(maxlen=max_history)

        # ── Live state (updated on events) ───────────────────────────────
        self._mark_prices: Dict[str, float] = {}
        self._weights: Dict[str, float] = {}
        self._nav: float = 0.0  # To Update as we sync from exchange

        # ── EventBus wiring ──────────────────────────────────────────────
        self._event_bus = None
        self._feeds: Dict = {}

        # ── Fill tracking (for ORDER_UPDATE → update_position() delta) ───
        # Tracks cumulative filled qty seen per order_id so that each
        # ORDER_UPDATE event applies only the *incremental* fill qty,
        # not the full cumulative qty.  Seeded at startup from open orders.
        # Note: order.commission from OrderUpdateEvent is already per-fill
        # (Binance field "n"), so no commission tracker is needed.
        self._fill_tracker: Dict[str, float] = {}  # order_id -> last known filled_qty
        self._completed_order_ids: Set[str] = set()  # guards terminal cleanup against duplicate events

        # ── Locked balance (pending LIMIT orders only) ────────────────────
        # LIMIT BUY: lock quote (USDT) = quantity * limit_price
        # LIMIT SELL: lock base qty = quantity
        # Released incrementally per partial fill; remainder on terminal status
        self._locked_cash: float = 0.0                                   # quote locked in pending LIMIT BUYs
        self._locked_quantities: Dict[str, float] = {}                   # symbol -> base qty locked in pending LIMIT SELLs
        self._locked_cash_per_order: Dict[str, float] = {}               # order_id -> remaining locked quote
        self._locked_qty_per_order: Dict[str, float] = {}                # order_id -> remaining locked base qty
        self._pending_order_id_to_intent: Dict[str, OrderIntent] = {}    # order_id -> intent

        # Exchange and Venue setting, only one exchange and venue per portfolio
        self.exchange = exchange
        self.venue = venue

    # ══════════════════════════════════════════════════════════════════════════
    #  Lifecycle — EventBus wiring
    # ══════════════════════════════════════════════════════════════════════════

    def sync_from_exchange(self, feeds: Optional[Dict] = None):
        """Full portfolio sync from a live exchange connector.

        Call once at startup *after* ``exchange.run()`` so that
        ``refresh_balances()`` has already populated ``exchange._balances``.
        Ongoing events (balance deltas, execution reports) keep state in
        sync from this point forward.

        Syncs:
          1. **Cash** — stablecoin balances → ``_cash_dict`` / ``_cash``
          2. **Positions** — non-stablecoin balances with qty > 0 → ``_positions``
             (entry price seeded from latest feed price; if no feed, stored as 0)
          3. **Mark prices** — latest prices from feeds for all tracked symbols
          4. **Open orders** — snapshot of the exchange's ``_open_orders``

        For multi-venue expansion: call once per exchange.
        """
        venue = self.venue
        self._feeds = feeds or self._feeds

        # ── 1. Cash: stablecoin balances ──────────────────────────────────
        for stable in self._STABLECOINS:
            bal = self.exchange.get_balance(stable)
            if bal > 0:
                self._cash_dict[stable] = bal

        self._cash = sum(self._cash_dict.values())

        # ── 2. Positions: non-stablecoin balances → Position objects ──────
        #    Build a lookup from base asset → trading pair symbol using
        #    the exchange's token_infos (e.g. "ETH" → "ETHUSDT").
        base_to_symbol = {}
        for sym, info in self.exchange._token_infos.items():
            base = info.get("base_asset")
            if base:
                base_to_symbol[base] = sym

        synced_positions = 0
        for asset, qty in self.exchange._balances.items():
            if asset in self._STABLECOINS or qty <= 0:
                continue

            symbol = base_to_symbol.get(asset)
            if symbol is None:
                logger.debug(f"[Portfolio] Skipping {asset} (qty={qty:.6f}): not in tracked pairs")
                continue

            # Seed entry price from feed if available, else 0
            entry_price = 0.0
            feed = feeds.get(symbol)
            if feed:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        entry_price = p
                        self._mark_prices[symbol] = p
                except Exception:
                    logger.warning(f"[Portfolio] Failed to get price for {symbol} during sync")

            self._positions[symbol] = Position(
                symbol=symbol,
                venue=venue,
                quantity=qty,
                avg_entry_price=entry_price,
            )
            logger.info(f"[Portfolio] Synced position: {symbol} qty={qty:.6f} entry={entry_price:.4f}")
            synced_positions += 1

        # ── 3. Mark prices: seed from all available feeds ─────────────────
        for sym, feed in feeds.items():
            if sym not in self._mark_prices:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        self._mark_prices[sym] = p
                except Exception:
                    pass

        # ── 4. Open orders: snapshot from exchange ────────────────────────
        self._open_orders: Dict[str, dict] = {}
        for sym, orders in self.exchange._open_orders.items():
            if orders:
                self._open_orders[sym] = dict(orders)

        # ── 5. Seed fill tracker from pre-existing partially-filled orders ─
        # Prevents double-counting on restart when orders were partially
        # filled before this Portfolio instance started.
        for sym, orders in self.exchange._open_orders.items():
            for oid, order in orders.items():
                if order.filled_qty > 0:
                    self._fill_tracker[oid] = order.filled_qty

        # ── Refresh derived state ─────────────────────────────────────────
        self._refresh_derived()  # Updates
        self._peak_equity = max(self._peak_equity, self._nav)

        total_open = sum(len(o) for o in self._open_orders.values())
        logger.info(
            f"Portfolio synced from {venue.value}: "
            f"cash={self._cash:.2f} ({self._cash_dict}), "
            f"{synced_positions} positions, "
            f"{len(self._mark_prices)} mark prices, "
            f"{total_open} open orders"
        )

    def start(self, event_bus, feeds: Optional[Dict] = None):
        """Subscribe to EventBus for continuous self-updating.

        After calling start(), the portfolio will:
          - On every KlineEvent: refresh mark price for that symbol,
            recompute weights, update drawdown/peak equity
          - On every BalanceUpdateEvent: sync cash from stablecoin balances
        """
        self._event_bus = event_bus
        self._feeds = feeds or {}

        # Seed mark prices from feeds that already have data
        for sym, feed in self._feeds.items():
            try:
                price = feed.latest_price
                if price and price > 0:
                    self._mark_prices[sym] = price
            except Exception:
                pass

        self._refresh_derived()
        event_bus.subscribe(EventType.KLINE, self._on_kline)
        event_bus.subscribe(EventType.BALANCE_UPDATE, self._on_balance)
        event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)

        logger.info(
            f"Portfolio started: cash={self._cash:.2f}, "
            f"{len(self._mark_prices)} price feeds seeded"
        )

    def stop(self):
        """Unsubscribe from EventBus."""
        if self._event_bus is None:
            return
        self._event_bus.unsubscribe(EventType.KLINE, self._on_kline)
        self._event_bus.unsubscribe(EventType.BALANCE_UPDATE, self._on_balance)
        self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._on_order_update)
        self._event_bus = None
        logger.info("Portfolio stopped")


    # ── Event handlers ───────────────────────────────────────────────────────
    async def _on_kline(self, event):
        """Update mark price from kline close, refresh weights and risk."""
        kline = event.kline
        if kline.close is not None and kline.close > 0 and event.venue == self.venue:
            self._mark_prices[event.symbol] = kline.close
            self._refresh_derived()


    async def _on_balance(self, event):
        """Sync cash from stablecoin balance updates.

        Uses ``event.delta`` (the publisher currently sends ``new_balance=0.0``).
        Maintains both per-asset ``_cash_dict`` and aggregate ``_cash``.
        Refreshes NAV/weights/drawdown after every cash change.
        
        BalanceUpdate Event refers to transfers of assets via deposit or withdrawals not just for quote_asset
        """
        if event.asset in self._STABLECOINS and event.venue == self.venue:
            old_cash = self._cash
            self._cash_dict[event.asset] = self._cash_dict.get(event.asset, 0.0) + event.delta
            self._cash = sum(self._cash_dict.values())
            logger.info(
                f"[Portfolio] Balance update: {event.asset} delta={event.delta:+.6f} "
                f"cash {old_cash:.2f} -> {self._cash:.2f}"
            )
            self._refresh_derived()
        
        elif event.venue == self.venue:
            curr_position = self._positions.get(event.asset, None)
            if not curr_position:
                # Making new position — seed entry price from mark price so unrealized PnL starts at 0
                self._positions[event.asset] = Position(event.asset,
                                                        venue=self.venue,
                                                        quantity=event.delta,
                                                        avg_entry_price=self._mark_prices.get(event.asset, 0.0),
                                                        )
            else:
                # Update existing position
                self._positions[event.asset] = Position(event.asset,
                                                        venue=self.venue,
                                                        quantity=curr_position.quantity + event.delta,
                                                        avg_entry_price= curr_position.avg_entry_price,
                                                        realized_pnl=curr_position.realized_pnl,
                                                        total_commission=curr_position.total_commission)
            logger.info(
            f"[Portfolio] Balance update: {event.asset} delta={event.delta:+.6f} "
            f"Old Position: {curr_position} to new Position: {self._positions[event.asset]}"
            )
            self._refresh_derived()
        else:
            return
        
    
    async def _on_order_update(self, event):
        """Route exchange fill events into update_position().

        Computes the incremental fill delta since the last seen state for
        this order so that partial fills and multi-fill orders are handled
        correctly without double-counting.

        Only processes FILLED and PARTIALLY_FILLED statuses; ignores NEW,
        OPEN, CANCELLED, REJECTED.
        """
        order = event.order
        if event.venue != self.venue:
            return
        if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            # Clean up tracker for terminal non-fill statuses (cancelled, rejected, expired)
            if order.is_terminal and order.id not in self._completed_order_ids:
                self._completed_order_ids.add(order.id)
                self._fill_tracker.pop(order.id, None)
                self.release_order_lock(order.id)
            return

        order_id = order.id
        prev_filled = self._fill_tracker.get(order_id, 0.0)
        delta_filled = order.filled_qty - prev_filled

        if delta_filled < 1e-10:
            if order.is_terminal and order_id not in self._completed_order_ids:
                self._completed_order_ids.add(order_id)
                self._fill_tracker.pop(order_id, None)
                self.release_order_lock(order_id)
            return

        # order.commission is already per-fill (Binance field "n" via _parse_execution_to_order)
        self._fill_tracker[order_id] = order.filled_qty

        # Release lock proportionally for this fill increment
        self._partial_release_lock(order_id, delta_filled)

        # BUY fills increase position (positive delta); SELL fills decrease it
        qty_delta = delta_filled if order.side == Side.BUY else -delta_filled

        self.update_position(
            symbol=order.symbol,
            qty_delta=qty_delta,
            price=order.avg_fill_price,
            commission=order.commission,
            commission_asset=order.commission_asset or "",
            venue=event.venue,
            order_id=order_id,
        )

        # Clean up completed orders from tracker; release any remaining lock
        if order.is_terminal and order_id not in self._completed_order_ids:
            self._completed_order_ids.add(order_id)
            self._fill_tracker.pop(order_id, None)
            self.release_order_lock(order_id)

    def _refresh_derived(self):
        """Recomputes from current mark prices:"
           1.weights,
           2. Nav
           3. peak_equity
           4. max_drawdown
        """
        self._nav = self._compute_total_value(self._mark_prices)
        if self._nav > 0:
            self._weights = {
                sym: (pos.quantity * self._mark_prices[sym]) / self._nav
                for sym, pos in self._positions.items()
                if not pos.is_flat() and sym in self._mark_prices
            }
        else:
            self._weights = {}

        # Drawdown tracking
        if self._nav > self._peak_equity:
            self._peak_equity = self._nav
            logger.debug(f"[Portfolio] New peak equity: {self._peak_equity:.2f}")
        if self._peak_equity > 0:
            dd = (self._peak_equity - self._nav) / self._peak_equity
            if dd > self._max_drawdown:
                old_dd = self._max_drawdown
                self._max_drawdown = dd
                logger.warning(
                    f"[Portfolio] New max drawdown: {old_dd:.4%} -> {self._max_drawdown:.4%} "
                    f"(nav={self._nav:.2f} peak={self._peak_equity:.2f})"
                )


    def _compute_total_value(self, prices: Dict[str, float]) -> float:
        pos_value = sum(
            pos.quantity * prices.get(pos.symbol, pos.avg_entry_price)
            for pos in self._positions.values()
        )
        return self._cash + pos_value


    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Position & Cash
    # ══════════════════════════════════════════════════════════════════════════

    def position(self, symbol: str) -> Position:
        """Return the position for *symbol*, or a flat placeholder."""
        return self._positions.get(symbol, Position(symbol=symbol))

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
    def cash_dict(self) -> Dict[str, float]:
        """Per-stablecoin cash breakdown."""
        return dict(self._cash_dict)

    @property
    def open_orders(self) -> Dict[str, dict]:
        """Snapshot of open orders synced from exchange at startup."""
        return dict(getattr(self, "_open_orders", {}))

    @property
    def fills(self) -> List[Fill]:
        return list(self._fills)
    
    @property
    def name(self) -> str:
        return f'Portfolio @{self.venue.value}'

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Valuation
    # ══════════════════════════════════════════════════════════════════════════

    def total_value(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        """NAV = cash + sum(position_notional).

        If *mark_prices* is None, uses the cached prices from the last kline.
        """
        prices = mark_prices if mark_prices is not None else self._mark_prices
        return self._compute_total_value(prices)

    def unrealized_pnl(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        prices = mark_prices if mark_prices is not None else self._mark_prices
        return sum(
            pos.unrealized_pnl(prices[pos.symbol])
            for pos in self._positions.values()
            if pos.symbol in prices
        )

    @property
    def nav(self) -> float:
        """Cached NAV from last kline update. Zero cost."""
        return self._nav

    @property
    def total_realized_pnl(self) -> float:
        return self._total_realized_pnl

    @property
    def total_commission(self) -> float:
        return self._total_commission

    def net_pnl(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return self._total_realized_pnl + self.unrealized_pnl(mark_prices) - self._total_commission

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Weight vector (cached, updated every kline)
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def weights(self) -> Dict[str, float]:
        """Cached weight vector from the last kline update. Zero cost."""
        return dict(self._weights)

    @property
    def mark_prices(self) -> Dict[str, float]:
        """Cached mark prices from the last kline update."""
        return dict(self._mark_prices)

    def current_weights(self, mark_prices: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """Compute the live portfolio weight vector.

        If *mark_prices* is None, returns the cached weights.
        """
        if mark_prices is None:
            return dict(self._weights)
        nav = self.total_value(mark_prices)
        if nav <= 0:
            return {}
        return {
            sym: (pos.quantity * mark_prices.get(sym, pos.avg_entry_price)) / nav
            for sym, pos in self._positions.items()
            if not pos.is_flat() and sym in mark_prices
        }

    def cash_weight(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        nav = self.total_value(mark_prices)
        return self._cash / nav if nav > 0 else 1.0

    def gross_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(abs(w) for w in self.current_weights(mark_prices).values())

    def net_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(self.current_weights(mark_prices).values())

    def long_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(w for w in self.current_weights(mark_prices).values() if w > 0)

    def short_exposure(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        return sum(abs(w) for w in self.current_weights(mark_prices).values() if w < 0)

    # ══════════════════════════════════════════════════════════════════════════
    #  READ — Risk metrics
    # ══════════════════════════════════════════════════════════════════════════

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

    def mark_to_market(self, mark_prices: Dict[str, float]):
        """Manual update (use when not event-bus driven)."""
        self._mark_prices.update(mark_prices)
        self._refresh_derived()

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
        """Apply a fill. ``qty_delta > 0`` → bought, ``< 0`` → sold."""
        side = Side.BUY if qty_delta > 0 else Side.SELL
        pos = self._positions.get(symbol, Position(symbol=symbol, venue=venue))
        old_qty = pos.quantity
        new_qty = pos.quantity + qty_delta

        logger.info(
            f"[Portfolio] Fill: {side.value} {symbol} qty={abs(qty_delta):.6f} "
            f"@ {price:.4f} (pos {old_qty:.6f} -> {new_qty:.6f}) "
            f"comm={commission:.6f} order={order_id}"
        )

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
            self._total_realized_pnl += pnl
            logger.info(
                f"[Portfolio] Realized PnL on {symbol}: {pnl:+.4f} "
                f"(total realized: {self._total_realized_pnl:+.4f})"
            )

            pos.quantity = new_qty
            if new_qty == 0:
                pos.avg_entry_price = 0.0
                logger.info(f"[Portfolio] Position closed: {symbol}")
            elif (new_qty > 0) != (old_qty > 0):
                
                # Position flipped direction — reset entry price to fill price
                pos.avg_entry_price = price
                logger.info(f"[Portfolio] Position flipped: {symbol} new_entry={price:.4f}")

        pos.total_commission += commission
        self._total_commission += commission

        self._positions[symbol] = pos
        # Remove flat positions to prevent stale entries in weight/NAV calculations
        if pos.is_flat():
            self._positions.pop(symbol, None)
        self._cash -= qty_delta * price
        self._cash -= commission

        # Update mark price from fill and refresh derived state
        self._mark_prices[symbol] = price
        self._refresh_derived()

        # Audit trail
        self._fills.append(Fill(
            symbol=symbol, venue=venue, side=side,
            quantity=abs(qty_delta), price=price,
            commission=commission, commission_asset=commission_asset,
            timestamp=int(time.time() * 1000), order_id=order_id,
        ))

    def set_cash(self, amount: float):
        self._cash = amount

    def adjust_cash(self, delta: float):
        self._cash += delta

    # ── Locked balance — LIMIT order reservations ─────────────────────────────

    def lock_for_order(self, order_id: str, intent: OrderIntent) -> None:
        """Reserve balance for a submitted LIMIT order. No-op for MARKET orders."""
        if intent.order_type != OrderType.LIMIT:
            return
        if order_id in self._pending_order_id_to_intent:
            return  # idempotent
        self._pending_order_id_to_intent[order_id] = intent
        if intent.side == Side.BUY:
            if intent.price is None:
                logger.warning(f"[Portfolio] lock_for_order: LIMIT order {order_id} has price=None, skipping lock")
                return
            notional = intent.quantity * intent.price
            self._locked_cash += notional
            self._locked_cash_per_order[order_id] = notional
            logger.info(
                f"[Portfolio] Locked {notional:.4f} quote for LIMIT BUY {intent.symbol} "
                f"qty={intent.quantity} @ {intent.price} order={order_id}"
            )
        else:  # SELL
            self._locked_quantities[intent.symbol] = (
                self._locked_quantities.get(intent.symbol, 0.0) + intent.quantity
            )
            self._locked_qty_per_order[order_id] = intent.quantity
            logger.info(
                f"[Portfolio] Locked {intent.quantity:.6f} {intent.symbol} "
                f"for LIMIT SELL order={order_id}"
            )

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
        """Release any remaining lock for a terminal order (filled/cancelled/rejected)."""
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

    # ══════════════════════════════════════════════════════════════════════════
    #  Snapshot / display
    # ══════════════════════════════════════════════════════════════════════════

    def snapshot(self) -> dict:
        return {
            "cash": self._cash,
            "nav": self._nav,
            "total_realized_pnl": self._total_realized_pnl,
            "total_commission": self._total_commission,
            "peak_equity": self._peak_equity,
            "max_drawdown": self._max_drawdown,
            "current_drawdown": self.current_drawdown(),
            "gross_exposure": self.gross_exposure(),
            "net_exposure": self.net_exposure(),
            "num_fills": len(self._fills),
            "positions": {sym: str(pos) for sym, pos in self._positions.items()},
            "weights": dict(self._weights),
        }

    def save_snapshot(self, venue=None):
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
                strategy_id=self.cfg.meta.strategy_id if self.cfg else 0,
                venue=venue or Venue.BINANCE,
                nav=self._nav,
                cash=self._cash,
                unrealized_pnl=self.unrealized_pnl(),
                realized_pnl=self._total_realized_pnl,
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
                f"[Portfolio] Snapshot saved: nav={self._nav:.2f} cash={self._cash:.2f} "
                f"dd={self._max_drawdown:.4%} positions={len(positions_json)}"
            )
        except Exception as e:
            logger.error(f"[Portfolio] Failed to save snapshot: {e}", exc_info=True)

    def __str__(self) -> str:
        active = [str(p) for p in self._positions.values() if not p.is_flat()]
        return (
            f"Portfolio(cash={self._cash:.2f} nav={self._nav:.2f} "
            f"realized_pnl={self._total_realized_pnl:.4f} "
            f"dd={self._max_drawdown:.4%} | "
            f"{', '.join(active) or 'flat'})"
        )
        
