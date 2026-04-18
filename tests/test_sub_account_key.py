"""
Tests for SubAccountKey and SubAccountPipeline.

Covers:
- SubAccountKey hashability and use as dict key
- SubAccountKey equality / inequality
- SubAccountKey __str__ format
- SubAccountPipeline creation and field access
"""

import pytest
from elysian_core.core.sub_account_pipeline import SubAccountKey, SubAccountPipeline
from elysian_core.core.enums import AssetType, Venue


# ─── SubAccountKey ────────────────────────────────────────────────────────────

class TestSubAccountKey:
    def test_basic_creation(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        assert key.strategy_id == 1
        assert key.asset_type == AssetType.SPOT
        assert key.venue == Venue.BINANCE
        assert key.sub_account_id == 0  # default

    def test_custom_sub_account_id(self):
        key = SubAccountKey(strategy_id=2, asset_type=AssetType.MARGIN, venue=Venue.BINANCE, sub_account_id=3)
        assert key.sub_account_id == 3

    def test_hashable_usable_as_dict_key(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        d = {key: "value"}
        assert d[key] == "value"

    def test_hashable_usable_in_set(self):
        k1 = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        k2 = SubAccountKey(strategy_id=2, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        s = {k1, k2}
        assert len(s) == 2

    def test_equality_same_fields(self):
        k1 = SubAccountKey(strategy_id=5, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        k2 = SubAccountKey(strategy_id=5, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        assert k1 == k2
        assert hash(k1) == hash(k2)

    def test_inequality_different_strategy_id(self):
        k1 = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        k2 = SubAccountKey(strategy_id=2, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        assert k1 != k2

    def test_inequality_different_asset_type(self):
        k1 = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        k2 = SubAccountKey(strategy_id=1, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        assert k1 != k2

    def test_inequality_different_sub_account_id(self):
        k1 = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE, sub_account_id=0)
        k2 = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE, sub_account_id=1)
        assert k1 != k2

    def test_str_format(self):
        key = SubAccountKey(strategy_id=7, asset_type=AssetType.MARGIN, venue=Venue.BINANCE, sub_account_id=2)
        assert str(key) == "s7:Margin:Binance:2"

    def test_str_default_sub_account_id(self):
        key = SubAccountKey(strategy_id=3, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        assert str(key) == "s3:Spot:Binance:0"

    def test_frozen_immutable(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        with pytest.raises((AttributeError, TypeError)):
            key.strategy_id = 99  # type: ignore

    def test_multiple_keys_as_dict(self):
        """Simulate routing map: key → pipeline."""
        spot_key = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        margin_key = SubAccountKey(strategy_id=1, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        routing = {spot_key: "spot_pipeline", margin_key: "margin_pipeline"}
        assert routing[spot_key] == "spot_pipeline"
        assert routing[margin_key] == "margin_pipeline"
        assert len(routing) == 2


# ─── SubAccountPipeline ───────────────────────────────────────────────────────

class TestSubAccountPipeline:
    def _make_pipeline(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        # Use plain objects as stand-ins — we only test the dataclass structure.
        return SubAccountPipeline(
            key=key,
            exchange=object(),
            private_event_bus=object(),
            shadow_book=object(),
            optimizer=object(),
            execution_engine=object(),
        )

    def test_fields_accessible(self):
        p = self._make_pipeline()
        assert p.key.strategy_id == 1
        assert p.rebalance_fsm is None  # optional, defaults to None

    def test_rebalance_fsm_can_be_set(self):
        p = self._make_pipeline()
        sentinel = object()
        p.rebalance_fsm = sentinel
        assert p.rebalance_fsm is sentinel

    def test_mutable_fields(self):
        p = self._make_pipeline()
        new_exchange = object()
        p.exchange = new_exchange
        assert p.exchange is new_exchange
