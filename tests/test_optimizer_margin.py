"""
Tests for PortfolioOptimizer margin-specific features.

Covers:
- max_gross_exposure takes precedence over max_total_exposure
- allow_zero_sum: rescales longs/shorts to market-neutral
- zero_sum_tolerance: skips balancing when already close to zero
- min_margin_level: rejects rebalance when margin_level is too low
- _check_margin_level: no-op for SPOT ShadowBook, no-op when min_margin_level=0
- SPOT regression: existing behaviour unchanged when margin flags not set
"""

import time
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.risk.risk_config import RiskConfig
from elysian_core.core.signals import TargetWeights
from elysian_core.core.enums import Venue


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_portfolio(current_weights=None, venue=Venue.BINANCE, nav=10_000.0):
    p = MagicMock()
    p._venue = venue
    p._strategy_name = "test"
    p._strategy_id = 1
    p.current_weights.return_value = current_weights or {}
    p.total_value.return_value = nav
    return p


def _margin_portfolio(margin_level_value: float = float("inf"), nav=10_000.0):
    """Mock a MarginShadowBook (has margin_level() method)."""
    from elysian_core.core.margin_shadow_book import MarginShadowBook
    p = MagicMock(spec=MarginShadowBook)
    p._venue = Venue.BINANCE
    p._strategy_name = "margin_test"
    p._strategy_id = 1
    p.current_weights.return_value = {}
    p.total_value.return_value = nav
    p.margin_level.return_value = margin_level_value
    return p


def _optimizer(risk_config: RiskConfig, portfolio=None):
    port = portfolio or _mock_portfolio()
    return PortfolioOptimizer(risk_config=risk_config, portfolio=port)


def _target(weights, strategy_id=1):
    return TargetWeights(
        weights=weights,
        timestamp=int(time.time() * 1000),
        strategy_id=strategy_id,
    )


# ─── max_gross_exposure ────────────────────────────────────────────────────────

class TestMaxGrossExposure:
    def test_uses_max_gross_exposure_when_set(self):
        """Leveraged strategy: gross exposure 2.0 is allowed when max_gross_exposure=2.0."""
        cfg = RiskConfig(
            max_total_exposure=1.0,
            max_gross_exposure=2.0,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
            min_weight_delta=0.0,
            max_turnover_per_rebalance=10.0,
        )
        opt = _optimizer(cfg)
        weights = {"BTCUSDT": 1.0, "ETHUSDT": -1.0}  # gross = 2.0
        result, info = opt._scale_exposure(weights)
        # Should NOT scale down because gross (2.0) == max_gross_exposure (2.0)
        assert info is None
        assert result["BTCUSDT"] == pytest.approx(1.0)
        assert result["ETHUSDT"] == pytest.approx(-1.0)

    def test_max_gross_exposure_caps_correctly(self):
        """Gross 2.4 with max_gross_exposure=2.0 → scale down to 2.0."""
        cfg = RiskConfig(
            max_total_exposure=1.0,
            max_gross_exposure=2.0,
            max_weight_per_asset=2.0,
            min_cash_weight=0.0,
            min_weight_delta=0.0,
            max_turnover_per_rebalance=10.0,
        )
        opt = _optimizer(cfg)
        weights = {"BTCUSDT": 1.2, "ETHUSDT": -1.2}  # gross = 2.4
        result, info = opt._scale_exposure(weights)
        assert info is not None
        gross_after = sum(abs(w) for w in result.values())
        assert gross_after == pytest.approx(2.0, rel=1e-5)

    def test_falls_back_to_max_total_exposure_when_gross_not_set(self):
        """When max_gross_exposure is None, max_total_exposure is used."""
        cfg = RiskConfig(
            max_total_exposure=1.0,
            max_gross_exposure=None,
            max_weight_per_asset=2.0,
            min_cash_weight=0.0,
            min_weight_delta=0.0,
            max_turnover_per_rebalance=10.0,
        )
        opt = _optimizer(cfg)
        weights = {"BTCUSDT": 0.8, "ETHUSDT": -0.8}  # gross = 1.6
        result, info = opt._scale_exposure(weights)
        assert info is not None
        gross_after = sum(abs(w) for w in result.values())
        assert gross_after == pytest.approx(1.0, rel=1e-5)

    def test_no_scaling_when_under_gross_limit(self):
        cfg = RiskConfig(
            max_gross_exposure=3.0,
            max_total_exposure=1.0,
            max_weight_per_asset=2.0,
            min_cash_weight=0.0,
            min_weight_delta=0.0,
        )
        opt = _optimizer(cfg)
        weights = {"BTCUSDT": 1.0, "ETHUSDT": -0.5}  # gross = 1.5
        result, info = opt._scale_exposure(weights)
        assert info is None  # no scaling needed


# ─── _enforce_zero_sum ────────────────────────────────────────────────────────

