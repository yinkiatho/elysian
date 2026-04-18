"""
Isolated-margin variant of ShadowBook.

MarginShadowBook extends ShadowBook with:
  - Collateral / borrowed / accrued-interest ledgers
  - Margin NAV formula: Σ(stablecoin_collateral·price)
                         + Σ(long_position.qty·price)   ← only qty > 0
                         − Σ(borrowed·price)
                         − Σ(interest·price)
    Short positions are excluded from the NAV sum because their USDT sale
    proceeds are already captured in _collateral["USDT"].  Including negative
    position values would double-subtract the short exposure.
  - margin_level() / liquidation_price() helpers
  - sync_from_exchange() reads connector's isolated margin state
  - _on_balance_update() routes USDT deltas to _collateral
  - _adjust_cash() routes fill proceeds/costs to _collateral (not _cash)

The parent ShadowBook is NEVER modified — SPOT strategies see no change.
Position.quantity is signed: negative quantity = short position.
_collateral tracks STABLECOINS only (USDT/BUSD).  Non-stablecoin base assets
are tracked exclusively in _positions so the NAV formula is consistent
between sync-from-exchange and live-trading states.
"""
from typing import Dict, Optional, TYPE_CHECKING

from elysian_core.core.shadow_book import ShadowBook
from elysian_core.core.enums import AssetType, Venue
from elysian_core.core.position import Position
from elysian_core.core.constants import STABLECOINS
from elysian_core.core.events import BalanceUpdateEvent
import elysian_core.utils.logger as log

if TYPE_CHECKING:
    from elysian_core.config.app_config import StrategyConfig


