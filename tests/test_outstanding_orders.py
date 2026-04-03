"""
Integration tests for order tracking in ShadowBook.

Portfolio is now a thin NAV aggregator — it no longer tracks orders or positions.
All per-strategy order, position, and cash tracking lives in ShadowBook.

In sub-account mode each ShadowBook receives events only from its own private
EventBus, so isolation is guaranteed at the bus level. These tests verify
ShadowBook correctly tracks fills, positions, and cash for a single strategy.

Tests:
  1. Order tracking — ShadowBook.active_orders lifecycle
  2. Position attribution — fill attribution to ShadowBook positions
  3. FSM transitions — ActiveOrder.sync_from_event valid and invalid state changes
  4. Partial fill lifecycle — OPEN -> PARTIAL x N -> FILLED end-to-end
  5. Portfolio aggregation — thin Portfolio.aggregate_nav() across ShadowBooks
"""

import asyncio
import pytest
from unittest.mock import MagicMock

from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.order import Order
from elysian_core.core.events import OrderUpdateEvent
from elysian_core.core.portfolio import Portfolio
from elysian_core.core.shadow_book import ShadowBook


BTCUSDT = "BTCUSDT"
ETHUSDT = "ETHUSDT"
BTC_PRICE = 50_000.0
ETH_PRICE = 3_000.0
INITIAL_CASH = 200_000.0

# Base/quote mapping for OrderUpdateEvent
_PAIR_MAP = {
    BTCUSDT: ("BTC", "USDT"),
    ETHUSDT: ("ETH", "USDT"),
}


def _shadow_book(strategy_id: int) -> ShadowBook:
    book = ShadowBook(strategy_id=strategy_id, venue=Venue.BINANCE)
    book.init_from_portfolio_cash(
        portfolio_cash=INITIAL_CASH,
        mark_prices={BTCUSDT: BTC_PRICE, ETHUSDT: ETH_PRICE},
    )
    return book


def _order(
    order_id: str,
    symbol: str = BTCUSDT,
    side: Side = Side.BUY,
    quantity: float = 0.1,
    status: OrderStatus = OrderStatus.OPEN,
    filled_qty: float = 0.0,
    avg_fill_price: float = 0.0,
    commission: float = 0.0,
    strategy_id: int = None,
) -> Order:
    price = BTC_PRICE if symbol == BTCUSDT else ETH_PRICE
    return Order(
        id=order_id,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
        avg_fill_price=avg_fill_price,
        filled_qty=filled_qty,
        status=status,
        venue=Venue.BINANCE,
        commission=commission,
        strategy_id=strategy_id,
    )


def _event(order: Order) -> OrderUpdateEvent:
    base, quote = _PAIR_MAP.get(order.symbol, ("UNKNOWN", "USDT"))
    return OrderUpdateEvent(
        symbol=order.symbol,
        base_asset=base,
        quote_asset=quote,
        venue=Venue.BINANCE,
        order=order,
        timestamp=0,
    )


def run(coro):
    return asyncio.run(coro)


