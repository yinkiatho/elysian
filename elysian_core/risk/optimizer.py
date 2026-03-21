"""
Portfolio risk optimizer (Stage 4).

Validates and adjusts strategy weight signals against risk constraints.
This is a stateless constraint projector — it clips, scales, and rejects
weights that violate the configured risk envelope.  It is *not* a
Markowitz-style optimizer (that logic, if desired, lives in the strategy).
"""

import time
from typing import Dict, Tuple

from elysian_core.core.portfolio import Portfolio
from elysian_core.core.signals import TargetWeights, ValidatedWeights
from elysian_core.risk.risk_config import RiskConfig
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class PortfolioOptimizer:

    def __init__(self, risk_config: RiskConfig, portfolio: Portfolio):
        self._config = risk_config
        self._portfolio = portfolio
        self._last_rebalance_ts: int = 0

    # ── Public ───────────────────────────────────────────────────────────────

    @property
    def config(self) -> RiskConfig:
        return self._config

    @config.setter
    def config(self, new_config: RiskConfig):
        self._config = new_config

    def validate(self, target: TargetWeights) -> ValidatedWeights:
        """Project *target* weights onto the risk-feasible set.

        Pipeline executed in order:
          1. Rate-limit check
          2. Symbol whitelist / blacklist filter
          3. Per-asset weight clipping
          4. Total-exposure scaling
          5. Cash-floor enforcement
          6. Turnover cap (requires mark prices → uses last known weights)
        """
        now_ms = int(time.time() * 1000)
        cfg = self._config

        # 1. Rate limit
        if self._last_rebalance_ts > 0:
            elapsed = now_ms - self._last_rebalance_ts
            if elapsed < cfg.min_rebalance_interval_ms:
                return ValidatedWeights(
                    original=target,
                    weights={},
                    clipped={},
                    rejected=True,
                    rejection_reason=(
                        f"Rate limit: {elapsed}ms since last rebalance "
                        f"(min {cfg.min_rebalance_interval_ms}ms)"
                    ),
                    timestamp=now_ms,
                )

        weights = dict(target.weights)  # mutable copy
        clips: Dict[str, float] = {}

        # 2. Symbol filter
        weights = self._filter_symbols(weights)

        # 3. Per-asset clip
        weights, clips = self._clip_per_asset(weights)

        # 4. Total exposure scaling
        weights = self._scale_exposure(weights)

        # 5. Cash floor
        weights = self._enforce_cash_floor(weights)

        # 6. Turnover cap (best-effort without mark prices;
        #    uses raw weight delta as proxy)
        weights = self._cap_turnover(weights, target.weights)

        self._last_rebalance_ts = now_ms

        return ValidatedWeights(
            original=target,
            weights=weights,
            clipped=clips,
            rejected=False,
            timestamp=now_ms,
        )

    def current_weights(self, mark_prices: Dict[str, float]) -> Dict[str, float]:
        """Compute the current portfolio weight vector from live positions."""
        total = self._portfolio.total_value(mark_prices)
        if total <= 0:
            return {}
        out: Dict[str, float] = {}
        for sym, pos in self._portfolio.positions.items():
            price = mark_prices.get(sym)
            if price and not pos.is_flat():
                out[sym] = (pos.quantity * price) / total
        return out

    def update_portfolio(self, portfolio: Portfolio):
        self._portfolio = portfolio

    # ── Private constraint steps ─────────────────────────────────────────────

    def _filter_symbols(self, weights: Dict[str, float]) -> Dict[str, float]:
        cfg = self._config
        filtered: Dict[str, float] = {}
        for sym, w in weights.items():
            if sym in cfg.blocked_symbols:
                logger.debug(f"[Optimizer] Blocked symbol removed: {sym}")
                continue
            if cfg.allowed_symbols is not None and sym not in cfg.allowed_symbols:
                logger.debug(f"[Optimizer] Symbol not in allowlist: {sym}")
                continue
            filtered[sym] = w
        return filtered

    def _clip_per_asset(
        self, weights: Dict[str, float]
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        cfg = self._config
        clipped: Dict[str, float] = {}
        out: Dict[str, float] = {}
        for sym, w in weights.items():
            clamped = w
            # Upper bound
            if clamped > cfg.max_weight_per_asset:
                clamped = cfg.max_weight_per_asset
            # Lower bound (handles shorts when max_short_weight > 0)
            floor = -cfg.max_short_weight if cfg.max_short_weight > 0 else cfg.min_weight_per_asset
            if clamped < floor:
                clamped = floor

            if clamped != w:
                clipped[sym] = w - clamped
                logger.debug(
                    f"[Optimizer] Clipped {sym}: {w:.4f} -> {clamped:.4f}"
                )
            out[sym] = clamped
        return out, clipped

    def _scale_exposure(self, weights: Dict[str, float]) -> Dict[str, float]:
        cfg = self._config
        gross = sum(abs(w) for w in weights.values())
        if gross <= cfg.max_total_exposure or gross == 0:
            return weights
        scale = cfg.max_total_exposure / gross
        logger.debug(
            f"[Optimizer] Scaling exposure: gross={gross:.4f} -> {cfg.max_total_exposure:.4f} "
            f"(factor={scale:.4f})"
        )
        return {sym: w * scale for sym, w in weights.items()}

    def _enforce_cash_floor(self, weights: Dict[str, float]) -> Dict[str, float]:
        cfg = self._config
        total_long = sum(w for w in weights.values() if w > 0)
        max_invested = 1.0 - cfg.min_cash_weight
        if total_long <= max_invested or total_long == 0:
            return weights
        scale = max_invested / total_long
        logger.debug(
            f"[Optimizer] Enforcing cash floor: invested={total_long:.4f} -> {max_invested:.4f}"
        )
        return {
            sym: (w * scale if w > 0 else w)
            for sym, w in weights.items()
        }

    def _cap_turnover(
        self, weights: Dict[str, float], original: Dict[str, float]
    ) -> Dict[str, float]:
        """Limit total weight change per rebalance.

        Without mark prices we approximate turnover as the sum of absolute
        weight deltas between the *previous validated weights* (stored as
        ``original``) and the current *adjusted* weights.  This is a
        best-effort guard; the execution engine applies a second notional
        filter before submitting orders.
        """
        cfg = self._config
        all_syms = set(weights) | set(original)
        turnover = sum(
            abs(weights.get(s, 0.0) - original.get(s, 0.0)) for s in all_syms
        )
        if turnover <= cfg.max_turnover_per_rebalance or turnover == 0:
            return weights

        scale = cfg.max_turnover_per_rebalance / turnover
        logger.debug(
            f"[Optimizer] Capping turnover: {turnover:.4f} -> {cfg.max_turnover_per_rebalance:.4f}"
        )
        # Scale the *delta* rather than the absolute weight.  Move each weight
        # only ``scale`` fraction of the way from original to target.
        result: Dict[str, float] = {}
        for sym in all_syms:
            orig_w = original.get(sym, 0.0)
            target_w = weights.get(sym, 0.0)
            result[sym] = orig_w + (target_w - orig_w) * scale
        # Drop zero weights
        return {s: w for s, w in result.items() if w != 0.0}
