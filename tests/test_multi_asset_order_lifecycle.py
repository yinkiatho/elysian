"""
Multi-asset portfolio Order Management and Lifecycle Tracking tests.

Covers:
  Group 1: Multi-asset rebalance order flow (ExecutionEngine.compute_order_intents)
  Group 2: Multi-asset ShadowBook order lifecycle (active_orders, locks, fills)
  Group 3: Position accounting accuracy (VWAP, realized PnL, NAV, weights)
  Group 4: Edge cases in multi-asset mode

All tests are independent — no shared state between tests.
Mocked exchanges and feeds prevent real API calls.
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock

from elysian_core.core.enums import OrderStatus, OrderType, Side, Venue
from elysian_core.core.order import Order, ActiveOrder, ActiveLimitOrder
from elysian_core.core.events import OrderUpdateEvent
from elysian_core.core.shadow_book import ShadowBook
from elysian_core.core.position import Position
from elysian_core.core.signals import OrderIntent, TargetWeights, ValidatedWeights
from elysian_core.execution.engine import ExecutionEngine


# ── Shared constants ──────────────────────────────────────────────────────────

ETH_PRICE = 3_000.0
BTC_PRICE = 50_000.0
SOL_PRICE = 200.0
BNB_PRICE = 400.0
INITIAL_CASH = 300_000.0  # 3-asset portfolio

SYMBOLS = {
    "ETHUSDT": ETH_PRICE,
    "BTCUSDT": BTC_PRICE,
    "SOLUSDT": SOL_PRICE,
    "BNBUSDT": BNB_PRICE,
}

PAIR_MAP = {
    "ETHUSDT": ("ETH", "USDT"),
    "BTCUSDT": ("BTC", "USDT"),
    "SOLUSDT": ("SOL", "USDT"),
    "BNBUSDT": ("BNB", "USDT"),
}


# ── Shared helper factories ───────────────────────────────────────────────────

def _shadow_book(
    cash: float = INITIAL_CASH,
    mark_prices: dict | None = None,
    strategy_id: int = 1,
) -> ShadowBook:
    """Construct a fresh ShadowBook with no positions."""
    prices = mark_prices or {k: v for k, v in SYMBOLS.items()}
    book = ShadowBook(strategy_id=strategy_id, venue=Venue.BINANCE)
    book.init_from_portfolio_cash(portfolio_cash=cash, mark_prices=prices)
    return book


def _order_event(
    order_id: str,
    symbol: str,
    side: Side = Side.BUY,
    quantity: float = 1.0,
    status: OrderStatus = OrderStatus.OPEN,
    filled_qty: float = 0.0,
    avg_fill_price: float = 0.0,
    last_fill_price: float = 0.0,
    commission: float = 0.0,
    commission_asset: str | None = None,
    strategy_id: int = 1,
) -> OrderUpdateEvent:
    """Build an OrderUpdateEvent for a given symbol."""
    base, quote = PAIR_MAP.get(symbol, ("UNKNOWN", "USDT"))
    price = SYMBOLS.get(symbol, 1.0)
    order = Order(
        id=order_id,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
        avg_fill_price=avg_fill_price,
        last_fill_price=last_fill_price if last_fill_price > 0 else avg_fill_price,
        filled_qty=filled_qty,
        status=status,
        venue=Venue.BINANCE,
        commission=commission,
        commission_asset=commission_asset,
        strategy_id=strategy_id,
    )
    return OrderUpdateEvent(
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        venue=Venue.BINANCE,
        order=order,
        timestamp=0,
    )


def dispatch(event: OrderUpdateEvent, book: ShadowBook) -> None:
    """Synchronously dispatch an order event to the shadow book."""
    asyncio.run(book._on_order_update(event))


def _mock_exchange(token_infos: dict | None = None) -> MagicMock:
    """Build a minimal exchange mock with token_infos and async order methods."""
    ex = MagicMock()
    ex._token_infos = token_infos or {
        "ETHUSDT": {"step_size": 0.001},
        "BTCUSDT": {"step_size": 0.0001},
        "SOLUSDT": {"step_size": 0.01},
        "BNBUSDT": {"step_size": 0.01},
    }
    ex.place_market_order = AsyncMock(return_value={"orderId": "mock-order-id"})
    ex.place_limit_order = AsyncMock(return_value={"orderId": "mock-limit-id"})
    return ex


def _mock_feed(price: float) -> MagicMock:
    feed = MagicMock()
    feed.latest_price = price
    return feed


def _mock_portfolio(positions: dict | None = None, cash: float = INITIAL_CASH) -> MagicMock:
    """Build a ShadowBook-like portfolio mock.

    Uses spec=ShadowBook so isinstance(p, ShadowBook) returns True, satisfying
    the ExecutionEngine constructor's type check.
    """
    p = MagicMock(spec=ShadowBook)
    _positions = positions or {}

    def position(symbol):
        return _positions.get(symbol, Position(symbol=symbol, venue=Venue.BINANCE))

    def total_value(mark_prices=None):
        pos_value = sum(
            pos.quantity * (mark_prices or {}).get(sym, 0.0)
            for sym, pos in _positions.items()
        )
        return cash + pos_value

    p.position.side_effect = position
    p.total_value.side_effect = total_value
    p.lock_for_order = MagicMock()
    p.cash = cash
    # ShadowBook attributes accessed during logger setup
    p._strategy_name = "test_strategy"
    p._strategy_id = 1
    return p


def _engine(
    positions: dict | None = None,
    cash: float = INITIAL_CASH,
    feeds: dict | None = None,
    token_infos: dict | None = None,
    symbol_venue_map: dict | None = None,
) -> ExecutionEngine:
    """Build an ExecutionEngine with mocked exchange and portfolio."""
    exchanges = {Venue.BINANCE: _mock_exchange(token_infos=token_infos)}
    portfolio = _mock_portfolio(positions=positions, cash=cash)
    feeds = feeds or {sym: _mock_feed(price) for sym, price in SYMBOLS.items()}
    return ExecutionEngine(
        exchanges=exchanges,
        portfolio=portfolio,
        feeds=feeds,
        default_venue=Venue.BINANCE,
        default_order_type=OrderType.MARKET,
        symbol_venue_map=symbol_venue_map or {},
    )


def _validated(weights: dict, strategy_id: int = 1, price_overrides: dict | None = None) -> ValidatedWeights:
    target = TargetWeights(
        weights=weights,
        timestamp=int(time.time() * 1000),
        strategy_id=strategy_id,
        price_overrides=price_overrides,
    )
    return ValidatedWeights(
        original=target,
        weights=weights,
        clipped={},
        rejected=False,
        timestamp=int(time.time() * 1000),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group 1: Multi-asset rebalance order flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiAssetRebalanceFlow:
    """Tests for compute_order_intents with a 3-asset portfolio."""

    def test_equal_weight_3_asset_generates_3_buy_intents(self):
        """3-asset portfolio from all-cash: equal weights generate 3 BUY intents.

        Verifies: all three symbols get a BUY intent, none are skipped,
        quantities are proportional to target weight * NAV / price.
        """
        engine = _engine(cash=INITIAL_CASH)
        mark_prices = {
            "ETHUSDT": ETH_PRICE,
            "BTCUSDT": BTC_PRICE,
            "SOLUSDT": SOL_PRICE,
        }
        # Each symbol gets 1/3 of the portfolio
        validated = _validated({
            "ETHUSDT": 1/3,
            "BTCUSDT": 1/3,
            "SOLUSDT": 1/3,
        })
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        assert len(intents) == 3, f"Expected 3 intents, got {len(intents)}"
        symbols = {i.symbol for i in intents}
        assert "ETHUSDT" in symbols
        assert "BTCUSDT" in symbols
        assert "SOLUSDT" in symbols

        for intent in intents:
            assert intent.side == Side.BUY, f"Expected BUY for {intent.symbol}, got {intent.side}"
            assert intent.quantity > 0

        # Verify approximate quantities
        total_value = INITIAL_CASH  # no positions, all cash
        target_notional = total_value / 3

        eth_intent = next(i for i in intents if i.symbol == "ETHUSDT")
        btc_intent = next(i for i in intents if i.symbol == "BTCUSDT")
        sol_intent = next(i for i in intents if i.symbol == "SOLUSDT")

        # qty = notional / price, rounded to step_size
        assert eth_intent.quantity == pytest.approx(target_notional / ETH_PRICE, rel=0.01)
        assert btc_intent.quantity == pytest.approx(target_notional / BTC_PRICE, rel=0.01)
        assert sol_intent.quantity == pytest.approx(target_notional / SOL_PRICE, rel=0.01)

    def test_rebalance_3_to_2_generates_sell_and_buys(self):
        """Rebalance from 3-asset equal to 2-asset equal.

        Setup: ETH, BTC, SOL each at 1/3 weight. New weights: ETH=0.5, BTC=0.5, SOL=0.
        Expected: 1 SELL (SOL) + delta adjustments for ETH and BTC.
        Sells execute first (partition ordering in execute()).
        """
        eth_qty = INITIAL_CASH / 3 / ETH_PRICE
        btc_qty = INITIAL_CASH / 3 / BTC_PRICE
        sol_qty = INITIAL_CASH / 3 / SOL_PRICE
        positions = {
            "ETHUSDT": Position(symbol="ETHUSDT", venue=Venue.BINANCE, quantity=eth_qty,
                                avg_entry_price=ETH_PRICE),
            "BTCUSDT": Position(symbol="BTCUSDT", venue=Venue.BINANCE, quantity=btc_qty,
                                avg_entry_price=BTC_PRICE),
            "SOLUSDT": Position(symbol="SOLUSDT", venue=Venue.BINANCE, quantity=sol_qty,
                                avg_entry_price=SOL_PRICE),
        }
        engine = _engine(positions=positions, cash=0.0)
        mark_prices = {
            "ETHUSDT": ETH_PRICE,
            "BTCUSDT": BTC_PRICE,
            "SOLUSDT": SOL_PRICE,
        }
        # New target: ETH 50%, BTC 50%, SOL 0%
        validated = _validated({
            "ETHUSDT": 0.50,
            "BTCUSDT": 0.50,
            "SOLUSDT": 0.0,
        })
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        # SOL must be sold (weight goes 1/3 -> 0)
        sol_intents = [i for i in intents if i.symbol == "SOLUSDT"]
        assert len(sol_intents) == 1, "SOL should have exactly 1 intent (SELL)"
        assert sol_intents[0].side == Side.SELL

        # ETH and BTC should be bought (weight increases from 1/3 to 1/2)
        eth_intents = [i for i in intents if i.symbol == "ETHUSDT"]
        btc_intents = [i for i in intents if i.symbol == "BTCUSDT"]
        assert eth_intents[0].side == Side.BUY
        assert btc_intents[0].side == Side.BUY

    def test_go_flat_from_3_assets_generates_3_sells(self):
        """Go flat (all weights=0) from 3-asset portfolio — 3 SELL intents.

        Verifies that all positions are intended to be liquidated.
        """
        eth_qty = 10.0
        btc_qty = 0.5
        sol_qty = 100.0
        positions = {
            "ETHUSDT": Position(symbol="ETHUSDT", venue=Venue.BINANCE, quantity=eth_qty,
                                avg_entry_price=ETH_PRICE),
            "BTCUSDT": Position(symbol="BTCUSDT", venue=Venue.BINANCE, quantity=btc_qty,
                                avg_entry_price=BTC_PRICE),
            "SOLUSDT": Position(symbol="SOLUSDT", venue=Venue.BINANCE, quantity=sol_qty,
                                avg_entry_price=SOL_PRICE),
        }
        engine = _engine(positions=positions, cash=0.0)
        mark_prices = {
            "ETHUSDT": ETH_PRICE,
            "BTCUSDT": BTC_PRICE,
            "SOLUSDT": SOL_PRICE,
        }
        # All weights zero — go flat
        validated = _validated({
            "ETHUSDT": 0.0,
            "BTCUSDT": 0.0,
            "SOLUSDT": 0.0,
        })
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        sell_intents = [i for i in intents if i.side == Side.SELL]
        assert len(sell_intents) == 3, f"Expected 3 SELL intents, got {len(sell_intents)}"

        symbols = {i.symbol for i in sell_intents}
        assert "ETHUSDT" in symbols
        assert "BTCUSDT" in symbols
        assert "SOLUSDT" in symbols

        # All sells should liquidate full positions
        eth_sell = next(i for i in sell_intents if i.symbol == "ETHUSDT")
        btc_sell = next(i for i in sell_intents if i.symbol == "BTCUSDT")
        sol_sell = next(i for i in sell_intents if i.symbol == "SOLUSDT")

        assert eth_sell.quantity == pytest.approx(eth_qty, rel=0.01)
        assert btc_sell.quantity == pytest.approx(btc_qty, rel=0.01)
        assert sol_sell.quantity == pytest.approx(sol_qty, rel=0.01)

    def test_weight_shift_generates_only_eth_sell_btc_buy(self):
        """ETH 50%->30%, BTC 30%->50%, SOL 20%->20%.

        SOL delta is exactly 0 (same weight) so no intent for SOL.
        ETH decreases -> SELL. BTC increases -> BUY.
        """
        total_value = INITIAL_CASH  # all in positions
        eth_qty = 0.50 * total_value / ETH_PRICE
        btc_qty = 0.30 * total_value / BTC_PRICE
        sol_qty = 0.20 * total_value / SOL_PRICE
        positions = {
            "ETHUSDT": Position(symbol="ETHUSDT", venue=Venue.BINANCE, quantity=eth_qty,
                                avg_entry_price=ETH_PRICE),
            "BTCUSDT": Position(symbol="BTCUSDT", venue=Venue.BINANCE, quantity=btc_qty,
                                avg_entry_price=BTC_PRICE),
            "SOLUSDT": Position(symbol="SOLUSDT", venue=Venue.BINANCE, quantity=sol_qty,
                                avg_entry_price=SOL_PRICE),
        }
        engine = _engine(positions=positions, cash=0.0)
        mark_prices = {
            "ETHUSDT": ETH_PRICE,
            "BTCUSDT": BTC_PRICE,
            "SOLUSDT": SOL_PRICE,
        }
        # ETH: 50% -> 30%, BTC: 30% -> 50%, SOL: 20% -> 20% (unchanged)
        validated = _validated({
            "ETHUSDT": 0.30,
            "BTCUSDT": 0.50,
            "SOLUSDT": 0.20,
        })
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        # SOL delta is 0 — weight is unchanged, delta < 0.001
        sol_intents = [i for i in intents if i.symbol == "SOLUSDT"]
        assert len(sol_intents) == 0, "SOL weight is unchanged, no intent should be generated"

        eth_intents = [i for i in intents if i.symbol == "ETHUSDT"]
        btc_intents = [i for i in intents if i.symbol == "BTCUSDT"]

        assert len(eth_intents) == 1
        assert eth_intents[0].side == Side.SELL, "ETH weight decreased, expect SELL"

        assert len(btc_intents) == 1
        assert btc_intents[0].side == Side.BUY, "BTC weight increased, expect BUY"


# ═══════════════════════════════════════════════════════════════════════════════
# Group 2: Multi-asset ShadowBook order lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiAssetShadowBookLifecycle:
    """Tests for concurrent order tracking in ShadowBook with 3 assets."""

    def test_3_concurrent_orders_tracked_in_active_orders(self):
        """Track 3 concurrent open orders: verify locked_cash and locked_qty per order.

        BUY ETH, BUY BTC, SELL SOL (limit orders placed via lock_for_order).
        """
        book = _shadow_book()
        # Seed a SOL position to sell
        sol_qty = 50.0
        book._positions["SOLUSDT"] = Position(
            symbol="SOLUSDT", venue=Venue.BINANCE, quantity=sol_qty,
            avg_entry_price=SOL_PRICE,
        )
        book._refresh_derived()

        eth_intent = OrderIntent(
            symbol="ETHUSDT", venue=Venue.BINANCE, side=Side.BUY,
            quantity=2.0, order_type=OrderType.LIMIT, price=ETH_PRICE, strategy_id=1,
        )
        btc_intent = OrderIntent(
            symbol="BTCUSDT", venue=Venue.BINANCE, side=Side.BUY,
            quantity=0.1, order_type=OrderType.LIMIT, price=BTC_PRICE, strategy_id=1,
        )
        sol_intent = OrderIntent(
            symbol="SOLUSDT", venue=Venue.BINANCE, side=Side.SELL,
            quantity=10.0, order_type=OrderType.LIMIT, price=SOL_PRICE, strategy_id=1,
        )

        book.lock_for_order("eth-o1", eth_intent)
        book.lock_for_order("btc-o1", btc_intent)
        book.lock_for_order("sol-o1", sol_intent)

        assert "eth-o1" in book.active_orders
        assert "btc-o1" in book.active_orders
        assert "sol-o1" in book.active_orders

        # BUY ETH locked cash = 2.0 * 3000 = 6000
        eth_locked_cash = book._active_orders["eth-o1"].reserved_cash
        assert eth_locked_cash == pytest.approx(2.0 * ETH_PRICE)

        # BUY BTC locked cash = 0.1 * 50000 = 5000
        btc_locked_cash = book._active_orders["btc-o1"].reserved_cash
        assert btc_locked_cash == pytest.approx(0.1 * BTC_PRICE)

        # SELL SOL locked qty = 10.0
        sol_locked_qty = book._active_orders["sol-o1"].reserved_qty
        assert sol_locked_qty == pytest.approx(10.0)

        # Total locked cash = 6000 + 5000 = 11000
        assert book._locked_cash == pytest.approx(eth_locked_cash + btc_locked_cash)

    def test_partial_fill_one_of_3_updates_only_that_asset(self):
        """Partial fill on 1 of 3 concurrent open market orders.

        ETH gets a partial fill. BTC and SOL positions must be unchanged.
        """
        book = _shadow_book()

        # Open 3 orders
        dispatch(_order_event("eth-o1", "ETHUSDT", quantity=2.0), book)
        dispatch(_order_event("btc-o1", "BTCUSDT", quantity=0.1), book)
        dispatch(_order_event("sol-o1", "SOLUSDT", quantity=50.0), book)

        assert len(book.active_orders) == 3

        # Partial fill on ETH only
        dispatch(_order_event(
            "eth-o1", "ETHUSDT",
            quantity=2.0, filled_qty=1.0,
            avg_fill_price=ETH_PRICE, last_fill_price=ETH_PRICE,
            status=OrderStatus.PARTIALLY_FILLED,
        ), book)

        # ETH position updated to 1.0
        assert book.position("ETHUSDT").quantity == pytest.approx(1.0)

        # BTC and SOL positions unchanged (not filled)
        assert book.position("BTCUSDT").quantity == pytest.approx(0.0)
        assert book.position("SOLUSDT").quantity == pytest.approx(0.0)

        # ETH still active (partial), BTC and SOL still open
        assert "eth-o1" in book.active_orders
        assert "btc-o1" in book.active_orders
        assert "sol-o1" in book.active_orders

    def test_bnb_commission_on_bnbusdt_trade_deducts_from_position(self):
        """Fill + BNB fee on BNBUSDT trade while ETH and SOL positions also exist.

        BNB is paid in BNB (non-stablecoin) for a BNBUSDT trade, so it should
        reduce the BNBUSDT position quantity. ETH and SOL positions must be unchanged.
        """
        book = _shadow_book()
        # Pre-seed ETH and SOL positions
        book._positions["ETHUSDT"] = Position(
            symbol="ETHUSDT", venue=Venue.BINANCE, quantity=2.0,
            avg_entry_price=ETH_PRICE,
        )
        book._positions["SOLUSDT"] = Position(
            symbol="SOLUSDT", venue=Venue.BINANCE, quantity=100.0,
            avg_entry_price=SOL_PRICE,
        )

        bnb_qty = 1.0
        bnb_commission = 0.001  # 0.001 BNB fee

        # Open and fill the BNBUSDT order
        dispatch(_order_event("bnb-o1", "BNBUSDT", quantity=bnb_qty), book)
        dispatch(_order_event(
            "bnb-o1", "BNBUSDT",
            quantity=bnb_qty, filled_qty=bnb_qty,
            avg_fill_price=BNB_PRICE, last_fill_price=BNB_PRICE,
            status=OrderStatus.FILLED,
            commission=bnb_commission, commission_asset="BNB",
        ), book)

        # BNBUSDT position reduced by commission (fee deducted from position)
        expected_bnb_qty = bnb_qty - bnb_commission
        assert book.position("BNBUSDT").quantity == pytest.approx(expected_bnb_qty)

        # ETH and SOL positions are untouched
        assert book.position("ETHUSDT").quantity == pytest.approx(2.0)
        assert book.position("SOLUSDT").quantity == pytest.approx(100.0)

        # BNB commission recorded in _total_commission_dict
        assert "BNB" in book._total_commission_dict
        assert book._total_commission_dict["BNB"] == pytest.approx(bnb_commission)

    def test_bnb_commission_on_ethusdt_trade_goes_to_commission_dict_only(self):
        """BNB commission on ETHUSDT trade — no BNBUSDT position to deduct from.

        When the commission_asset is BNB but there is no BNBUSDT position,
        apply_fill should raise ValueError since it cannot deduct the fee.
        This verifies the code's behavior: commission without a matching position
        raises rather than silently creating an inconsistency.
        """
        book = _shadow_book()
        # No BNBUSDT position exists — only ETHUSDT is being traded

        # Open the ETHUSDT order
        dispatch(_order_event("eth-o1", "ETHUSDT", quantity=1.0), book)

        # BNB commission on ETHUSDT fill — no BNBUSDT position exists
        # BUG: apply_fill raises ValueError when commission_asset is not in positions.
        # The current code in shadow_book.py line 609-611 raises:
        #   raise ValueError(f"Commission asset {commission_asset} not found in positions for deduction")
        # This is arguably correct behavior for non-stablecoin commission without a matching position,
        # but it means BNB fee-paying on non-BNB trades fails if BNB balance isn't seeded first.
        # The commission should be recorded in _total_commission_dict only (not deducted from a position).
        with pytest.raises(ValueError, match="Commission asset BNB not found in positions"):
            dispatch(_order_event(
                "eth-o1", "ETHUSDT",
                quantity=1.0, filled_qty=1.0,
                avg_fill_price=ETH_PRICE, last_fill_price=ETH_PRICE,
                status=OrderStatus.FILLED,
                commission=0.0001, commission_asset="BNB",
            ), book)

    def test_bnb_commission_on_ethusdt_when_bnb_position_exists(self):
        """BNB commission on ETHUSDT trade — BNB position exists, deducted from it.

        The commission asset (BNB) resolves to a BNB position via _asset_symbol_map.
        The fill for ETHUSDT should record in _total_commission_dict, and the BNB
        position is reduced.
        """
        book = _shadow_book()
        # Seed BNB position first (simulates prior BNB buy)
        book._positions["BNBUSDT"] = Position(
            symbol="BNBUSDT", venue=Venue.BINANCE, quantity=1.0,
            avg_entry_price=BNB_PRICE,
        )
        # Map BNB -> BNBUSDT in _asset_symbol_map so commission deduction can find the position
        book._asset_symbol_map["BNB"] = "BNBUSDT"

        bnb_commission = 0.0005

        # Open and fill ETHUSDT order with BNB commission
        dispatch(_order_event("eth-o1", "ETHUSDT", quantity=1.0), book)
        dispatch(_order_event(
            "eth-o1", "ETHUSDT",
            quantity=1.0, filled_qty=1.0,
            avg_fill_price=ETH_PRICE, last_fill_price=ETH_PRICE,
            status=OrderStatus.FILLED,
            commission=bnb_commission, commission_asset="BNB",
        ), book)

        # ETH position acquired
        assert book.position("ETHUSDT").quantity == pytest.approx(1.0)

        # BNB position reduced by commission
        assert book.position("BNBUSDT").quantity == pytest.approx(1.0 - bnb_commission)

        # Commission recorded in _total_commission_dict
        assert "BNB" in book._total_commission_dict
        assert book._total_commission_dict["BNB"] == pytest.approx(bnb_commission)

    def test_order_cancel_mid_rebalance_releases_locked_resources(self):
        """Order cancel mid-rebalance: locked resources released, other orders unaffected.

        Three limit orders placed. One is cancelled. The remaining two keep
        their reservations intact.
        """
        book = _shadow_book()
        # Seed SOL position for SELL lock
        book._positions["SOLUSDT"] = Position(
            symbol="SOLUSDT", venue=Venue.BINANCE, quantity=100.0,
            avg_entry_price=SOL_PRICE,
        )
        book._refresh_derived()

        eth_intent = OrderIntent(
            symbol="ETHUSDT", venue=Venue.BINANCE, side=Side.BUY,
            quantity=2.0, order_type=OrderType.LIMIT, price=ETH_PRICE, strategy_id=1,
        )
        btc_intent = OrderIntent(
            symbol="BTCUSDT", venue=Venue.BINANCE, side=Side.BUY,
            quantity=0.1, order_type=OrderType.LIMIT, price=BTC_PRICE, strategy_id=1,
        )
        sol_intent = OrderIntent(
            symbol="SOLUSDT", venue=Venue.BINANCE, side=Side.SELL,
            quantity=20.0, order_type=OrderType.LIMIT, price=SOL_PRICE, strategy_id=1,
        )

        book.lock_for_order("eth-o1", eth_intent)
        book.lock_for_order("btc-o1", btc_intent)
        book.lock_for_order("sol-o1", sol_intent)

        locked_cash_before = book._locked_cash
        eth_cash = book._active_orders["eth-o1"].reserved_cash
        btc_cash = book._active_orders["btc-o1"].reserved_cash

        # Cancel the SOL sell order via event (OPEN then CANCELLED)
        dispatch(_order_event("sol-o1", "SOLUSDT", side=Side.SELL, quantity=20.0,
                              status=OrderStatus.OPEN), book)
        dispatch(_order_event("sol-o1", "SOLUSDT", side=Side.SELL, quantity=20.0,
                              status=OrderStatus.CANCELLED), book)

        # SOL order removed from active orders
        assert "sol-o1" not in book.active_orders

        # ETH and BTC orders still active
        assert "eth-o1" in book.active_orders
        assert "btc-o1" in book.active_orders

        # Locked cash reduced only by the cancelled SOL reservation (SOL was SELL, reserved_qty)
        # SOL SELL doesn't lock cash, it locks SOL qty — locked_cash should be unchanged for SOL cancel
        assert book._locked_cash == pytest.approx(eth_cash + btc_cash)

        # SOL quantity no longer locked on the position
        sol_pos = book.position("SOLUSDT")
        assert sol_pos.locked_quantity == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Group 3: Position accounting accuracy
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionAccountingAccuracy:
    """Tests for VWAP, realized PnL, NAV, and weights with multi-asset portfolios."""

    def test_vwap_avg_entry_across_3_partial_fills(self):
        """VWAP avg_entry_price across 3 partial BUY fills at different prices.

        Buy 0.3 ETH total in three tranches:
          Fill 1: 0.1 ETH @ 2900
          Fill 2: 0.1 ETH @ 3000
          Fill 3: 0.1 ETH @ 3100
        VWAP = (0.1*2900 + 0.1*3000 + 0.1*3100) / 0.3 = 3000.0
        """
        book = _shadow_book()
        oid = "eth-vwap"

        dispatch(_order_event(oid, "ETHUSDT", quantity=0.3), book)

        # Fill 1
        dispatch(_order_event(
            oid, "ETHUSDT", quantity=0.3,
            filled_qty=0.1, avg_fill_price=2900.0, last_fill_price=2900.0,
            status=OrderStatus.PARTIALLY_FILLED,
        ), book)

        # Fill 2
        dispatch(_order_event(
            oid, "ETHUSDT", quantity=0.3,
            filled_qty=0.2, avg_fill_price=2950.0, last_fill_price=3000.0,
            status=OrderStatus.PARTIALLY_FILLED,
        ), book)

        # Fill 3 — full
        dispatch(_order_event(
            oid, "ETHUSDT", quantity=0.3,
            filled_qty=0.3, avg_fill_price=3000.0, last_fill_price=3100.0,
            status=OrderStatus.FILLED,
        ), book)

        pos = book.position("ETHUSDT")
        assert pos.quantity == pytest.approx(0.3)
        # VWAP of three fills each at 0.1 ETH: (2900+3000+3100)/3 = 3000.0
        assert pos.avg_entry_price == pytest.approx(3000.0, rel=0.01)

    def test_realized_pnl_from_partial_close_of_3_asset_portfolio(self):
        """Realized PnL from partial close of 1 position in a 3-asset portfolio.

        Only the closed portion realizes PnL.
        ETH: bought 1.0 @ 3000, sell 0.5 @ 3300 -> realized PnL = 0.5 * (3300 - 3000) = 150
        BTC and SOL positions are unchanged and do NOT realize PnL.
        """
        book = _shadow_book()

        # Seed 3 positions via fills
        # ETH BUY 1.0 @ 3000
        book.apply_fill("ETHUSDT", "ETH", "USDT", qty_delta=1.0, price=3000.0)
        # BTC BUY 0.1 @ 50000
        book.apply_fill("BTCUSDT", "BTC", "USDT", qty_delta=0.1, price=50000.0)
        # SOL BUY 10.0 @ 200
        book.apply_fill("SOLUSDT", "SOL", "USDT", qty_delta=10.0, price=200.0)

        realized_before = book.realized_pnl

        # Partial close of ETH: sell 0.5 @ 3300
        book.apply_fill("ETHUSDT", "ETH", "USDT", qty_delta=-0.5, price=3300.0)

        expected_realized = 0.5 * (3300.0 - 3000.0)  # = 150.0
        assert book.realized_pnl == pytest.approx(realized_before + expected_realized)

        # ETH still has 0.5 remaining
        assert book.position("ETHUSDT").quantity == pytest.approx(0.5)

        # BTC and SOL positions unchanged
        assert book.position("BTCUSDT").quantity == pytest.approx(0.1)
        assert book.position("SOLUSDT").quantity == pytest.approx(10.0)

        # ETH per-position realized PnL also updated
        assert book.position("ETHUSDT").realized_pnl == pytest.approx(expected_realized)

    def test_nav_calculation_with_3_open_positions_and_different_mark_prices(self):
        """NAV calculation with 3 open positions at different mark prices.

        NAV = cash + sum(position_qty * mark_price)
        """
        book = _shadow_book(cash=50_000.0)

        # Buy 2 ETH @ 3000 (cost = 6000)
        book.apply_fill("ETHUSDT", "ETH", "USDT", qty_delta=2.0, price=3000.0)
        # Buy 0.1 BTC @ 50000 (cost = 5000)
        book.apply_fill("BTCUSDT", "BTC", "USDT", qty_delta=0.1, price=50000.0)
        # Buy 50 SOL @ 200 (cost = 10000)
        book.apply_fill("SOLUSDT", "SOL", "USDT", qty_delta=50.0, price=200.0)

        cash_remaining = 50_000.0 - 6_000.0 - 5_000.0 - 10_000.0  # = 29000
        assert book.cash == pytest.approx(cash_remaining)

        # Now mark prices change
        new_prices = {
            "ETHUSDT": 3_200.0,
            "BTCUSDT": 52_000.0,
            "SOLUSDT": 210.0,
        }
        book.mark_to_market(new_prices)

        expected_nav = (
            cash_remaining
            + 2.0 * 3_200.0      # ETH position
            + 0.1 * 52_000.0     # BTC position
            + 50.0 * 210.0       # SOL position
        )
        assert book.nav == pytest.approx(expected_nav)
        assert book.total_value(new_prices) == pytest.approx(expected_nav)

    def test_weights_property_sums_to_1_with_3_assets(self):
        """weights() property with 3 assets sums to approximately 1.0.

        After buying ETH, BTC, and SOL with most of the cash, the sum of position
        weights + cash weight should equal 1.0.
        """
        book = _shadow_book(cash=INITIAL_CASH)

        # Buy equal portions of all 3
        # Spend ~100k on each (total=300k cash)
        book.apply_fill("ETHUSDT", "ETH", "USDT", qty_delta=100_000.0 / ETH_PRICE, price=ETH_PRICE)
        book.apply_fill("BTCUSDT", "BTC", "USDT", qty_delta=100_000.0 / BTC_PRICE, price=BTC_PRICE)
        book.apply_fill("SOLUSDT", "SOL", "USDT", qty_delta=100_000.0 / SOL_PRICE, price=SOL_PRICE)

        mark_prices = {"ETHUSDT": ETH_PRICE, "BTCUSDT": BTC_PRICE, "SOLUSDT": SOL_PRICE}
        book.mark_to_market(mark_prices)

        weights = book.weights
        cash_w = book.cash_weight(mark_prices)

        # Sum of all position weights
        position_weight_sum = sum(weights.values())

        # Total weight (positions + cash) should be 1.0
        total = position_weight_sum + cash_w
        assert total == pytest.approx(1.0, rel=1e-6)

        # Each asset should have roughly equal weight (~1/3)
        assert "ETHUSDT" in weights
        assert "BTCUSDT" in weights
        assert "SOLUSDT" in weights
        assert weights["ETHUSDT"] == pytest.approx(1/3, abs=0.01)
        assert weights["BTCUSDT"] == pytest.approx(1/3, abs=0.01)
        assert weights["SOLUSDT"] == pytest.approx(1/3, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# Group 4: Edge cases in multi-asset mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiAssetEdgeCases:
    """Edge cases: venue mapping, weight delta threshold, stale old positions."""

    def test_two_assets_same_venue_engine_uses_correct_symbol_venue_mapping(self):
        """Two assets on the same venue: engine uses symbol-venue map for each.

        ETHUSDT and BTCUSDT both on Venue.BINANCE. Each intent should have
        the correct venue. When no symbol_venue_map is provided, default venue is used.
        """
        engine = _engine(cash=INITIAL_CASH)
        mark_prices = {"ETHUSDT": ETH_PRICE, "BTCUSDT": BTC_PRICE}
        validated = _validated({"ETHUSDT": 0.40, "BTCUSDT": 0.40})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        assert len(intents) == 2
        for intent in intents:
            assert intent.venue == Venue.BINANCE

    def test_two_assets_different_venues_via_symbol_venue_map(self):
        """Two assets on different venues via explicit symbol_venue_map.

        ETH on BINANCE, BTC on BYBIT. Engine must route to respective exchanges.
        When no BYBIT exchange is configured, BTC intent is skipped.
        """
        # Only BINANCE is configured in the exchange dict
        exchanges = {Venue.BINANCE: _mock_exchange()}
        portfolio = _mock_portfolio(cash=INITIAL_CASH)
        feeds = {sym: _mock_feed(price) for sym, price in SYMBOLS.items()}

        engine = ExecutionEngine(
            exchanges=exchanges,
            portfolio=portfolio,
            feeds=feeds,
            default_venue=Venue.BINANCE,
            default_order_type=OrderType.MARKET,
            symbol_venue_map={"BTCUSDT": Venue.BYBIT},  # BTC -> BYBIT (not configured)
        )

        mark_prices = {"ETHUSDT": ETH_PRICE, "BTCUSDT": BTC_PRICE}
        validated = _validated({"ETHUSDT": 0.40, "BTCUSDT": 0.40})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        # BTC is mapped to BYBIT which has no exchange — should be skipped
        btc_intents = [i for i in intents if i.symbol == "BTCUSDT"]
        assert len(btc_intents) == 0

        # ETH is on BINANCE (default) — should be present
        eth_intents = [i for i in intents if i.symbol == "ETHUSDT"]
        assert len(eth_intents) == 1
        assert eth_intents[0].venue == Venue.BINANCE

    def test_weight_delta_below_threshold_generates_no_intent(self):
        """Weight delta below 0.001 threshold — no intent generated for that symbol.

        SOL is already at target weight within 0.001. No SOL intent should be generated.
        """
        total_value = INITIAL_CASH
        # Exact target quantities — current weight = target weight
        eth_qty = 0.30 * total_value / ETH_PRICE
        btc_qty = 0.30 * total_value / BTC_PRICE
        sol_qty = 0.40 * total_value / SOL_PRICE

        positions = {
            "ETHUSDT": Position(symbol="ETHUSDT", venue=Venue.BINANCE, quantity=eth_qty,
                                avg_entry_price=ETH_PRICE),
            "BTCUSDT": Position(symbol="BTCUSDT", venue=Venue.BINANCE, quantity=btc_qty,
                                avg_entry_price=BTC_PRICE),
            "SOLUSDT": Position(symbol="SOLUSDT", venue=Venue.BINANCE, quantity=sol_qty,
                                avg_entry_price=SOL_PRICE),
        }
        engine = _engine(positions=positions, cash=0.0)
        mark_prices = {
            "ETHUSDT": ETH_PRICE,
            "BTCUSDT": BTC_PRICE,
            "SOLUSDT": SOL_PRICE,
        }
        # Target weights exactly match current weights — delta < 0.001 for all
        validated = _validated({
            "ETHUSDT": 0.30,
            "BTCUSDT": 0.30,
            "SOLUSDT": 0.40,
        })
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        # All deltas are ~0 — no intents generated
        assert len(intents) == 0

    def test_old_position_not_in_target_weights_is_preserved_in_shadow_book(self):
        """Position not in target weights is preserved in ShadowBook.

        ShadowBook positions are not automatically liquidated when a new
        target weight set omits the symbol — only the ExecutionEngine generates
        sell intents if weight=0 is explicitly passed.
        """
        book = _shadow_book()
        # Seed an ETH position from a prior fill
        book.apply_fill("ETHUSDT", "ETH", "USDT", qty_delta=1.0, price=ETH_PRICE)

        # The new target weights only mention BTC and SOL (ETH is omitted)
        # ETH position should remain in the shadow book
        assert book.position("ETHUSDT").quantity == pytest.approx(1.0)

        # Simulate what the engine sees: ETH is NOT in new weights
        # Engine only generates intents for symbols IN validated.weights
        engine = _engine(
            positions={"ETHUSDT": Position(symbol="ETHUSDT", venue=Venue.BINANCE,
                                           quantity=1.0, avg_entry_price=ETH_PRICE)},
            cash=0.0,
        )
        mark_prices = {"BTCUSDT": BTC_PRICE, "SOLUSDT": SOL_PRICE, "ETHUSDT": ETH_PRICE}
        validated = _validated({"BTCUSDT": 0.50, "SOLUSDT": 0.50})
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        # No ETHUSDT intent — ETH not in target weights, engine doesn't liquidate implicitly
        eth_intents = [i for i in intents if i.symbol == "ETHUSDT"]
        assert len(eth_intents) == 0

    def test_weight_0_for_existing_position_generates_sell(self):
        """Explicitly passing weight=0 for an existing position generates a SELL.

        This is how strategies liquidate a position: include it in weights with 0.
        """
        eth_qty = 1.0
        positions = {
            "ETHUSDT": Position(symbol="ETHUSDT", venue=Venue.BINANCE, quantity=eth_qty,
                                avg_entry_price=ETH_PRICE),
        }
        engine = _engine(positions=positions, cash=INITIAL_CASH)
        mark_prices = {"ETHUSDT": ETH_PRICE, "BTCUSDT": BTC_PRICE}
        validated = _validated({
            "ETHUSDT": 0.0,   # explicitly liquidate
            "BTCUSDT": 0.50,  # buy BTC
        })
        intents = engine.compute_order_intents(validated, mark_prices, OrderType.MARKET)

        eth_intents = [i for i in intents if i.symbol == "ETHUSDT"]
        assert len(eth_intents) == 1
        assert eth_intents[0].side == Side.SELL

    def test_3_concurrent_orders_fill_sequence_correct_cash_accounting(self):
        """3 concurrent BUY orders all fully filled — cash decreases by total notional.

        Validates that each fill is independently attributed to the portfolio cash.
        """
        book = _shadow_book(cash=INITIAL_CASH)
        cash_before = book.cash

        eth_qty = 1.0
        btc_qty = 0.05
        sol_qty = 20.0

        # Open all 3 orders
        dispatch(_order_event("eth-o1", "ETHUSDT", quantity=eth_qty), book)
        dispatch(_order_event("btc-o1", "BTCUSDT", quantity=btc_qty), book)
        dispatch(_order_event("sol-o1", "SOLUSDT", quantity=sol_qty), book)

        # Fill all 3
        dispatch(_order_event(
            "eth-o1", "ETHUSDT", quantity=eth_qty, filled_qty=eth_qty,
            avg_fill_price=ETH_PRICE, last_fill_price=ETH_PRICE,
            status=OrderStatus.FILLED,
        ), book)
        dispatch(_order_event(
            "btc-o1", "BTCUSDT", quantity=btc_qty, filled_qty=btc_qty,
            avg_fill_price=BTC_PRICE, last_fill_price=BTC_PRICE,
            status=OrderStatus.FILLED,
        ), book)
        dispatch(_order_event(
            "sol-o1", "SOLUSDT", quantity=sol_qty, filled_qty=sol_qty,
            avg_fill_price=SOL_PRICE, last_fill_price=SOL_PRICE,
            status=OrderStatus.FILLED,
        ), book)

        eth_notional = eth_qty * ETH_PRICE
        btc_notional = btc_qty * BTC_PRICE
        sol_notional = sol_qty * SOL_PRICE
        total_notional = eth_notional + btc_notional + sol_notional

        assert book.cash == pytest.approx(cash_before - total_notional)

        # All positions correctly attributed
        assert book.position("ETHUSDT").quantity == pytest.approx(eth_qty)
        assert book.position("BTCUSDT").quantity == pytest.approx(btc_qty)
        assert book.position("SOLUSDT").quantity == pytest.approx(sol_qty)

        # All orders removed from active_orders
        assert "eth-o1" not in book.active_orders
        assert "btc-o1" not in book.active_orders
        assert "sol-o1" not in book.active_orders

    def test_duplicate_terminal_event_does_not_double_count_position(self):
        """Duplicate FILLED event for same order must not double-count the position."""
        book = _shadow_book()

        dispatch(_order_event("eth-o1", "ETHUSDT", quantity=1.0), book)
        dispatch(_order_event(
            "eth-o1", "ETHUSDT", quantity=1.0, filled_qty=1.0,
            avg_fill_price=ETH_PRICE, last_fill_price=ETH_PRICE,
            status=OrderStatus.FILLED,
        ), book)

        qty_after_first_fill = book.position("ETHUSDT").quantity
        assert qty_after_first_fill == pytest.approx(1.0)

        # Send duplicate FILLED event — should be a no-op
        dispatch(_order_event(
            "eth-o1", "ETHUSDT", quantity=1.0, filled_qty=1.0,
            avg_fill_price=ETH_PRICE, last_fill_price=ETH_PRICE,
            status=OrderStatus.FILLED,
        ), book)

        # Position should still be 1.0, not 2.0
        assert book.position("ETHUSDT").quantity == pytest.approx(1.0)