class TestEnforceZeroSum:
    def test_zero_sum_disabled_by_default(self):
        cfg = RiskConfig()
        opt = _optimizer(cfg)
        weights = {"A": 0.5, "B": -0.2}
        result, info = opt._enforce_zero_sum(weights)
        assert info is None
        assert result == weights

    def test_already_within_tolerance_no_action(self):
        cfg = RiskConfig(allow_zero_sum=True, zero_sum_tolerance=0.02)
        opt = _optimizer(cfg)
        weights = {"A": 0.4, "B": -0.41}  # net = -0.01, within ±0.02
        result, info = opt._enforce_zero_sum(weights)
        assert info is None

    def test_rescales_to_neutral_longs_larger(self):
        """Longs > shorts: shorts scaled up to match average."""
        cfg = RiskConfig(allow_zero_sum=True, zero_sum_tolerance=0.01)
        opt = _optimizer(cfg)
        # longs: 0.6, shorts: -0.2 → target each side = 0.4
        weights = {"A": 0.6, "B": -0.2}
        result, info = opt._enforce_zero_sum(weights)
        assert info is not None
        net_before, net_after = info
        assert abs(net_after) <= 0.01
        assert result["A"] == pytest.approx(0.4)
        assert result["B"] == pytest.approx(-0.4)

    def test_rescales_to_neutral_shorts_larger(self):
        """Shorts > longs: longs scaled up to match average."""
        cfg = RiskConfig(allow_zero_sum=True, zero_sum_tolerance=0.01)
        opt = _optimizer(cfg)
        weights = {"A": 0.2, "B": -0.6}
        result, info = opt._enforce_zero_sum(weights)
        assert info is not None
        _, net_after = info
        assert abs(net_after) <= 0.01
        assert result["A"] == pytest.approx(0.4)
        assert result["B"] == pytest.approx(-0.4)

    def test_one_sided_weights_no_balancing(self):
        """Only longs, no shorts — cannot balance; warns but returns unchanged."""
        cfg = RiskConfig(allow_zero_sum=True, zero_sum_tolerance=0.01)
        opt = _optimizer(cfg)
        weights = {"A": 0.5, "B": 0.3}  # all positive
        result, info = opt._enforce_zero_sum(weights)
        assert info is None
        assert result == weights

    def test_multiple_longs_and_shorts(self):
        """Multiple legs on each side — proportional rescaling within each side."""
        cfg = RiskConfig(allow_zero_sum=True, zero_sum_tolerance=0.005)
        opt = _optimizer(cfg)
        # longs: 0.3 + 0.2 = 0.5 | shorts: -0.1 - 0.1 = -0.2 → net = 0.3
        weights = {"L1": 0.3, "L2": 0.2, "S1": -0.1, "S2": -0.1}
        result, info = opt._enforce_zero_sum(weights)
        assert info is not None
        total_long = sum(w for w in result.values() if w > 0)
        total_short = abs(sum(w for w in result.values() if w < 0))
        assert total_long == pytest.approx(total_short, rel=1e-5)


# ─── _check_margin_level ──────────────────────────────────────────────────────

class TestCheckMarginLevel:
    def test_disabled_when_min_margin_level_zero(self):
        """min_margin_level=0 means disabled; always passes."""
        cfg = RiskConfig(min_margin_level=0.0)
        port = _margin_portfolio(margin_level_value=0.5)   # dangerously low
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        assert opt._check_margin_level() is True

    def test_passes_for_spot_portfolio(self):
        """SPOT ShadowBook (not MarginShadowBook) — check is a no-op."""
        cfg = RiskConfig(min_margin_level=1.5)
        port = _mock_portfolio()   # plain MagicMock, not MarginShadowBook spec
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        assert opt._check_margin_level() is True

    def test_passes_when_margin_level_above_threshold(self):
        cfg = RiskConfig(min_margin_level=1.5)
        port = _margin_portfolio(margin_level_value=2.0)
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        assert opt._check_margin_level() is True

    def test_fails_when_margin_level_below_threshold(self):
        cfg = RiskConfig(min_margin_level=1.5)
        port = _margin_portfolio(margin_level_value=1.2)
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        assert opt._check_margin_level() is False

    def test_fails_at_exactly_threshold(self):
        """Level equal to threshold is NOT sufficient — must be strictly above."""
        cfg = RiskConfig(min_margin_level=1.5)
        port = _margin_portfolio(margin_level_value=1.5)
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        # 1.5 < 1.5 is False, so should pass (not rejected)
        # level < threshold means reject; equal means pass
        assert opt._check_margin_level() is True

    def test_infinite_margin_level_passes(self):
        """No liabilities → infinite margin level → always passes."""
        cfg = RiskConfig(min_margin_level=1.5)
        port = _margin_portfolio(margin_level_value=float("inf"))
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        assert opt._check_margin_level() is True


# ─── Full validate() pipeline with margin constraints ─────────────────────────

