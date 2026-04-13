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

import datetime
import time
import asyncio
from collections import deque
from typing import Deque, Dict, List, Optional, Set
from elysian_core.db.models import CexTrade
from elysian_core.core.enums import AssetType, OrderStatus, OrderType, Side, Venue
from elysian_core.core.events import EventType
from elysian_core.core.order import ActiveLimitOrder, ActiveOrder, Order
from elysian_core.core.market_data import OrderBook, Kline
from elysian_core.core.portfolio import Fill
from elysian_core.core.position import Position
from elysian_core.core.signals import OrderIntent
from elysian_core.db.models import PortfolioSnapshot
from elysian_core.core.events import BalanceUpdateEvent, OrderUpdateEvent
from elysian_core.config.app_config import StrategyConfig
import elysian_core.utils.logger as log
from elysian_core.core.constants import STABLECOINS, FILL_HISTORY_MAXLEN, QTY_EPSILON

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
                       venue: Venue = Venue.BINANCE,
                       strategy_name: str = "",
                       strategy_config: Optional[StrategyConfig] = None):
        self._strategy_id = strategy_id
        self._strategy_name = strategy_name or f"strategy_{strategy_id}"
        self.logger = log.setup_custom_logger(f"{self._strategy_name}_{strategy_id}")
        self._venue = venue

        # Symbols this strategy is allowed to track; None means track all
        symbols = (strategy_config.symbols or []) if strategy_config else []
        self._tracked_symbols: Optional[frozenset] = frozenset(symbols) if symbols else None
        self._asset_type: AssetType = (
            AssetType[strategy_config.asset_type.upper()]
            if strategy_config and strategy_config.asset_type
            else AssetType.SPOT
        )

        # Position state
        self._positions: Dict[str, Position] = {}
        self._cash: float = 0.0

        # Mark prices (updated via strategy's kline dispatch)
        # Stablecoins seeded at 1.0 so total_commission USDT conversion is correct
        self._mark_prices: Dict[str, float] = {symbol: 1.0 for symbol in STABLECOINS}
        self._klines : Dict[str, Kline] = {}

        # PnL tracking
        self._realized_pnl: float = 0.0
        
        #self._total_commission: float = 0.0
        self._total_commission_dict: Dict[str, float] = {}

        # Trade history (ring buffer, mirrors Portfolio._fills)
        self._fills: Deque[Fill] = deque(maxlen=FILL_HISTORY_MAXLEN)

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

        # Maps base_asset → trading symbol for positions currently held.
        # Used to resolve commission_asset → position for non-stablecoin fee deduction.
        self._asset_symbol_map: Dict[str, str] = {}

        # Tracks non-stablecoin commission quantities deducted from position qty.
        # Excluded from total_commission() to avoid double-counting in net_pnl
        # (the cost is already reflected via the reduced position value in NAV).
        self._position_deducted_comm: Dict[str, float] = {}
        
    #  ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    # ── Read — Identity ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    #  ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────

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

    #  ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    # ── Read — Valuation ────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    # ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    
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
        return self._realized_pnl + self.unrealized_pnl(mark_prices) - self.total_commission(mark_prices)

    @property
    def fills(self) -> List[Fill]:
        return list(self._fills)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def total_commission(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        """Total commissions converted to USDT.

        Excludes amounts already reflected in position qty reductions
        (_position_deducted_comm) to avoid double-counting in net_pnl.
        Uses _asset_symbol_map to correctly resolve raw asset names (e.g. 'BNB')
        to their trading-pair mark price (e.g. 'BNBUSDT').
        """
        prices = mark_prices if mark_prices is not None else self._mark_prices
        total = 0.0
        for asset, gross_qty in self._total_commission_dict.items():
            net_qty = max(0.0, gross_qty - self._position_deducted_comm.get(asset, 0.0))
            symbol = self._asset_symbol_map.get(asset, asset)
            price = prices.get(symbol) or prices.get(asset, 0.0)
            total += net_qty * price
        return total

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

    #  ────────────────────────────────────────────────────── ────────────────────────────────────────────────────── ──────────────────────────────────────────────────────
    # ────────────────────────────────────────────────────── Initialization ────────────────────────────────────────────────────
    #  ────────────────────────────────────────────────────── ────────────────────────────────────────────────────── ──────────────────────────────────────────────────────
    def sync_from_exchange(self, exchange, feeds: Optional[Dict] = None):
        """Initialize from real exchange state. Called once at startup for sub-account mode.

        Sets up cash, positions, mark prices, and open orders from the exchange.
        Feeds may be empty at sync time — mark prices self-correct on the first KLINE event.
        """
        self._exchange = exchange
        feeds = feeds or {}

        # Cash: stablecoin balances
        for stable in STABLECOINS:
            bal = exchange.get_balance(stable)
            if bal > 0:
                self._cash_dict[stable] = bal
        self._cash = sum(self._cash_dict.values())

        # Positions: non-stablecoin balances — map base asset → trading symbol
        for asset, qty in exchange._balances.items():
            if asset in STABLECOINS or qty <= 0:
                continue
            symbol = exchange.base_asset_to_symbol(asset)
            if symbol is None or (self._tracked_symbols and symbol not in self._tracked_symbols):
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
                    self.logger.warning(f"[ShadowBook-{self._strategy_id}] Failed to get price for {symbol} during sync: {e}")
            self._positions[symbol] = Position(
                symbol=symbol, venue=self._venue, quantity=qty, avg_entry_price=entry_price
            )
            self._asset_symbol_map[asset] = symbol

        # Mark prices from all remaining feeds
        for sym, feed in feeds.items():
            if sym not in self._mark_prices:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        self._mark_prices[sym] = p
                except Exception:
                    self.logger.warning(f"[ShadowBook-{self._strategy_id}] Failed to get price for {sym} during sync")

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
        self.logger.info(f"[ShadowBook-{self._strategy_id}] Snapshot after sync:\n{self.log_snapshot()}")

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


    # ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # ── Lifecycle — EventBus wiring ────────────────────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
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
        
        self.logger.info(
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
        self.logger.info(f"[ShadowBook-{self._strategy_id}] stopped")

    async def _on_kline(self, event):
        """Update mark prices from kline close price and refresh derived metrics."""
        if event.venue == self._venue and event.kline.close and event.kline.close > 0:
            self._mark_prices[event.symbol] = event.kline.close
            self._klines[event.symbol] = event.kline
            
            # Patch avg_entry_price for positions synced at startup before feeds had data.
            # Using current mark price as cost basis avoids inflated unrealized/realized PnL.
            pos = self._positions.get(event.symbol)
            if pos is not None and pos.avg_entry_price == 0.0 and pos.quantity > 0:
                pos.avg_entry_price = event.kline.close
                self.logger.info(
                    f"[ShadowBook-{self._strategy_id}] Patched avg_entry_price for "
                    f"{event.symbol} to {event.kline.close:.6f} (was 0.0 at sync-time)"
                )
            self._refresh_derived()
            
            
    def on_balance(self, event) -> None:
        """Mirror Portfolio._on_balance(), scaled by allocation.

        Called automatically by base_strategy._dispatch_balance() so that
        deposits and withdrawals are reflected in the shadow book.
        In sub-account mode, returns early — handled by _on_balance_update on private bus.
        """
        # Technically not needed, base_strategy class calls the shadow_book.on_balance_update(event)
        return


    async def _on_balance_update(self, event: BalanceUpdateEvent):
        """Handle balance updates from the private sub-account event bus."""
        if event.asset in STABLECOINS:
            old_cash = self._cash
            self._cash_dict[event.asset] = self._cash_dict.get(event.asset, 0.0) + event.delta
            self._cash = sum(self._cash_dict.values())
            self.logger.info(
                f"[ShadowBook-{self._strategy_id}] BalanceUpdate: {event.asset} "
                f"delta={event.delta:+.4f} cash {old_cash:.4f} -> {self._cash:.4f}"
            )
        else:
            # Map raw asset (e.g. "ETH") to trading symbol (e.g. "ETHUSDT")
            symbol = (
                self._exchange.base_asset_to_symbol(event.asset)
                if self._exchange else event.asset
            )
            if symbol is None or (self._tracked_symbols and symbol not in self._tracked_symbols):
                self.logger.debug(
                    f"[ShadowBook-{self._strategy_id}] BalanceUpdate: {event.asset} -> symbol={symbol} not tracked, skipping"
                )
                self._refresh_derived()
                return
            curr = self._positions.get(symbol)
            old_qty = curr.quantity if curr else 0.0
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
                    commission_by_asset=dict(curr.commission_by_asset),
                    locked_quantity=curr.locked_quantity,
                )
            self.logger.info(
                f"[ShadowBook-{self._strategy_id}] BalanceUpdate: {event.asset} ({symbol}) "
                f"delta={event.delta:+.6f} qty {old_qty:.6f} -> {old_qty + event.delta:.6f}"
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
            active = self._register_new_order(order, order_id)

        delta_filled = active.sync_from_event(order)
        if delta_filled > 0:
            self._process_fill_delta(active, event, order, order_id, delta_filled)

        if active.is_terminal:
            self._finalize_terminal_order(active, order, order_id)

    def _register_new_order(self, order, order_id: str) -> ActiveOrder:
        """Wrap a first-seen order in an ActiveOrder and register it for tracking."""
        active = ActiveOrder(
            id=order.id, symbol=order.symbol, side=order.side,
            order_type=order.order_type, quantity=order.quantity,
            price=order.price, status=OrderStatus.PENDING,
            venue=order.venue or self._venue, strategy_id=order.strategy_id,
        )
        self._active_orders[order_id] = active
        self.logger.info(
            f"[ShadowBook-{self._strategy_id}] NEW ORDER tracked: "
            f"{order.side.value} {order.symbol} qty={order.quantity:.6f} "
            f"type={order.order_type.value} order_id={order_id} "
            f"active_orders_count={len(self._active_orders)}"
        )
        return active

    def _process_fill_delta(self, active: ActiveOrder, event: OrderUpdateEvent,
                            order, order_id: str, delta_filled: float):
        """Release partial lock and apply a fill increment to positions/cash."""
        fill_price = order.last_fill_price if order.last_fill_price > 0 else order.avg_fill_price
        self.logger.info(
            f"[ShadowBook-{self._strategy_id}] FILL: {order.side.value} {order.symbol} "
            f"delta_filled={delta_filled:.6f} @ {fill_price:.4f} "
            f"total_filled={order.filled_qty:.6f}/{order.quantity:.6f} "
            f"order_id={order_id} commission={order.commission:.6f} {order.commission_asset}"
        )

        if isinstance(active, ActiveLimitOrder):
            cash_rel, qty_rel = active.release_partial(delta_filled)
            self._locked_cash = max(0.0, self._locked_cash - cash_rel)
            if qty_rel > 0:
                pos = self._positions.get(order.symbol)
                if pos is not None:
                    pos.locked_quantity = max(0.0, pos.locked_quantity - qty_rel)
            self.logger.debug(
                f"[ShadowBook-{self._strategy_id}] Lock released: "
                f"cash_rel={cash_rel:.4f} qty_rel={qty_rel:.6f} "
                f"locked_cash={self._locked_cash:.4f}"
            )

        qty_delta = delta_filled if order.side == Side.BUY else -delta_filled
        self.apply_fill(
            symbol=order.symbol,
            base_asset=event.base_asset,
            quote_asset=event.quote_asset,
            qty_delta=qty_delta,
            price=fill_price,
            commission=order.commission,
            commission_asset=order.commission_asset,
            venue=event.venue,
            order_id=order_id,
            timestamp=order.last_updated_timestamp or int(time.time() * 1000),
        )

    def _finalize_terminal_order(self, active: ActiveOrder, order, order_id: str):
        """Drain remaining lock, mark order completed, and schedule trade recording."""
        self.logger.info(
            f"[ShadowBook-{self._strategy_id}] ORDER TERMINAL: {order_id} "
            f"status={order.status.value} symbol={order.symbol} "
            f"filled={order.filled_qty:.6f}/{order.quantity:.6f} "
            f"avg_fill_price={order.avg_fill_price:.4f}"
        )
        if isinstance(active, ActiveLimitOrder):
            cash_rem, qty_rem = active.release_all()
            self._locked_cash = max(0.0, self._locked_cash - cash_rem)
            if qty_rem > 0:
                pos = self._positions.get(order.symbol)
                if pos is not None:
                    pos.locked_quantity = max(0.0, pos.locked_quantity - qty_rem)
            if cash_rem > 0 or qty_rem > 0:
                self.logger.info(
                    f"[ShadowBook-{self._strategy_id}] Remaining lock drained: "
                    f"cash_rem={cash_rem:.4f} qty_rem={qty_rem:.6f}"
                )
        self._active_orders.pop(order_id, None)
        self._completed_order_ids.add(order_id)
        asyncio.create_task(self._async_record_trade(active))
    
    # ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # ── Write — Fill attribution ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    def apply_fill(self, symbol: str, base_asset: str, quote_asset: str, qty_delta: float, price: float,
                   commission: float = 0.0, commission_asset: Optional[str] = None,
                   venue: Optional[Venue] = None, order_id: Optional[str] = None,
                   timestamp: Optional[int] = None):
        """Attribute a fill to this shadow book.

        Mirrors Portfolio.update_position() logic but operates on this
        strategy's virtual positions only.  Called by the fill attribution
        system after execution completes.

        Updates positions, realized PnL, and cash.
        Cash is adjusted here because _on_balance_update only handles
        deposits/withdrawals, not trade fills.
        """
        self._asset_symbol_map[base_asset] = symbol
        if abs(qty_delta) < QTY_EPSILON:
            return

        venue = venue or self._venue
        side = Side.BUY if qty_delta > 0 else Side.SELL

        # Capture before ANY mutation so the log reflects the true pre-fill cash.
        # _track_commission() deducts USDT commissions from _cash before _adjust_cash
        # runs, so old_cash must be captured here — not inside _adjust_cash.
        old_cash = self._cash

        pos, old_qty = self._update_position_on_fill(symbol, venue, qty_delta, price)
        self._track_commission(pos, commission_asset, commission, quote_asset)

        # Deferred flat-position cleanup: commission block may have further reduced qty
        # (e.g. SELL-to-zero BNBUSDT + BNB commission). pop() is a no-op if already removed.
        if pos.is_flat():
            self._positions.pop(symbol, None)

        notional = self._adjust_cash(qty_delta, price, quote_asset)
        self._refresh_derived()

        self._fills.append(Fill(
            symbol=symbol, venue=venue, side=side,
            quantity=abs(qty_delta), price=price,
            commission=commission,
            commission_asset=commission_asset,
            timestamp=timestamp or int(time.time() * 1000),
            order_id=order_id,
        ))

        self.logger.info(
            f"[ShadowBook-{self._strategy_id}] apply_fill: {side.value} {symbol} "
            f"qty={abs(qty_delta):.6f} @ {price:.4f} "
            f"pos {old_qty:.6f} -> {pos.quantity:.6f} "
            f"cash {old_cash:.4f} -> {self._cash:.4f} "
            f"notional={notional:.4f} commission={commission:.6f} {commission_asset or ''} | "
            f"NAV={self._nav:.4f} realized_pnl={self._realized_pnl:.4f} "
            f"comm_gross=({', '.join(f'{qty:.6f} {a}' for a, qty in self._total_commission_dict.items()) or 'none'}) "
            f"net_comm_usdt={self.total_commission():.4f}"
        )
        self.logger.info(f"[ShadowBook-{self._strategy_id}] Snapshot:\n{self.log_snapshot()}")

    def _update_position_on_fill(self, symbol: str, venue: Venue, qty_delta: float, price: float):
        """Update position quantity and avg_entry for a fill. Commits to self._positions.

        Returns (pos, old_qty). Position is committed before returning so that
        commission deduction (same-symbol lookup) can find it.
        """
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
                pos.avg_entry_price = price  # position flipped direction

        # Commit BEFORE commission block so same-symbol lookups (e.g. BNB fee on BNBUSDT)
        # can find the position. Flat-position pop is deferred until after commission handling.
        self._positions[symbol] = pos
        return pos, old_qty

    def _track_commission(self, pos, commission_asset: Optional[str], commission: float, quote_asset: str):
        """Record commission amounts and deduct non-stablecoin fees from positions/cash."""
        if commission_asset is None or commission <= 0:
            return

        pos.commission_by_asset[commission_asset] = (
            pos.commission_by_asset.get(commission_asset, 0.0) + commission
        )
        self._total_commission_dict[commission_asset] = (
            self._total_commission_dict.get(commission_asset, 0.0) + commission
        )

        if commission_asset not in STABLECOINS:
            # Non-stablecoin fee reduces the real asset balance on the exchange.
            # Mirror this by reducing the position qty so it stays in sync.
            comm_symbol = self._asset_symbol_map.get(commission_asset)
            if comm_symbol and comm_symbol in self._positions:
                fee_pos = self._positions[comm_symbol]
                fee_pos.quantity = max(0.0, fee_pos.quantity - commission)
                self._position_deducted_comm[commission_asset] = (
                    self._position_deducted_comm.get(commission_asset, 0.0) + commission
                )
                if fee_pos.is_flat():
                    self._positions.pop(comm_symbol, None)
            else:
                raise ValueError(
                    f"Commission asset {commission_asset} not found in positions for deduction"
                )
        else:
            # Stablecoin fee reduces cash directly.
            self._cash -= commission
            self._cash_dict[commission_asset] = (
                self._cash_dict.get(commission_asset, 0.0) - commission
            )

    def _adjust_cash(self, qty_delta: float, price: float, quote_asset: str) -> float:
        """Adjust cash for a fill. Returns notional."""
        notional = abs(qty_delta) * price
        if qty_delta > 0:  # BUY: spend quote asset
            self._cash -= notional
            self._cash_dict[quote_asset] = self._cash_dict.get(quote_asset, 0.0) - notional
        else:              # SELL: receive quote asset
            self._cash += notional
            self._cash_dict[quote_asset] = self._cash_dict.get(quote_asset, 0.0) + notional
        return notional
        
        
    

    def mark_to_market(self, mark_prices: Dict[str, float]):
        """Manual mark price update (mirrors Portfolio.mark_to_market)."""
        self._mark_prices.update(mark_prices)
        self._refresh_derived()

    def set_cash(self, amount: float):
        self._cash = amount

    def adjust_cash(self, delta: float):
        self._cash += delta

    

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
            self.logger.warning(
                f"[ShadowBook-{self._strategy_id}] lock_for_order: LIMIT BUY {order_id} has price=None, skipping lock"
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

        self.logger.info(
            f"[ShadowBook-{self._strategy_id}] lock_for_order: {intent.side.value} {intent.symbol} "
            f"order_id={order_id} qty={intent.quantity:.6f} @ {intent.price} "
            f"reserved_cash={active.reserved_cash:.4f} reserved_qty={active.reserved_qty:.6f} "
            f"locked_cash={self._locked_cash:.4f} free_cash={self.free_cash:.4f}"
        )

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



    #  ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    # ─────────────────────────────────── Display & Utility Functions ─────────────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    #  ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ─────────────────────────────────────────────────── ───────────────────────────────────────────────────
    
    async def _async_record_trade(self, active: ActiveOrder) -> None:
        '''
        Trade recording to db after order reaches terminal status (FILLED/CANCELED/REJECTED/EXPIRED).
        - Skip terminal orders with no fill — cancelled/rejected/expired with 0 qty have no trade to record
        - ie. we dont log cancelled trades with 0 fills, but we do log cancelled trades with partial fills
        '''
        
        if active.filled_qty <= 0:
            self.logger.debug(
                f"[ShadowBook-{self._strategy_id}] Skipping record_trade for "
                f"{active.symbol} {active.status.value} order {active.id} — no fills"
            )
            return

        comm_asset = active.commission_asset or ""
        ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        try:
            CexTrade.create(
                datetime=ts,
                strategy_id=self._strategy_id,
                strategy_name=self._strategy_name,
                venue=self._venue,
                asset_type=self._asset_type,
                symbol=active.symbol,
                side=active.side,
                base_amount=active.filled_qty,
                quote_amount=active.filled_qty * active.avg_fill_price,
                price=active.avg_fill_price,
                commission_asset=comm_asset,
                total_commission=active.commission,
                total_commission_quote=(
                    active.commission * active.avg_fill_price
                    if comm_asset.upper() not in STABLECOINS
                    else active.commission
                ),
                order_id=str(active.id),
                status=active.status,
                order_side=active.side,
                order_type=active.order_type,
            )
            self.logger.info(
                f"[ShadowBook-{self._strategy_id}] Trade recorded: "
                f"{active.symbol} {active.side.value} {active.filled_qty} @ {active.avg_fill_price}"
            )
        except Exception as e:
            self.logger.error(f"[ShadowBook-{self._strategy_id}] record_trade error: {e}")
            

    def log_snapshot(self, num_fills_show: int = 10) -> str:
        """Return a formatted multi-line snapshot string for log comparison.

        Enhanced version: includes locked cash, total exposure, position-deducted
        commissions, per-order details, per-position free/locked quantity,
        and the last 10 fill audits.
        """
        mark = self._mark_prices
        unrealized = self.unrealized_pnl(mark)
        net = self._realized_pnl + unrealized - self.total_commission(mark)
        raw_comm_usdt = sum(
            qty * mark.get(self._asset_symbol_map.get(a, a), mark.get(a, 1.0))
            for a, qty in self._total_commission_dict.items()
        )
        pos_deducted_usdt = sum(
            qty * mark.get(self._asset_symbol_map.get(a, a), mark.get(a, 1.0))
            for a, qty in self._position_deducted_comm.items()
        )
        total_exposure = sum(
            pos.notional(mark.get(pos.symbol, pos.avg_entry_price))
            for pos in self.active_positions.values()
        )

        lines = [
            f"┌─ ShadowBook-{self._strategy_id} ({self._strategy_name}) {'─' * 40}",
            f"│  {'Strategy':<20} {self._strategy_name}  |  Venue: {self._venue.name}  |  Type: {self._asset_type.name}",
            f"│  {'NAV':<20} {self._nav:>12.4f} USDT",
            f"│  {'Cash':<20} {self._cash:>12.4f} USDT  (free={self.free_cash:.4f})",
            f"│  {'Locked Cash':<20} {self._locked_cash:>12.4f} USDT",
            f"│  {'Total Exposure':<20} {total_exposure:>12.4f} USDT",
            f"│  {'Realized PnL':<20} {self._realized_pnl:>+12.4f} USDT",
            f"│  {'Unrealized PnL':<20} {unrealized:>+12.4f} USDT",
            f"│  {'Net PnL':<20} {net:>+12.4f} USDT",
            f"│  {'Commission (raw)':<20} {raw_comm_usdt:>12.4f} USDT  "
            f"({', '.join(f'{qty:.6f} {a}' for a, qty in self._total_commission_dict.items()) or 'none'})",
            f"│  {'Position-Deducted Comm':<20} {pos_deducted_usdt:>12.4f} USDT  "
            f"({', '.join(f'{qty:.6f} {a}' for a, qty in self._position_deducted_comm.items()) or 'none'})",
            f"│  {'Peak Equity':<20} {self._peak_equity:>12.4f} USDT",
            f"│  {'Max Drawdown':<20} {self._max_drawdown:>12.4%}  (current={self.current_drawdown():.4%})",
        ]

        # Active orders block
        if self._active_orders:
            lines.append(f"├─ Active Orders ({len(self._active_orders)}) {'─' * 46}")
            for oid, order in self._active_orders.items():
                if order.price is not None:  # ActiveLimitOrder
                    reserved = getattr(order, 'reserved_cash', 0.0)
                    lines.append(
                        f"│    {oid:<12} {order.side.name:<6} qty={order.quantity:>10.6f} "
                        f"price={order.price:>10.4f} reserved_cash={reserved:>10.4f}"
                    )
                else:  # ActiveOrder (market)
                    lines.append(
                        f"│    {oid:<12} {order.side.name:<6} qty={order.quantity:>10.6f} "
                        f"(market)"
                    )
        else:
            lines.append(f"│  {'Active Orders':<20} []")

        # Positions block
        lines.append(f"├─ Positions ({len(self.active_positions)}) {'─' * 43}")
        if not self.active_positions:
            lines.append("│  (flat)")
        for sym, pos in self.active_positions.items():
            mp = mark.get(sym, pos.avg_entry_price)
            upnl = pos.unrealized_pnl(mp)
            comm_str = ", ".join(f"{qty:.6f} {a}" for a, qty in pos.commission_by_asset.items()) or "none"
            w = self._weights.get(sym, 0.0)
            lines.append(
                f"│  {sym:<12}  qty={pos.quantity:>10.6f} (free={pos.free_quantity:>10.6f} locked={pos.locked_quantity:>10.6f})  "
                f"entry={pos.avg_entry_price:>10.4f}  mark={mp:>10.4f}  upnl={upnl:>+8.4f}  "
                f"rpnl={pos.realized_pnl:>+8.4f}  w={w:.4f}  comm=[{comm_str}]"
            )

        # Fill audits block (last 10 fills, most recent first)
        fills = list(self._fills)
        if fills:
            lines.append(f"├─ Fill Audits (last {min(num_fills_show, len(fills))} of {len(fills)}) {'─' * 38}")
            # Show most recent fills first (deque maxlen retains newest at right)
            for fill in reversed(fills[-num_fills_show:]):
                ts = fill.timestamp_str
                # Format timestamp as milliseconds (or convert to readable if needed)
                comm_str = f"{fill.commission:.6f} {fill.commission_asset}" if fill.commission else "none"
                lines.append(
                    f"│    {ts:<14} {fill.symbol:<8} {fill.side.name:<5} "
                    f"qty={fill.quantity:>10.6f} price={fill.price:>10.4f} "
                    f"comm=({comm_str:<12}) order={fill.order_id}"
                )
        else:
            lines.append("│  Fill Audits: (none)")

        lines.append(f"└─{'─' * 80}")
        return "\n".join(lines)
    
    
    def save_snapshot(self):
        positions_json = {
            sym: {
                "qty": pos.quantity,
                "avg_entry": pos.avg_entry_price,
                "realized_pnl": pos.realized_pnl,
                "comm_by_asset": dict(pos.commission_by_asset),
                }
            for sym, pos in self._positions.items()
            if not pos.is_flat()
        }

        try:
            self.logger.info(f"[ShadowBook-{self._strategy_id}] Saving snapshot to DB... \n{self.log_snapshot()}")
            PortfolioSnapshot.create(
                strategy_id=self._strategy_id,
                venue=self._venue,
                nav=self._nav,
                cash=self._cash,
                unrealized_pnl=self.unrealized_pnl(),
                realized_pnl=self._realized_pnl,
                total_commission=self.total_commission(),
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
            self.logger.info(
                f"[ShadowBook-{self._strategy_id}] Snapshot saved: nav={self._nav:.2f} "
                f"cash={self._cash:.2f} dd={self._max_drawdown:.4%} "
                f"positions={len(positions_json)}"
            )
        except Exception as e:
            self.logger.error(
                f"[ShadowBook-{self._strategy_id}] Failed to save snapshot: {e}",
                exc_info=True,
            )

    def __repr__(self) -> str:
        active = len(self.active_positions)
        return (
            f"ShadowBook(strategy={self._strategy_id}"
            f"cash={self._cash:.2f} nav={self._nav:.2f} positions={active})"
        )