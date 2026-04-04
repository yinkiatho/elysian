"""Tests for order_fsm: validate_order_transition, is_terminal, OrderFSM class."""

import pytest
from elysian_core.core.enums import OrderStatus, OrderType
from elysian_core.core.order_fsm import validate_order_transition, is_terminal, OrderFSM


class TestValidateOrderTransition:

    @pytest.mark.parametrize("from_status,to_status", [
        (OrderStatus.PENDING, OrderStatus.OPEN),
        (OrderStatus.PENDING, OrderStatus.REJECTED),
        (OrderStatus.PENDING, OrderStatus.CANCELLED),
        (OrderStatus.PENDING, OrderStatus.FILLED),
        (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.OPEN, OrderStatus.FILLED),
        (OrderStatus.OPEN, OrderStatus.CANCELLED),
        (OrderStatus.OPEN, OrderStatus.EXPIRED),
        (OrderStatus.OPEN, OrderStatus.TRIGGERED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.EXPIRED),
        (OrderStatus.TRIGGERED, OrderStatus.OPEN),
        (OrderStatus.TRIGGERED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.TRIGGERED, OrderStatus.FILLED),
        (OrderStatus.TRIGGERED, OrderStatus.CANCELLED),
        (OrderStatus.TRIGGERED, OrderStatus.EXPIRED),
    ])
    def test_valid_transitions(self, from_status, to_status):
        assert validate_order_transition(from_status, to_status) is True

    @pytest.mark.parametrize("from_status,to_status", [
        (OrderStatus.FILLED, OrderStatus.OPEN),
        (OrderStatus.CANCELLED, OrderStatus.OPEN),
        (OrderStatus.REJECTED, OrderStatus.OPEN),
        (OrderStatus.EXPIRED, OrderStatus.OPEN),
        (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED),
    ])
    def test_invalid_transitions_return_false(self, from_status, to_status):
        assert validate_order_transition(from_status, to_status) is False

    def test_same_state_transition_is_valid(self):
        assert validate_order_transition(OrderStatus.OPEN, OrderStatus.OPEN) is True
        assert validate_order_transition(OrderStatus.FILLED, OrderStatus.FILLED) is True


class TestIsTerminal:

    @pytest.mark.parametrize("status,expected", [
        (OrderStatus.FILLED, True),
        (OrderStatus.CANCELLED, True),
        (OrderStatus.REJECTED, True),
        (OrderStatus.EXPIRED, True),
        (OrderStatus.PENDING, False),
        (OrderStatus.OPEN, False),
        (OrderStatus.PARTIALLY_FILLED, False),
        (OrderStatus.TRIGGERED, False),
    ])
    def test_is_terminal(self, status, expected):
        assert is_terminal(status) == expected


class TestOrderFSM:

    def test_initial_status(self):
        fsm = OrderFSM(OrderStatus.PENDING)
        assert fsm.current_status == OrderStatus.PENDING

    def test_transition_valid(self):
        fsm = OrderFSM(OrderStatus.PENDING)
        result = fsm.transition(OrderStatus.OPEN, order_id="t1")
        assert result is True
        assert fsm.current_status == OrderStatus.OPEN

    def test_transition_invalid_still_applies(self):
        fsm = OrderFSM(OrderStatus.FILLED)
        result = fsm.transition(OrderStatus.OPEN, order_id="t2")
        assert result is False
        assert fsm.current_status == OrderStatus.OPEN

    def test_is_terminal_property(self):
        fsm = OrderFSM(OrderStatus.FILLED)
        assert fsm.is_terminal is True

    def test_is_active_property(self):
        fsm = OrderFSM(OrderStatus.OPEN)
        assert fsm.is_active is True

    def test_can_fill(self):
        assert OrderFSM(OrderStatus.OPEN).can_fill is True
        assert OrderFSM(OrderStatus.PARTIALLY_FILLED).can_fill is True
        assert OrderFSM(OrderStatus.TRIGGERED).can_fill is True
        assert OrderFSM(OrderStatus.PENDING).can_fill is False
        assert OrderFSM(OrderStatus.FILLED).can_fill is False

    def test_is_conditional(self):
        fsm = OrderFSM(OrderStatus.PENDING, order_type=OrderType.STOP_MARKET)
        assert fsm.is_conditional is True

    def test_is_not_conditional_market(self):
        fsm = OrderFSM(OrderStatus.PENDING, order_type=OrderType.MARKET)
        assert fsm.is_conditional is False

    def test_is_not_conditional_none(self):
        fsm = OrderFSM(OrderStatus.PENDING, order_type=None)
        assert fsm.is_conditional is False

    def test_take_profit_is_conditional(self):
        fsm = OrderFSM(OrderStatus.PENDING, order_type=OrderType.TAKE_PROFIT_MARKET)
        assert fsm.is_conditional is True

    def test_repr(self):
        fsm = OrderFSM(OrderStatus.OPEN, order_type=OrderType.LIMIT)
        r = repr(fsm)
        assert "OPEN" in r
        assert "LIMIT" in r

    def test_full_lifecycle(self):
        fsm = OrderFSM(OrderStatus.PENDING)
        fsm.transition(OrderStatus.OPEN)
        fsm.transition(OrderStatus.PARTIALLY_FILLED)
        fsm.transition(OrderStatus.FILLED)
        assert fsm.is_terminal is True
        assert fsm.is_active is False
