"""
Typed event dataclasses emitted by exchange connectors and consumed by strategy hooks.

All event classes are frozen (immutable) to prevent strategies from accidentally
mutating shared data.  The ``event_type`` field is set automatically and used by
:class:`EventBus` for subscriber dispatch.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from elysian_core.core.enums import AssetType, Venue
from elysian_core.core.market_data import Kline, OrderBook
from elysian_core.core.order import Order
from elysian_core.core.signals import RebalanceResult, ValidatedWeights


class EventType(Enum):
    KLINE = auto()
    ORDERBOOK_UPDATE = auto()
    ORDER_UPDATE = auto()
    BALANCE_UPDATE = auto()
    REBALANCE_COMPLETE = auto()
    LIFECYCLE = auto()
    REBALANCE_CYCLE = auto()


@dataclass(frozen=True)
class KlineEvent:
    symbol: str
    venue: Venue
    kline: Kline
    timestamp: int  # epoch ms
    event_type: EventType = field(default=EventType.KLINE, init=False)


@dataclass(frozen=True)
class OrderBookUpdateEvent:
    symbol: str
    venue: Venue
    orderbook: OrderBook
    timestamp: int
    event_type: EventType = field(default=EventType.ORDERBOOK_UPDATE, init=False)


@dataclass(frozen=True)
class OrderUpdateEvent:
    symbol: str
    base_asset: str
    quote_asset: str
    venue: Venue
    order: Order
    timestamp: int
    asset_type: AssetType = AssetType.SPOT   # SPOT | MARGIN | PERPETUAL — for sub-account routing
    sub_account_id: int = 0                  # strategy sub-account that owns this event
    event_type: EventType = field(default=EventType.ORDER_UPDATE, init=False)


@dataclass(frozen=True)
class BalanceUpdateEvent:
    asset: str
    venue: Venue
    delta: float
    new_balance: float  # Optional: Binance does not always send this
    timestamp: int
    asset_type: AssetType = AssetType.SPOT   # SPOT | MARGIN | PERPETUAL — for sub-account routing
    sub_account_id: int = 0                  # strategy sub-account that owns this event
    event_type: EventType = field(default=EventType.BALANCE_UPDATE, init=False)


@dataclass(frozen=True)
class RebalanceCompleteEvent:
    """Published after the execution engine completes a rebalance cycle."""

    result: RebalanceResult
    validated_weights: ValidatedWeights
    timestamp: int
    strategy_id: int = 0
    event_type: EventType = field(default=EventType.REBALANCE_COMPLETE, init=False)


@dataclass(frozen=True)
class LifecycleEvent:
    """Published when a strategy or runner changes lifecycle state."""

    component: str          # e.g. "EqualWeightStrategy", "StrategyRunner"
    old_state: Any          # StrategyState or RunnerState enum value
    new_state: Any
    timestamp: int
    event_type: EventType = field(default=EventType.LIFECYCLE, init=False)


@dataclass(frozen=True)
class RebalanceCycleEvent:
    """Published on each rebalance FSM state transition."""

    old_state: Any          # RebalanceState enum value
    new_state: Any
    trigger: str            # the trigger name that caused the transition
    timestamp: int
    metadata: dict = field(default_factory=dict)
    event_type: EventType = field(default=EventType.REBALANCE_CYCLE, init=False)
