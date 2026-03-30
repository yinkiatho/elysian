"""
Integration tests for outstanding order tracking across Portfolio and ShadowBook.

Tests:
  1. Multi-strategy order tally — Portfolio.open_orders vs ShadowBook.outstanding_orders
  2. Position tally invariant — sum(shadow book positions) ≈ portfolio positions
  3. Weight tally — allocation-weighted sum of shadow book weights = portfolio weight
  4. FSM transitions — valid and invalid order state changes in _update_order
  5. Strategy isolation — orders and positions not cross-contaminated between strategies
  6. Partial fill lifecycle — OPEN → PARTIAL × N → FILLED end-to-end
"""

import asyncio
import pytest
from unittest.mock import MagicMock

from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.order import Order
from elysian_core.core.events import OrderUpdateEvent
from elysian_core.core.portfolio import Portfolio
from elysian_core.core.shadow_book import ShadowBook


# ── Constants ─────────────────────────────────────────────────────────────────

BTCUSDT = "BTCUSDT"
ETHUSDT = "ETHUSDT"
BTC_PRICE = 50_000.0
ETH_PRICE = 3_000.0
INITIAL_CASH = 200_000.0
STRAT1_ALLOC = 0.6   # 120,000 USDT
STRAT2_ALLOC = 0.4   # 80,000 USDT


# ── Builders ──────────────────────────────────────────────────────────────────

def _make_exchange():
    exc = MagicMock()
    exc._open_orders = {}
    exc._token_infos = {}
    exc._balances = {}
    exc.get_balance.return_value = 0.0
    return exc


def _portfolio(cash: float = INITIAL_CASH) -> Portfolio:
    p = Portfolio(exchange=_make_exchange(), venue=Venue.BINANCE)
    p._cash = cash
    p._cash_dict = {"USDT": cash}
    p._mark_prices = {BTCUSDT: BTC_PRICE, ETHUSDT: ETH_PRICE}
    p._refresh_derived()
    return p


def _shadow_book(strategy_id: int, allocation: float) -> ShadowBook:
    book = ShadowBook(strategy_id=strategy_id, allocation=allocation, venue=Venue.BINANCE)
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


def _event(order: Order, venue: Venue = Venue.BINANCE) -> OrderUpdateEvent:
    return OrderUpdateEvent(symbol=order.symbol, venue=venue, order=order, timestamp=0)


async def _broadcast(event: OrderUpdateEvent, portfolio: Portfolio, *books: ShadowBook):
    """Simulate EventBus broadcasting ORDER_UPDATE to portfolio and all shadow books."""
    await portfolio._on_order_update(event)
    for book in books:
        await book._on_order_update(event)


