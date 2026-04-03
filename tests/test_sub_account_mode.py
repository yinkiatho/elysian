"""
Tests for sub-account-per-strategy architecture.

Verifies:
- ShadowBook.sync_from_exchange() correctly loads cash/positions/open orders
- _on_balance_update() maps raw asset → trading symbol via base_to_symbol
- Private bus ORDER_UPDATE reaches the shadow book but NOT another shadow book
- Shared bus BALANCE_UPDATE does NOT reach a sub-account shadow book (on_balance is no-op)
- All strategies use sub-account mode (no shared-account fallback)
- Portfolio.register_shadow_book() and aggregate_nav() work in mixed mode
- StrategyConfig sub_account_api_key/secret fields are parsed
"""

import asyncio
import os
import sys
import pytest
from unittest.mock import MagicMock, AsyncMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from elysian_core.core.shadow_book import ShadowBook
from elysian_core.core.event_bus import EventBus
from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.order import Order
from elysian_core.core.events import EventType, OrderUpdateEvent, BalanceUpdateEvent
from elysian_core.core.portfolio import Portfolio


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_mock_exchange(
    balances: dict = None,
    token_infos: dict = None,
    open_orders: dict = None,
):
    """Build a minimal mock exchange with the fields ShadowBook.sync_from_exchange() reads."""
    exchange = MagicMock()
    exchange._balances = balances or {}
    exchange._token_infos = token_infos or {}
    exchange._open_orders = open_orders or {}

    def get_balance(asset):
        return exchange._balances.get(asset, 0.0)

    def base_asset_to_symbol(asset):
        for sym, info in exchange._token_infos.items():
            if info.get("base_asset") == asset:
                return sym
        return None

    exchange.get_balance.side_effect = get_balance
    exchange.base_asset_to_symbol.side_effect = base_asset_to_symbol
    return exchange


def _make_order(
    order_id: str,
    symbol: str,
    side: Side,
    quantity: float,
    strategy_id: int,
    status: OrderStatus = OrderStatus.OPEN,
    filled_qty: float = 0.0,
):
    order = Order(
        id=order_id,
        symbol=symbol,
        venue=Venue.BINANCE,
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        strategy_id=strategy_id,
        status=status,
        filled_qty=filled_qty,
        avg_fill_price=0.0,
    )
    return order


_PAIR_MAP = {
    "ETHUSDT": ("ETH", "USDT"),
    "BTCUSDT": ("BTC", "USDT"),
}


def _make_order_update_event(order: Order) -> OrderUpdateEvent:
    base, quote = _PAIR_MAP.get(order.symbol, ("UNKNOWN", "USDT"))
    return OrderUpdateEvent(
        symbol=order.symbol,
        base_asset=base,
        quote_asset=quote,
        venue=order.venue,
        order=order,
        timestamp=0,
    )


def _make_balance_update_event(asset: str, delta: float, venue: Venue = Venue.BINANCE):
    return BalanceUpdateEvent(
        asset=asset,
        venue=venue,
        delta=delta,
        new_balance=delta,
        timestamp=0,
    )


# ── sync_from_exchange tests ──────────────────────────────────────────────────

class TestSyncFromExchange:

    def test_cash_loaded_from_stablecoins(self):
        exchange = _make_mock_exchange(
            balances={"USDT": 5000.0, "ETH": 1.5, "BTC": 0.1},
            token_infos={
                "ETHUSDT": {"base_asset": "ETH"},
                "BTCUSDT": {"base_asset": "BTC"},
            },
        )
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb.sync_from_exchange(exchange, feeds={})

        assert sb.cash == pytest.approx(5000.0)
        assert sb._cash_dict["USDT"] == pytest.approx(5000.0)

    def test_positions_loaded_for_non_stablecoins(self):
        exchange = _make_mock_exchange(
            balances={"USDT": 1000.0, "ETH": 2.0, "BTC": 0.05},
            token_infos={
                "ETHUSDT": {"base_asset": "ETH"},
                "BTCUSDT": {"base_asset": "BTC"},
            },
        )
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb.sync_from_exchange(exchange, feeds={})

        assert "ETHUSDT" in sb.positions
        assert sb.positions["ETHUSDT"].quantity == pytest.approx(2.0)
        assert "BTCUSDT" in sb.positions
        assert sb.positions["BTCUSDT"].quantity == pytest.approx(0.05)

    def test_unknown_base_asset_skipped(self):
        exchange = _make_mock_exchange(
            balances={"USDT": 1000.0, "UNKNOWN": 100.0},
            token_infos={"ETHUSDT": {"base_asset": "ETH"}},
        )
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb.sync_from_exchange(exchange, feeds={})

        assert "UNKNOWN" not in sb.positions
        assert len(sb.positions) == 0

    def test_open_orders_and_fill_tracker_loaded(self):
        order = _make_order("ord1", "ETHUSDT", Side.BUY, 1.0, strategy_id=1,
                            status=OrderStatus.PARTIALLY_FILLED, filled_qty=0.5)
        exchange = _make_mock_exchange(
            balances={"USDT": 1000.0},
            token_infos={},
            open_orders={"ETHUSDT": {"ord1": order}},
        )
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb.sync_from_exchange(exchange, feeds={})

        assert "ord1" in sb.active_orders
        assert sb._active_orders["ord1"]._prev_filled == pytest.approx(0.5)

    def test_exchange_stored_on_shadow_book(self):
        exchange = _make_mock_exchange()
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb.sync_from_exchange(exchange, feeds={})

        assert sb._exchange is exchange


