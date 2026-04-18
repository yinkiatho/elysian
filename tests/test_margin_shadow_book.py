"""
Comprehensive tests for MarginShadowBook.

Tests cover:
- NAV formula correctness (long, short, mixed, no-position)
- Interest draining collateral (NAV erosion)
- margin_level() — infinite, healthy, at-risk, one-sided
- liquidation_price() — pure short, mixed, long-only-borrow (None)
- sync_from_exchange() — stablecoin-only in _collateral, net positions
- _on_balance_update() — USDT delta, BTC delta, negative-clamp
- apply_fill() / _adjust_cash() — buy, sell, stablecoin commission, BNB commission
- Real-life scenarios: BTC price rise on short, interest draining to negative NAV
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from elysian_core.core.margin_shadow_book import MarginShadowBook
from elysian_core.core.enums import AssetType, Venue
from elysian_core.core.events import BalanceUpdateEvent
from elysian_core.core.position import Position


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_book(strategy_id: int = 1, strategy_name: str = "test") -> MarginShadowBook:
    return MarginShadowBook(strategy_id=strategy_id, venue=Venue.BINANCE, strategy_name=strategy_name)


def make_exchange(
    collateral: dict | None = None,    # symbol -> {asset: qty}
    borrowed: dict | None = None,      # symbol -> {asset: qty}
    accrued_interest: dict | None = None,
    open_orders: dict | None = None,
) -> MagicMock:
    """Build a mock exchange connector with the given margin state."""
    exc = MagicMock()
    exc._collateral = collateral or {}
    exc._borrowed = borrowed or {}
    exc._accrued_interest = accrued_interest or {}
    exc._open_orders = open_orders or {}
    exc.base_asset_to_symbol = lambda asset: f"{asset}USDT"
    return exc


PRICES_BTC = {"BTCUSDT": 50_000.0}
PRICES_ETH = {"ETHUSDT": 2_000.0}
PRICES_BOTH = {"BTCUSDT": 50_000.0, "ETHUSDT": 2_000.0}


# ─── 1. Asset type ────────────────────────────────────────────────────────────

class TestAssetType:
    def test_always_margin(self):
        book = make_book()
        assert book._asset_type == AssetType.MARGIN


# ─── 2. total_value() — NAV formula ──────────────────────────────────────────

class TestTotalValue:
    def test_empty_book_is_zero(self):
        book = make_book()
        assert book.total_value() == 0.0

    def test_usdt_only_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        assert book.total_value() == pytest.approx(1_000.0)

    def test_long_btc_no_debt(self):
        """Hold 0.1 BTC bought with own USDT — no loan."""
        book = make_book()
        book._collateral["USDT"] = 0.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        assert book.total_value(PRICES_BTC) == pytest.approx(5_000.0)

    def test_long_btc_with_borrowed_usdt(self):
        """Classic 5× leveraged long: own 1000, borrow 4000, buy 0.1 BTC @ 50k."""
        book = make_book()
        book._collateral["USDT"] = 0.0            # spent all USDT on the buy
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._borrowed["USDT"] = 4_000.0
        assert book.total_value(PRICES_BTC) == pytest.approx(1_000.0)

    def test_short_btc_proceeds_in_collateral(self):
        """Short 0.1 BTC @ 50k: borrow BTC, sell it, proceeds go to collateral."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0        # 1000 own + 5000 sale proceeds
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1, avg_entry_price=50_000.0)
        book._borrowed["BTC"] = 0.1
        # Short position must NOT be subtracted (proceeds already in collateral)
        assert book.total_value(PRICES_BTC) == pytest.approx(1_000.0)

    def test_short_price_rises_nav_falls(self):
        """BTC rises from 50k to 60k — short seller loses."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1, avg_entry_price=50_000.0)
        book._borrowed["BTC"] = 0.1
        nav = book.total_value({"BTCUSDT": 60_000.0})
        assert nav == pytest.approx(0.0)          # 6000 - 0.1*60000 = 0

    def test_short_price_falls_nav_rises(self):
        """BTC falls from 50k to 40k — short seller profits."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1, avg_entry_price=50_000.0)
        book._borrowed["BTC"] = 0.1
        nav = book.total_value({"BTCUSDT": 40_000.0})
        assert nav == pytest.approx(2_000.0)      # 6000 - 0.1*40000 = 2000

    def test_market_neutral_long_and_short(self):
        """Long 0.1 BTC + short 0.05 ETH, own capital = 2000 USDT."""
        book = make_book()
        # Long BTC: own 2000 USDT deposited, borrow 3000 USDT, buy 0.1 BTC
        book._collateral["USDT"] = 100.0          # 0 after spending 5k, +100 from ETH short
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._positions["ETHUSDT"] = Position("ETHUSDT", quantity=-0.05, avg_entry_price=2_000.0)
        book._borrowed["USDT"] = 3_000.0          # USDT loan for BTC long
        book._borrowed["ETH"] = 0.05              # ETH loan for ETH short
        # NAV = 100 + 0.1*50000 - 3000 - 0.05*2000 = 100 + 5000 - 3000 - 100 = 2000
        nav = book.total_value(PRICES_BOTH)
        assert nav == pytest.approx(2_000.0)

    def test_interest_reduces_nav(self):
        """Accrued interest reduces NAV directly."""
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        book._accrued_interest["USDT"] = 5.0
        assert book.total_value() == pytest.approx(995.0)

    def test_interest_in_btc_reduces_nav(self):
        """Interest charged in BTC (rare but possible) reduces NAV.

        Scenario: own 1000 USDT → spent on BTC buy (collateral = 0),
        borrow 4000 USDT for the rest of the 5x long.
        """
        book = make_book()
        book._collateral["USDT"] = 0.0            # spent all USDT buying 0.1 BTC
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._borrowed["USDT"] = 4_000.0
        book._accrued_interest["BTC"] = 0.001    # small BTC interest ≈ $50 at 50k
        nav = book.total_value(PRICES_BTC)
        # 0 + 0.1*50000 - 4000 - 0.001*50000 = 5000 - 4000 - 50 = 950
        assert nav == pytest.approx(950.0)

    def test_negative_nav_over_leveraged(self):
        """Huge borrowing relative to collateral → negative NAV."""
        book = make_book()
        book._collateral["USDT"] = 100.0
        book._borrowed["BTC"] = 1.0               # short 1 BTC — now price spiked
        # short at 50k but price went to 200k:
        nav = book.total_value({"BTCUSDT": 200_000.0})
        assert nav == pytest.approx(100.0 - 200_000.0)

    def test_mark_prices_cached_fallback(self):
        """Falls back to _mark_prices when called with no argument."""
        book = make_book()
        book._collateral["USDT"] = 500.0
        book._mark_prices["BTCUSDT"] = 50_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.01, avg_entry_price=50_000.0)
        nav = book.total_value()   # no mark_prices arg — uses self._mark_prices
        assert nav == pytest.approx(500.0 + 0.01 * 50_000.0)

    def test_busd_collateral_treated_as_stablecoin(self):
        """BUSD in collateral is included at price 1.0."""
        book = make_book()
        book._collateral["BUSD"] = 750.0
        book._collateral["USDT"] = 250.0
        assert book.total_value() == pytest.approx(1_000.0)


