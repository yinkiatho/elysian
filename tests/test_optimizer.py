"""Tests for PortfolioOptimizer: clipping, scaling, turnover capping."""

import pytest
import time
from unittest.mock import MagicMock

from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.risk.risk_config import RiskConfig
from elysian_core.core.signals import TargetWeights, ValidatedWeights
from elysian_core.core.enums import Venue


def _mock_portfolio(current_weights=None, venue=Venue.BINANCE):
    """Mock a ShadowBook-like portfolio for the optimizer."""
    p = MagicMock()
    p._venue = venue
    p.current_weights.return_value = current_weights or {}
    return p


def _optimizer(risk_config=None, current_weights=None, **kw):
    cfg = risk_config or RiskConfig(**kw)
    portfolio = _mock_portfolio(current_weights=current_weights)
    return PortfolioOptimizer(risk_config=cfg, portfolio=portfolio)


def _target(weights, strategy_id=1):
    return TargetWeights(
        weights=weights,
        timestamp=int(time.time() * 1000),
        strategy_id=strategy_id,
    )


class TestClipPerAsset:

    def test_no_clipping_within_bounds(self):
        opt = _optimizer(max_weight_per_asset=0.25)
        out, clips = opt._clip_per_asset({"BTC": 0.20, "ETH": 0.15})
        assert out["BTC"] == pytest.approx(0.20)
        assert out["ETH"] == pytest.approx(0.15)
        assert clips == {}

    def test_clips_above_max(self):
        opt = _optimizer(max_weight_per_asset=0.25)
        out, clips = opt._clip_per_asset({"BTC": 0.40, "ETH": 0.15})
        assert out["BTC"] == pytest.approx(0.25)
        assert "BTC" in clips
        assert clips["BTC"] == pytest.approx(0.15)  # 0.40 - 0.25

    def test_clips_below_min(self):
        opt = _optimizer(max_weight_per_asset=1.0, min_weight_per_asset=0.05)
        out, clips = opt._clip_per_asset({"BTC": 0.01})
        assert out["BTC"] == pytest.approx(0.05)

    def test_short_weight_floor(self):
        opt = _optimizer(max_short_weight=0.10, max_weight_per_asset=1.0)
        out, clips = opt._clip_per_asset({"BTC": -0.20})
        assert out["BTC"] == pytest.approx(-0.10)


class TestScaleExposure:

    def test_within_limit_no_scaling(self):
        opt = _optimizer(max_total_exposure=1.0)
        result = opt._scale_exposure({"BTC": 0.4, "ETH": 0.3})
        assert result["BTC"] == pytest.approx(0.4)
        assert result["ETH"] == pytest.approx(0.3)

    def test_exceeds_limit_scaled_down(self):
        opt = _optimizer(max_total_exposure=1.0)
        result = opt._scale_exposure({"BTC": 0.8, "ETH": 0.6})
        # gross = 1.4, scale = 1.0/1.4
        assert abs(result["BTC"]) + abs(result["ETH"]) == pytest.approx(1.0)

    def test_empty_weights(self):
        opt = _optimizer(max_total_exposure=1.0)
        result = opt._scale_exposure({})
        assert result == {}


class TestEnforceCashFloor:

    def test_within_cash_floor(self):
        opt = _optimizer(min_cash_weight=0.10)
        result = opt._enforce_cash_floor({"BTC": 0.5, "ETH": 0.3})
        assert result["BTC"] == pytest.approx(0.5)

    def test_exceeds_cash_floor(self):
        opt = _optimizer(min_cash_weight=0.10)
        result = opt._enforce_cash_floor({"BTC": 0.6, "ETH": 0.5})
        total_long = sum(w for w in result.values() if w > 0)
        assert total_long <= 0.90 + 1e-9


class TestCapTurnover:

    def test_within_turnover_limit(self):
        opt = _optimizer(max_turnover_per_rebalance=0.5)
        result = opt._cap_turnover(
            {"BTC": 0.30, "ETH": 0.20},
            original={"BTC": 0.25, "ETH": 0.15},
        )
        # turnover = 0.05+0.05 = 0.1, well within 0.5
        assert result["BTC"] == pytest.approx(0.30)
        assert result["ETH"] == pytest.approx(0.20)

    def test_exceeds_turnover_scaled(self):
        opt = _optimizer(max_turnover_per_rebalance=0.10)
        result = opt._cap_turnover(
            {"BTC": 0.50},
            original={"BTC": 0.0},
        )
        # turnover = 0.5, cap = 0.1, scale = 0.2
        # result = 0.0 + (0.5 - 0.0) * 0.2 = 0.1
        assert result["BTC"] == pytest.approx(0.10)

    def test_turnover_with_new_and_removed_symbols(self):
        opt = _optimizer(max_turnover_per_rebalance=0.10)
        result = opt._cap_turnover(
            {"ETH": 0.50},
            original={"BTC": 0.50},
        )
        # turnover = |0-0.5| + |0.5-0| = 1.0, cap = 0.1, scale = 0.1
        assert result["ETH"] == pytest.approx(0.05)
        assert result["BTC"] == pytest.approx(0.45)


class TestValidateIntegration:

    def test_validate_returns_validated_weights(self):
        opt = _optimizer(
            max_weight_per_asset=0.25,
            max_total_exposure=1.0,
            max_turnover_per_rebalance=1.0,
            current_weights={},
        )
        target = _target({"BTC": 0.20, "ETH": 0.15})
        result = opt.validate(target)

        assert isinstance(result, ValidatedWeights)
        assert result.rejected is False
        assert result.original is target
        assert "BTC" in result.weights
        assert "ETH" in result.weights

    def test_validate_clips_and_reports(self):
        opt = _optimizer(
            max_weight_per_asset=0.20,
            max_total_exposure=1.0,
            max_turnover_per_rebalance=1.0,
            current_weights={},
        )
        target = _target({"BTC": 0.30})
        result = opt.validate(target)
        assert result.weights["BTC"] == pytest.approx(0.20)
        assert "BTC" in result.clipped

    def test_config_property(self):
        cfg = RiskConfig(max_weight_per_asset=0.5)
        opt = _optimizer(risk_config=cfg)
        assert opt.config.max_weight_per_asset == 0.5

    def test_config_setter(self):
        opt = _optimizer()
        new_cfg = RiskConfig(max_weight_per_asset=0.10)
        opt.config = new_cfg
        assert opt.config.max_weight_per_asset == 0.10

    def test_venue_property(self):
        opt = _optimizer()
        assert opt.venue == Venue.BINANCE
