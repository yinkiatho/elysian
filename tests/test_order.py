"""Tests for Order, ActiveOrder, and ActiveLimitOrder classes."""

import pytest
from elysian_core.core.enums import Side, OrderType, OrderStatus, Venue
from elysian_core.core.order import Order, ActiveOrder, ActiveLimitOrder


def _order(**overrides) -> Order:
    defaults = dict(
        id="o1", symbol="BTCUSDT", side=Side.BUY, order_type=OrderType.MARKET,
        quantity=1.0, price=50000.0, status=OrderStatus.OPEN, venue=Venue.BINANCE,
    )
    defaults.update(overrides)
    return Order(**defaults)


class TestOrderProperties:

    def test_remaining_qty(self):
        o = _order(quantity=1.0, filled_qty=0.3)
        assert o.remaining_qty == pytest.approx(0.7)

    def test_remaining_qty_fully_filled(self):
        o = _order(quantity=1.0, filled_qty=1.0)
        assert o.remaining_qty == pytest.approx(0.0)

    def test_remaining_qty_never_negative(self):
        o = _order(quantity=1.0, filled_qty=1.5)
        assert o.remaining_qty == pytest.approx(0.0)

    def test_is_filled_true(self):
        o = _order(status=OrderStatus.FILLED)
        assert o.is_filled is True

    def test_is_filled_false(self):
        o = _order(status=OrderStatus.OPEN)
        assert o.is_filled is False

    def test_is_active(self):
        assert _order(status=OrderStatus.OPEN).is_active is True
        assert _order(status=OrderStatus.PENDING).is_active is True
        assert _order(status=OrderStatus.PARTIALLY_FILLED).is_active is True
        assert _order(status=OrderStatus.FILLED).is_active is False
        assert _order(status=OrderStatus.CANCELLED).is_active is False

    def test_is_terminal(self):
        assert _order(status=OrderStatus.FILLED).is_terminal is True
        assert _order(status=OrderStatus.CANCELLED).is_terminal is True
        assert _order(status=OrderStatus.REJECTED).is_terminal is True
        assert _order(status=OrderStatus.EXPIRED).is_terminal is True
        assert _order(status=OrderStatus.OPEN).is_terminal is False


class TestOrderTransitionTo:

    def test_valid_transition(self):
        o = _order(status=OrderStatus.OPEN)
        o.transition_to(OrderStatus.FILLED)
        assert o.status == OrderStatus.FILLED

    def test_invalid_transition_still_applies(self):
        o = _order(status=OrderStatus.FILLED)
        o.transition_to(OrderStatus.OPEN)  # invalid but warn-mode
        assert o.status == OrderStatus.OPEN


class TestOrderUpdateFill:

    def test_update_fill_basic(self):
        o = _order(quantity=1.0, filled_qty=0.0, avg_fill_price=0.0, status=OrderStatus.OPEN)
        o.update_fill(0.5, 50000.0)
        assert o.filled_qty == pytest.approx(0.5)
        assert o.avg_fill_price == pytest.approx(50000.0)
        assert o.status == OrderStatus.PARTIALLY_FILLED

    def test_update_fill_to_full(self):
        o = _order(quantity=1.0, filled_qty=0.0, avg_fill_price=0.0, status=OrderStatus.OPEN)
        o.update_fill(1.0, 50000.0)
        assert o.status == OrderStatus.FILLED

    def test_update_fill_weighted_average(self):
        o = _order(quantity=1.0, filled_qty=0.0, avg_fill_price=0.0, status=OrderStatus.OPEN)
        o.update_fill(0.5, 50000.0)
        o.update_fill(1.0, 52000.0)
        # avg = (0.5*50000 + 0.5*52000) / 1.0 = 51000
        assert o.avg_fill_price == pytest.approx(51000.0)

    def test_update_fill_stale_ignored(self):
        o = _order(quantity=1.0, filled_qty=0.5, avg_fill_price=50000.0, status=OrderStatus.PARTIALLY_FILLED)
        o.update_fill(0.3, 48000.0)  # stale: 0.3 < 0.5
        assert o.filled_qty == pytest.approx(0.5)
        assert o.avg_fill_price == pytest.approx(50000.0)

    def test_str_representation(self):
        o = _order()
        s = str(o)
        assert "Order o1" in s
        assert "BTCUSDT" in s


