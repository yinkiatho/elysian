"""Tests for OrderBook, Kline, BinanceOrderBook, AsterOrderBook."""

import asyncio
import datetime
import pytest
from sortedcontainers import SortedDict

from elysian_core.core.market_data import OrderBook, Kline, BinanceOrderBook, AsterOrderBook


def _orderbook(**kw) -> OrderBook:
    defaults = dict(
        last_update_id=1, last_timestamp=1000, ticker="BTCUSDT", interval=None,
    )
    defaults.update(kw)
    return OrderBook(**defaults)


class TestOrderBookFromLists:

    def test_from_lists_basic(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="ETHUSDT", interval=None,
            bid_levels=[[3000.0, 1.5], [2999.0, 2.0]],
            ask_levels=[[3001.0, 0.5], [3002.0, 1.0]],
        )
        assert ob.ticker == "ETHUSDT"
        assert len(ob.bid_orders) == 2
        assert len(ob.ask_orders) == 2

    def test_from_lists_empty(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="X", interval=None,
            bid_levels=[], ask_levels=[],
        )
        assert len(ob.bid_orders) == 0
        assert len(ob.ask_orders) == 0


class TestOrderBookBestPrices:

    def test_best_bid_price(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="T", interval=None,
            bid_levels=[[100.0, 1.0], [101.0, 2.0], [99.0, 3.0]],
            ask_levels=[],
        )
        assert ob.best_bid_price == pytest.approx(101.0)
        assert ob.best_bid_amount == pytest.approx(2.0)

    def test_best_ask_price(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="T", interval=None,
            bid_levels=[],
            ask_levels=[[200.0, 1.0], [199.0, 0.5], [201.0, 2.0]],
        )
        assert ob.best_ask_price == pytest.approx(199.0)
        assert ob.best_ask_amount == pytest.approx(0.5)

    def test_empty_book_prices_zero(self):
        ob = _orderbook()
        assert ob.best_bid_price == pytest.approx(0.0)
        assert ob.best_ask_price == pytest.approx(0.0)
        assert ob.best_bid_amount == pytest.approx(0.0)
        assert ob.best_ask_amount == pytest.approx(0.0)


class TestOrderBookDerived:

    def test_mid_price(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="T", interval=None,
            bid_levels=[[100.0, 1.0]],
            ask_levels=[[102.0, 1.0]],
        )
        assert ob.mid_price == pytest.approx(101.0)

    def test_spread(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="T", interval=None,
            bid_levels=[[100.0, 1.0]],
            ask_levels=[[102.0, 1.0]],
        )
        assert ob.spread == pytest.approx(2.0)

    def test_spread_bps(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="T", interval=None,
            bid_levels=[[100.0, 1.0]],
            ask_levels=[[102.0, 1.0]],
        )
        # spread=2, mid=101, bps = 2/101*10000 ≈ 198.02
        assert ob.spread_bps == pytest.approx(2.0 / 101.0 * 10000)

    def test_spread_bps_empty(self):
        ob = _orderbook()
        assert ob.spread_bps == pytest.approx(0.0)


class TestOrderBookApplyUpdate:

    def test_apply_update_add_bid(self):
        ob = _orderbook()
        ob.apply_update("bid", 100.0, 5.0)
        assert ob.bid_orders[100.0] == pytest.approx(5.0)

    def test_apply_update_remove_bid(self):
        ob = _orderbook()
        ob.apply_update("bid", 100.0, 5.0)
        ob.apply_update("bid", 100.0, 0)
        assert 100.0 not in ob.bid_orders

    def test_apply_update_ask(self):
        ob = _orderbook()
        ob.apply_update("ask", 200.0, 3.0)
        assert ob.ask_orders[200.0] == pytest.approx(3.0)

    def test_apply_update_overwrite(self):
        ob = _orderbook()
        ob.apply_update("ask", 200.0, 3.0)
        ob.apply_update("ask", 200.0, 7.0)
        assert ob.ask_orders[200.0] == pytest.approx(7.0)


class TestOrderBookStr:

    def test_str(self):
        ob = OrderBook.from_lists(
            last_update_id=1, last_timestamp=100, ticker="BTCUSDT", interval=None,
            bid_levels=[[50000.0, 1.0]], ask_levels=[[50001.0, 1.0]],
        )
        s = str(ob)
        assert "BTCUSDT" in s
        assert "bid" in s
        assert "ask" in s


class TestBinanceOrderBook:

    def test_apply_update_lists(self):
        ob = BinanceOrderBook(
            last_update_id=1, last_timestamp=100, ticker="BTCUSDT", interval=None,
        )
        asyncio.run(ob.apply_update_lists("bid", [[100.0, 5.0], [101.0, 3.0]]))
        assert len(ob.bid_orders) == 2
        assert ob.bid_orders[100.0] == pytest.approx(5.0)

    def test_apply_update_lists_remove_zero(self):
        ob = BinanceOrderBook(
            last_update_id=1, last_timestamp=100, ticker="BTCUSDT", interval=None,
        )
        asyncio.run(ob.apply_update_lists("ask", [[200.0, 5.0]]))
        asyncio.run(ob.apply_update_lists("ask", [[200.0, 0.0]]))
        assert 200.0 not in ob.ask_orders

    def test_apply_both_updates(self):
        ob = BinanceOrderBook(
            last_update_id=1, last_timestamp=100, ticker="BTCUSDT", interval=None,
        )
        asyncio.run(ob.apply_both_updates(
            last_timestamp=200, new_update_id=2,
            bid_levels=[[100.0, 5.0]], ask_levels=[[200.0, 3.0]],
        ))
        assert ob.last_update_id == 2
        assert ob.last_timestamp == 200
        assert len(ob.bid_orders) == 1
        assert len(ob.ask_orders) == 1

    def test_apply_both_updates_none_levels(self):
        ob = BinanceOrderBook(
            last_update_id=1, last_timestamp=100, ticker="BTCUSDT", interval=None,
        )
        asyncio.run(ob.apply_both_updates(
            last_timestamp=200, new_update_id=2,
            bid_levels=None, ask_levels=None,
        ))
        assert ob.last_update_id == 2


class TestAsterOrderBook:

    def test_apply_both_updates(self):
        ob = AsterOrderBook(
            last_update_id=1, last_timestamp=100, ticker="ETHUSDT", interval=None,
        )
        asyncio.run(ob.apply_both_updates(
            last_timestamp=300, new_update_id=3,
            bid_levels=[[50.0, 10.0]], ask_levels=[[51.0, 8.0]],
        ))
        assert ob.last_update_id == 3
        assert ob.bid_orders[50.0] == pytest.approx(10.0)
        assert ob.ask_orders[51.0] == pytest.approx(8.0)


class TestKline:

    def test_kline_creation(self):
        now = datetime.datetime.now()
        k = Kline(
            ticker="BTCUSDT", interval="1m",
            start_time=now, end_time=now,
            open=50000.0, high=51000.0, low=49000.0, close=50500.0, volume=100.0,
        )
        assert k.ticker == "BTCUSDT"
        assert k.close == pytest.approx(50500.0)

    def test_kline_str(self):
        now = datetime.datetime.now()
        k = Kline(ticker="ETHUSDT", interval="5m", start_time=now, end_time=now,
                   open=3000.0, high=3100.0, low=2900.0, close=3050.0, volume=50.0)
        s = str(k)
        assert "ETHUSDT" in s
        assert "5m" in s