# ── sub-account mode property and start() tests ───────────────────────────────

class TestSubAccountMode:

    def test_subscribes_kline_on_shared_bus(self):
        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1)
        sb.start(shared_bus, private_event_bus=private_bus)
        assert sb._on_kline in shared_bus._subscribers.get(EventType.KLINE, [])
        sb.stop()

    def test_stop_unsubscribes_all(self):
        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1)
        sb.start(shared_bus, private_event_bus=private_bus)
        sb.stop()

        assert sb._on_order_update not in private_bus._subscribers.get(EventType.ORDER_UPDATE, [])
        assert sb._on_balance_update not in private_bus._subscribers.get(EventType.BALANCE_UPDATE, [])
        assert sb._on_kline not in shared_bus._subscribers.get(EventType.KLINE, [])
        assert sb._event_bus is None
        assert sb._private_event_bus is None


# ── Event isolation tests ─────────────────────────────────────────────────────

class TestEventIsolation:

    def test_order_update_on_private_bus_reaches_sub_account_shadow_book(self):
        """Fill event on private bus is processed by the sub-account ShadowBook."""
        shared_bus = EventBus()
        private_bus = EventBus()

        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 1000.0
        sb.start(shared_bus, private_event_bus=private_bus)
        # Strategy class subscribes ORDER_UPDATE on the private bus for us
        private_bus.subscribe(EventType.ORDER_UPDATE, sb._on_order_update)

        order = _make_order("ord1", "ETHUSDT", Side.BUY, 1.0, strategy_id=1,
                            status=OrderStatus.FILLED, filled_qty=1.0)
        order.avg_fill_price = 100.0
        event = _make_order_update_event(order)

        asyncio.run(private_bus.publish(event))

        assert "ETHUSDT" in sb.positions
        assert sb.positions["ETHUSDT"].quantity == pytest.approx(1.0)
        sb.stop()

    def test_order_update_on_private_bus_does_not_reach_other_sub_account(self):
        """Sub-account ShadowBook 1's fills on its private bus don't affect ShadowBook 2."""
        shared_bus = EventBus()
        private_bus_1 = EventBus()
        private_bus_2 = EventBus()

        sb1 = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb1._cash = 1000.0
        sb1.start(shared_bus, private_event_bus=private_bus_1)
        private_bus_1.subscribe(EventType.ORDER_UPDATE, sb1._on_order_update)

        sb2 = ShadowBook(strategy_id=2, venue=Venue.BINANCE)
        sb2._cash = 1000.0
        sb2.start(shared_bus, private_event_bus=private_bus_2)
        private_bus_2.subscribe(EventType.ORDER_UPDATE, sb2._on_order_update)

        # Publish fill to strategy 1's private bus only
        order = _make_order("ord1", "ETHUSDT", Side.BUY, 1.0, strategy_id=1,
                            status=OrderStatus.FILLED, filled_qty=1.0)
        order.avg_fill_price = 100.0
        event = _make_order_update_event(order)

        asyncio.run(private_bus_1.publish(event))

        # sb1 got the fill
        assert "ETHUSDT" in sb1.positions
        # sb2 did NOT get the fill (different private bus)
        assert "ETHUSDT" not in sb2.positions

        sb1.stop()
        sb2.stop()

    def test_on_balance_noop_in_sub_account_mode(self):
        """on_balance() is a no-op in sub-account mode (handled by private bus subscription)."""
        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 1000.0
        sb.start(shared_bus, private_event_bus=private_bus)

        event = _make_balance_update_event("USDT", 500.0)
        sb.on_balance(event)  # should be no-op

        assert sb.cash == pytest.approx(1000.0)  # unchanged
        sb.stop()

    def test_shared_bus_balance_update_does_not_reach_sub_account_shadow_book(self):
        """BALANCE_UPDATE published on shared bus should not change sub-account ShadowBook state."""
        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 1000.0
        sb.start(shared_bus, private_event_bus=private_bus)

        # Publish BALANCE_UPDATE on the SHARED bus — should not be picked up by sub-account sb
        event = _make_balance_update_event("USDT", 999.0)
        asyncio.run(shared_bus.publish(event))

        assert sb.cash == pytest.approx(1000.0)  # unchanged
        sb.stop()


# ── _on_balance_update asset-to-symbol mapping ───────────────────────────────

