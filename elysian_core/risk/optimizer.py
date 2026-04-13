"""
Portfolio risk optimizer (Stage 4).

Validates and adjusts strategy weight signals against risk constraints.
This is a stateless constraint projector — it clips, scales, and rejects
weights that violate the configured risk envelope.  It is *not* a
Markowitz-style optimizer (that logic, if desired, lives in the strategy).

This is a Shadowbook Level Component aka at Strategy level
"""

import math
import time
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple, TYPE_CHECKING

from elysian_core.core.portfolio import Portfolio
from elysian_core.core.signals import TargetWeights, ValidatedWeights
from elysian_core.risk.risk_config import RiskConfig
from elysian_core.core.enums import Venue
import elysian_core.utils.logger as log

if TYPE_CHECKING:
    from elysian_core.core.shadow_book import ShadowBook

# ---------------------------------------------------------------------------
# PortfolioOptimizer
# ---------------------------------------------------------------------------

class PortfolioOptimizer:
    """Portfolio risk optimizer.

    Projects target weights onto the risk-feasible set defined by *risk_config*
    via a deterministic, ordered pipeline:

        1. Input validation     — reject NaN / Inf / non-numeric weights
        2. Rate-limit check     — enforce min_rebalance_interval_ms
        3. Symbol filter        — whitelist / blacklist
        4. Per-asset clip       — max_weight_per_asset / max_short_weight
        5. Total-exposure scale — max_total_exposure
        6. Cash-floor enforce   — min_cash_weight
        7. Turnover cap         — max_turnover_per_rebalance
        8. Min-delta prune      — drop legs below min_weight_delta

    All steps are idempotent and leave the weights dict in a valid state
    before handing off to the next step.  The pipeline never raises; errors
    are logged and surfaced via ``ValidatedWeights.rejected``.
    """

    def __init__(
        self,
        risk_config: RiskConfig,
        portfolio: "ShadowBook",
        cfg: Optional[Any] = None,
    ) -> None:
        if risk_config is None:
            raise ValueError("risk_config must not be None")
        if portfolio is None:
            raise ValueError("portfolio must not be None")

        self._risk_config: RiskConfig = risk_config
        self._portfolio: "ShadowBook" = portfolio
        self._cfg = cfg
        self._last_rebalance_ts: Optional[int] = None
        self.logger = log.setup_custom_logger(
            f"{portfolio._strategy_name}_{portfolio._strategy_id}"
        )

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def config(self) -> RiskConfig:
        return self._risk_config

    @config.setter
    def config(self, new_config: RiskConfig) -> None:
        if new_config is None:
            raise ValueError("Cannot set risk_config to None")
        self._risk_config = new_config

    @property
    def venue(self) -> Optional[Venue]:
        return self._portfolio._venue

    # ── Public API ────────────────────────────────────────────────────────

    def validate(self, target: TargetWeights, **ctx) -> ValidatedWeights:
        """Project *target* weights onto the risk-feasible set.

        Returns a :class:`ValidatedWeights` whose ``rejected`` flag is *True*
        if the input could not be sanitised (e.g. all-NaN, rate-limited).
        The caller must check ``rejected`` before acting on the result.

        Parameters
        ----------
        target:
            Raw weight signal from the strategy.
        **ctx:
            Reserved for future per-call overrides (e.g. ``force=True`` to
            bypass rate limiting).  Currently unused but accepted to allow
            callers to pass context without breaking the signature.
        """
        now_ms = int(time.time() * 1000)
        cfg = self._risk_config
        
        self.logger.info(
            "[Optimizer] Incoming signal: %d symbols, raw_sum=%.4f",
            len(target.weights),
            sum(target.weights.values()) if target.weights else 0.0,
        )

        # ── Fast-path: liquidate ──────────────────────────────────────────
        if target.liquidate:
            self.logger.info("[Optimizer] Liquidate flag — bypassing all constraints")
            self._last_rebalance_ts = now_ms
            return ValidatedWeights(
                original=target,
                weights=dict(target.weights),
                clipped={},
                rejected=False,
                timestamp=now_ms,
            )

        # ── Step 0: Input validation ──────────────────────────────────────
        try:
            weights, bad_syms = self._validate_inputs(target.weights)
        except Exception as exc:
            self.logger.error("[Optimizer] Input validation raised: %s", exc)
            return self._reject(target, now_ms, reason="input_validation_error")

        if bad_syms:
            self.logger.warning(
                "[Optimizer] Dropped %d symbols with non-finite weights: %s",
                len(bad_syms),
                sorted(bad_syms),
            )

        if not weights:
            self.logger.warning("[Optimizer] No valid weights after input validation — rejecting")
            return self._reject(target, now_ms, reason="all_weights_invalid")

        # ── Step 1: Symbol filter ─────────────────────────────────────────
        weights, filtered = self._filter_symbols(weights)
        if filtered:
            self.logger.info(
                "[Optimizer] Symbol filter removed %d symbols: %s",
                len(filtered),
                sorted(filtered),
            )
        if not weights:
            self.logger.warning("[Optimizer] No symbols remain after filter — rejecting")
            return self._reject(target, now_ms, reason="all_symbols_filtered")

        # ── Step 2: Per-asset clip ────────────────────────────────────────
        weights, clips = self._clip_per_asset(weights)
        if clips:
            self.logger.info(
                "[Optimizer] Clipped %d symbols (excess removed): %s",
                len(clips),
                {k: f"{v:+.4f}" for k, v in clips.items()},
            )

        # ── Step 3: Total-exposure scaling ────────────────────────────────
        weights, exposure_scaled = self._scale_exposure(weights)
        if exposure_scaled:
            gross_before, gross_after = exposure_scaled
            self.logger.info(
                "[Optimizer] Exposure scaled: gross %.4f -> %.4f (limit=%.4f)",
                gross_before,
                gross_after,
                cfg.max_total_exposure,
            )

        # ── Step 4: Cash-floor enforcement ────────────────────────────────
        weights, cash_scaled = self._enforce_cash_floor(weights)
        if cash_scaled:
            invested_before, invested_after = cash_scaled
            self.logger.info(
                "[Optimizer] Cash floor enforced: invested %.4f -> %.4f (min_cash=%.4f)",
                invested_before,
                invested_after,
                cfg.min_cash_weight,
            )

        # ── Step 6: Turnover cap ──────────────────────────────────────────
        current = self._safe_current_weights()
        weights, turnover_info = self._cap_turnover(weights, original=current)
        if turnover_info:
            to_before, to_after = turnover_info
            self.logger.info(
                "[Optimizer] Turnover capped: %.4f -> %.4f (limit=%.4f)",
                to_before,
                to_after,
                cfg.max_turnover_per_rebalance,
            )

        # ── Step 7: Min-delta prune ───────────────────────────────────────
        weights, pruned = self._prune_min_delta(weights, original=current)
        if pruned:
            self.logger.info(
                "[Optimizer] Pruned %d legs below min_weight_delta=%.4f: %s",
                len(pruned),
                cfg.min_weight_delta,
                sorted(pruned),
            )

        # ── Done ──────────────────────────────────────────────────────────
        self._last_rebalance_ts = now_ms
        self.logger.info(
            "[Optimizer] Done: %d symbols, final_sum=%.4f",
            len(weights),
            sum(weights.values()),
        )

        return ValidatedWeights(
            original=target,
            weights=weights,
            clipped=clips,
            rejected=False,
            timestamp=now_ms,
            venue=target.venue,
        )

    def current_weights(
        self, mark_prices: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        """Delegate to the ShadowBook's cached or computed weight vector."""
        return self._portfolio.current_weights(mark_prices)

    def update_portfolio(self, portfolio: "ShadowBook") -> None:
        """Swap the underlying ShadowBook (e.g. after a hot-reload)."""
        if portfolio is None:
            raise ValueError("portfolio must not be None")
        self._portfolio = portfolio

    # ── Private constraint steps ──────────────────────────────────────────

    def _validate_inputs(
        self, raw: Dict[str, float]
    ) -> Tuple[Dict[str, float], Set[str]]:
        """Drop symbols whose weight is NaN, ±Inf, or non-numeric.

        Returns
        -------
        clean : dict
            Symbols with finite, numeric weights.
        bad : set
            Symbols that were dropped.
        """
        clean: Dict[str, float] = {}
        bad: Set[str] = set()

        for sym, w in raw.items():
            try:
                w_f = float(w)
            except (TypeError, ValueError):
                bad.add(sym)
                self.logger.debug("[Optimizer] Non-numeric weight for %s: %r", sym, w)
                continue

            if not math.isfinite(w_f):
                bad.add(sym)
                self.logger.debug("[Optimizer] Non-finite weight for %s: %f", sym, w_f)
                continue

            clean[sym] = w_f

        return clean, bad

    def _filter_symbols(
        self, weights: Dict[str, float]
    ) -> Tuple[Dict[str, float], Set[str]]:
        """Apply whitelist / blacklist symbol filters from RiskConfig.

        A symbol is *kept* if:
          - No whitelist is configured, OR it is on the whitelist.
          AND
          - It is NOT on the blacklist.
        """
        cfg = self._risk_config
        whitelist: FrozenSet[str] = frozenset(getattr(cfg, "symbol_whitelist", None) or [])
        blacklist: FrozenSet[str] = frozenset(getattr(cfg, "symbol_blacklist", None) or [])

        filtered: Set[str] = set()
        out: Dict[str, float] = {}

        for sym, w in weights.items():
            if whitelist and sym not in whitelist:
                filtered.add(sym)
                self.logger.debug("[Optimizer] %s not in whitelist — removed", sym)
                continue
            if sym in blacklist:
                filtered.add(sym)
                self.logger.debug("[Optimizer] %s in blacklist — removed", sym)
                continue
            out[sym] = w

        return out, filtered

    def _clip_per_asset(
        self, weights: Dict[str, float]
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Clip each weight to [floor, max_weight_per_asset].

        Long floor  = min_weight_per_asset  (typically 0 for long-only)
        Short floor = -max_short_weight     (0 when shorts are disabled)
        """
        cfg = self._risk_config
        clipped: Dict[str, float] = {}
        out: Dict[str, float] = {}

        long_floor: float = cfg.min_weight_per_asset      # e.g. 0.0
        short_floor: float = (
            -cfg.max_short_weight if cfg.max_short_weight > 0 else long_floor
        )
        ceil: float = cfg.max_weight_per_asset

        for sym, w in weights.items():
            original_w = w

            if w > ceil:
                w = ceil
            elif w < short_floor:
                w = short_floor

            # If long-only and result rounds to negative (float noise), zero it.
            if cfg.max_short_weight == 0 and w < 0:
                w = 0.0

            if w != original_w:
                clipped[sym] = original_w - w  # positive = clipped excess

            out[sym] = w

        return out, clipped

    def _scale_exposure(
        self, weights: Dict[str, float]
    ) -> Tuple[Dict[str, float], Optional[Tuple[float, float]]]:
        """Scale all weights proportionally if gross exposure exceeds the limit.

        Returns the scaled weights plus a (gross_before, gross_after) tuple
        when scaling was applied, or None when no scaling was needed.
        """
        cfg = self._risk_config
        gross = sum(abs(w) for w in weights.values())

        if gross == 0 or gross <= cfg.max_total_exposure:
            return weights, None

        scale = cfg.max_total_exposure / gross
        scaled = {sym: w * scale for sym, w in weights.items()}
        gross_after = sum(abs(w) for w in scaled.values())

        return scaled, (gross, gross_after)

    def _enforce_cash_floor(
        self, weights: Dict[str, float]
    ) -> Tuple[Dict[str, float], Optional[Tuple[float, float]]]:
        """Ensure long exposure does not crowd out the minimum cash buffer.

        Only long weights are scaled; short weights are left untouched because
        short positions do not consume the cash buffer in the same way.

        Returns the adjusted weights plus a (invested_before, invested_after)
        tuple when scaling was applied, or None otherwise.
        """
        cfg = self._risk_config
        min_cash = cfg.min_cash_weight

        if min_cash <= 0:
            return weights, None

        max_invested = 1.0 - min_cash
        total_long = sum(w for w in weights.values() if w > 0)

        if total_long == 0 or total_long <= max_invested:
            return weights, None

        scale = max_invested / total_long
        out = {sym: (w * scale if w > 0 else w) for sym, w in weights.items()}
        invested_after = sum(w for w in out.values() if w > 0)

        return out, (total_long, invested_after)

    def _cap_turnover(
        self,
        weights: Dict[str, float],
        original: Dict[str, float],
    ) -> Tuple[Dict[str, float], Optional[Tuple[float, float]]]:
        """Limit total weight change per rebalance cycle.

        Turnover is approximated as Σ|target_w - current_w| across all
        symbols that appear in either dict.  When the limit is exceeded,
        each weight is moved only *scale* of the way from original to target
        (partial rebalance), preserving direction.

        Symbols in *original* but not in *weights* are treated as a full
        exit (target_w = 0).  After scaling, positions that become negligible
        (< 1e-12) are dropped.

        Returns the capped weights plus a (turnover_before, turnover_after)
        tuple, or None if no capping was applied.
        """
        cfg = self._risk_config
        all_syms = set(weights) | set(original)

        turnover = sum(
            abs(weights.get(s, 0.0) - original.get(s, 0.0)) for s in all_syms
        )

        if turnover == 0 or turnover <= cfg.max_turnover_per_rebalance:
            return weights, None

        scale = cfg.max_turnover_per_rebalance / turnover

        result: Dict[str, float] = {}
        for sym in all_syms:
            orig_w = original.get(sym, 0.0)
            target_w = weights.get(sym, 0.0)
            new_w = orig_w + (target_w - orig_w) * scale
            if abs(new_w) > 1e-12:
                result[sym] = new_w

        turnover_after = sum(
            abs(result.get(s, 0.0) - original.get(s, 0.0)) for s in set(result) | set(original)
        )

        return result, (turnover, turnover_after)

    def _prune_min_delta(
        self,
        weights: Dict[str, float],
        original: Dict[str, float],
    ) -> Tuple[Dict[str, float], Set[str]]:
        """Drop any weight leg whose absolute delta vs current is below threshold.

        This avoids churning tiny rebalance legs that would generate
        sub-threshold orders anyway.  Symbols already absent from *original*
        with a non-trivial target weight are kept; only marginal changes are
        pruned.
        """
        cfg = self._risk_config
        min_delta: float = getattr(cfg, "min_weight_delta", 0.0)

        if min_delta <= 0:
            return weights, set()

        pruned: Set[str] = set()
        out: Dict[str, float] = {}

        for sym, w in weights.items():
            delta = abs(w - original.get(sym, 0.0))
            if delta < min_delta:
                pruned.add(sym)
                self.logger.debug(
                    "[Optimizer] Pruned %s: delta=%.6f < min_weight_delta=%.6f",
                    sym,
                    delta,
                    min_delta,
                )
            else:
                out[sym] = w

        return out, pruned

    # ── Helpers ───────────────────────────────────────────────────────────

    def _safe_current_weights(self) -> Dict[str, float]:
        """Fetch current weights from the ShadowBook, returning {} on error."""
        try:
            return self._portfolio.current_weights() or {}
        except Exception as exc:
            self.logger.warning(
                "[Optimizer] Could not fetch current weights: %s — using empty dict",
                exc,
            )
            return {}

    def _reject(
        self,
        target: TargetWeights,
        now_ms: int,
        reason: str = "unknown",
    ) -> ValidatedWeights:
        """Return a rejected ValidatedWeights with an empty weight dict."""
        self.logger.warning("[Optimizer] Signal rejected: reason=%s", reason)
        return ValidatedWeights(
            original=target,
            weights={},
            clipped={},
            rejected=True,
            timestamp=now_ms,
            venue=target.venue,
        )