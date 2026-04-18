"""
Sub-account pipeline primitives.

SubAccountKey   — frozen, hashable (strategy_id, asset_type, venue, sub_account_id) tuple
                  used as routing key for multi-sub-account strategies.
SubAccountPipeline — mutable container that owns one full Stage 3-5 pipeline for
                     a single (asset_type, venue) sub-account.
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional
from elysian_core.core.enums import AssetType, Venue

if TYPE_CHECKING:
    from elysian_core.connectors.base import SpotExchangeConnector
    from elysian_core.core.event_bus import EventBus
    from elysian_core.core.shadow_book import ShadowBook
    from elysian_core.risk.optimizer import PortfolioOptimizer
    from elysian_core.core.rebalance_fsm import RebalanceFSM
    from elysian_core.execution.engine import ExecutionEngine


@dataclass(frozen=True)
class SubAccountKey:
    """Immutable routing key for one sub-account pipeline.

    Hashable so it can be used as a dict key.  Two keys are equal iff all
    four fields match — (strategy_id, asset_type, venue, sub_account_id).
    """

    strategy_id: int
    asset_type: AssetType
    venue: Venue
    sub_account_id: int = 0

    def __str__(self) -> str:
        return (
            f"s{self.strategy_id}:{self.asset_type.value}"
            f":{self.venue.value}:{self.sub_account_id}"
        )


@dataclass
class SubAccountPipeline:
    """Owns one complete Stage 3-5 pipeline for a single sub-account.

    Holds the exchange connector, private EventBus, ShadowBook (or
    MarginShadowBook), PortfolioOptimizer, ExecutionEngine, and
    optionally the RebalanceFSM for this sub-account.

    MarginShadowBook is a subtype of ShadowBook, so shadow_book is typed
    as ShadowBook — no cast needed at call sites.
    """

    key: SubAccountKey
    exchange: "SpotExchangeConnector"
    private_event_bus: "EventBus"
    shadow_book: "ShadowBook"
    optimizer: "PortfolioOptimizer"
    execution_engine: "ExecutionEngine"
    rebalance_fsm: Optional["RebalanceFSM"] = None
    symbols: List[str] = field(default_factory=list)  # symbols this pipeline tracks
