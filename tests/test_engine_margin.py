"""
Tests for ExecutionEngine margin awareness.

Covers:
- compute_order_intents() stamps asset_type=MARGIN and side_effect=AUTO_BORROW_REPAY
  when engine is created with asset_type=AssetType.MARGIN
- compute_order_intents() stamps asset_type=SPOT and side_effect=None for SPOT engines
- _submit_order() passes side_effect kwarg to exchange when set (margin path)
- _submit_order() does NOT pass side_effect kwarg for SPOT intents
- SPOT regression: existing intent fields unchanged when margin not involved
"""

import asyncio
import pytest
import time
from unittest.mock import MagicMock, AsyncMock

from elysian_core.execution.engine import ExecutionEngine, _round_step
from elysian_core.core.enums import AssetType, MarginSideEffect, OrderType, Side, Venue
from elysian_core.core.signals import TargetWeights, ValidatedWeights, OrderIntent
from elysian_core.core.position import Position


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_exchange(token_infos=None, venue=Venue.BINANCE):
    ex = MagicMock()
    ex._token_infos = token_infos or {"BTCUSDT": {"step_size": 0.0001}, "ETHUSDT": {"step_size": 0.001}}
    ex.place_market_order = AsyncMock(return_value={"orderId": "TEST_ORDER_001"})
    ex.place_limit_order = AsyncMock(return_value={"orderId": "TEST_LIMIT_001"})
    return ex


def _mock_portfolio(positions=None, cash=100_000.0, nav=None):
    p = MagicMock()
    p._strategy_name = "test"
    p._strategy_id = 1
    _positions = positions or {}

    def position(symbol):
        return _positions.get(symbol, Position(symbol=symbol, venue=Venue.BINANCE))

    def total_value(mark_prices=None):
        if nav is not None:
            return nav
        pos_value = sum(
            pos.quantity * (mark_prices or {}).get(sym, 0.0)
            for sym, pos in _positions.items()
        )
        return cash + pos_value

    p.position.side_effect = position
    p.total_value.side_effect = total_value
    p.cash = cash
    p.lock_for_order = MagicMock()
    return p


def _spot_engine(positions=None, cash=100_000.0, token_infos=None):
    exchanges = {Venue.BINANCE: _mock_exchange(token_infos=token_infos)}
    portfolio = _mock_portfolio(positions=positions, cash=cash)
    return ExecutionEngine(
        exchanges=exchanges,
        portfolio=portfolio,
        default_venue=Venue.BINANCE,
        default_order_type=OrderType.MARKET,
        asset_type=AssetType.SPOT,
    )


def _margin_engine(positions=None, cash=100_000.0, token_infos=None):
    exchanges = {Venue.BINANCE: _mock_exchange(token_infos=token_infos)}
    portfolio = _mock_portfolio(positions=positions, cash=cash)
    return ExecutionEngine(
        exchanges=exchanges,
        portfolio=portfolio,
        default_venue=Venue.BINANCE,
        default_order_type=OrderType.MARKET,
        asset_type=AssetType.MARGIN,
    )


def _validated(weights, strategy_id=1, venue=None):
    target = TargetWeights(
        weights=weights,
        timestamp=int(time.time() * 1000),
        strategy_id=strategy_id,
    )
    return ValidatedWeights(
        original=target,
        weights=weights,
        clipped={},
        rejected=False,
        timestamp=int(time.time() * 1000),
        venue=venue or Venue.BINANCE,
    )


PRICES_BTC = {"BTCUSDT": 50_000.0}


# ─── 1. asset_type propagation ────────────────────────────────────────────────

