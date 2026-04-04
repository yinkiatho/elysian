"""Tests for Position class: PnL calculations, quantity tracking, state checks."""

import pytest
from elysian_core.core.position import Position
from elysian_core.core.enums import Venue


def _pos(**kw) -> Position:
    defaults = dict(symbol="BTCUSDT", venue=Venue.BINANCE)
    defaults.update(kw)
    return Position(**defaults)


class TestPositionState:

    def test_is_flat_default(self):
        assert _pos().is_flat() is True

    def test_is_long(self):
        assert _pos(quantity=1.0).is_long() is True
        assert _pos(quantity=1.0).is_short() is False
        assert _pos(quantity=1.0).is_flat() is False

    def test_is_short(self):
        assert _pos(quantity=-1.0).is_short() is True
        assert _pos(quantity=-1.0).is_long() is False

    def test_is_flat_zero(self):
        assert _pos(quantity=0.0).is_flat() is True


class TestFreeQuantity:

    def test_free_quantity_no_locked(self):
        p = _pos(quantity=5.0, locked_quantity=0.0)
        assert p.free_quantity == pytest.approx(5.0)

    def test_free_quantity_with_locked(self):
        p = _pos(quantity=5.0, locked_quantity=2.0)
        assert p.free_quantity == pytest.approx(3.0)

    def test_free_quantity_fully_locked(self):
        p = _pos(quantity=5.0, locked_quantity=5.0)
        assert p.free_quantity == pytest.approx(0.0)

    def test_free_quantity_over_locked_clamps_to_zero(self):
        p = _pos(quantity=2.0, locked_quantity=10.0)
        assert p.free_quantity == pytest.approx(0.0)


class TestPnl:

    def test_unrealized_pnl_long(self):
        p = _pos(quantity=2.0, avg_entry_price=50000.0)
        assert p.unrealized_pnl(52000.0) == pytest.approx(4000.0)

    def test_unrealized_pnl_short(self):
        p = _pos(quantity=-1.0, avg_entry_price=50000.0)
        assert p.unrealized_pnl(48000.0) == pytest.approx(2000.0)

    def test_unrealized_pnl_flat(self):
        p = _pos(quantity=0.0, avg_entry_price=50000.0)
        assert p.unrealized_pnl(60000.0) == pytest.approx(0.0)

    def test_total_pnl(self):
        p = _pos(quantity=1.0, avg_entry_price=50000.0, realized_pnl=1000.0)
        assert p.total_pnl(51000.0) == pytest.approx(2000.0)

    def test_net_pnl(self):
        p = _pos(quantity=1.0, avg_entry_price=50000.0, realized_pnl=1000.0, total_commission=200.0)
        # total_pnl = 1000 + 1000 = 2000, net = 2000 - 200 = 1800
        assert p.net_pnl(51000.0) == pytest.approx(1800.0)


class TestNotional:

    def test_notional_long(self):
        p = _pos(quantity=2.0)
        assert p.notional(50000.0) == pytest.approx(100000.0)

    def test_notional_short(self):
        p = _pos(quantity=-1.5)
        assert p.notional(50000.0) == pytest.approx(75000.0)

    def test_notional_flat(self):
        p = _pos(quantity=0.0)
        assert p.notional(50000.0) == pytest.approx(0.0)


class TestPositionStr:

    def test_long_str(self):
        s = str(_pos(quantity=1.0, avg_entry_price=50000.0))
        assert "LONG" in s
        assert "BTCUSDT" in s

    def test_short_str(self):
        s = str(_pos(quantity=-1.0))
        assert "SHORT" in s

    def test_flat_str(self):
        s = str(_pos(quantity=0.0))
        assert "FLAT" in s