class TestBalanceUpdateMapping:

    def test_stablecoin_delta_updates_cash(self):
        """BALANCE_UPDATE for USDT increments _cash and _cash_dict."""
        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 1000.0
        sb._cash_dict = {"USDT": 1000.0}
        sb.start(shared_bus, private_event_bus=private_bus)

        event = _make_balance_update_event("USDT", 500.0)
        asyncio.run(sb._on_balance_update(event))

        assert sb._cash_dict["USDT"] == pytest.approx(1500.0)
        assert sb.cash == pytest.approx(1500.0)
        sb.stop()

    def test_raw_asset_mapped_to_symbol(self):
        """BALANCE_UPDATE for 'ETH' maps to position keyed as 'ETHUSDT'."""
        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 1000.0

        # Wire exchange so base_to_symbol lookup works
        exchange = _make_mock_exchange(
            token_infos={"ETHUSDT": {"base_asset": "ETH"}},
        )
        sb._exchange = exchange
        sb.start(shared_bus, private_event_bus=private_bus)

        event = _make_balance_update_event("ETH", 2.0)
        asyncio.run(sb._on_balance_update(event))

        assert "ETHUSDT" in sb.positions
        assert sb.positions["ETHUSDT"].quantity == pytest.approx(2.0)
        sb.stop()

    def test_existing_position_quantity_updated(self):
        """Second BALANCE_UPDATE for same asset increments existing position."""
        from elysian_core.core.position import Position

        shared_bus = EventBus()
        private_bus = EventBus()
        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 1000.0
        sb._positions["ETHUSDT"] = Position(symbol="ETHUSDT", venue=Venue.BINANCE, quantity=1.0)

        exchange = _make_mock_exchange(
            token_infos={"ETHUSDT": {"base_asset": "ETH"}},
        )
        sb._exchange = exchange
        sb.start(shared_bus, private_event_bus=private_bus)

        event = _make_balance_update_event("ETH", 0.5)
        asyncio.run(sb._on_balance_update(event))

        assert sb.positions["ETHUSDT"].quantity == pytest.approx(1.5)
        sb.stop()


# ── Portfolio aggregation tests ───────────────────────────────────────────────

class TestPortfolioAggregation:

    def _make_portfolio(self, nav: float = 0.0):
        portfolio = MagicMock(spec=Portfolio)
        portfolio._nav = nav
        portfolio._cash = nav
        portfolio._shadow_books = {}
        return portfolio

    def test_register_shadow_book_stores_by_strategy_id(self):
        portfolio = Portfolio(venue=Venue.BINANCE)

        sb = ShadowBook(strategy_id=42, venue=Venue.BINANCE)
        portfolio.register_shadow_book(sb)

        assert 42 in portfolio._shadow_books
        assert portfolio._shadow_books[42] is sb

    def test_aggregate_nav_sums_shadow_book_navs(self):
        """aggregate_nav() = sum of registered shadow book NAVs."""
        portfolio = Portfolio(venue=Venue.BINANCE)

        sb1 = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb1._cash = 3000.0
        sb1._refresh_derived()

        sb2 = ShadowBook(strategy_id=2, venue=Venue.BINANCE)
        sb2._cash = 2000.0
        sb2._refresh_derived()

        portfolio.register_shadow_book(sb1)
        portfolio.register_shadow_book(sb2)

        assert portfolio.aggregate_nav() == pytest.approx(3000.0 + 2000.0)

    def test_aggregate_nav_no_sub_accounts_returns_zero(self):
        portfolio = Portfolio(venue=Venue.BINANCE)
        assert portfolio.aggregate_nav() == pytest.approx(0.0)

    def test_aggregate_cash_sums_shadow_books(self):
        portfolio = Portfolio(venue=Venue.BINANCE)

        sb = ShadowBook(strategy_id=1, venue=Venue.BINANCE)
        sb._cash = 500.0
        portfolio.register_shadow_book(sb)

        assert portfolio.aggregate_cash() == pytest.approx(500.0)


# ── StrategyConfig sub-account fields ─────────────────────────────────────────

class TestStrategyConfigSubAccount:

    def test_default_sub_account_fields_empty(self):
        from elysian_core.config.app_config import StrategyConfig
        sc = StrategyConfig()
        assert sc.sub_account_api_key == ""
        assert sc.sub_account_api_secret == ""

    def test_sub_account_fields_populated(self):
        from elysian_core.config.app_config import StrategyConfig
        sc = StrategyConfig(
            strategy_id=1,
            sub_account_api_key="test_key",
            sub_account_api_secret="test_secret",
        )
        assert sc.sub_account_api_key == "test_key"
        assert sc.sub_account_api_secret == "test_secret"

    def test_has_sub_account_helper(self):
        from elysian_core.run_strategy import StrategyRunner
        from elysian_core.config.app_config import StrategyConfig

        runner = StrategyRunner(
            trading_config_yaml='elysian_core/config/trading_config.yaml',
        )
        sc_with = StrategyConfig(sub_account_api_key="key123")
        sc_without = StrategyConfig(sub_account_api_key="")

        assert runner._has_sub_account(sc_with) is True
        assert runner._has_sub_account(sc_without) is False
