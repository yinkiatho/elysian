"""
Typed event dataclasses emitted by exchange connectors and consumed by strategy hooks.

All event classes are frozen (immutable) to prevent strategies from accidentally
mutating shared data.  The ``event_type`` field is set automatically and used by
:class:`EventBus` for subscriber dispatch.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from elysian_core.core.enums import Venue
from elysian_core.core.market_data import Kline, OrderBook
from elysian_core.core.order import Order
from elysian_core.core.signals import RebalanceResult, ValidatedWeights


class EventType(Enum):
    KLINE = auto()
    ORDERBOOK_UPDATE = auto()
    ORDER_UPDATE = auto()
    BALANCE_UPDATE = auto()
    REBALANCE_COMPLETE = auto()


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
    venue: Venue
    order: Order
    timestamp: int
    event_type: EventType = field(default=EventType.ORDER_UPDATE, init=False)


@dataclass(frozen=True)
class BalanceUpdateEvent:
    asset: str
    venue: Venue
    delta: float
    new_balance: float
    timestamp: int
    event_type: EventType = field(default=EventType.BALANCE_UPDATE, init=False)


@dataclass(frozen=True)
class RebalanceCompleteEvent:
    """Published after the execution engine completes a rebalance cycle."""

    result: RebalanceResult
    validated_weights: ValidatedWeights
    timestamp: int
    event_type: EventType = field(default=EventType.REBALANCE_COMPLETE, init=False)
