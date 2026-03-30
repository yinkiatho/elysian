"""
Capital allocation + weight registry for multiple strategies on a shared pipeline.

One WeightAggregator per (asset_type, venue) pair. It manages **target weights**
(what strategies want), while Portfolio manages **actual state** (what we have).

Each strategy registers with:
  - A capital allocation fraction (e.g., 0.6 = 60% of portfolio)
  - A symbol set (the symbols it trades)
  - Its latest target weights (updated each rebalance cycle)
  - A ShadowBook for per-strategy position attribution

The merged weights are scaled by allocation::

    Strategy A (alloc=0.6): {ETHUSDT: 0.5, BTCUSDT: 0.5}
    Strategy B (alloc=0.4): {SOLUSDT: 1.0}
    Merged: {ETHUSDT: 0.30, BTCUSDT: 0.30, SOLUSDT: 0.40}
"""

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set

import elysian_core.utils.logger as log

if TYPE_CHECKING:
    from elysian_core.core.shadow_book import ShadowBook

logger = log.setup_custom_logger("root")


class WeightAggregator:
    """Merges weight vectors from multiple strategies into one combined vector,
    each scaled by its capital allocation fraction.

    Thread-safe via asyncio.Lock for concurrent FSM access.
    """

    def __init__(self):
        self._allocations: Dict[int, float] = {}         # strategy_id -> fraction [0, 1]
        self._symbols: Dict[int, Set[str]] = {}           # strategy_id -> symbol set
        self._weights: Dict[int, Dict[str, float]] = {}   # strategy_id -> latest weights
        self._shadow_books: Dict[int, "ShadowBook"] = {}  # strategy_id -> shadow book
        self._lock = asyncio.Lock()

    def register_strategy(
        self,
        strategy_id: int,
        allocation: float,
        symbols: Optional[Set[str]] = None,
        shadow_book: Optional["ShadowBook"] = None,
    ) -> None:
        """Register a strategy with its capital allocation and optional symbol set.

        Parameters
        ----------
        strategy_id:
            Unique identifier for the strategy.
        allocation:
            Fraction of total portfolio capital allocated to this strategy.
            All allocations should sum to <= 1.0.
        symbols:
            Optional set of symbols this strategy may trade. Used for
            informational/auditing purposes.
        shadow_book:
            The strategy's ShadowBook for per-strategy fill attribution.
        """
        self._allocations[strategy_id] = allocation
        self._symbols[strategy_id] = symbols or set()
        self._weights[strategy_id] = {}
        if shadow_book is not None:
            self._shadow_books[strategy_id] = shadow_book

        total = self.total_allocation
        if total > 1.0:
            logger.warning(
                f"[WeightAggregator] Total allocation is {total:.2f} (> 1.0) "
                f"after registering strategy {strategy_id} with alloc={allocation:.2f}. "
                f"Downstream optimizer will likely scale weights down."
            )
        logger.info(
            f"[WeightAggregator] Registered strategy {strategy_id}: "
            f"allocation={allocation:.2f}, symbols={len(symbols or set())}, "
            f"total_alloc={total:.2f}"
        )

    async def update_weights(self, strategy_id: int, weights: Dict[str, float]) -> None:
        """Store latest target weights from a strategy.

        Called by the strategy's RebalanceFSM during the COMPUTING stage.
        """
        async with self._lock:
            self._weights[strategy_id] = dict(weights)

    async def get_merged_weights(self) -> Dict[str, float]:
        """Merge all strategies' weights, each scaled by its allocation fraction.

        For overlapping symbols across strategies, the weights are summed
        (after scaling). The downstream optimizer enforces per-asset caps.

        Returns
        -------
        Dict[str, float]
            Combined weight vector ready for the optimizer.
        """
        async with self._lock:
            merged: Dict[str, float] = {}
            for strat_id, weights in self._weights.items():
                alloc = self._allocations.get(strat_id, 0.0)
                if alloc <= 0:
                    continue
                for sym, w in weights.items():
                    merged[sym] = merged.get(sym, 0.0) + w * alloc
            return merged

    def get_strategy_weights(self, strategy_id: int) -> Dict[str, float]:
        """Return the latest raw (unscaled) weights for a specific strategy."""
        return dict(self._weights.get(strategy_id, {}))

    @property
    def total_allocation(self) -> float:
        """Sum of all strategy allocations. Should be <= 1.0."""
        return sum(self._allocations.values())

    @property
    def registered_strategies(self) -> Set[int]:
        """Set of registered strategy IDs."""
        return set(self._allocations.keys())

    @property
    def shadow_books(self) -> Dict[int, "ShadowBook"]:
        """All registered shadow books, keyed by strategy_id."""
        return dict(self._shadow_books)

    async def attribute_fills(
        self,
        mark_prices: Dict[str, float],
        portfolio_total_value: float,
    ) -> None:
        """No-op: fill attribution is now handled in real-time via ORDER_UPDATE events.

        Previously this called reconcile_shadow_books() which overwrote event-driven
        fills with mark-price approximations. Kept as a no-op for API compatibility.
        """
        pass

    def __repr__(self) -> str:
        entries = ", ".join(
            f"{sid}({alloc:.0%})" for sid, alloc in self._allocations.items()
        )
        return f"WeightAggregator([{entries}] total={self.total_allocation:.0%})"