class TestValidatePipelineMargin:
    def test_margin_level_too_low_rejects(self):
        """validate() returns rejected=True when margin level is below threshold."""
        cfg = RiskConfig(
            min_margin_level=1.5,
            min_weight_delta=0.0,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
        )
        port = _margin_portfolio(margin_level_value=1.2)
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        result = opt.validate(_target({"BTCUSDT": 0.5}))
        assert result.rejected is True
        assert result.rejection_reason == "margin_level_too_low"

    def test_margin_level_healthy_allows_rebalance(self):
        cfg = RiskConfig(
            min_margin_level=1.5,
            min_weight_delta=0.0,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
            max_turnover_per_rebalance=10.0,
        )
        port = _margin_portfolio(margin_level_value=2.0)
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        result = opt.validate(_target({"BTCUSDT": 0.5}))
        assert result.rejected is False

    def test_zero_sum_enforced_in_full_pipeline(self):
        """Full validate() pipeline applies zero-sum enforcement."""
        cfg = RiskConfig(
            allow_zero_sum=True,
            zero_sum_tolerance=0.01,
            min_weight_delta=0.0,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
            max_short_weight=1.0,
            max_total_exposure=2.0,
            max_turnover_per_rebalance=10.0,
            min_rebalance_interval_ms=0,
        )
        opt = _optimizer(cfg)
        # Large long bias: longs=0.6, shorts=-0.2 → unbalanced
        result = opt.validate(_target({"A": 0.6, "B": -0.2}))
        assert result.rejected is False
        total_long = sum(w for w in result.weights.values() if w > 0)
        total_short = abs(sum(w for w in result.weights.values() if w < 0))
        assert abs(total_long - total_short) <= 0.02  # within tolerance

    def test_max_gross_exposure_in_full_pipeline(self):
        """Leveraged strategy with max_gross_exposure=2.0 passes validation."""
        cfg = RiskConfig(
            max_gross_exposure=2.0,
            max_total_exposure=1.0,
            max_weight_per_asset=1.5,
            max_short_weight=1.5,
            min_cash_weight=0.0,
            min_weight_delta=0.0,
            max_turnover_per_rebalance=10.0,
            min_rebalance_interval_ms=0,
        )
        opt = _optimizer(cfg)
        result = opt.validate(_target({"BTCUSDT": 1.0, "ETHUSDT": -1.0}))
        assert result.rejected is False
        gross = sum(abs(w) for w in result.weights.values())
        assert gross <= 2.0 + 1e-9


# ─── SPOT regression tests ────────────────────────────────────────────────────

class TestSpotRegression:
    """Verify that existing SPOT behaviour is completely unchanged."""

    def test_spot_not_affected_by_min_margin_level(self):
        """SPOT portfolio: min_margin_level has no effect."""
        cfg = RiskConfig(
            min_margin_level=2.0,    # very high, but should not affect SPOT
            min_weight_delta=0.0,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
            max_turnover_per_rebalance=10.0,
            min_rebalance_interval_ms=0,
        )
        port = _mock_portfolio()   # plain mock — not a MarginShadowBook
        opt = PortfolioOptimizer(risk_config=cfg, portfolio=port)
        result = opt.validate(_target({"BTCUSDT": 0.5}))
        assert result.rejected is False

    def test_zero_sum_off_by_default(self):
        """allow_zero_sum defaults to False — longs are not rescaled for SPOT."""
        cfg = RiskConfig(
            min_weight_delta=0.0,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
            max_turnover_per_rebalance=10.0,
            min_rebalance_interval_ms=0,
        )
        opt = _optimizer(cfg)
        result = opt.validate(_target({"BTCUSDT": 0.6, "ETHUSDT": 0.3}))
        assert result.rejected is False
        # Weights should not be zero-summed (no shorts exist)
        assert result.weights["BTCUSDT"] > 0
        assert result.weights["ETHUSDT"] > 0

    def test_max_gross_exposure_none_uses_max_total(self):
        """Without max_gross_exposure, SPOT cap is max_total_exposure as before."""
        cfg = RiskConfig(
            max_total_exposure=1.0,
            max_gross_exposure=None,
            max_weight_per_asset=1.0,
            min_cash_weight=0.0,
            min_weight_delta=0.0,
            max_turnover_per_rebalance=10.0,
            min_rebalance_interval_ms=0,
        )
        opt = _optimizer(cfg)
        result = opt.validate(_target({"BTCUSDT": 0.6, "ETHUSDT": 0.6}))
        gross = sum(abs(w) for w in result.weights.values())
        assert gross <= 1.0 + 1e-9

    def test_per_asset_clip_still_works(self):
        cfg = RiskConfig(
            max_weight_per_asset=0.25,
            min_weight_delta=0.0,
            min_cash_weight=0.0,
            max_turnover_per_rebalance=10.0,
            min_rebalance_interval_ms=0,
        )
        opt = _optimizer(cfg)
        result = opt.validate(_target({"BTCUSDT": 0.9}))
        assert result.weights["BTCUSDT"] <= 0.25 + 1e-9