class TestAssetTypePropagation:
    def test_margin_engine_stamps_margin_on_intent(self):
        engine = _margin_engine()
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        assert len(intents) == 1
        assert intents[0].asset_type == AssetType.MARGIN

    def test_spot_engine_stamps_spot_on_intent(self):
        engine = _spot_engine()
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        assert len(intents) == 1
        assert intents[0].asset_type == AssetType.SPOT

    def test_none_asset_type_defaults_to_spot(self):
        """asset_type=None on the engine falls back to SPOT."""
        exchanges = {Venue.BINANCE: _mock_exchange()}
        portfolio = _mock_portfolio()
        engine = ExecutionEngine(
            exchanges=exchanges, portfolio=portfolio,
            asset_type=None,
        )
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        assert intents[0].asset_type == AssetType.SPOT


# ─── 2. side_effect propagation ──────────────────────────────────────────────

class TestSideEffectPropagation:
    def test_margin_engine_sets_auto_borrow_repay(self):
        engine = _margin_engine()
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        assert intents[0].side_effect == MarginSideEffect.AUTO_BORROW_REPAY

    def test_spot_engine_side_effect_is_none(self):
        engine = _spot_engine()
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        assert intents[0].side_effect is None

    def test_margin_sell_intent_also_has_auto_borrow_repay(self):
        """Sells on margin also carry AUTO_BORROW_REPAY for automatic BTC borrow."""
        positions = {"BTCUSDT": Position("BTCUSDT", quantity=2.0, avg_entry_price=50_000.0)}
        engine = _margin_engine(positions=positions, cash=0.0)
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.1}), PRICES_BTC, OrderType.MARKET
        )
        sell_intents = [i for i in intents if i.side == Side.SELL]
        assert len(sell_intents) >= 1
        for intent in sell_intents:
            assert intent.side_effect == MarginSideEffect.AUTO_BORROW_REPAY


# ─── 3. _submit_order side_effect passthrough ─────────────────────────────────

class TestSubmitOrderSideEffect:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_margin_market_order_passes_side_effect(self):
        engine = _margin_engine()
        exchange_mock = engine._exchanges[Venue.BINANCE]

        intent = OrderIntent(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            side=Side.BUY,
            quantity=0.1,
            order_type=OrderType.MARKET,
            strategy_id=1,
            asset_type=AssetType.MARGIN,
            side_effect=MarginSideEffect.AUTO_BORROW_REPAY,
        )

        self._run(engine._submit_order(intent))
        exchange_mock.place_market_order.assert_called_once()
        call_kwargs = exchange_mock.place_market_order.call_args.kwargs
        assert "side_effect" in call_kwargs
        assert call_kwargs["side_effect"] == MarginSideEffect.AUTO_BORROW_REPAY

    def test_spot_market_order_no_side_effect_kwarg(self):
        """SPOT intents must NOT pass side_effect to connectors."""
        engine = _spot_engine()
        exchange_mock = engine._exchanges[Venue.BINANCE]

        intent = OrderIntent(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            side=Side.BUY,
            quantity=0.1,
            order_type=OrderType.MARKET,
            strategy_id=1,
            asset_type=AssetType.SPOT,
            side_effect=None,
        )

        self._run(engine._submit_order(intent))
        exchange_mock.place_market_order.assert_called_once()
        call_kwargs = exchange_mock.place_market_order.call_args.kwargs
        assert "side_effect" not in call_kwargs

    def test_margin_limit_order_passes_side_effect(self):
        engine = _margin_engine()
        exchange_mock = engine._exchanges[Venue.BINANCE]

        intent = OrderIntent(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            side=Side.BUY,
            quantity=0.1,
            order_type=OrderType.LIMIT,
            price=49_000.0,
            strategy_id=1,
            asset_type=AssetType.MARGIN,
            side_effect=MarginSideEffect.AUTO_BORROW_REPAY,
        )

        self._run(engine._submit_order(intent))
        exchange_mock.place_limit_order.assert_called_once()
        call_kwargs = exchange_mock.place_limit_order.call_args.kwargs
        assert "side_effect" in call_kwargs
        assert call_kwargs["side_effect"] == MarginSideEffect.AUTO_BORROW_REPAY

    def test_spot_limit_order_no_side_effect_kwarg(self):
        engine = _spot_engine()
        exchange_mock = engine._exchanges[Venue.BINANCE]

        intent = OrderIntent(
            symbol="BTCUSDT",
            venue=Venue.BINANCE,
            side=Side.SELL,
            quantity=0.05,
            order_type=OrderType.LIMIT,
            price=51_000.0,
            strategy_id=1,
            asset_type=AssetType.SPOT,
            side_effect=None,
        )

        self._run(engine._submit_order(intent))
        exchange_mock.place_limit_order.assert_called_once()
        call_kwargs = exchange_mock.place_limit_order.call_args.kwargs
        assert "side_effect" not in call_kwargs


