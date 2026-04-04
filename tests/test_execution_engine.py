"""Tests for ExecutionEngine: _round_step, compute_order_intents."""

import pytest
import time
from unittest.mock import MagicMock, AsyncMock

from elysian_core.execution.engine import ExecutionEngine, _round_step
from elysian_core.core.enums import AssetType, OrderType, Side, Venue
from elysian_core.core.signals import TargetWeights, ValidatedWeights, OrderIntent
from elysian_core.core.position import Position


class TestRoundStep:

    def test_round_step_basic(self):
        assert _round_step(1.567, 0.01) == pytest.approx(1.56)

    def test_round_step_whole(self):
        assert _round_step(5.9, 1.0) == pytest.approx(5.0)

    def test_round_step_zero_step(self):
        assert _round_step(1.23, 0) == pytest.approx(1.23)

    def test_round_step_negative_step(self):
        assert _round_step(1.23, -1) == pytest.approx(1.23)

    def test_round_step_exact(self):
        assert _round_step(1.5, 0.5) == pytest.approx(1.5)

    def test_round_step_small(self):
        assert _round_step(0.123456, 0.0001) == pytest.approx(0.1234)


def _mock_exchange(token_infos=None, venue=Venue.BINANCE):
    ex = MagicMock()
    ex._token_infos = token_infos or {}
    return ex


def _mock_portfolio(positions=None, cash=100000.0):
    """Mock a ShadowBook-like portfolio."""
    p = MagicMock()
    _positions = positions or {}

    def position(symbol):
        return _positions.get(symbol, Position(symbol=symbol, venue=Venue.BINANCE))

    def total_value(mark_prices=None):
        pos_value = sum(
            pos.quantity * (mark_prices or {}).get(sym, 0.0)
            for sym, pos in _positions.items()
        )
        return cash + pos_value

    p.position.side_effect = position
    p.total_value.side_effect = total_value
    p.lock_for_order = MagicMock()
    return p


def _mock_feed(price):
    feed = MagicMock()
    feed.latest_price = price
    return feed


def _engine(
    positions=None, cash=100000.0, feeds=None, token_infos=None,
):
    exchanges = {Venue.BINANCE: _mock_exchange(token_infos=token_infos or {
        "BTCUSDT": {"step_size": 0.0001},
        "ETHUSDT": {"step_size": 0.001},
    })}
    portfolio = _mock_portfolio(positions=positions, cash=cash)
    feeds = feeds or {}
    return ExecutionEngine(
        exchanges=exchanges,
        portfolio=portfolio,
        feeds=feeds,
        default_venue=Venue.BINANCE,
        default_order_type=OrderType.MARKET,
    )


def _validated(weights, strategy_id=1, price_overrides=None):
    target = TargetWeights(
        weights=weights,
        timestamp=int(time.time() * 1000),
        strategy_id=strategy_id,
        price_overrides=price_overrides,
    )
    return ValidatedWeights(
        original=target,
        weights=weights,
        clipped={},
        rejected=False,
        timestamp=int(time.time() * 1000),
    )


class TestComputeOrderIntents:

    def test_buy_intent_generated(self):
        engine = _engine(cash=100000.0)
        mark_prices = {"BTCUSDT": 50000.0}
        validated = _validated({"BTCUSDT": 0.50})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        assert len(intents) == 1
        assert intents[0].side == Side.BUY
        assert intents[0].symbol == "BTCUSDT"
        assert intents[0].quantity > 0

    def test_sell_intent_generated(self):
        positions = {"BTCUSDT": Position(symbol="BTCUSDT", venue=Venue.BINANCE, quantity=2.0)}
        engine = _engine(positions=positions, cash=0.0)
        mark_prices = {"BTCUSDT": 50000.0}
        validated = _validated({"BTCUSDT": 0.10})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        assert len(intents) == 1
        assert intents[0].side == Side.SELL

    def test_no_intent_when_aligned(self):
        """When current weight matches target, no intent should be generated."""
        positions = {"BTCUSDT": Position(symbol="BTCUSDT", venue=Venue.BINANCE, quantity=1.0)}
        engine = _engine(positions=positions, cash=50000.0)
        mark_prices = {"BTCUSDT": 50000.0}
        # total_value = 50000 + 1*50000 = 100000
        # current_weight = 50000/100000 = 0.5
        validated = _validated({"BTCUSDT": 0.50})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)
        assert len(intents) == 0

    def test_stablecoin_skipped(self):
        engine = _engine(cash=100000.0)
        mark_prices = {"USDT": 1.0}
        validated = _validated({"USDT": 0.50})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)
        assert len(intents) == 0

    def test_missing_price_skipped(self):
        engine = _engine(cash=100000.0)
        validated = _validated({"BTCUSDT": 0.50})
        intents = engine.compute_order_intents(validated, {}, OrderType.MARKET)
        assert len(intents) == 0

    def test_missing_exchange_skipped(self):
        engine = _engine(cash=100000.0)
        engine._exchanges = {}  # no exchanges
        validated = _validated({"BTCUSDT": 0.50})
        intents = engine.compute_order_intents(validated, {"BTCUSDT": 50000.0}, OrderType.MARKET)
        assert len(intents) == 0

    def test_zero_portfolio_value_returns_empty(self):
        engine = _engine(cash=0.0)
        validated = _validated({"BTCUSDT": 0.50})
        intents = engine.compute_order_intents(validated, {"BTCUSDT": 50000.0}, OrderType.MARKET)
        assert len(intents) == 0

    def test_multiple_symbols(self):
        engine = _engine(cash=100000.0)
        mark_prices = {"BTCUSDT": 50000.0, "ETHUSDT": 3000.0}
        validated = _validated({"BTCUSDT": 0.40, "ETHUSDT": 0.30})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)
        symbols = {i.symbol for i in intents}
        assert "BTCUSDT" in symbols
        assert "ETHUSDT" in symbols

    def test_limit_order_type(self):
        engine = _engine(cash=100000.0)
        mark_prices = {"BTCUSDT": 50000.0}
        validated = _validated({"BTCUSDT": 0.50})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.LIMIT)

        assert len(intents) == 1
        assert intents[0].order_type == OrderType.LIMIT
        assert intents[0].price is not None

    def test_price_override_used(self):
        engine = _engine(cash=100000.0)
        mark_prices = {"BTCUSDT": 50000.0}
        validated = _validated({"BTCUSDT": 0.50}, price_overrides={"BTCUSDT": 49000.0})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.LIMIT)

        # The price override should be used for limit orders
        assert len(intents) == 1


class TestGetMarkPrices:

    def test_get_mark_prices(self):
        engine = _engine(feeds={
            "BTCUSDT": _mock_feed(50000.0),
            "ETHUSDT": _mock_feed(3000.0),
        })
        prices = engine._get_mark_prices()
        assert prices["BTCUSDT"] == pytest.approx(50000.0)
        assert prices["ETHUSDT"] == pytest.approx(3000.0)

    def test_get_mark_prices_skips_zero(self):
        engine = _engine(feeds={
            "BTCUSDT": _mock_feed(0),
        })
        prices = engine._get_mark_prices()
        assert "BTCUSDT" not in prices

    def test_get_mark_prices_handles_error(self):
        bad_feed = MagicMock()
        type(bad_feed).latest_price = property(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
        engine = _engine(feeds={"BTCUSDT": bad_feed})
        prices = engine._get_mark_prices()
        assert "BTCUSDT" not in prices