# ─── 3. free_cash ─────────────────────────────────────────────────────────────

class TestFreeCash:
    def test_no_locked(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        assert book.free_cash == pytest.approx(1_000.0)

    def test_locked_reduces_free_cash(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        book._locked_cash = 300.0
        assert book.free_cash == pytest.approx(700.0)

    def test_free_cash_clamped_at_zero(self):
        book = make_book()
        book._collateral["USDT"] = 100.0
        book._locked_cash = 200.0
        assert book.free_cash == 0.0

    def test_includes_busd(self):
        book = make_book()
        book._collateral["USDT"] = 400.0
        book._collateral["BUSD"] = 600.0
        assert book.free_cash == pytest.approx(1_000.0)


# ─── 4. margin_level() ────────────────────────────────────────────────────────

class TestMarginLevel:
    def test_no_liabilities_is_infinite(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        assert book.margin_level() == float("inf")

    def test_healthy_long(self):
        """Long 0.1 BTC, borrowed 4000 USDT, margin level = 5000/4000 = 1.25."""
        book = make_book()
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1)
        book._borrowed["USDT"] = 4_000.0
        book._mark_prices["BTCUSDT"] = 50_000.0
        level = book.margin_level(PRICES_BTC)
        assert level == pytest.approx(1.25)

    def test_healthy_short(self):
        """Short 0.1 BTC @ 50k, collateral 6000, borrowed BTC 0.1 → level = 6000/5000 = 1.2."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)
        book._borrowed["BTC"] = 0.1
        level = book.margin_level(PRICES_BTC)
        assert level == pytest.approx(1.2)

    def test_short_price_rises_level_falls(self):
        """BTC goes to 60k → margin level = 6000/6000 = 1.0 (liquidation!)"""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._borrowed["BTC"] = 0.1
        level = book.margin_level({"BTCUSDT": 60_000.0})
        assert level == pytest.approx(1.0)

    def test_interest_adds_to_liabilities(self):
        """Interest increases liability → margin level decreases."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._borrowed["BTC"] = 0.1
        book._accrued_interest["BTC"] = 0.001     # small extra liability
        # liability_value = (0.1 + 0.001) * 50000 = 5050
        # asset_value = 6000
        # level = 6000 / 5050 ≈ 1.188
        level = book.margin_level(PRICES_BTC)
        assert level == pytest.approx(6_000.0 / ((0.1 + 0.001) * 50_000.0))

    def test_zero_liabilities_after_repay(self):
        """After repaying all loans, margin level is infinite."""
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        book._borrowed["BTC"] = 0.0               # all repaid
        assert book.margin_level(PRICES_BTC) == float("inf")

    def test_uses_cached_mark_prices(self):
        """Without explicit prices argument, falls back to _mark_prices."""
        book = make_book()
        book._collateral["USDT"] = 5_500.0
        book._borrowed["BTC"] = 0.1
        book._mark_prices["BTCUSDT"] = 50_000.0
        # 5500 / 5000 = 1.1
        assert book.margin_level() == pytest.approx(1.1)


# ─── 5. liquidation_price() ──────────────────────────────────────────────────

class TestLiquidationPrice:
    def test_flat_position_returns_none(self):
        book = make_book()
        assert book.liquidation_price("BTCUSDT") is None

    def test_long_no_borrowed_base_returns_none(self):
        """Long with borrowed USDT: no borrowed BTC → None."""
        book = make_book()
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1)
        book._borrowed["USDT"] = 4_000.0       # borrowed USDT, not BTC
        assert book.liquidation_price("BTCUSDT") is None

    def test_pure_short_liquidation_price(self):
        """Short 0.1 BTC with 6000 USDT collateral.
        liq_price = collateral_usdt / ((1 + mmr) * borrowed_qty)
                   = 6000 / (1.1 * 0.1)
                   = 54545.45...
        """
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)
        book._borrowed["BTC"] = 0.1
        liq = book.liquidation_price("BTCUSDT", mmr=0.10)
        assert liq == pytest.approx(6_000.0 / (1.1 * 0.1))

    def test_liquidation_price_custom_mmr(self):
        """Maintenance margin rate of 0.05 (5%) instead of default 10%."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._borrowed["BTC"] = 0.1
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)
        liq = book.liquidation_price("BTCUSDT", mmr=0.05)
        assert liq == pytest.approx(6_000.0 / (1.05 * 0.1))

    def test_liq_price_above_current_for_short(self):
        """Liq price for a short must be above current price (BTC would need to rise to liquidate)."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0    # shorted at ~50k
        book._borrowed["BTC"] = 0.1
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)
        liq = book.liquidation_price("BTCUSDT")
        assert liq > 50_000.0  # BTC must rise to liquidate a short

    def test_large_collateral_high_liq_price(self):
        """More USDT collateral → higher liq price (safer short)."""
        book_small = make_book()
        book_small._collateral["USDT"] = 2_000.0
        book_small._borrowed["BTC"] = 0.1
        book_small._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)

        book_large = make_book()
        book_large._collateral["USDT"] = 10_000.0
        book_large._borrowed["BTC"] = 0.1
        book_large._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)

        assert book_large.liquidation_price("BTCUSDT") > book_small.liquidation_price("BTCUSDT")

    def test_denom_zero_returns_none(self):
        """Edge: borrowed_qty = 0 (already checked before reaching denom calc)."""
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)
        book._borrowed["BTC"] = 0.0            # no loan
        assert book.liquidation_price("BTCUSDT") is None

    def test_busd_collateral_included(self):
        """BUSD also counts as collateral for liquidation price."""
        book = make_book()
        book._collateral["USDT"] = 3_000.0
        book._collateral["BUSD"] = 3_000.0     # total 6000
        book._borrowed["BTC"] = 0.1
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1)
        liq = book.liquidation_price("BTCUSDT")
        assert liq == pytest.approx(6_000.0 / (1.1 * 0.1))


