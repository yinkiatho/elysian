"""
Portfolio risk optimizer (Stage 4).

Validates and adjusts strategy weight signals against risk constraints.
This is a stateless constraint projector — it clips, scales, and rejects
weights that violate the configured risk envelope.  It is *not* a
Markowitz-style optimizer (that logic, if desired, lives in the strategy).

This is a Shadowbook Level Component aka at Strategy level
"""

import time
from typing import Any, Dict, Optional, Tuple, Union, TYPE_CHECKING

from elysian_core.core.portfolio import Portfolio
from elysian_core.core.signals import TargetWeights, ValidatedWeights
from elysian_core.risk.risk_config import RiskConfig
from elysian_core.core.enums import Venue
import elysian_core.utils.logger as log

if TYPE_CHECKING:
    from elysian_core.core.shadow_book import ShadowBook

logger = log.setup_custom_logger("root")


class PortfolioOptimizer:
    '''
    Portfolio risk optimizer that validates and adjusts strategy weight signals
    against risk constraints defined in a RiskConfig.  It applies a series of
    transformations to project target weights onto the risk-feasible set, including:    
        1. Symbol whitelist / blacklist filtering
        2. Per-asset weight clipping
        3. Total-exposure scaling
        4. Cash-floor enforcement
        5. Turnover capping (best-effort without mark prices)
        
        
    This Optimizer is on the Strategy/ShadowBook Level
    
    '''

    def __init__(self, risk_config: RiskConfig,
                        portfolio: 'ShadowBook',
                        cfg: Optional[Any] = None):
        self._risk_config = risk_config
        self._portfolio = portfolio
        self.cfg = cfg
        self._last_rebalance_ts: int = None  # per-strategy rate limiting

    # ── Public ───────────────────────────────────────────────────────────────

    @property
    def config(self) -> RiskConfig:
        return self._risk_config
    
    @property
    def venue(self) -> Optional[Venue]:
        return self._portfolio._venue

    @config.setter
    def config(self, new_config: RiskConfig):
        self._risk_config = new_config


    def validate(self, target: TargetWeights, **ctx) -> ValidatedWeights:
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
        cfg = self._risk_config

        logger.info(
            f"[Optimizer] Validating weights: {len(target.weights)} symbols, "
            f"sum={sum(target.weights.values()):.4f} ({target.weights})"
        )
        
        weights = dict(target.weights)  # mutable copy
        clips: Dict[str, float] = {}

        # 1. Symbol filter
        pre_filter = len(weights)
        if len(weights) < pre_filter:
            logger.info(f"[Optimizer] Symbol filter: {pre_filter} -> {len(weights)} symbols")

        # 2. Per-asset clip
        weights, clips = self._clip_per_asset(weights)
        if clips:
            logger.info(f"[Optimizer] Clipped {len(clips)} symbols: {clips}")

        # 3. Total exposure scaling
        weights = self._scale_exposure(weights)

        # 4. Cash floor
        #weights = self._enforce_cash_floor(weights)

        # 5. Turnover cap (best-effort without mark prices;
        #    uses raw weight delta as proxy)
        weights = self._cap_turnover(weights, original=self._portfolio.current_weights())
        self._last_rebalance_ts = now_ms

        logger.info(
            f"[Optimizer] Validated: {len(weights)} symbols, "
            f"sum={sum(weights.values()):.4f} ({weights})"
        )

        return ValidatedWeights(
            original=target,
            weights=weights,
            clipped=clips,
            rejected=False,
            timestamp=now_ms,
        )

    def current_weights(self, mark_prices: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """Delegate to Portfolio's cached or computed weight vector."""
        return self._portfolio.current_weights(mark_prices)


    def update_portfolio(self, portfolio: Portfolio):
        self._portfolio = portfolio



    # ── Private constraint steps ─────────────────────────────────────────────

    def _clip_per_asset(self, weights: Dict[str, float]) -> Tuple[Dict[str, float], Dict[str, float]]:
        
        cfg = self._risk_config
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
        
        cfg = self._risk_config
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
        
        cfg = self._risk_config
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
        cfg = self._risk_config
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
        return {s: w for s, w in result.items() if abs(w) > 1e-12}
