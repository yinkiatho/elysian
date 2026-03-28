"""
Order lifecycle state machine.

Validates ``OrderStatus`` transitions and logs warnings on invalid ones.
Operates in **warn-mode** — the exchange is authoritative, so illegal
transitions are logged but accepted rather than raising exceptions.

Full lifecycle reference::

    Normal order:    PENDING → OPEN → PARTIALLY_FILLED* → FILLED
                                    └→ CANCELLED
                                    └→ EXPIRED (GTT/IOC)
    Instant fill:    PENDING → FILLED (market orders)
    Conditional SL:  PENDING → OPEN → TRIGGERED → PARTIALLY_FILLED* → FILLED
    Conditional TP:  PENDING → OPEN → TRIGGERED → PARTIALLY_FILLED* → FILLED

Module-level functions ``validate_order_transition()`` and ``is_terminal()``
are retained for backwards compatibility.  New code should prefer the
``OrderFSM`` class for per-order lifecycle management.

Usage (module-level functions)::

    validate_order_transition(OrderStatus.PENDING, OrderStatus.OPEN)   # True
    is_terminal(OrderStatus.FILLED)                                      # True

Usage (OrderFSM class)::

    fsm = OrderFSM(OrderStatus.PENDING)
    fsm.transition(OrderStatus.OPEN, order_id="123")
    fsm.transition(OrderStatus.TRIGGERED, order_id="123")
    assert fsm.can_fill
    assert fsm.is_active
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional

from elysian_core.core.enums import OrderStatus, OrderType
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------
# Valid transitions: from_status -> frozenset of allowed to_statuses.
# Each entry carries a comment explaining what triggers that state change.

_VALID_TRANSITIONS: Dict[OrderStatus, FrozenSet[OrderStatus]] = {

    # PENDING: order submitted to exchange, awaiting acknowledgement.
    OrderStatus.PENDING: frozenset({
        OrderStatus.OPEN,             # exchange acknowledged the resting order
        OrderStatus.REJECTED,         # exchange rejected the order outright
        OrderStatus.CANCELLED,        # cancelled before exchange ack (rare, client-side)
        OrderStatus.FILLED,           # market orders can fill instantly without resting
    }),

    # OPEN: order is resting on the exchange order book.
    OrderStatus.OPEN: frozenset({
        OrderStatus.PARTIALLY_FILLED, # first partial fill arrived
        OrderStatus.FILLED,           # fully filled in one shot
        OrderStatus.CANCELLED,        # user or system cancelled
        OrderStatus.EXPIRED,          # GTT/IOC time-in-force lapsed without full fill
        OrderStatus.TRIGGERED,        # conditional order (SL/TP): trigger price was hit
    }),

    # PARTIALLY_FILLED: one or more partial fills received, order still open.
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED, # additional partial fills arriving
        OrderStatus.FILLED,           # final fill completes the order
        OrderStatus.CANCELLED,        # remaining quantity cancelled mid-fill
        OrderStatus.EXPIRED,          # GTT/IOC remainder expired after partial fill
    }),

    # TRIGGERED: conditional order (SL/TP) whose trigger condition was met;
    #            the child order is now active and executing on the exchange.
    OrderStatus.TRIGGERED: frozenset({
        OrderStatus.OPEN,             # triggered order placed as a resting limit order
        OrderStatus.PARTIALLY_FILLED, # triggered order began filling immediately
        OrderStatus.FILLED,           # triggered order filled in full immediately
        OrderStatus.CANCELLED,        # triggered but then cancelled before fill
        OrderStatus.EXPIRED,          # triggered but expired before fill (IOC child)
    }),

    # Terminal states — no valid outbound transitions.
    OrderStatus.FILLED:    frozenset(),  # order fully executed; lifecycle complete
    OrderStatus.CANCELLED: frozenset(),  # order cancelled; no further fills possible
    OrderStatus.REJECTED:  frozenset(),  # exchange rejected; order never rested
    OrderStatus.EXPIRED:   frozenset(),  # GTT/IOC lapsed; distinct from CANCELLED
}

_TERMINAL: FrozenSet[OrderStatus] = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
})

# Conditional order types — those that go through the TRIGGERED state.
_CONDITIONAL_ORDER_TYPES: FrozenSet[OrderType] = frozenset({
    OrderType.STOP_MARKET,
    OrderType.TAKE_PROFIT_MARKET,
})


# ---------------------------------------------------------------------------
# Module-level functions (backwards-compatible API)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# OrderFSM class — per-order lifecycle wrapper
# ---------------------------------------------------------------------------

class OrderFSM:
    """Per-order state machine wrapping the ``_VALID_TRANSITIONS`` table.

    Provides a clean, object-oriented API for tracking a single order's
    lifecycle.  Designed as groundwork for Stop-Loss / Take-Profit order
    management; the ``is_conditional`` and ``can_fill`` properties make it
    straightforward to branch logic once SL/TP execution is implemented.

    Operates in the same **warn-mode** as the module-level functions: invalid
    transitions are logged but still applied, because the exchange is the
    authoritative source of truth.

    Args:
        initial_status: Starting status for the order (typically PENDING).
        order_type:     Optional order type; used by ``is_conditional``.

    Example::

        fsm = OrderFSM(OrderStatus.PENDING, order_type=OrderType.STOP_MARKET)
        fsm.transition(OrderStatus.OPEN, order_id="sl-001")
        fsm.transition(OrderStatus.TRIGGERED, order_id="sl-001")
        assert fsm.is_conditional
        assert fsm.can_fill
        assert not fsm.is_terminal
    """

    def __init__(
        self,
        initial_status: OrderStatus = OrderStatus.PENDING,
        order_type: Optional[OrderType] = None,
    ) -> None:
        self.current_status: OrderStatus = initial_status
        self.order_type: Optional[OrderType] = order_type

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def transition(self, new_status: OrderStatus, order_id: str = "") -> bool:
        """Attempt to transition to ``new_status``.

        Validates the transition, logs a warning if invalid, and applies the
        new status regardless (warn-mode).

        Returns:
            ``True`` if the transition was valid, ``False`` if it was not.
        """
        valid = validate_order_transition(self.current_status, new_status, order_id)
        self.current_status = new_status
        return valid

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        """True if the order has reached a final, non-actionable state."""
        return self.current_status in _TERMINAL

    @property
    def is_active(self) -> bool:
        """True if the order is still live (may receive fills or updates)."""
        return self.current_status.is_active()

    @property
    def can_fill(self) -> bool:
        """True if the order is in a state where fill events are expected.

        Covers OPEN and PARTIALLY_FILLED for normal orders, and TRIGGERED for
        conditional orders whose child order is now executing.
        """
        return self.current_status in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.TRIGGERED,
        )

    @property
    def is_conditional(self) -> bool:
        """True if this order was placed as a conditional (SL/TP) order type.

        Based on the ``order_type`` supplied at construction.  Returns False
        when no order type was provided.
        """
        if self.order_type is None:
            return False
        return self.order_type in _CONDITIONAL_ORDER_TYPES

    def __repr__(self) -> str:
        return (
            f"OrderFSM(status={self.current_status.name}, "
            f"order_type={self.order_type.name if self.order_type else None}, "
            f"terminal={self.is_terminal})"
        )
