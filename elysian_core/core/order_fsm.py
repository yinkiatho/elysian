"""
Order lifecycle state machine.

Validates ``OrderStatus`` transitions and logs warnings on invalid ones.
Operates in **warn-mode** — the exchange is authoritative, so illegal
transitions are logged but accepted rather than raising exceptions.

Usage::

    order = Order(id="123", ...)
    order.transition_to(OrderStatus.OPEN)        # PENDING -> OPEN  (valid)
    order.transition_to(OrderStatus.FILLED)       # OPEN -> FILLED   (valid)
    order.transition_to(OrderStatus.OPEN)         # FILLED -> OPEN   (warns, still applied)
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Set

from elysian_core.core.enums import OrderStatus
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

# Valid transitions: from_status -> set of allowed to_statuses
_VALID_TRANSITIONS: Dict[OrderStatus, FrozenSet[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({
        OrderStatus.OPEN,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,       # can be cancelled before ack
        OrderStatus.FILLED,          # market orders can fill instantly
    }),
    OrderStatus.OPEN: frozenset({
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED,  # additional partial fills
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    }),
    # Terminal states — no valid outbound transitions
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

_TERMINAL: FrozenSet[OrderStatus] = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
})


def validate_order_transition(
    current: OrderStatus,
    target: OrderStatus,
    order_id: str = "",
) -> bool:
    """Check whether ``current -> target`` is a valid order status transition.

    Returns ``True`` if valid.  Logs a warning and returns ``False`` on
    illegal transitions — the caller should still apply the change (the
    exchange is the source of truth).
    """
    allowed = _VALID_TRANSITIONS.get(current, frozenset())
    if target in allowed:
        return True

    if current == target:
        # Same-state "transition" — harmless, skip the warning.
        return True

    logger.warning(
        "[OrderFSM] Invalid transition %s -> %s for order %s "
        "(applying anyway — exchange is authoritative)",
        current.name, target.name, order_id,
    )
    return False


def is_terminal(status: OrderStatus) -> bool:
    """True if the status is a terminal (final) order state."""
    return status in _TERMINAL
