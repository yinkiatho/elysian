"""
Tests for MarginStrategy multi-sub-account base class.

Covers:
- start() creates one RebalanceFSM per pipeline with correct sub_account_key
- start() subscribes to shared bus and per-pipeline private buses
- stop() unsubscribes all buses and stops all shadow books
- _dispatch_order() routes to correct pipeline's ShadowBook by sub-account key
- _dispatch_balance() routes to correct pipeline's ShadowBook by sub-account key
- _dispatch_kline() filters by symbol union across all pipelines
- request_rebalance(sub_account_key) triggers only that pipeline's FSM
- request_rebalance() (no key) triggers all FSMs concurrently
- _shadow_book property returns first pipeline's book (backward compat)
- shadow_books property maps all keys → shadow books
- get_pipeline() returns the correct pipeline or None
- Unknown sub-account key in dispatch logs warning, does not raise
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from elysian_core.core.enums import AssetType, Venue, StrategyState
from elysian_core.core.events import (
    EventType, OrderUpdateEvent, BalanceUpdateEvent, KlineEvent,
)
from elysian_core.core.sub_account_pipeline import SubAccountKey, SubAccountPipeline
from elysian_core.strategy.margin_strategy import MarginStrategy


# ─── Fixtures ─────────────────────────────────────────────────────────────────

STRATEGY_ID = 1
SPOT_KEY = SubAccountKey(strategy_id=STRATEGY_ID, asset_type=AssetType.SPOT, venue=Venue.BINANCE, sub_account_id=0)
MARGIN_KEY = SubAccountKey(strategy_id=STRATEGY_ID, asset_type=AssetType.MARGIN, venue=Venue.BINANCE, sub_account_id=1)


def _make_event_bus():
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    bus.publish = AsyncMock()
    return bus


def _make_shadow_book():
    sb = MagicMock()
    sb.stop = MagicMock()
    sb._on_order_update = AsyncMock()
    sb._on_balance_update = AsyncMock()
    sb.nav = 1000.0
    return sb


def _make_pipeline(key: SubAccountKey, symbols=None):
    return SubAccountPipeline(
        key=key,
        exchange=MagicMock(),
        private_event_bus=_make_event_bus(),
        shadow_book=_make_shadow_book(),
        optimizer=MagicMock(),
        execution_engine=MagicMock(),
        rebalance_fsm=None,
        symbols=symbols or [],
    )


def _make_strategy(shared_bus=None) -> MarginStrategy:
    """Build a minimal MarginStrategy with mocked infrastructure."""
    strategy = MarginStrategy(
        strategy_name="test_margin_strategy",
        strategy_id=STRATEGY_ID,
    )
    strategy._shared_event_bus = shared_bus or _make_event_bus()
    strategy.cfg = MagicMock()
    strategy.strategy_config = MagicMock()
    strategy.strategy_config.symbols = []
    return strategy


def _inject_pipelines(strategy: MarginStrategy, spot_symbols=None, margin_symbols=None):
    """Inject SPOT + MARGIN pipelines into strategy._pipelines."""
    spot_pipeline = _make_pipeline(SPOT_KEY, symbols=spot_symbols or ["ETHUSDT"])
    margin_pipeline = _make_pipeline(MARGIN_KEY, symbols=margin_symbols or ["BTCUSDT"])
    strategy._pipelines[SPOT_KEY] = spot_pipeline
    strategy._pipelines[MARGIN_KEY] = margin_pipeline
    return spot_pipeline, margin_pipeline


# ─── Lifecycle ────────────────────────────────────────────────────────────────

class TestMarginStrategyStart:
    def test_start_creates_fsm_per_pipeline(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM") as MockFSM:
            mock_fsm = MagicMock()
            MockFSM.return_value = mock_fsm
            asyncio.run(strategy.start())

        assert MockFSM.call_count == 2

    def test_start_fsm_receives_correct_sub_account_keys(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)

        created_keys = []
        def capture_fsm(**kwargs):
            created_keys.append(kwargs.get("sub_account_key"))
            return MagicMock()

        with patch(
            "elysian_core.strategy.margin_strategy.RebalanceFSM",
            side_effect=lambda *a, **kw: capture_fsm(**kw),
        ):
            asyncio.run(strategy.start())

        assert SPOT_KEY in created_keys
        assert MARGIN_KEY in created_keys

    def test_start_assigns_fsm_to_pipelines(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM") as MockFSM:
            mock_fsm = MagicMock()
            MockFSM.return_value = mock_fsm
            asyncio.run(strategy.start())

        assert spot_p.rebalance_fsm is not None
        assert margin_p.rebalance_fsm is not None

    def test_start_subscribes_shared_bus(self):
        shared_bus = _make_event_bus()
        strategy = _make_strategy(shared_bus)
        _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())

        shared_bus.subscribe.assert_any_call(EventType.KLINE, strategy._dispatch_kline)
        shared_bus.subscribe.assert_any_call(EventType.ORDERBOOK_UPDATE, strategy._dispatch_ob)

    def test_start_subscribes_all_private_buses(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())

        for p in [spot_p, margin_p]:
            p.private_event_bus.subscribe.assert_any_call(
                EventType.ORDER_UPDATE, strategy._dispatch_order
            )
            p.private_event_bus.subscribe.assert_any_call(
                EventType.BALANCE_UPDATE, strategy._dispatch_balance
            )
            p.private_event_bus.subscribe.assert_any_call(
                EventType.REBALANCE_COMPLETE, strategy._dispatch_rebalance
            )

    def test_start_collects_symbols_union(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy, spot_symbols=["ETHUSDT"], margin_symbols=["BTCUSDT"])

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())

        assert "ETHUSDT" in strategy._symbols
        assert "BTCUSDT" in strategy._symbols

    def test_start_collects_venues(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())

        assert Venue.BINANCE in strategy.venues

    def test_start_sets_state_to_ready(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())

        assert strategy._state == StrategyState.READY

    def test_start_idempotent_if_not_created(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)

        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())
            # Second call should be a no-op
            asyncio.run(strategy.start())

        assert strategy._state == StrategyState.READY


class TestMarginStrategyStop:
    def _started_strategy(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)
        with patch("elysian_core.strategy.margin_strategy.RebalanceFSM"):
            asyncio.run(strategy.start())
        return strategy, spot_p, margin_p

    def test_stop_unsubscribes_shared_bus(self):
        strategy, spot_p, margin_p = self._started_strategy()
        asyncio.run(strategy.stop())

        strategy._shared_event_bus.unsubscribe.assert_any_call(
            EventType.KLINE, strategy._dispatch_kline
        )
        strategy._shared_event_bus.unsubscribe.assert_any_call(
            EventType.ORDERBOOK_UPDATE, strategy._dispatch_ob
        )

    def test_stop_unsubscribes_private_buses(self):
        strategy, spot_p, margin_p = self._started_strategy()
        asyncio.run(strategy.stop())

        for p in [spot_p, margin_p]:
            p.private_event_bus.unsubscribe.assert_any_call(
                EventType.ORDER_UPDATE, strategy._dispatch_order
            )
            p.private_event_bus.unsubscribe.assert_any_call(
                EventType.BALANCE_UPDATE, strategy._dispatch_balance
            )

    def test_stop_calls_shadow_book_stop_for_all_pipelines(self):
        strategy, spot_p, margin_p = self._started_strategy()
        asyncio.run(strategy.stop())

        spot_p.shadow_book.stop.assert_called_once()
        margin_p.shadow_book.stop.assert_called_once()

    def test_stop_sets_state_to_stopped(self):
        strategy, _, _ = self._started_strategy()
        asyncio.run(strategy.stop())
        assert strategy._state == StrategyState.STOPPED

    def test_stop_idempotent(self):
        strategy, spot_p, _ = self._started_strategy()
        asyncio.run(strategy.stop())
        asyncio.run(strategy.stop())  # second stop is a no-op
        spot_p.shadow_book.stop.assert_called_once()


# ─── Event dispatch ───────────────────────────────────────────────────────────

def _make_order_event(sub_account_id: int, asset_type: AssetType = AssetType.SPOT):
    """Build a minimal OrderUpdateEvent with routing fields."""
    from elysian_core.core.events import OrderUpdateEvent
    from elysian_core.core.order import Order, OrderStatus
    order = MagicMock(spec=Order)
    evt = MagicMock(spec=OrderUpdateEvent)
    evt.asset_type = asset_type
    evt.venue = Venue.BINANCE
    evt.sub_account_id = sub_account_id
    evt.order = order
    return evt


def _make_balance_event(sub_account_id: int, asset_type: AssetType = AssetType.SPOT):
    """Build a minimal BalanceUpdateEvent with routing fields."""
    from elysian_core.core.events import BalanceUpdateEvent
    evt = MagicMock(spec=BalanceUpdateEvent)
    evt.asset_type = asset_type
    evt.venue = Venue.BINANCE
    evt.sub_account_id = sub_account_id
    return evt


class TestDispatchOrder:
    def _setup(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)
        strategy._state = StrategyState.RUNNING  # bypass start()
        return strategy, spot_p, margin_p

    def test_spot_order_routed_to_spot_shadow_book(self):
        strategy, spot_p, margin_p = self._setup()
        event = _make_order_event(sub_account_id=0, asset_type=AssetType.SPOT)

        asyncio.run(strategy._dispatch_order(event))

        spot_p.shadow_book._on_order_update.assert_awaited_once_with(event)
        margin_p.shadow_book._on_order_update.assert_not_awaited()

    def test_margin_order_routed_to_margin_shadow_book(self):
        strategy, spot_p, margin_p = self._setup()
        event = _make_order_event(sub_account_id=1, asset_type=AssetType.MARGIN)

        asyncio.run(strategy._dispatch_order(event))

        margin_p.shadow_book._on_order_update.assert_awaited_once_with(event)
        spot_p.shadow_book._on_order_update.assert_not_awaited()

    def test_unknown_sub_account_key_logs_warning(self, caplog):
        strategy, _, _ = self._setup()
        event = _make_order_event(sub_account_id=99, asset_type=AssetType.SPOT)

        import logging
        with caplog.at_level(logging.WARNING):
            asyncio.run(strategy._dispatch_order(event))

        assert any("unknown sub-account" in r.message.lower() for r in caplog.records)


class TestDispatchBalance:
    def _setup(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)
        strategy._state = StrategyState.RUNNING
        return strategy, spot_p, margin_p

    def test_spot_balance_routed_to_spot_shadow_book(self):
        strategy, spot_p, margin_p = self._setup()
        event = _make_balance_event(sub_account_id=0, asset_type=AssetType.SPOT)

        asyncio.run(strategy._dispatch_balance(event))

        spot_p.shadow_book._on_balance_update.assert_awaited_once_with(event)
        margin_p.shadow_book._on_balance_update.assert_not_awaited()

    def test_margin_balance_routed_to_margin_shadow_book(self):
        strategy, spot_p, margin_p = self._setup()
        event = _make_balance_event(sub_account_id=1, asset_type=AssetType.MARGIN)

        asyncio.run(strategy._dispatch_balance(event))

        margin_p.shadow_book._on_balance_update.assert_awaited_once_with(event)
        spot_p.shadow_book._on_balance_update.assert_not_awaited()

    def test_unknown_sub_account_logs_warning(self, caplog):
        strategy, _, _ = self._setup()
        event = _make_balance_event(sub_account_id=99, asset_type=AssetType.MARGIN)

        import logging
        with caplog.at_level(logging.WARNING):
            asyncio.run(strategy._dispatch_balance(event))

        assert any("unknown sub-account" in r.message.lower() for r in caplog.records)


class TestDispatchKline:
    def _make_kline_event(self, symbol: str, venue=Venue.BINANCE):
        evt = MagicMock(spec=KlineEvent)
        evt.symbol = symbol
        evt.venue = venue
        return evt

    def test_known_symbol_calls_on_kline(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy, spot_symbols=["ETHUSDT"], margin_symbols=["BTCUSDT"])
        strategy._symbols = {"ETHUSDT", "BTCUSDT"}
        strategy.venues = frozenset({Venue.BINANCE})
        strategy._state = StrategyState.RUNNING

        called = []
        async def on_kline(evt):
            called.append(evt)
        strategy.on_kline = on_kline

        evt = self._make_kline_event("ETHUSDT")
        asyncio.run(strategy._dispatch_kline(evt))
        assert len(called) == 1

    def test_unknown_symbol_skipped(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)
        strategy._symbols = {"ETHUSDT"}
        strategy.venues = frozenset({Venue.BINANCE})
        strategy._state = StrategyState.RUNNING

        called = []
        async def on_kline(evt):
            called.append(evt)
        strategy.on_kline = on_kline

        evt = self._make_kline_event("SOLUSDT")
        asyncio.run(strategy._dispatch_kline(evt))
        assert len(called) == 0

    def test_wrong_venue_skipped(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)
        strategy._symbols = {"ETHUSDT"}
        strategy.venues = frozenset({Venue.BINANCE})
        strategy._state = StrategyState.RUNNING

        called = []
        async def on_kline(evt):
            called.append(evt)
        strategy.on_kline = on_kline

        evt = self._make_kline_event("ETHUSDT", venue=Venue.ASTER)
        asyncio.run(strategy._dispatch_kline(evt))
        assert len(called) == 0


# ─── Rebalance API ────────────────────────────────────────────────────────────

class TestRequestRebalance:
    def _strategy_with_mock_fsms(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)

        spot_fsm = MagicMock()
        spot_fsm.request = AsyncMock(return_value=True)
        margin_fsm = MagicMock()
        margin_fsm.request = AsyncMock(return_value=True)

        spot_p.rebalance_fsm = spot_fsm
        margin_p.rebalance_fsm = margin_fsm
        strategy._state = StrategyState.RUNNING

        return strategy, spot_p, margin_p, spot_fsm, margin_fsm

    def test_specific_key_triggers_only_that_fsm(self):
        strategy, spot_p, margin_p, spot_fsm, margin_fsm = self._strategy_with_mock_fsms()

        result = asyncio.run(strategy.request_rebalance(sub_account_key=SPOT_KEY))

        assert result is True
        spot_fsm.request.assert_awaited_once()
        margin_fsm.request.assert_not_awaited()

    def test_no_key_triggers_all_fsms(self):
        strategy, spot_p, margin_p, spot_fsm, margin_fsm = self._strategy_with_mock_fsms()

        result = asyncio.run(strategy.request_rebalance())

        assert result is True
        spot_fsm.request.assert_awaited_once()
        margin_fsm.request.assert_awaited_once()

    def test_returns_false_if_any_fsm_busy(self):
        strategy, spot_p, margin_p, spot_fsm, margin_fsm = self._strategy_with_mock_fsms()
        margin_fsm.request = AsyncMock(return_value=False)

        result = asyncio.run(strategy.request_rebalance())

        assert result is False

    def test_unknown_key_returns_false(self):
        strategy, _, _, _, _ = self._strategy_with_mock_fsms()
        unknown_key = SubAccountKey(
            strategy_id=STRATEGY_ID, asset_type=AssetType.PERPETUAL,
            venue=Venue.BINANCE, sub_account_id=99
        )
        result = asyncio.run(strategy.request_rebalance(sub_account_key=unknown_key))
        assert result is False

    def test_no_pipelines_returns_false(self):
        strategy = _make_strategy()
        result = asyncio.run(strategy.request_rebalance())
        assert result is False


# ─── Properties ───────────────────────────────────────────────────────────────

class TestProperties:
    def test_shadow_book_returns_first_pipeline_book(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)
        first_book = next(iter(strategy._pipelines.values())).shadow_book
        assert strategy._shadow_book is first_book

    def test_shadow_book_returns_none_when_no_pipelines(self):
        strategy = _make_strategy()
        assert strategy._shadow_book is None

    def test_shadow_book_setter_is_noop(self):
        strategy = _make_strategy()
        spot_p, _ = _inject_pipelines(strategy)
        original = spot_p.shadow_book
        strategy._shadow_book = object()   # should be ignored
        assert strategy._shadow_book is original

    def test_shadow_books_maps_all_keys(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)
        books = strategy.shadow_books
        assert books[SPOT_KEY] is spot_p.shadow_book
        assert books[MARGIN_KEY] is margin_p.shadow_book

    def test_get_pipeline_returns_correct_pipeline(self):
        strategy = _make_strategy()
        spot_p, margin_p = _inject_pipelines(strategy)
        assert strategy.get_pipeline(SPOT_KEY) is spot_p
        assert strategy.get_pipeline(MARGIN_KEY) is margin_p

    def test_get_pipeline_returns_none_for_unknown(self):
        strategy = _make_strategy()
        _inject_pipelines(strategy)
        unknown = SubAccountKey(99, AssetType.SPOT, Venue.BINANCE)
        assert strategy.get_pipeline(unknown) is None


# ─── SubAccountPipeline.symbols field ────────────────────────────────────────

class TestSubAccountPipelineSymbols:
    def test_symbols_defaults_to_empty_list(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.SPOT, venue=Venue.BINANCE)
        p = SubAccountPipeline(
            key=key,
            exchange=object(),
            private_event_bus=object(),
            shadow_book=object(),
            optimizer=object(),
            execution_engine=object(),
        )
        assert p.symbols == []

    def test_symbols_can_be_set_at_construction(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        p = SubAccountPipeline(
            key=key,
            exchange=object(),
            private_event_bus=object(),
            shadow_book=object(),
            optimizer=object(),
            execution_engine=object(),
            symbols=["BTCUSDT", "ETHUSDT"],
        )
        assert p.symbols == ["BTCUSDT", "ETHUSDT"]
