"""
Per-strategy Position ledger (shadow book).

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
from typing import Deque, Dict, List, Optional, Set

from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.events import EventType
from elysian_core.core.order import ActiveLimitOrder, ActiveOrder, Order
from elysian_core.core.portfolio import Fill
from elysian_core.core.position import Position
from elysian_core.core.signals import OrderIntent
from elysian_core.db.models import PortfolioSnapshot
from elysian_core.core.events import BalanceUpdateEvent, OrderUpdateEvent
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

    def __init__(self, strategy_id: int, 
                       venue: Venue = Venue.BINANCE):
        self._strategy_id = strategy_id
        self._venue = venue

        # Position state
        self._positions: Dict[str, Position] = {}
        self._cash: float = 0.0

        # Mark prices (updated via strategy's kline dispatch)
        # Stablecoins seeded at 1.0 so total_commission USDT conversion is correct
        self._mark_prices: Dict[str, float] = {symbol: 1.0 for symbol in _STABLECOINS}

        # PnL tracking
        self._realized_pnl: float = 0.0
        #self._total_commission: float = 0.0
        self._total_commission_dict: Dict[str, float] = {}

        # Trade history (ring buffer, mirrors Portfolio._fills)
        self._fills: Deque[Fill] = deque(maxlen=10_000)

        # Risk metrics (updated on every _refresh_derived)
        self._peak_equity: float = 0.0
        self._max_drawdown: float = 0.0
        self._nav: float = 0.0
        self._weights: Dict[str, float] = {}

        # EventBus wiring
        self._event_bus = None

        # Guards terminal cleanup against duplicate events
        self._completed_order_ids: Set[str] = set()

        # ── Active orders & locked balance ────────────────────────────────
        # Active orders: order_id -> ActiveOrder | ActiveLimitOrder
        # LIMIT orders are stored as ActiveLimitOrder (carries reservation state).
        # MARKET orders are stored as ActiveOrder (fill-delta tracking only).
        self._active_orders: Dict[str, ActiveOrder] = {}
        self._locked_cash: float = 0.0  # sum of all ActiveLimitOrder.reserved_cash

        # Sub-account state
        self._exchange = None                  # set by sync_from_exchange()
        self._cash_dict: Dict[str, float] = {}
        self._private_event_bus = None         # per-strategy bus for user data events


    # ── Initialization ────────────────────────────────────────────────────
    def sync_from_exchange(self, exchange, feeds: Optional[Dict] = None):
        """Initialize from real exchange state. Called once at startup for sub-account mode.

        Sets up cash, positions, mark prices, and open orders from the exchange.
        Feeds may be empty at sync time — mark prices self-correct on the first KLINE event.
        """
        self._exchange = exchange
        feeds = feeds or {}

        # Cash: stablecoin balances
        for stable in _STABLECOINS:
            bal = exchange.get_balance(stable)
            if bal > 0:
                self._cash_dict[stable] = bal
        self._cash = sum(self._cash_dict.values())

        # Positions: non-stablecoin balances — map base asset → trading symbol
        for asset, qty in exchange._balances.items():
            if asset in _STABLECOINS or qty <= 0:
                continue
            symbol = exchange.base_asset_to_symbol(asset)
            if symbol is None:
                continue
            entry_price = 0.0
            feed = feeds.get(symbol)
            if feed:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        entry_price = p
                        self._mark_prices[symbol] = p
                except Exception as e:
                    logger.warning(f"[ShadowBook-{self._strategy_id}] Failed to get price for {symbol} during sync: {e}")
            self._positions[symbol] = Position(
                symbol=symbol, venue=self._venue, quantity=qty, avg_entry_price=entry_price
            )

        # Mark prices from all remaining feeds
        for sym, feed in feeds.items():
            if sym not in self._mark_prices:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        self._mark_prices[sym] = p
                except Exception:
                    logger.warning(f"[ShadowBook-{self._strategy_id}] Failed to get price for {sym} during sync")

        # Wrap open orders as ActiveOrder with _prev_filled seeded to avoid double-counting
        for sym, orders in exchange._open_orders.items():
            for oid, order in orders.items():
                active = ActiveOrder(
                    id=order.id, symbol=order.symbol, side=order.side,
                    order_type=order.order_type, quantity=order.quantity,
                    price=order.price, status=order.status,
                    venue=order.venue or self._venue, strategy_id=order.strategy_id,
                    filled_qty=order.filled_qty, avg_fill_price=order.avg_fill_price,
                    commission=order.commission, commission_asset=order.commission_asset,
                    last_updated_timestamp=order.last_updated_timestamp,
                    _prev_filled=order.filled_qty,
                )
                self._active_orders[oid] = active

        self._refresh_derived()
        self._peak_equity = max(self._peak_equity, self._nav)
        logger.info(
            f"[ShadowBook-{self._strategy_id}] Synced from exchange: "
            f"cash={self._cash:.2f} ({self._cash_dict}), "
            f"{len(self._positions)} positions, "
            f"{len(self._mark_prices)} mark prices, "
            f"{len(self._active_orders)} open orders"
        )

    def init_from_portfolio_cash(
        self, portfolio_cash: float, mark_prices: Optional[Dict[str, float]] = None,
    ) -> None:
        """Initialize for fresh deployment — no prior positions.

        Sets cash (defaulted to USDT) and mark prices, then refreshes derived metrics.
        """
        self._cash = portfolio_cash
        self._cash_dict = {"USDT": portfolio_cash}
        if mark_prices:
            self._mark_prices.update(mark_prices)
        self._refresh_derived()
        self._peak_equity = max(self._peak_equity, self._nav)

    # ── Lifecycle — EventBus wiring ──────────────────────────────────────

    def start(self, event_bus, private_event_bus=None):
        """Subscribe to event buses for fill attribution and mark price updates.

        Parameters
        ----------
        event_bus:
            Shared EventBus instance (market data: KLINE).
        private_event_bus:
            Per-strategy EventBus for user data events.
        """
        self._event_bus = event_bus
        self._private_event_bus = private_event_bus


        # Curerntly our strategy class subscribes to the private event bus for us and calls the on_order/on_balnce funcs
        # # ORDER_UPDATE from private bus (sub-account) or shared bus (shared-account)
        # self._private_event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)
        
        # # BALANCE_UPDATE from private bus (account-specific)
        # self._private_event_bus.subscribe(EventType.BALANCE_UPDATE, self._on_balance_update)

        # KLINE from shared bus (market data)
        self._event_bus.subscribe(EventType.KLINE, self._on_kline)
        
        logger.info(
            f"[ShadowBook-{self._strategy_id}] started in sub-account mode — "
            f"ORDER_UPDATE+BALANCE_UPDATE on private bus, KLINE on shared bus"
        )

    def stop(self):
        """Unsubscribe from all event buses."""
        if self._event_bus:
            self._event_bus.unsubscribe(EventType.KLINE, self._on_kline)
            self._event_bus = None
        if self._private_event_bus:
            self._private_event_bus.unsubscribe(EventType.ORDER_UPDATE, self._on_order_update)
            self._private_event_bus.unsubscribe(EventType.BALANCE_UPDATE, self._on_balance_update)
            self._private_event_bus = None
        logger.info(f"[ShadowBook-{self._strategy_id}] stopped")

    async def _on_kline(self, event):
        """Update mark prices from kline close price and refresh derived metrics."""
        if event.venue == self._venue and event.kline.close and event.kline.close > 0:
            self._mark_prices[event.symbol] = event.kline.close
            self._refresh_derived()

    async def _on_balance_update(self, event: BalanceUpdateEvent):
        """Handle balance updates from the private sub-account event bus."""
        if event.asset in _STABLECOINS:
            self._cash_dict[event.asset] = self._cash_dict.get(event.asset, 0.0) + event.delta
            self._cash = sum(self._cash_dict.values())
        else:
            # Map raw asset (e.g. "ETH") to trading symbol (e.g. "ETHUSDT")
            symbol = (
                self._exchange.base_asset_to_symbol(event.asset)
                if self._exchange else event.asset
            )
            curr = self._positions.get(symbol)
            if curr is None:
                self._positions[symbol] = Position(symbol=symbol, venue=self._venue, quantity=event.delta,
                                                   avg_entry_price=self._mark_prices.get(symbol, 0.0),
                                                   locked_quantity=0.0,
                )
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, venue=curr.venue,
                    quantity=curr.quantity + event.delta,
                    avg_entry_price=curr.avg_entry_price,
                    realized_pnl=curr.realized_pnl,
                    total_commission=curr.total_commission,
                    locked_quantity=curr.locked_quantity,
                )
        self._refresh_derived()

    async def _on_order_update(self, event: OrderUpdateEvent):
        """Route exchange order events into ActiveOrder bookkeeping and apply_fill().

        Events arrive on the private EventBus so all orders belong to this strategy.
        Fill-delta tracking lives on the ActiveOrder._prev_filled field.
        Lock release lives on ActiveLimitOrder.release_partial / release_all.
        Commission is already per-fill from Binance field "n".
        """
        order = event.order
        order_id = order.id

        if order_id in self._completed_order_ids:
            return  # guard against duplicate terminal events

        active = self._active_orders.get(order_id)

        if active is None:
            # First time seeing this order — wrap in ActiveOrder for tracking.
            # LIMIT orders created by lock_for_order() are already ActiveLimitOrder;
            # this branch handles MARKET orders and externally-placed orders.
            active = ActiveOrder(
                id=order.id, symbol=order.symbol, side=order.side,
                order_type=order.order_type, quantity=order.quantity,
                price=order.price, status=OrderStatus.PENDING,
                venue=order.venue or self._venue, strategy_id=order.strategy_id,
            )
            self._active_orders[order_id] = active

        delta_filled = active.sync_from_event(order)

        if delta_filled > 0:
            # Release lock proportional to this fill increment
            if isinstance(active, ActiveLimitOrder):
                cash_rel, qty_rel = active.release_partial(delta_filled)
                self._locked_cash = max(0.0, self._locked_cash - cash_rel)
                if qty_rel > 0:
                    pos = self._positions.get(order.symbol)
                    if pos is not None:
                        pos.locked_quantity = max(0.0, pos.locked_quantity - qty_rel)

            qty_delta = delta_filled if order.side == Side.BUY else -delta_filled
            fill_price = order.last_fill_price if order.last_fill_price > 0 else order.avg_fill_price
            self.apply_fill(
                symbol=order.symbol,
                base_asset=event.base_asset,
                quote_asset=event.quote_asset,
                qty_delta=qty_delta,
                price=fill_price,
                commission=order.commission,
                commission_asset=order.commission_asset,
                venue=event.venue,
            )

        if active.is_terminal:
            # Drain any remaining reservation (handles partial-fill-then-cancel)
            if isinstance(active, ActiveLimitOrder):
                cash_rem, qty_rem = active.release_all()
                self._locked_cash = max(0.0, self._locked_cash - cash_rem)
                if qty_rem > 0:
                    pos = self._positions.get(order.symbol)
                    if pos is not None:
                        pos.locked_quantity = max(0.0, pos.locked_quantity - qty_rem)
            self._active_orders.pop(order_id, None)
            self._completed_order_ids.add(order_id)

    # ── Read — Identity ───────────────────────────────────────────────────

    @property
    def active_orders(self) -> Dict[str, ActiveOrder]:
        """Live active orders for this strategy, keyed by order_id."""
        return dict(self._active_orders)

    @property
    def outstanding_orders(self) -> Dict[str, ActiveOrder]:
        """Alias for active_orders (backward compatibility)."""
        return self.active_orders

    @property
    def strategy_id(self) -> int:
        return self._strategy_id

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
            if not pos.is_flat() and prices.get(sym, pos.avg_entry_price) > 0
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
        return self._realized_pnl + self.unrealized_pnl(mark_prices) - self.total_commission

    @property
    def fills(self) -> List[Fill]:
        return list(self._fills)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def total_commission(self) -> float:
        total_com = sum(self.mark_prices.get(symbol, 1.0) * 
                        quantity for symbol, quantity in self._total_commission_dict.items())
        return total_com

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
    def apply_fill(self, symbol: str, base_asset: str, quote_asset: str, qty_delta: float, price: float,
                   commission: float = 0.0, commission_asset: Optional[str] = None,
                   venue: Optional[Venue] = None):
        """Attribute a fill to this shadow book.

        Mirrors Portfolio.update_position() logic but operates on this
        strategy's virtual positions only.  Called by the fill attribution
        system after execution completes.

        Updates positions, realized PnL, and cash.
        Cash is adjusted here because _on_balance_update only handles
        deposits/withdrawals, not trade fills.
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

        # Commission tracking — always record in dict keyed by asset.
        # Position.total_commission is a raw quantity (not USDT-converted);
        # total_commission property does the USDT conversion via mark_prices.
        pos.total_commission += commission
        if commission_asset is not None and commission > 0:
            self._total_commission_dict[commission_asset] = (
                self._total_commission_dict.get(commission_asset, 0.0) + commission
            )

        self._positions[symbol] = pos
        if pos.is_flat():
            self._positions.pop(symbol, None)

        # ── Cash adjustment ───────────────────────────────────────────────
        # Both _cash (total) and _cash_dict (per-stablecoin breakdown) are
        # updated in parallel. _cash is authoritative; _cash_dict is the
        # breakdown. Non-stablecoin commissions (BNB etc.) are tracked only
        # in _total_commission_dict and do NOT reduce the stablecoin balance.
        notional = abs(qty_delta) * price
        if qty_delta > 0:  # BUY: spend quote asset
            self._cash -= notional
            self._cash_dict[quote_asset] = self._cash_dict.get(quote_asset, 0.0) - notional
        else:              # SELL: receive quote asset
            self._cash += notional
            self._cash_dict[quote_asset] = self._cash_dict.get(quote_asset, 0.0) + notional

        # Stablecoin commissions reduce the cash balance; non-stablecoin
        # commissions (e.g. BNB) are paid from a separate BNB balance and
        # do not affect the quote-asset cash.
        if commission_asset in _STABLECOINS and commission > 0:
            self._cash -= commission
            self._cash_dict[commission_asset] = (
                self._cash_dict.get(commission_asset, 0.0) - commission
            )

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
        In sub-account mode, returns early — handled by _on_balance_update on private bus.
        """
        # Technically not needed, base_strategy class calls the shadow_book.on_balance_update(event)
        return 

        # ── Locked balance — LIMIT order reservations ─────────────────────────
        #
        # Both sides are locked:
        #   BUY  → locks quote asset (cash) via _locked_cash & ActiveLimitOrder.reserved_cash
        #   SELL → locks base asset qty via Position.locked_quantity & ActiveLimitOrder.reserved_qty
        #
        # free_cash = _cash - _locked_cash        (available quote for new BUY orders)
        # free_quantity = pos.quantity - pos.locked_quantity  (available base for new SELL orders)

    def lock_for_order(self, order_id: str, intent: OrderIntent) -> None:
        """Reserve balance for a submitted LIMIT order.  No-op for MARKET orders.

        Creates an ActiveLimitOrder, initializes its reservation, and stores
        it in _active_orders.  BUY locks cash, SELL locks base-asset quantity.
        """
        if intent.order_type != OrderType.LIMIT:
            return
        if order_id in self._active_orders:
            return  # idempotent
        if intent.side == Side.BUY and intent.price is None:
            logger.warning(
                f"[ShadowBook] lock_for_order: LIMIT BUY {order_id} has price=None, skipping lock"
            )
            return

        active = ActiveLimitOrder(
            id=order_id, symbol=intent.symbol, side=intent.side,
            order_type=intent.order_type, quantity=intent.quantity,
            price=intent.price, status=OrderStatus.PENDING,
            venue=intent.venue or self._venue, strategy_id=intent.strategy_id,
        )
        active.initialize_reservation()
        self._active_orders[order_id] = active

        # BUY: lock quote-asset cash (USDT) = qty × price
        self._locked_cash += active.reserved_cash

        # SELL: lock base-asset quantity on the Position
        if active.reserved_qty > 0:
            pos = self._positions.get(intent.symbol)
            if pos is not None:
                pos.locked_quantity += active.reserved_qty

    @property
    def free_cash(self) -> float:
        """Cash available for new BUY orders (excludes LIMIT BUY reservations)."""
        return max(0.0, self._cash - self._locked_cash)

    def free_quantity(self, symbol: str) -> float:
        """Base-asset quantity available for new SELL orders (excludes LIMIT SELL reservations)."""
        return self.position(symbol).free_quantity

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
                sym: (pos.quantity * self._mark_prices.get(sym, pos.avg_entry_price)) / self._nav
                for sym, pos in self._positions.items()
                if not pos.is_flat()
                and self._mark_prices.get(sym, pos.avg_entry_price) > 0
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
            "cash": self._cash,
            "nav": self._nav,
            "realized_pnl": self._realized_pnl,
            "total_commission": self.total_commission,
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
                venue=self._venue,
                nav=self._nav,
                cash=self._cash,
                unrealized_pnl=self.unrealized_pnl(),
                realized_pnl=self._realized_pnl,
                total_commission=self.total_commission,
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
            f"ShadowBook(strategy={self._strategy_id}"
            f"cash={self._cash:.2f} nav={self._nav:.2f} positions={active})"
        )