class MarginShadowBook(ShadowBook):
    """Isolated-margin position and collateral ledger for one sub-account.

    Parameters
    ----------
    strategy_id:
        Unique identifier for the owning strategy.
    venue:
        Default venue (e.g. Venue.BINANCE).
    strategy_name:
        Human-readable name for log prefixes.
    strategy_config:
        Optional YAML-loaded config; used for max_leverage and
        isolated_symbol params.
    """

    def __init__(
        self,
        strategy_id: int,
        venue: Venue = Venue.BINANCE,
        strategy_name: str = "",
        strategy_config: Optional["StrategyConfig"] = None,
    ) -> None:
        # Parent sets up _positions, _mark_prices, _asset_type, _cash, etc.
        super().__init__(strategy_id, venue, strategy_name, strategy_config)

        # Override asset_type — this class is always MARGIN.
        self._asset_type = AssetType.MARGIN

        # ── Margin ledgers (per asset, not per symbol) ─────────────────────
        # Total collateral transferred into the isolated account
        self._collateral: Dict[str, float] = {}
        # Outstanding loan from Binance (grows on borrow, shrinks on repay)
        self._borrowed: Dict[str, float] = {}
        # Unpaid interest; Binance debits hourly, polled every 60 s by connector
        self._accrued_interest: Dict[str, float] = {}

        # Max leverage for this sub-account (from strategy YAML params.max_leverage)
        params = strategy_config.params if strategy_config else {}
        self._max_leverage: float = float(params.get("max_leverage", 1.0))

        # Optional pinned symbol (e.g. "BTCUSDT") for single-symbol isolated accounts.
        self._isolated_symbol: Optional[str] = params.get("isolated_symbol")

        # Re-point logger with margin tag so logs are distinguishable
        self.logger = log.setup_custom_logger(
            f"margin_{strategy_name or strategy_id}_{strategy_id}"
        )

    # ── Valuation ─────────────────────────────────────────────────────────

    def _price_for_asset(self, asset: str, prices: Dict[str, float]) -> float:
        """Resolve an asset name (e.g. "BTC") to a USDT price.

        Stablecoins are always 1.0.  Non-stablecoins are looked up via
        _asset_symbol_map first (populated by sync_from_exchange), falling back
        to the conventional "{asset}USDT" symbol name.  Returns 0.0 if not found
        so that an unknown asset contributes zero to NAV/liability calculations
        rather than silently using 1.0.
        """
        if asset in STABLECOINS:
            return 1.0
        symbol = self._asset_symbol_map.get(asset, f"{asset}USDT")
        return prices.get(symbol, prices.get(asset, 0.0))

    def total_value(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        """Margin NAV.

        NAV = Σ(stablecoin_collateral · 1.0)
            + Σ(long_position.qty · price)   ← only qty > 0
            − Σ(borrowed_asset · price)
            − Σ(interest_asset · price)

        Short positions are NOT included: their USDT sale proceeds are already
        in _collateral["USDT"].  Including negative qty would double-subtract
        the short exposure.  _collateral holds stablecoins only; base-asset
        positions (long or short) live exclusively in _positions.

        Stablecoins default to price 1.0 when not in mark_prices.
        """
        prices = mark_prices if mark_prices is not None else self._mark_prices

        collateral_usdt = sum(
            qty * prices.get(asset, 1.0)
            for asset, qty in self._collateral.items()
        )
        borrowed_usdt = sum(
            qty * self._price_for_asset(asset, prices)
            for asset, qty in self._borrowed.items()
        )
        interest_usdt = sum(
            qty * self._price_for_asset(asset, prices)
            for asset, qty in self._accrued_interest.items()
        )
        # Only long positions contribute to NAV — their BTC/ETH is not in _collateral.
        # Short positions (qty < 0) are skipped; proceeds already in _collateral["USDT"].
        long_position_usdt = sum(
            pos.quantity * prices.get(pos.symbol, pos.avg_entry_price)
            for pos in self._positions.values()
            if pos.quantity > 0
        )
        return collateral_usdt + long_position_usdt - borrowed_usdt - interest_usdt

    @property
    def free_cash(self) -> float:
        """Available quote collateral minus LIMIT BUY reservations."""
        quote = self._collateral.get("USDT", 0.0) + self._collateral.get("BUSD", 0.0)
        return max(0.0, quote - self._locked_cash)

    # ── Margin health ──────────────────────────────────────────────────────

    def margin_level(self, prices: Optional[Dict[str, float]] = None) -> float:
        """Binance margin level = total_asset_value / total_liability_value.

        Returns float('inf') when there are no liabilities (healthy).
        Binance triggers liquidation on most isolated pairs when level < 1.1.
        """
        prices = prices or self._mark_prices

        asset_value = sum(
            qty * prices.get(asset, 1.0)
            for asset, qty in self._collateral.items()
        ) + sum(
            pos.quantity * prices.get(pos.symbol, pos.avg_entry_price)
            for pos in self._positions.values()
            if pos.quantity > 0
        )

        liability_assets = set(self._borrowed) | set(self._accrued_interest)
        liability_value = sum(
            (self._borrowed.get(asset, 0.0) + self._accrued_interest.get(asset, 0.0))
            * self._price_for_asset(asset, prices)
            for asset in liability_assets
        )

        return asset_value / liability_value if liability_value > 0 else float("inf")

    def liquidation_price(self, symbol: str, mmr: float = 0.10) -> Optional[float]:
        """Price at which margin_level would hit (1 + mmr).

        Uses the analytical solution for a single-symbol isolated account.
        Returns None if the position is flat or there is no borrowed base asset.

        Derivation:
          margin_level(p) = (collateral_usdt + long_qty * p) / (borrowed_qty * p) = 1 + mmr

          For a pure short  (long_qty = 0, borrowed_qty > 0):
            p = collateral_usdt / ((1 + mmr) * borrowed_qty)

          For a mixed case  (long_qty > 0, borrowed_qty > long_qty, e.g. leveraged long
          that also borrows base asset):
            p = collateral_usdt / ((1 + mmr) * borrowed_qty − long_qty)

          ``long_qty = max(0, pos.quantity)`` — short positions (qty < 0) do not
          contribute to the asset side (their proceeds are already in collateral_usdt).
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.is_flat():
            return None

        # Extract base asset name (e.g. "BTCUSDT" → "BTC")
        base_asset = symbol.replace("USDT", "").replace("BUSD", "")
        borrowed_qty = self._borrowed.get(base_asset, 0.0)
        if borrowed_qty <= 0:
            return None

        collateral_usdt = (
            self._collateral.get("USDT", 0.0) + self._collateral.get("BUSD", 0.0)
        )
        long_qty = max(0.0, pos.quantity)
        denom = (1.0 + mmr) * borrowed_qty - long_qty
        if denom <= 0:
            return None
        return collateral_usdt / denom

    # ── Initialization ────────────────────────────────────────────────────

    def sync_from_exchange(self, exchange, feeds: Optional[Dict] = None) -> None:
        """Initialize from isolated margin account state in the connector.

        The connector's ``refresh_margin_account()`` must have been called
        before this so ``exchange._collateral``, ``exchange._borrowed``, and
        ``exchange._accrued_interest`` are populated (keyed [symbol][asset]).

        Flattens those per-symbol dicts into per-asset dicts on this book.
        """
        self._exchange = exchange
        feeds = feeds or {}

        # Flatten connector's per-symbol dicts into flat per-asset dicts.
        # _collateral tracks STABLECOINS only — base assets (BTC, ETH, …) go
        # into _positions so that total_value() is consistent between sync and
        # live-trading states (where _adjust_cash() only updates stablecoins).
        for asset_dict in exchange._collateral.values():
            for asset, qty in asset_dict.items():
                if asset in STABLECOINS:
                    self._collateral[asset] = self._collateral.get(asset, 0.0) + qty

        for asset_dict in exchange._borrowed.values():
            for asset, qty in asset_dict.items():
                self._borrowed[asset] = self._borrowed.get(asset, 0.0) + qty

        for asset_dict in exchange._accrued_interest.values():
            for asset, qty in asset_dict.items():
                self._accrued_interest[asset] = (
                    self._accrued_interest.get(asset, 0.0) + qty
                )

        # Build Position entries: net_qty = base_collateral − borrowed_base.
        # Read base collateral directly from the per-symbol exchange dict so we
        # are not constrained by the stablecoin-only _collateral above.
        for symbol in exchange._collateral:
            base_asset = symbol.replace("USDT", "").replace("BUSD", "")
            if base_asset in STABLECOINS:
                continue
            base_collateral = exchange._collateral.get(symbol, {}).get(base_asset, 0.0)
            borrowed_base = exchange._borrowed.get(symbol, {}).get(base_asset, 0.0)
            net_qty = base_collateral - borrowed_base
            entry_price = 0.0
            feed = feeds.get(symbol)
            if feed:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        entry_price = p
                        self._mark_prices[symbol] = p
                except Exception:
                    pass
            self._positions[symbol] = Position(
                symbol=symbol,
                venue=self._venue,
                quantity=net_qty,
                avg_entry_price=entry_price,
            )
            self._asset_symbol_map[base_asset] = symbol

        # Mark prices for any remaining feeds
        for sym, feed in feeds.items():
            if sym not in self._mark_prices:
                try:
                    p = feed.latest_price
                    if p and p > 0:
                        self._mark_prices[sym] = p
                except Exception:
                    pass

        # Sync open orders from exchange
        for sym, orders in exchange._open_orders.items():
            from elysian_core.core.order import ActiveOrder
            for oid, order in orders.items():
                active = ActiveOrder(
                    id=order.id, symbol=order.symbol, side=order.side,
                    order_type=order.order_type, quantity=order.quantity,
                    price=order.price, status=order.status,
                    venue=order.venue or self._venue, strategy_id=order.strategy_id,
                    filled_qty=order.filled_qty, avg_fill_price=order.avg_fill_price,
                    commission=order.commission, commission_asset=order.commission_asset,
                    last_updated_timestamp=order.last_updated_timestamp,
                    _prev_filled=order.filled_qty,
                )
                self._active_orders[oid] = active

        self._refresh_derived()
        self._peak_equity = max(self._peak_equity, self._nav)
        self.logger.info(
            f"[MarginShadowBook-{self._strategy_id}] Sync complete:\n"
            f"{self.log_snapshot()}"
        )

    # ── Event handlers ────────────────────────────────────────────────────

    async def _on_balance_update(self, event: BalanceUpdateEvent) -> None:
        """Route isolated-margin balance events to the correct ledger.

        Quote-asset (USDT/BUSD) deltas update _collateral.
        Base-asset deltas update _positions.
        The connector's periodic margin poll (every 60 s) reconciles any drift.
        """
        asset = event.asset
        delta = event.delta

        if asset in STABLECOINS:
            old_val = self._collateral.get(asset, 0.0)
            self._collateral[asset] = max(0.0, old_val + delta)
            self.logger.info(
                f"[MarginShadowBook-{self._strategy_id}] CollateralUpdate: "
                f"{asset} delta={delta:+.4f} "
                f"{old_val:.4f} -> {self._collateral[asset]:.4f}"
            )
        else:
            symbol = (
                self._exchange.base_asset_to_symbol(asset)
                if self._exchange else None
            )
            if symbol and (
                not self._tracked_symbols or symbol in self._tracked_symbols
            ):
                curr = self._positions.get(symbol)
                old_qty = curr.quantity if curr else 0.0
                new_qty = old_qty + delta
                if curr is None:
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        venue=self._venue,
                        quantity=new_qty,
                        avg_entry_price=self._mark_prices.get(symbol, 0.0),
                    )
                else:
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        venue=curr.venue,
                        quantity=new_qty,
                        avg_entry_price=curr.avg_entry_price,
                        realized_pnl=curr.realized_pnl,
                        commission_by_asset=dict(curr.commission_by_asset),
                        locked_quantity=curr.locked_quantity,
                    )
                self.logger.info(
                    f"[MarginShadowBook-{self._strategy_id}] PositionUpdate: "
                    f"{symbol} qty {old_qty:.6f} -> {new_qty:.6f}"
                )
            else:
                self.logger.debug(
                    f"[MarginShadowBook-{self._strategy_id}] BalanceUpdate: "
                    f"{asset} -> symbol={symbol} not tracked, skipping"
                )

        self._refresh_derived()

    # ── Fill attribution ──────────────────────────────────────────────────

    def _adjust_cash(self, qty_delta: float, price: float, quote_asset: str) -> float:
        """Route fill proceeds / costs to isolated margin collateral.

        Overrides parent's _adjust_cash() which modifies self._cash.
        For isolated margin, the quote asset lives in self._collateral,
        not in self._cash.  self._cash stays at 0.0 throughout this book's
        lifetime so that inherited NAV reads via total_value() always use
        the MARGIN formula.
        """
        notional = abs(qty_delta) * price
        if qty_delta > 0:   # BUY: spend quote collateral
            self._collateral[quote_asset] = (
                self._collateral.get(quote_asset, 0.0) - notional
            )
        else:               # SELL: receive quote collateral
            self._collateral[quote_asset] = (
                self._collateral.get(quote_asset, 0.0) + notional
            )
        return notional

    def _track_commission(
        self,
        pos,
        commission_asset: Optional[str],
        commission: float,
        quote_asset: str,
    ) -> None:
        """Record commission amounts; route stablecoin fees to _collateral.

        Overrides parent which deducts stablecoin fees from self._cash.
        For isolated margin, the stablecoin balance lives in _collateral.
        Non-stablecoin fees (e.g. BNB) still reduce the position qty as in SPOT.
        """
        if commission_asset is None or commission <= 0:
            return

        from elysian_core.core.constants import STABLECOINS as _STABLECOINS

        pos.commission_by_asset[commission_asset] = (
            pos.commission_by_asset.get(commission_asset, 0.0) + commission
        )
        self._total_commission_dict[commission_asset] = (
            self._total_commission_dict.get(commission_asset, 0.0) + commission
        )

        if commission_asset not in _STABLECOINS:
            # Non-stablecoin fee: reduce position qty (same as parent).
            comm_symbol = self._asset_symbol_map.get(commission_asset)
            if comm_symbol and comm_symbol in self._positions:
                fee_pos = self._positions[comm_symbol]
                fee_pos.quantity = max(0.0, fee_pos.quantity - commission)
                self._position_deducted_comm[commission_asset] = (
                    self._position_deducted_comm.get(commission_asset, 0.0) + commission
                )
                if fee_pos.is_flat():
                    self._positions.pop(comm_symbol, None)
            else:
                raise ValueError(
                    f"Commission asset {commission_asset} not found in positions for deduction"
                )
        else:
            # Stablecoin fee: deduct from collateral (not from _cash).
            # No floor — collateral may temporarily go negative between
            # _track_commission() and _adjust_cash() within apply_fill(),
            # matching the parent ShadowBook behaviour for self._cash.
            self._collateral[commission_asset] = (
                self._collateral.get(commission_asset, 0.0) - commission
            )