def dispatch(event: OrderUpdateEvent, book: ShadowBook):
    """Simulate private bus delivering ORDER_UPDATE to one shadow book."""
    run(book._on_order_update(event))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Order tracking lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrderTracking:

    def setup_method(self):
        self.book = _shadow_book(strategy_id=1)

    def test_open_order_added(self):
        order = _order("btc-1")
        dispatch(_event(order), self.book)
        assert "btc-1" in self.book.active_orders

    def test_filled_order_removed(self):
        dispatch(_event(_order("btc-1")), self.book)
        dispatch(_event(_order("btc-1", status=OrderStatus.FILLED,
                               filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert "btc-1" not in self.book.active_orders

    def test_cancelled_order_removed(self):
        dispatch(_event(_order("btc-1")), self.book)
        dispatch(_event(_order("btc-1", status=OrderStatus.CANCELLED)), self.book)
        assert "btc-1" not in self.book.active_orders

    def test_partial_fill_stays_active(self):
        dispatch(_event(_order("btc-1")), self.book)
        dispatch(_event(_order("btc-1", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.05, avg_fill_price=BTC_PRICE)), self.book)
        assert "btc-1" in self.book.active_orders

    def test_active_orders_is_copy(self):
        dispatch(_event(_order("btc-1")), self.book)
        snap = self.book.active_orders
        snap["injected"] = MagicMock()
        assert "injected" not in self.book._active_orders


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Position attribution
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionAttribution:

    def setup_method(self):
        self.book = _shadow_book(strategy_id=1)

    def test_buy_fill_creates_position(self):
        dispatch(_event(_order("btc-1", status=OrderStatus.FILLED,
                               filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert self.book.position(BTCUSDT).quantity == pytest.approx(0.1)

    def test_sell_fill_reduces_position(self):
        dispatch(_event(_order("buy", status=OrderStatus.FILLED,
                               quantity=0.2, filled_qty=0.2, avg_fill_price=BTC_PRICE)), self.book)
        dispatch(_event(_order("sell", side=Side.SELL, status=OrderStatus.FILLED,
                               quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert self.book.position(BTCUSDT).quantity == pytest.approx(0.1)

    def test_partial_fills_accumulate(self):
        dispatch(_event(_order("btc-1", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.04, avg_fill_price=BTC_PRICE)), self.book)
        dispatch(_event(_order("btc-1", status=OrderStatus.FILLED,
                               quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert self.book.position(BTCUSDT).quantity == pytest.approx(0.1)

    def test_buy_fill_decreases_cash(self):
        """BUY fills decrease cash by notional = qty * price."""
        cash_before = self.book.cash
        dispatch(_event(_order("btc-1", status=OrderStatus.FILLED,
                               filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert self.book.cash == pytest.approx(cash_before - 0.1 * BTC_PRICE)

    def test_eth_fill_does_not_affect_btc_position(self):
        dispatch(_event(_order("eth-1", symbol=ETHUSDT, status=OrderStatus.FILLED,
                               quantity=2.0, filled_qty=2.0, avg_fill_price=ETH_PRICE)), self.book)
        assert self.book.position(BTCUSDT).quantity == pytest.approx(0.0)
        assert self.book.position(ETHUSDT).quantity == pytest.approx(2.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. FSM transitions via _on_order_update
# ═══════════════════════════════════════════════════════════════════════════════

class TestFsmTransitions:

    def setup_method(self):
        self.book = _shadow_book(strategy_id=1)

    def test_pending_to_open(self):
        dispatch(_event(_order("o1", status=OrderStatus.PENDING)), self.book)
        assert "o1" in self.book._active_orders
        dispatch(_event(_order("o1", status=OrderStatus.OPEN)), self.book)
        assert self.book._active_orders["o1"].status == OrderStatus.OPEN

    def test_open_to_partially_filled(self):
        dispatch(_event(_order("o2", status=OrderStatus.OPEN)), self.book)
        dispatch(_event(_order("o2", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.05, avg_fill_price=BTC_PRICE)), self.book)
        tracked = self.book._active_orders["o2"]
        assert tracked.status == OrderStatus.PARTIALLY_FILLED
        assert tracked.filled_qty == pytest.approx(0.05)

    def test_partially_filled_increments(self):
        dispatch(_event(_order("o3", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.03, avg_fill_price=BTC_PRICE)), self.book)
        dispatch(_event(_order("o3", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.07, avg_fill_price=BTC_PRICE)), self.book)
        assert self.book._active_orders["o3"].filled_qty == pytest.approx(0.07)

    def test_partially_filled_to_filled_removes(self):
        dispatch(_event(_order("o4", status=OrderStatus.PARTIALLY_FILLED,
                               quantity=0.1, filled_qty=0.05, avg_fill_price=BTC_PRICE)), self.book)
        dispatch(_event(_order("o4", status=OrderStatus.FILLED,
                               quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert "o4" not in self.book._active_orders

    def test_open_to_cancelled_removes(self):
        dispatch(_event(_order("o5", status=OrderStatus.OPEN)), self.book)
        dispatch(_event(_order("o5", status=OrderStatus.CANCELLED)), self.book)
        assert "o5" not in self.book._active_orders

    def test_open_to_expired_removes(self):
        dispatch(_event(_order("o6", status=OrderStatus.OPEN)), self.book)
        dispatch(_event(_order("o6", status=OrderStatus.EXPIRED)), self.book)
        assert "o6" not in self.book._active_orders

    def test_stale_lower_fill_qty_ignored(self):
        dispatch(_event(_order("o8", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.07, avg_fill_price=BTC_PRICE)), self.book)
        dispatch(_event(_order("o8", status=OrderStatus.PARTIALLY_FILLED,
                               filled_qty=0.03, avg_fill_price=BTC_PRICE)), self.book)
        assert self.book._active_orders["o8"].filled_qty == pytest.approx(0.07)

    def test_duplicate_terminal_event_not_readded(self):
        dispatch(_event(_order("o9", status=OrderStatus.PARTIALLY_FILLED,
                               quantity=0.1, filled_qty=0.05, avg_fill_price=BTC_PRICE)), self.book)
        dispatch(_event(_order("o9", status=OrderStatus.FILLED,
                               quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert "o9" not in self.book._active_orders
        dispatch(_event(_order("o9", status=OrderStatus.FILLED,
                               quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE)), self.book)
        assert "o9" not in self.book._active_orders


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Partial fill lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestPartialFillLifecycle:

    ORDER_ID = "btc-lifecycle"
    TOTAL_QTY = 0.3
    FILLS = [
        (OrderStatus.PARTIALLY_FILLED, 0.1, BTC_PRICE),
        (OrderStatus.PARTIALLY_FILLED, 0.2, BTC_PRICE),
        (OrderStatus.FILLED, 0.3, BTC_PRICE),
    ]

    def setup_method(self):
        self.book = _shadow_book(strategy_id=1)

    def test_active_orders_track_lifecycle(self):
        dispatch(_event(_order(self.ORDER_ID, quantity=self.TOTAL_QTY)), self.book)
        assert self.ORDER_ID in self.book.active_orders

        for status, filled_qty, price in self.FILLS:
            o = _order(self.ORDER_ID, status=status, quantity=self.TOTAL_QTY,
                       filled_qty=filled_qty, avg_fill_price=price)
            dispatch(_event(o), self.book)
            if status == OrderStatus.FILLED:
                assert self.ORDER_ID not in self.book.active_orders
            else:
                assert self.ORDER_ID in self.book.active_orders

    def test_positions_accumulate_through_partial_fills(self):
        for status, filled_qty, price in self.FILLS:
            dispatch(_event(_order(self.ORDER_ID, status=status, quantity=self.TOTAL_QTY,
                                   filled_qty=filled_qty, avg_fill_price=price)), self.book)
        assert self.book.position(BTCUSDT).quantity == pytest.approx(self.TOTAL_QTY)

    def test_cash_decreases_through_buy_fills(self):
        """BUY fills decrease cash by total notional across all partial fills."""
        cash_before = self.book.cash
        for status, filled_qty, price in self.FILLS:
            dispatch(_event(_order(self.ORDER_ID, status=status, quantity=self.TOTAL_QTY,
                                   filled_qty=filled_qty, avg_fill_price=price)), self.book)
        assert self.book.cash == pytest.approx(cash_before - self.TOTAL_QTY * BTC_PRICE)

    def test_filled_qty_tracks_cumulative(self):
        dispatch(_event(_order(self.ORDER_ID, quantity=self.TOTAL_QTY)), self.book)
        dispatch(_event(_order(self.ORDER_ID, status=OrderStatus.PARTIALLY_FILLED,
                               quantity=self.TOTAL_QTY, filled_qty=0.1,
                               avg_fill_price=BTC_PRICE)), self.book)
        assert self.book._active_orders[self.ORDER_ID].filled_qty == pytest.approx(0.1)

        dispatch(_event(_order(self.ORDER_ID, status=OrderStatus.PARTIALLY_FILLED,
                               quantity=self.TOTAL_QTY, filled_qty=0.2,
                               avg_fill_price=BTC_PRICE)), self.book)
        assert self.book._active_orders[self.ORDER_ID].filled_qty == pytest.approx(0.2)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Portfolio aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPortfolioAggregation:

    def test_register_shadow_book(self):
        portfolio = Portfolio(venue=Venue.BINANCE)
        sb = ShadowBook(strategy_id=42, venue=Venue.BINANCE)
        portfolio.register_shadow_book(sb)
        assert 42 in portfolio._shadow_books
        assert portfolio._shadow_books[42] is sb

    def test_aggregate_nav_sums_shadow_books(self):
        portfolio = Portfolio(venue=Venue.BINANCE)
        sb1 = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb1._cash = 3000.0
        sb1._refresh_derived()
        sb2 = ShadowBook(strategy_id=2, venue=Venue.BINANCE)
        sb2._cash = 2000.0
        sb2._refresh_derived()
        portfolio.register_shadow_book(sb1)
        portfolio.register_shadow_book(sb2)
        assert portfolio.aggregate_nav() == pytest.approx(5000.0)

    def test_aggregate_nav_empty_returns_zero(self):
        assert Portfolio(venue=Venue.BINANCE).aggregate_nav() == pytest.approx(0.0)

    def test_aggregate_cash_sums_shadow_books(self):
        portfolio = Portfolio(venue=Venue.BINANCE)
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 500.0
        portfolio.register_shadow_book(sb)
        assert portfolio.aggregate_cash() == pytest.approx(500.0)