def run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Order tally — Portfolio.open_orders vs ShadowBook.outstanding_orders
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrderTally:
    """Portfolio and shadow books track the same live order set from their own perspective."""

    def setup_method(self):
        self.portfolio = _portfolio()
        self.book1 = _shadow_book(strategy_id=1, allocation=STRAT1_ALLOC)
        self.book2 = _shadow_book(strategy_id=2, allocation=STRAT2_ALLOC)

    def test_open_order_appears_in_portfolio_and_owning_shadow_book(self):
        """OPEN event: portfolio tracks it, owning shadow book tracks it, other does not."""
        order = _order("btc-1", strategy_id=1)
        run(_broadcast(_event(order), self.portfolio, self.book1, self.book2))

        assert "btc-1" in self.portfolio.open_orders
        assert "btc-1" in self.book1.outstanding_orders
        assert "btc-1" not in self.book2.outstanding_orders

    def test_two_strategy_orders_tracked_independently(self):
        """Each strategy's order is visible at portfolio level and only its own shadow book."""
        o1 = _order("btc-1", symbol=BTCUSDT, strategy_id=1)
        o2 = _order("eth-2", symbol=ETHUSDT, strategy_id=2)

        run(_broadcast(_event(o1), self.portfolio, self.book1, self.book2))
        run(_broadcast(_event(o2), self.portfolio, self.book1, self.book2))

        # Portfolio sees both
        assert "btc-1" in self.portfolio.open_orders
        assert "eth-2" in self.portfolio.open_orders

        # Each shadow book sees only its own
        assert "btc-1" in self.book1.outstanding_orders
        assert "eth-2" not in self.book1.outstanding_orders
        assert "eth-2" in self.book2.outstanding_orders
        assert "btc-1" not in self.book2.outstanding_orders

    def test_union_of_shadow_books_equals_portfolio_open_orders(self):
        """Union of shadow book outstanding orders == portfolio open orders (when all tagged)."""
        o1 = _order("btc-1", symbol=BTCUSDT, strategy_id=1)
        o2 = _order("eth-2", symbol=ETHUSDT, strategy_id=2)

        run(_broadcast(_event(o1), self.portfolio, self.book1, self.book2))
        run(_broadcast(_event(o2), self.portfolio, self.book1, self.book2))

        union = set(self.book1.outstanding_orders) | set(self.book2.outstanding_orders)
        assert union == set(self.portfolio.open_orders)

    def test_filled_order_removed_from_all_levels(self):
        """FILLED event: removed from portfolio and owning shadow book simultaneously."""
        # Place
        o_open = _order("btc-1", strategy_id=1)
        run(_broadcast(_event(o_open), self.portfolio, self.book1, self.book2))

        # Fill
        o_fill = _order("btc-1", status=OrderStatus.FILLED, filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(o_fill), self.portfolio, self.book1, self.book2))

        assert "btc-1" not in self.portfolio.open_orders
        assert "btc-1" not in self.book1.outstanding_orders
        assert "btc-1" not in self.book2.outstanding_orders

    def test_cancelled_order_removed_from_all_levels(self):
        """CANCELLED event: removed from portfolio and owning shadow book."""
        o_open = _order("eth-2", symbol=ETHUSDT, strategy_id=2)
        run(_broadcast(_event(o_open), self.portfolio, self.book1, self.book2))

        o_cancel = _order("eth-2", symbol=ETHUSDT, status=OrderStatus.CANCELLED, strategy_id=2)
        run(_broadcast(_event(o_cancel), self.portfolio, self.book1, self.book2))

        assert "eth-2" not in self.portfolio.open_orders
        assert "eth-2" not in self.book2.outstanding_orders

    def test_partial_fill_stays_in_outstanding(self):
        """PARTIALLY_FILLED event: order stays in all relevant dicts."""
        o_open = _order("btc-1", strategy_id=1)
        run(_broadcast(_event(o_open), self.portfolio, self.book1, self.book2))

        o_partial = _order("btc-1", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.05,
                           avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(o_partial), self.portfolio, self.book1, self.book2))

        assert "btc-1" in self.portfolio.open_orders
        assert "btc-1" in self.book1.outstanding_orders

    def test_open_orders_and_outstanding_orders_are_copies(self):
        """Mutating the returned dict does not affect internal state."""
        o = _order("btc-1", strategy_id=1)
        run(_broadcast(_event(o), self.portfolio, self.book1, self.book2))

        p_snap = self.portfolio.open_orders
        b_snap = self.book1.outstanding_orders

        p_snap["injected"] = MagicMock()
        b_snap["injected"] = MagicMock()

        assert "injected" not in self.portfolio._outstanding_orders
        assert "injected" not in self.book1._outstanding_orders


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Position tally — sum(shadow book quantities) == portfolio quantity
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionTally:
    """
    Invariant: sum(book.position(sym).quantity for all books) ≈ portfolio.position(sym).quantity

    This holds when every fill event is broadcast to both the portfolio and
    the owning shadow book with the correct strategy_id.
    """

    def setup_method(self):
        self.portfolio = _portfolio()
        self.book1 = _shadow_book(strategy_id=1, allocation=STRAT1_ALLOC)
        self.book2 = _shadow_book(strategy_id=2, allocation=STRAT2_ALLOC)

    def test_single_strategy_fill_positions_tally(self):
        """Strategy 1 buys 0.1 BTC — portfolio and book1 should both show 0.1 BTC."""
        fill = _order("btc-1", status=OrderStatus.FILLED, filled_qty=0.1,
                      avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(fill), self.portfolio, self.book1, self.book2))

        p_qty = self.portfolio.position(BTCUSDT).quantity
        b1_qty = self.book1.position(BTCUSDT).quantity
        b2_qty = self.book2.position(BTCUSDT).quantity

        assert p_qty == pytest.approx(0.1)
        assert b1_qty == pytest.approx(0.1)
        assert b2_qty == pytest.approx(0.0)
        assert b1_qty + b2_qty == pytest.approx(p_qty)

    def test_two_strategy_fills_position_tally(self):
        """Strategy 1 buys 0.1 BTC, strategy 2 buys 2.0 ETH — positions tally for each symbol."""
        btc_fill = _order("btc-1", symbol=BTCUSDT, status=OrderStatus.FILLED,
                          quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        eth_fill = _order("eth-2", symbol=ETHUSDT, status=OrderStatus.FILLED,
                          quantity=2.0, filled_qty=2.0, avg_fill_price=ETH_PRICE, strategy_id=2)

        run(_broadcast(_event(btc_fill), self.portfolio, self.book1, self.book2))
        run(_broadcast(_event(eth_fill), self.portfolio, self.book1, self.book2))

        # BTC tally
        p_btc = self.portfolio.position(BTCUSDT).quantity
        b1_btc = self.book1.position(BTCUSDT).quantity
        b2_btc = self.book2.position(BTCUSDT).quantity
        assert b1_btc + b2_btc == pytest.approx(p_btc)
        assert p_btc == pytest.approx(0.1)

        # ETH tally
        p_eth = self.portfolio.position(ETHUSDT).quantity
        b1_eth = self.book1.position(ETHUSDT).quantity
        b2_eth = self.book2.position(ETHUSDT).quantity
        assert b1_eth + b2_eth == pytest.approx(p_eth)
        assert p_eth == pytest.approx(2.0)

    def test_partial_fills_accumulate_correctly(self):
        """Two partial fills summing to full quantity tally correctly."""
        p1 = _order("btc-1", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.04,
                    avg_fill_price=BTC_PRICE, strategy_id=1)
        p2 = _order("btc-1", status=OrderStatus.FILLED, quantity=0.1, filled_qty=0.1,
                    avg_fill_price=BTC_PRICE, strategy_id=1)

        run(_broadcast(_event(p1), self.portfolio, self.book1, self.book2))
        run(_broadcast(_event(p2), self.portfolio, self.book1, self.book2))

        p_qty = self.portfolio.position(BTCUSDT).quantity
        b1_qty = self.book1.position(BTCUSDT).quantity
        assert p_qty == pytest.approx(0.1)
        assert b1_qty == pytest.approx(0.1)
        assert b1_qty == pytest.approx(p_qty)

    def test_sell_fill_reduces_position_consistently(self):
        """Buy then partial sell — both portfolio and shadow book reduce together."""
        # Buy 0.2
        buy = _order("btc-buy", status=OrderStatus.FILLED, quantity=0.2,
                     filled_qty=0.2, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(buy), self.portfolio, self.book1, self.book2))

        # Sell 0.1
        sell = _order("btc-sell", side=Side.SELL, status=OrderStatus.FILLED,
                      quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(sell), self.portfolio, self.book1, self.book2))

        p_qty = self.portfolio.position(BTCUSDT).quantity
        b1_qty = self.book1.position(BTCUSDT).quantity
        assert p_qty == pytest.approx(0.1)
        assert b1_qty == pytest.approx(0.1)
        assert b1_qty + self.book2.position(BTCUSDT).quantity == pytest.approx(p_qty)

    def test_nav_tally_after_fills(self):
        """Sum of shadow book NAVs ≈ portfolio NAV after fills."""
        fill = _order("btc-1", status=OrderStatus.FILLED, filled_qty=0.1,
                      avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(fill), self.portfolio, self.book1, self.book2))

        total_book_nav = self.book1.nav + self.book2.nav
        assert total_book_nav == pytest.approx(self.portfolio.nav, rel=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Weight tally — allocation-weighted shadow book weights == portfolio weights
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeightTally:
    """
    portfolio.weights[sym] ≈ sum_i( book_i.nav / portfolio.nav * book_i.weights[sym] )

    This is the allocation-weighted composition of per-strategy weights.
    """

    def setup_method(self):
        self.portfolio = _portfolio()
        self.book1 = _shadow_book(strategy_id=1, allocation=STRAT1_ALLOC)
        self.book2 = _shadow_book(strategy_id=2, allocation=STRAT2_ALLOC)

    def _weighted_shadow_weight(self, sym: str) -> float:
        """Compute the allocation-weighted shadow book weight for a symbol."""
        total_nav = self.portfolio.nav
        if total_nav <= 0:
            return 0.0
        return sum(
            (book.nav / total_nav) * book.weights.get(sym, 0.0)
            for book in (self.book1, self.book2)
        )

    def test_weights_tally_after_single_strategy_fill(self):
        """After strategy 1 fills 0.1 BTC, portfolio BTC weight == weighted shadow weight."""
        fill = _order("btc-1", status=OrderStatus.FILLED, filled_qty=0.1,
                      avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(fill), self.portfolio, self.book1, self.book2))

        assert self.portfolio.weights.get(BTCUSDT, 0.0) == pytest.approx(
            self._weighted_shadow_weight(BTCUSDT), rel=1e-5
        )

    def test_weights_tally_after_two_strategy_fills(self):
        """Both BTCUSDT and ETHUSDT weights tally after two-strategy fills."""
        btc_fill = _order("btc-1", symbol=BTCUSDT, status=OrderStatus.FILLED,
                          quantity=0.1, filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        eth_fill = _order("eth-2", symbol=ETHUSDT, status=OrderStatus.FILLED,
                          quantity=2.0, filled_qty=2.0, avg_fill_price=ETH_PRICE, strategy_id=2)

        run(_broadcast(_event(btc_fill), self.portfolio, self.book1, self.book2))
        run(_broadcast(_event(eth_fill), self.portfolio, self.book1, self.book2))

        for sym in (BTCUSDT, ETHUSDT):
            assert self.portfolio.weights.get(sym, 0.0) == pytest.approx(
                self._weighted_shadow_weight(sym), rel=1e-5
            )

    def test_weights_are_zero_before_any_fills(self):
        """No fills → no positions → all weights zero."""
        assert self.portfolio.weights == {}
        assert self.book1.weights == {}
        assert self.book2.weights == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. FSM transitions
# ═══════════════════════════════════════════════════════════════════════════════

class TestFsmTransitions:
    """
    _update_order delegates status changes to Order.transition_to / update_fill.
    Valid transitions proceed cleanly; invalid ones are applied in warn-mode.
    """

    def setup_method(self):
        self.portfolio = _portfolio()
        self.book1 = _shadow_book(strategy_id=1, allocation=1.0)

    # ── Valid transitions ────────────────────────────────────────────────────

    def test_pending_to_open(self):
        """PENDING → OPEN: order added to outstanding dicts."""
        o = _order("o1", status=OrderStatus.PENDING)
        self.portfolio._update_order(o)
        assert "o1" in self.portfolio._outstanding_orders

        o_open = _order("o1", status=OrderStatus.OPEN)
        self.portfolio._update_order(o_open)
        assert self.portfolio._outstanding_orders["o1"].status == OrderStatus.OPEN

    def test_open_to_partially_filled(self):
        """OPEN → PARTIALLY_FILLED: status updated via update_fill, stays in dict."""
        o = _order("o2", status=OrderStatus.OPEN)
        self.portfolio._outstanding_orders["o2"] = o

        update = _order("o2", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.05, avg_fill_price=BTC_PRICE)
        self.portfolio._update_order(update)

        tracked = self.portfolio._outstanding_orders["o2"]
        assert tracked.status == OrderStatus.PARTIALLY_FILLED
        assert tracked.filled_qty == pytest.approx(0.05)

    def test_partially_filled_to_partially_filled(self):
        """PARTIALLY_FILLED → PARTIALLY_FILLED: additional fill applied, stays in dict."""
        o = _order("o3", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.03)
        self.portfolio._outstanding_orders["o3"] = o

        update = _order("o3", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.07, avg_fill_price=BTC_PRICE)
        self.portfolio._update_order(update)

        tracked = self.portfolio._outstanding_orders["o3"]
        assert tracked.filled_qty == pytest.approx(0.07)
        assert "o3" in self.portfolio._outstanding_orders

    def test_partially_filled_to_filled(self):
        """PARTIALLY_FILLED → FILLED: order removed from dict."""
        o = _order("o4", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.05)
        self.portfolio._outstanding_orders["o4"] = o

        update = _order("o4", status=OrderStatus.FILLED, filled_qty=0.1, avg_fill_price=BTC_PRICE)
        self.portfolio._update_order(update)

        assert "o4" not in self.portfolio._outstanding_orders

    def test_open_to_cancelled(self):
        """OPEN → CANCELLED: order removed from dict."""
        o = _order("o5", status=OrderStatus.OPEN)
        self.portfolio._outstanding_orders["o5"] = o

        update = _order("o5", status=OrderStatus.CANCELLED)
        self.portfolio._update_order(update)

        assert "o5" not in self.portfolio._outstanding_orders

    def test_open_to_expired(self):
        """OPEN → EXPIRED: order removed from dict (terminal)."""
        o = _order("o6", status=OrderStatus.OPEN)
        self.portfolio._outstanding_orders["o6"] = o

        update = _order("o6", status=OrderStatus.EXPIRED)
        self.portfolio._update_order(update)

        assert "o6" not in self.portfolio._outstanding_orders

    # ── Invalid transitions (warn-mode: applied anyway) ───────────────────────

    def test_invalid_open_to_rejected_still_removes(self):
        """OPEN → REJECTED is not in FSM table but warn-mode applies it; REJECTED is terminal."""
        o = _order("o7", status=OrderStatus.OPEN)
        self.portfolio._outstanding_orders["o7"] = o

        # REJECTED is not a valid transition from OPEN, but is terminal
        update = _order("o7", status=OrderStatus.REJECTED)
        self.portfolio._update_order(update)

        # In warn-mode the transition is applied; REJECTED is terminal → removed
        assert "o7" not in self.portfolio._outstanding_orders

    def test_stale_fill_lower_qty_ignored(self):
        """Sending a lower filled_qty than what is already tracked is silently ignored."""
        o = _order("o8", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.07)
        self.portfolio._outstanding_orders["o8"] = o

        stale = _order("o8", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.03, avg_fill_price=BTC_PRICE)
        self.portfolio._update_order(stale)

        assert self.portfolio._outstanding_orders["o8"].filled_qty == pytest.approx(0.07)

    def test_terminal_order_not_readded_on_second_terminal_event(self):
        """A FILLED event for an already-removed order is ignored (not re-added)."""
        o = _order("o9", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.05)
        self.portfolio._outstanding_orders["o9"] = o

        # Fill completely
        fill1 = _order("o9", status=OrderStatus.FILLED, filled_qty=0.1, avg_fill_price=BTC_PRICE)
        self.portfolio._update_order(fill1)
        assert "o9" not in self.portfolio._outstanding_orders

        # Duplicate FILLED event — should not re-add since it's terminal
        fill2 = _order("o9", status=OrderStatus.FILLED, filled_qty=0.1, avg_fill_price=BTC_PRICE)
        self.portfolio._update_order(fill2)
        assert "o9" not in self.portfolio._outstanding_orders

    def test_fsm_transition_also_applies_in_shadow_book(self):
        """FSM transitions work identically in ShadowBook._update_order."""
        o = _order("s1", status=OrderStatus.OPEN, strategy_id=1)
        self.book1._outstanding_orders["s1"] = o

        # OPEN → PARTIALLY_FILLED
        update = _order("s1", status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.06, avg_fill_price=BTC_PRICE, strategy_id=1)
        self.book1._update_order(update)
        assert self.book1._outstanding_orders["s1"].status == OrderStatus.PARTIALLY_FILLED

        # PARTIALLY_FILLED → FILLED
        fill = _order("s1", status=OrderStatus.FILLED, filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        self.book1._update_order(fill)
        assert "s1" not in self.book1._outstanding_orders


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Strategy isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyIsolation:
    """Orders and fills are strictly isolated between strategies."""

    def setup_method(self):
        self.portfolio = _portfolio()
        self.book1 = _shadow_book(strategy_id=1, allocation=STRAT1_ALLOC)
        self.book2 = _shadow_book(strategy_id=2, allocation=STRAT2_ALLOC)

    def test_strategy2_fill_does_not_appear_in_strategy1_shadow_book(self):
        fill = _order("eth-2", symbol=ETHUSDT, status=OrderStatus.FILLED,
                      quantity=2.0, filled_qty=2.0, avg_fill_price=ETH_PRICE, strategy_id=2)
        run(_broadcast(_event(fill), self.portfolio, self.book1, self.book2))

        assert self.book1.position(ETHUSDT).quantity == pytest.approx(0.0)
        assert self.book2.position(ETHUSDT).quantity == pytest.approx(2.0)

    def test_strategy1_fill_does_not_appear_in_strategy2_shadow_book(self):
        fill = _order("btc-1", status=OrderStatus.FILLED,
                      filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(fill), self.portfolio, self.book1, self.book2))

        assert self.book2.position(BTCUSDT).quantity == pytest.approx(0.0)
        assert self.book1.position(BTCUSDT).quantity == pytest.approx(0.1)

    def test_cancelled_order_from_strategy1_does_not_affect_strategy2_positions(self):
        """Cancel of a strategy-1 order does not touch strategy-2 state."""
        o2_open = _order("eth-2", symbol=ETHUSDT, strategy_id=2)
        run(_broadcast(_event(o2_open), self.portfolio, self.book1, self.book2))

        cancel1 = _order("btc-1", status=OrderStatus.CANCELLED, strategy_id=1)
        run(_broadcast(_event(cancel1), self.portfolio, self.book1, self.book2))

        # eth-2 still outstanding in book2
        assert "eth-2" in self.book2.outstanding_orders

    def test_wrong_venue_completely_ignored_by_portfolio(self):
        """Orders on a different venue are not tracked by the portfolio."""
        o = _order("bybit-1", status=OrderStatus.OPEN)
        run(self.portfolio._on_order_update(_event(o, venue=Venue.BYBIT)))
        assert "bybit-1" not in self.portfolio.open_orders

    def test_multiple_strategies_cash_accounted_separately(self):
        """Each shadow book tracks its own cash — fills deduct only from the owning book."""
        book1_cash_before = self.book1.cash
        book2_cash_before = self.book2.cash

        fill = _order("btc-1", status=OrderStatus.FILLED,
                      filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(fill), self.portfolio, self.book1, self.book2))

        expected_cost = 0.1 * BTC_PRICE
        assert self.book1.cash == pytest.approx(book1_cash_before - expected_cost)
        assert self.book2.cash == pytest.approx(book2_cash_before)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Partial fill lifecycle — end-to-end OPEN → PARTIAL × N → FILLED