# ─── 6. sync_from_exchange() ─────────────────────────────────────────────────

class TestSyncFromExchange:
    def test_stablecoin_only_in_collateral(self):
        """Non-stablecoin base assets must NOT be placed in _collateral."""
        exc = make_exchange(
            collateral={"BTCUSDT": {"BTC": 0.1, "USDT": 0.0}},
            borrowed={"BTCUSDT": {"USDT": 4_000.0}},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        assert "BTC" not in book._collateral
        assert "USDT" in book._collateral or book._collateral.get("USDT", 0.0) == 0.0

    def test_usdt_collateral_populated(self):
        """USDT collateral from exchange goes into _collateral["USDT"]."""
        exc = make_exchange(
            collateral={"BTCUSDT": {"USDT": 1_000.0, "BTC": 0.0}},
            borrowed={},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        assert book._collateral.get("USDT", 0.0) == pytest.approx(1_000.0)

    def test_long_btc_position_correct_net_qty(self):
        """Long BTC: net_qty = base_collateral(0.1) - borrowed_base(0) = 0.1."""
        exc = make_exchange(
            collateral={"BTCUSDT": {"BTC": 0.1, "USDT": 0.0}},
            borrowed={},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        pos = book._positions.get("BTCUSDT")
        assert pos is not None
        assert pos.quantity == pytest.approx(0.1)

    def test_short_btc_position_correct_net_qty(self):
        """Short BTC: net_qty = base_collateral(0) - borrowed_base(0.1) = -0.1."""
        exc = make_exchange(
            collateral={"BTCUSDT": {"BTC": 0.0, "USDT": 6_000.0}},
            borrowed={"BTCUSDT": {"BTC": 0.1}},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        pos = book._positions.get("BTCUSDT")
        assert pos is not None
        assert pos.quantity == pytest.approx(-0.1)

    def test_borrowed_flattened(self):
        exc = make_exchange(
            collateral={"BTCUSDT": {"BTC": 0.0, "USDT": 6_000.0}},
            borrowed={"BTCUSDT": {"BTC": 0.1}},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        assert book._borrowed.get("BTC", 0.0) == pytest.approx(0.1)

    def test_accrued_interest_flattened(self):
        exc = make_exchange(
            collateral={"BTCUSDT": {"USDT": 1_000.0}},
            borrowed={},
            accrued_interest={"BTCUSDT": {"USDT": 2.5}},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        assert book._accrued_interest.get("USDT", 0.0) == pytest.approx(2.5)

    def test_nav_correct_after_sync_short(self):
        """After syncing a pure short, NAV should be collateral_usdt - borrowed_btc_value."""
        exc = make_exchange(
            collateral={"BTCUSDT": {"USDT": 6_000.0, "BTC": 0.0}},
            borrowed={"BTCUSDT": {"BTC": 0.1}},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        nav = book.total_value(PRICES_BTC)
        assert nav == pytest.approx(1_000.0)  # 6000 - 0.1*50000

    def test_nav_correct_after_sync_long(self):
        """After syncing a long, NAV should include position value minus borrowed USDT."""
        exc = make_exchange(
            collateral={"BTCUSDT": {"USDT": 0.0, "BTC": 0.1}},
            borrowed={"BTCUSDT": {"USDT": 4_000.0}},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        nav = book.total_value(PRICES_BTC)
        # collateral_usdt=0, long_pos=0.1*50000=5000, borrowed_usdt=4000
        assert nav == pytest.approx(1_000.0)

    def test_multi_symbol_two_positions(self):
        exc = make_exchange(
            collateral={
                "BTCUSDT": {"USDT": 0.0, "BTC": 0.1},
                "ETHUSDT": {"USDT": 100.0, "ETH": 0.0},
            },
            borrowed={
                "BTCUSDT": {"USDT": 4_000.0},
                "ETHUSDT": {"ETH": 0.05},
            },
        )
        book = make_book()
        book.sync_from_exchange(exc)
        assert "BTCUSDT" in book._positions
        assert "ETHUSDT" in book._positions
        assert book._positions["BTCUSDT"].quantity == pytest.approx(0.1)
        assert book._positions["ETHUSDT"].quantity == pytest.approx(-0.05)

    def test_empty_exchange_no_positions(self):
        exc = make_exchange()
        book = make_book()
        book.sync_from_exchange(exc)
        assert book._positions == {}
        assert book._collateral == {}
        assert book._borrowed == {}

    def test_peak_equity_set_after_sync(self):
        exc = make_exchange(
            collateral={"BTCUSDT": {"USDT": 1_000.0}},
            borrowed={},
        )
        book = make_book()
        book.sync_from_exchange(exc)
        # NAV = 1000; peak_equity should be >= NAV
        assert book._peak_equity >= 0.0


# ─── 7. _on_balance_update() ─────────────────────────────────────────────────

class TestOnBalanceUpdate:
    def _make_event(self, asset: str, delta: float) -> BalanceUpdateEvent:
        return BalanceUpdateEvent(
            asset=asset, venue=Venue.BINANCE, delta=delta,
            new_balance=0.0, timestamp=1_000_000,
        )

    def test_usdt_delta_updates_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        event = self._make_event("USDT", 500.0)
        asyncio.run(book._on_balance_update(event))
        assert book._collateral["USDT"] == pytest.approx(1_500.0)

    def test_usdt_negative_delta_updates_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        event = self._make_event("USDT", -300.0)
        asyncio.run(book._on_balance_update(event))
        assert book._collateral["USDT"] == pytest.approx(700.0)

    def test_usdt_negative_clamped_at_zero(self):
        """Balance cannot go negative from a balance update."""
        book = make_book()
        book._collateral["USDT"] = 100.0
        event = self._make_event("USDT", -500.0)
        asyncio.run(book._on_balance_update(event))
        assert book._collateral["USDT"] == 0.0

    def test_btc_delta_updates_position(self):
        """BTC balance update routes to _positions."""
        book = make_book()
        exc = MagicMock()
        exc.base_asset_to_symbol = lambda asset: f"{asset}USDT"
        book._exchange = exc
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.0)
        event = self._make_event("BTC", 0.1)
        asyncio.run(book._on_balance_update(event))
        assert book._positions["BTCUSDT"].quantity == pytest.approx(0.1)

    def test_btc_delta_creates_position_if_absent(self):
        """BTC balance update creates a new position entry."""
        book = make_book()
        exc = MagicMock()
        exc.base_asset_to_symbol = lambda asset: f"{asset}USDT"
        book._exchange = exc
        event = self._make_event("BTC", 0.05)
        asyncio.run(book._on_balance_update(event))
        assert "BTCUSDT" in book._positions
        assert book._positions["BTCUSDT"].quantity == pytest.approx(0.05)

    def test_nav_refreshed_after_update(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        event = self._make_event("USDT", 500.0)
        asyncio.run(book._on_balance_update(event))
        assert book._nav == pytest.approx(1_500.0)


# ─── 8. _adjust_cash() — fill accounting ──────────────────────────────────────

class TestAdjustCash:
    def test_buy_reduces_usdt_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 5_000.0
        book._adjust_cash(qty_delta=0.1, price=50_000.0, quote_asset="USDT")
        assert book._collateral["USDT"] == pytest.approx(0.0)

    def test_sell_increases_usdt_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        book._adjust_cash(qty_delta=-0.1, price=50_000.0, quote_asset="USDT")
        assert book._collateral["USDT"] == pytest.approx(6_000.0)

    def test_adjust_cash_does_not_touch_self_cash(self):
        """_cash stays 0 — margin collateral lives in _collateral only."""
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        book._adjust_cash(qty_delta=-0.1, price=50_000.0, quote_asset="USDT")
        assert book._cash == 0.0

    def test_returns_notional(self):
        book = make_book()
        book._collateral["USDT"] = 5_000.0
        notional = book._adjust_cash(qty_delta=0.1, price=50_000.0, quote_asset="USDT")
        assert notional == pytest.approx(5_000.0)


# ─── 9. apply_fill() via _on_order_update path ────────────────────────────────

class TestApplyFill:
    def test_buy_fill_increases_position_decreases_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 5_000.0
        book._asset_symbol_map["BTC"] = "BTCUSDT"
        book.apply_fill(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            qty_delta=0.1, price=50_000.0, commission=5.0, commission_asset="USDT",
        )
        pos = book._positions.get("BTCUSDT")
        assert pos is not None
        assert pos.quantity == pytest.approx(0.1)
        # USDT collateral: 5000 - 5000 (notional) - 5 (commission) = -5 (allow negative in fill)
        assert book._collateral["USDT"] == pytest.approx(-5.0)

    def test_sell_fill_decreases_position_increases_collateral(self):
        """Sell closes a long position; proceeds go to _collateral."""
        book = make_book()
        book._collateral["USDT"] = 0.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._asset_symbol_map["BTC"] = "BTCUSDT"
        book.apply_fill(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            qty_delta=-0.1, price=50_000.0, commission=5.0, commission_asset="USDT",
        )
        # commission deducted first (-5), then sell proceeds added (+5000) = 4995
        assert book._collateral["USDT"] == pytest.approx(4_995.0)
        pos = book._positions.get("BTCUSDT")
        # Position should be removed (flat)
        assert pos is None or pos.is_flat()

    def test_stablecoin_commission_deducted_from_collateral(self):
        """USDT commission comes from collateral, not _cash."""
        book = make_book()
        book._collateral["USDT"] = 2_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=45_000.0)
        book._asset_symbol_map["BTC"] = "BTCUSDT"
        book.apply_fill(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            qty_delta=-0.1, price=50_000.0, commission=10.0, commission_asset="USDT",
        )
        # 2000 + 5000 (proceeds) - 10 (commission) = 6990
        assert book._collateral["USDT"] == pytest.approx(6_990.0)
        assert book._cash == 0.0

    def test_bnb_commission_deducted_from_bnb_position(self):
        """Non-stablecoin BNB commission reduces BNB position qty."""
        book = make_book()
        book._collateral["USDT"] = 5_000.0
        book._positions["BNBUSDT"] = Position("BNBUSDT", quantity=1.0, avg_entry_price=300.0)
        book._asset_symbol_map["BTC"] = "BTCUSDT"
        book._asset_symbol_map["BNB"] = "BNBUSDT"
        book.apply_fill(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            qty_delta=0.1, price=50_000.0, commission=0.001, commission_asset="BNB",
        )
        bnb_pos = book._positions.get("BNBUSDT")
        assert bnb_pos is not None
        assert bnb_pos.quantity == pytest.approx(1.0 - 0.001)

    def test_realized_pnl_on_close(self):
        """Closing a long at higher price generates positive realized PnL."""
        book = make_book()
        book._collateral["USDT"] = 0.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=40_000.0)
        book._asset_symbol_map["BTC"] = "BTCUSDT"
        book.apply_fill(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            qty_delta=-0.1, price=50_000.0,
        )
        # realized PnL = (50000 - 40000) * 0.1 = 1000
        assert book._realized_pnl == pytest.approx(1_000.0)


# ─── 10. _track_commission() override ────────────────────────────────────────

class TestTrackCommission:
    def test_no_commission_noop(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        pos = Position("BTCUSDT", quantity=0.1)
        book._track_commission(pos, None, 0.0, "USDT")
        assert book._collateral["USDT"] == pytest.approx(1_000.0)

    def test_usdt_commission_reduces_collateral(self):
        book = make_book()
        book._collateral["USDT"] = 1_000.0
        pos = Position("BTCUSDT", quantity=0.1)
        book._track_commission(pos, "USDT", 7.5, "USDT")
        assert book._collateral["USDT"] == pytest.approx(992.5)
        assert book._cash == 0.0


# ─── 11. Real-life scenarios ──────────────────────────────────────────────────

class TestRealLifeScenarios:
    def test_interest_erodes_nav_to_zero(self):
        """Daily interest of 0.1% on a $5k loan erodes NAV over time.

        Scenario: own capital (1000 USDT) spent buying 0.1 BTC → collateral = 0,
        borrowed 4000 USDT for the rest of the position.  Starting NAV = 1000.
        250 days × 4 USDT/day interest = 1000 USDT total interest → NAV → 0.
        """
        book = make_book()
        book._collateral["USDT"] = 0.0        # own capital already spent on BTC purchase
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._borrowed["USDT"] = 4_000.0
        # Each "day" accumulate 0.1% interest on 4000 USDT = 4 USDT
        for day in range(250):
            book._accrued_interest["USDT"] = book._accrued_interest.get("USDT", 0.0) + 4.0
        nav = book.total_value(PRICES_BTC)
        # 0 + 0.1*50000 - 4000 - 250*4 = 5000 - 4000 - 1000 = 0
        assert nav == pytest.approx(0.0)

    def test_short_btc_price_spike_wipes_nav(self):
        """BTC spikes from $50k to $60k on a short position, NAV approaches 0."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0   # shorted at 50k
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1, avg_entry_price=50_000.0)
        book._borrowed["BTC"] = 0.1
        nav = book.total_value({"BTCUSDT": 60_000.0})
        assert nav == pytest.approx(0.0)  # 6000 - 0.1*60000 = 0

    def test_margin_level_falls_below_liquidation_threshold(self):
        """As BTC rises, margin level for a short falls toward 1.0."""
        book = make_book()
        book._collateral["USDT"] = 5_500.0
        book._borrowed["BTC"] = 0.1
        # At 50k: 5500/5000 = 1.1
        assert book.margin_level({"BTCUSDT": 50_000.0}) == pytest.approx(1.1)
        # At 55k: 5500/5500 = 1.0 — liquidation!
        assert book.margin_level({"BTCUSDT": 55_000.0}) == pytest.approx(1.0)

    def test_long_then_partial_close_nav_consistent(self):
        """Buy 0.1 BTC, then sell half. NAV should reflect reduced exposure."""
        book = make_book()
        book._collateral["USDT"] = 0.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._borrowed["USDT"] = 4_000.0
        book._asset_symbol_map["BTC"] = "BTCUSDT"
        # Sell 0.05 BTC at 50k
        book.apply_fill(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
            qty_delta=-0.05, price=50_000.0,
        )
        # After sell: position=0.05, collateral_usdt=0+2500=2500
        nav = book.total_value(PRICES_BTC)
        # 2500 (usdt) + 0.05*50000 (long btc) - 4000 (borrowed) = 2500 + 2500 - 4000 = 1000
        assert nav == pytest.approx(1_000.0)

    def test_short_btc_usdt_interest_reduces_nav(self):
        """Interest accrual on a short: interest charged in USDT reduces collateral NAV."""
        book = make_book()
        book._collateral["USDT"] = 6_000.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=-0.1, avg_entry_price=50_000.0)
        book._borrowed["BTC"] = 0.1
        # Accrue $50 interest over time
        book._accrued_interest["USDT"] = 50.0
        nav = book.total_value(PRICES_BTC)
        # 6000 - 0.1*50000 - 50 = 950
        assert nav == pytest.approx(950.0)

    def test_long_price_fall_wipes_nav(self):
        """BTC falls from 50k to 40k; long position nearly wiped."""
        book = make_book()
        book._collateral["USDT"] = 0.0
        book._positions["BTCUSDT"] = Position("BTCUSDT", quantity=0.1, avg_entry_price=50_000.0)
        book._borrowed["USDT"] = 4_500.0
        nav = book.total_value({"BTCUSDT": 40_000.0})
        # 0 + 0.1*40000 - 4500 = -500 (underwater)
        assert nav == pytest.approx(-500.0)

    def test_insufficient_collateral_for_full_interest(self):
        """Interest charges exceed available collateral — negative NAV."""
        book = make_book()
        book._collateral["USDT"] = 50.0        # tiny collateral
        book._borrowed["BTC"] = 0.1
        book._accrued_interest["USDT"] = 200.0  # interest > collateral
        nav = book.total_value(PRICES_BTC)
        # 50 - 0.1*50000 - 200 = 50 - 5000 - 200 = -5150
        assert nav < 0


# ─── 12. max_leverage / isolated_symbol from config ──────────────────────────

class TestConfigParams:
    def test_max_leverage_from_config(self):
        cfg = MagicMock()
        cfg.params = {"max_leverage": 5}
        cfg.symbols = []
        cfg.asset_type = "MARGIN"
        book = MarginShadowBook(strategy_id=1, strategy_config=cfg)
        assert book._max_leverage == 5.0

    def test_isolated_symbol_from_config(self):
        cfg = MagicMock()
        cfg.params = {"isolated_symbol": "BTCUSDT", "max_leverage": 3}
        cfg.symbols = []
        cfg.asset_type = "MARGIN"
        book = MarginShadowBook(strategy_id=1, strategy_config=cfg)
        assert book._isolated_symbol == "BTCUSDT"

    def test_defaults_without_config(self):
        book = make_book()
        assert book._max_leverage == 1.0
        assert book._isolated_symbol is None