class TestActiveOrder:

    def _active_order(self, **kw) -> ActiveOrder:
        defaults = dict(
            id="a1", symbol="BTCUSDT", side=Side.BUY, order_type=OrderType.MARKET,
            quantity=1.0, price=50000.0, status=OrderStatus.OPEN, venue=Venue.BINANCE,
        )
        defaults.update(kw)
        return ActiveOrder(**defaults)

    def test_sync_from_event_returns_delta(self):
        ao = self._active_order()
        event_order = _order(filled_qty=0.3, avg_fill_price=50000.0, status=OrderStatus.PARTIALLY_FILLED)
        delta = ao.sync_from_event(event_order)
        assert delta == pytest.approx(0.3)
        assert ao._prev_filled == pytest.approx(0.3)
        assert ao.filled_qty == pytest.approx(0.3)

    def test_sync_from_event_incremental(self):
        ao = self._active_order()
        e1 = _order(filled_qty=0.3, avg_fill_price=50000.0, status=OrderStatus.PARTIALLY_FILLED)
        ao.sync_from_event(e1)

        e2 = _order(filled_qty=0.7, avg_fill_price=51000.0, status=OrderStatus.PARTIALLY_FILLED)
        delta2 = ao.sync_from_event(e2)
        assert delta2 == pytest.approx(0.4)
        assert ao._prev_filled == pytest.approx(0.7)

    def test_sync_from_event_no_fill_delta(self):
        ao = self._active_order(_prev_filled=0.5, filled_qty=0.5,
                                avg_fill_price=50000.0, status=OrderStatus.PARTIALLY_FILLED)
        e = _order(filled_qty=0.5, avg_fill_price=50000.0, status=OrderStatus.PARTIALLY_FILLED)
        delta = ao.sync_from_event(e)
        assert delta == pytest.approx(0.0)

    def test_sync_from_event_status_change_without_fill(self):
        ao = self._active_order(status=OrderStatus.OPEN)
        e = _order(filled_qty=0.0, avg_fill_price=0.0, status=OrderStatus.CANCELLED)
        delta = ao.sync_from_event(e)
        assert delta == pytest.approx(0.0)
        assert ao.status == OrderStatus.CANCELLED


class TestActiveLimitOrder:

    def _limit_order(self, **kw) -> ActiveLimitOrder:
        defaults = dict(
            id="l1", symbol="BTCUSDT", side=Side.BUY, order_type=OrderType.LIMIT,
            quantity=1.0, price=50000.0, status=OrderStatus.OPEN, venue=Venue.BINANCE,
        )
        defaults.update(kw)
        return ActiveLimitOrder(**defaults)

    def test_initialize_reservation_buy(self):
        lo = self._limit_order(side=Side.BUY, quantity=2.0, price=50000.0)
        lo.initialize_reservation()
        assert lo.reserved_cash == pytest.approx(100000.0)
        assert lo.reserved_qty == pytest.approx(0.0)

    def test_initialize_reservation_sell(self):
        lo = self._limit_order(side=Side.SELL, quantity=2.0)
        lo.initialize_reservation()
        assert lo.reserved_qty == pytest.approx(2.0)
        assert lo.reserved_cash == pytest.approx(0.0)

    def test_release_partial_buy(self):
        lo = self._limit_order(side=Side.BUY, quantity=2.0, price=50000.0)
        lo.initialize_reservation()
        cash_rel, qty_rel = lo.release_partial(0.5)
        assert cash_rel == pytest.approx(25000.0)
        assert qty_rel == pytest.approx(0.0)
        assert lo.reserved_cash == pytest.approx(75000.0)

    def test_release_partial_sell(self):
        lo = self._limit_order(side=Side.SELL, quantity=2.0)
        lo.initialize_reservation()
        cash_rel, qty_rel = lo.release_partial(0.8)
        assert cash_rel == pytest.approx(0.0)
        assert qty_rel == pytest.approx(0.8)
        assert lo.reserved_qty == pytest.approx(1.2)

    def test_release_all(self):
        lo = self._limit_order(side=Side.BUY, quantity=1.0, price=50000.0)
        lo.initialize_reservation()
        cash, qty = lo.release_all()
        assert cash == pytest.approx(50000.0)
        assert lo.reserved_cash == pytest.approx(0.0)
        assert lo.reserved_qty == pytest.approx(0.0)

    def test_release_partial_capped_at_reserved(self):
        lo = self._limit_order(side=Side.SELL, quantity=1.0)
        lo.initialize_reservation()
        _, qty_rel = lo.release_partial(5.0)  # more than reserved
        assert qty_rel == pytest.approx(1.0)
        assert lo.reserved_qty == pytest.approx(0.0)
