"""
Risk constraint configuration for the :class:`PortfolioOptimizer`.

All weight-based limits are expressed as fractions of total portfolio value.
This dataclass is intentionally *mutable* so limits can be tightened at runtime
(e.g. during volatility spikes or drawdown breakers).
"""

from dataclasses import dataclass, field
from typing import FrozenSet, Optional


@dataclass
class RiskConfig:
    # ── Per-asset weight bounds ──────────────────────────────────────────────
    max_weight_per_asset: float = 0.25          # no single asset > 25%
    min_weight_per_asset: float = 0.0           # 0 = long-only is fine

    # ── Portfolio-level exposure ─────────────────────────────────────────────
    max_total_exposure: float = 1.0             # sum(abs(w)); 1.0 for unlevered
    min_cash_weight: float = 0.05               # always keep >= 5% in cash

    # ── Turnover control ─────────────────────────────────────────────────────
    max_turnover_per_rebalance: float = 0.5     # max sum(abs(delta_w)) per cycle

    # ── Symbol filters ───────────────────────────────────────────────────────
    allowed_symbols: Optional[FrozenSet[str]] = None    # None = all allowed
    blocked_symbols: FrozenSet[str] = field(default_factory=frozenset)

    # ── Futures / leverage ───────────────────────────────────────────────────
    max_leverage: float = 1.0
    max_short_weight: float = 0.0               # 0 = no shorts

    # ── Notional filters ─────────────────────────────────────────────────────
    min_order_notional: float = 10.0            # skip orders below this USD
    max_order_notional: float = 100_000.0       # cap single-order size

    # ── Rate limiting ────────────────────────────────────────────────────────
    min_rebalance_interval_ms: int = 60_000     # 1 minute between rebalances