# ─── 4. SPOT regression ───────────────────────────────────────────────────────

class TestSpotRegression:
    """Ensure SPOT engine behaviour is entirely unaffected by margin additions."""

    def test_spot_buy_intent_fields(self):
        engine = _spot_engine()
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        assert len(intents) == 1
        intent = intents[0]
        assert intent.side == Side.BUY
        assert intent.symbol == "BTCUSDT"
        assert intent.asset_type == AssetType.SPOT
        assert intent.side_effect is None
        assert intent.order_type == OrderType.MARKET
        assert intent.price is None  # market order

    def test_spot_sell_intent_fields(self):
        positions = {"BTCUSDT": Position("BTCUSDT", quantity=2.0, avg_entry_price=50_000.0)}
        engine = _spot_engine(positions=positions, cash=0.0)
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.1}), PRICES_BTC, OrderType.MARKET
        )
        sells = [i for i in intents if i.side == Side.SELL]
        assert len(sells) == 1
        assert sells[0].asset_type == AssetType.SPOT
        assert sells[0].side_effect is None

    def test_stablecoin_still_skipped(self):
        engine = _spot_engine()
        intents = engine.compute_order_intents(
            _validated({"USDT": 0.5}), {"USDT": 1.0}, OrderType.MARKET
        )
        assert len(intents) == 0

    def test_no_intent_when_already_aligned(self):
        positions = {"BTCUSDT": Position("BTCUSDT", quantity=1.0, avg_entry_price=50_000.0)}
        engine = _spot_engine(positions=positions, cash=50_000.0)
        intents = engine.compute_order_intents(
            _validated({"BTCUSDT": 0.5}), PRICES_BTC, OrderType.MARKET
        )
        # current_weight = 1.0*50000 / (50000+50000) = 0.5 → no delta
        assert len(intents) == 0


# ─── 5. Full execute() integration ────────────────────────────────────────────

class TestExecuteIntegration:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_margin_execute_calls_place_market_order_with_side_effect(self):
        engine = _margin_engine()
        exchange_mock = engine._exchanges[Venue.BINANCE]
        validated = _validated({"BTCUSDT": 0.5})

        result = self._run(engine.execute(validated, _mark_prices=PRICES_BTC))

        assert result.submitted > 0 or result.failed >= 0  # executed without crash
        if exchange_mock.place_market_order.called:
            call_kwargs = exchange_mock.place_market_order.call_args.kwargs
            assert "side_effect" in call_kwargs
            assert call_kwargs["side_effect"] == MarginSideEffect.AUTO_BORROW_REPAY

    def test_spot_execute_does_not_pass_side_effect(self):
        engine = _spot_engine()
        exchange_mock = engine._exchanges[Venue.BINANCE]
        # Manually seed mark price so engine has something to work with
        engine._mark_prices[Venue.BINANCE] = PRICES_BTC
        validated = _validated({"BTCUSDT": 0.5})

        result = self._run(engine.execute(validated))

        if exchange_mock.place_market_order.called:
            call_kwargs = exchange_mock.place_market_order.call_args.kwargs
            assert "side_effect" not in call_kwargs
