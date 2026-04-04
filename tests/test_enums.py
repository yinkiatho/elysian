"""Tests for enums: Side, OrderStatus, OrderType, and Binance mapping dicts."""

import pytest
from elysian_core.core.enums import (
    Side, OrderStatus, OrderType, Venue, Chain,
    AssetType, RebalanceState, StrategyState, RunnerState,
    _BINANCE_SIDE, _BINANCE_STATUS,
)


class TestSide:

    def test_opposite_buy(self):
        assert Side.BUY.opposite() == Side.SELL

    def test_opposite_sell(self):
        assert Side.SELL.opposite() == Side.BUY

    def test_opposite_is_involution(self):
        for side in Side:
            assert side.opposite().opposite() == side


class TestOrderStatus:

    @pytest.mark.parametrize("status,expected", [
        (OrderStatus.PENDING, True),
        (OrderStatus.OPEN, True),
        (OrderStatus.PARTIALLY_FILLED, True),
        (OrderStatus.TRIGGERED, True),
        (OrderStatus.FILLED, False),
        (OrderStatus.CANCELLED, False),
        (OrderStatus.REJECTED, False),
        (OrderStatus.EXPIRED, False),
    ])
    def test_is_active(self, status, expected):
        assert status.is_active() == expected


class TestBinanceMappings:

    def test_binance_side_buy(self):
        assert _BINANCE_SIDE["BUY"] == Side.BUY

    def test_binance_side_sell(self):
        assert _BINANCE_SIDE["SELL"] == Side.SELL

    @pytest.mark.parametrize("binance_str,expected", [
        ("NEW", OrderStatus.OPEN),
        ("PARTIALLY_FILLED", OrderStatus.PARTIALLY_FILLED),
        ("FILLED", OrderStatus.FILLED),
        ("CANCELED", OrderStatus.CANCELLED),
        ("PENDING_CANCEL", OrderStatus.PENDING),
        ("REJECTED", OrderStatus.REJECTED),
        ("EXPIRED", OrderStatus.EXPIRED),
    ])
    def test_binance_status_mapping(self, binance_str, expected):
        assert _BINANCE_STATUS[binance_str] == expected


class TestChain:

    def test_to_dict(self):
        assert Chain.SUI.to_dict() == {"chain": "sui"}
        assert Chain.ETHEREUM.to_dict() == {"chain": "ethereum"}


class TestEnumValues:

    def test_asset_type_values(self):
        assert AssetType.SPOT.value == "Spot"
        assert AssetType.PERPETUAL.value == "Perpetuals"

    def test_order_type_values(self):
        assert OrderType.LIMIT.value == "Limit"
        assert OrderType.MARKET.value == "Market"
        assert OrderType.STOP_MARKET.value == "StopMarket"

    def test_rebalance_state_enum_members(self):
        states = {s.name for s in RebalanceState}
        assert states == {"IDLE", "COMPUTING", "VALIDATING", "EXECUTING", "COOLDOWN", "SUSPENDED", "ERROR"}

    def test_strategy_state_enum_members(self):
        states = {s.name for s in StrategyState}
        assert states == {"CREATED", "STARTING", "READY", "RUNNING", "STOPPING", "STOPPED", "FAILED"}

    def test_runner_state_enum_members(self):
        states = {s.name for s in RunnerState}
        assert states == {"CREATED", "CONFIGURING", "CONNECTING", "SYNCING", "RUNNING", "STOPPING", "STOPPED", "FAILED"}
