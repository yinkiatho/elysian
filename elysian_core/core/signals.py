"""
Data contracts for the alpha -> risk -> execution pipeline.

These frozen dataclasses flow between stages:
  Stage 3 (Strategy)  ->  TargetWeights
  Stage 4 (Risk)       ->  ValidatedWeights
  Stage 5 (Execution)  ->  OrderIntent / RebalanceResult
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from elysian_core.core.enums import OrderType, Side, Venue


@dataclass(frozen=True)
class TargetWeights:
    """Raw alpha output from a strategy: target portfolio weights.

    ``weights`` maps trading pair symbols (e.g. "ETHUSDT") to a target
    portfolio weight.  Cash weight is implicit: ``1.0 - sum(weights.values())``.

    For long-only spot, each weight is in [0, 1] and the sum <= 1.
    For futures with leverage, weights may exceed 1.0 (within leverage bounds).
    """

    weights: Dict[str, float]
    timestamp: int                                  # epoch ms when signal was generated
    strategy_id: str = ""
    venue: Optional[Venue] = None                   # target venue (None = default)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedWeights:
    """Risk-adjusted weights output from the :class:`PortfolioOptimizer`.

    Carries the original signal for audit trail, the adjusted weights,
    and per-symbol clip amounts so strategies can inspect what changed.
    """

    original: TargetWeights
    weights: Dict[str, float]                       # adjusted weights
    clipped: Dict[str, float]                       # per-symbol clip: original - adjusted
    rejected: bool = False
    rejection_reason: str = ""
    timestamp: int = 0                              # epoch ms when validation completed


@dataclass(frozen=True)
class OrderIntent:
    """A single order to be submitted, computed by the :class:`ExecutionEngine`."""

    symbol: str
    venue: Venue
    side: Side
    quantity: float                                 # base asset quantity
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None                   # None for market orders


@dataclass(frozen=True)
class RebalanceResult:
    """Summary returned by :class:`ExecutionEngine` after a rebalance cycle."""

    intents: Tuple[OrderIntent, ...]
    submitted: int
    failed: int
    timestamp: int                                  # epoch ms
    errors: Tuple[str, ...] = ()