# ═══════════════════════════════════════════════════════════════════════════════

class TestPartialFillLifecycle:
    """
    Exercises the full order lifecycle with multiple partial fills,
    verifying outstanding orders, positions, and cash at every step.
    """

    ORDER_ID = "btc-lifecycle"
    TOTAL_QTY = 0.3
    FILLS = [
        (OrderStatus.PARTIALLY_FILLED, 0.1, BTC_PRICE),
        (OrderStatus.PARTIALLY_FILLED, 0.2, BTC_PRICE),
        (OrderStatus.FILLED,           0.3, BTC_PRICE),
    ]

    def setup_method(self):
        self.portfolio = _portfolio()
        self.book = _shadow_book(strategy_id=1, allocation=1.0)

    def test_outstanding_orders_track_lifecycle(self):
        """Order is tracked as outstanding throughout partial fills, then removed on FILLED."""
        # OPEN
        o_open = _order(self.ORDER_ID, quantity=self.TOTAL_QTY, strategy_id=1)
        run(_broadcast(_event(o_open), self.portfolio, self.book))
        assert self.ORDER_ID in self.portfolio.open_orders
        assert self.ORDER_ID in self.book.outstanding_orders

        # Partial fills
        for i, (status, filled_qty, price) in enumerate(self.FILLS):
            o = _order(self.ORDER_ID, status=status, quantity=self.TOTAL_QTY,
                       filled_qty=filled_qty, avg_fill_price=price, strategy_id=1)
            run(_broadcast(_event(o), self.portfolio, self.book))

            if status == OrderStatus.FILLED:
                assert self.ORDER_ID not in self.portfolio.open_orders
                assert self.ORDER_ID not in self.book.outstanding_orders
            else:
                assert self.ORDER_ID in self.portfolio.open_orders
                assert self.ORDER_ID in self.book.outstanding_orders

    def test_positions_accumulate_correctly_through_partial_fills(self):
        """Position quantity increases incrementally with each partial fill."""
        for status, filled_qty, price in self.FILLS:
            o = _order(self.ORDER_ID, status=status, quantity=self.TOTAL_QTY,
                       filled_qty=filled_qty, avg_fill_price=price, strategy_id=1)
            run(_broadcast(_event(o), self.portfolio, self.book))

        assert self.portfolio.position(BTCUSDT).quantity == pytest.approx(self.TOTAL_QTY)
        assert self.book.position(BTCUSDT).quantity == pytest.approx(self.TOTAL_QTY)

    def test_cash_reduced_by_total_fill_notional(self):
        """Cash is reduced by exactly TOTAL_QTY * PRICE after all fills are processed."""
        portfolio_cash_before = self.portfolio.cash
        book_cash_before = self.book.cash
        expected_cost = self.TOTAL_QTY * BTC_PRICE

        for status, filled_qty, price in self.FILLS:
            o = _order(self.ORDER_ID, status=status, quantity=self.TOTAL_QTY,
                       filled_qty=filled_qty, avg_fill_price=price, strategy_id=1)
            run(_broadcast(_event(o), self.portfolio, self.book))

        assert self.portfolio.cash == pytest.approx(portfolio_cash_before - expected_cost)
        assert self.book.cash == pytest.approx(book_cash_before - expected_cost)

    def test_filled_qty_in_tracked_order_matches_cumulative(self):
        """The tracked order's filled_qty reflects the last cumulative fill."""
        # Prime outstanding dicts with OPEN
        o_open = _order(self.ORDER_ID, quantity=self.TOTAL_QTY, strategy_id=1)
        run(_broadcast(_event(o_open), self.portfolio, self.book))

        # First partial
        o_p1 = _order(self.ORDER_ID, status=OrderStatus.PARTIALLY_FILLED, quantity=self.TOTAL_QTY,
                      filled_qty=0.1, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(o_p1), self.portfolio, self.book))
        assert self.portfolio._outstanding_orders[self.ORDER_ID].filled_qty == pytest.approx(0.1)
        assert self.book._outstanding_orders[self.ORDER_ID].filled_qty == pytest.approx(0.1)

        # Second partial
        o_p2 = _order(self.ORDER_ID, status=OrderStatus.PARTIALLY_FILLED, quantity=self.TOTAL_QTY,
                      filled_qty=0.2, avg_fill_price=BTC_PRICE, strategy_id=1)
        run(_broadcast(_event(o_p2), self.portfolio, self.book))
        assert self.portfolio._outstanding_orders[self.ORDER_ID].filled_qty == pytest.approx(0.2)
        assert self.book._outstanding_orders[self.ORDER_ID].filled_qty == pytest.approx(0.2)
