"""
Abstract base class for isolated-margin exchange connectors.

Extends :class:`SpotExchangeConnector` with margin-specific state and operations.
Concrete implementations (e.g. ``BinanceMarginExchange``) inherit from this class.

Margin state tracked per isolated symbol
-----------------------------------------
_collateral       : Free + locked assets in the isolated margin wallet per symbol.
_borrowed         : Outstanding loan amounts per asset per symbol.
_accrued_interest : Accumulated unpaid interest per asset per symbol.
_margin_level     : Current margin level (net asset / borrowed) per symbol.
_max_leverage     : Max leverage limit for this connector.
_default_side_effect : Default sideEffectType sent with every margin order.

NAV formula (per isolated symbol)
-----------------------------------
NAV = Σ(collateral * price) - Σ(borrowed * price) - Σ(interest * price)
"""

import asyncio
import argparse
from abc import abstractmethod
from typing import Dict, List, Optional

from elysian_core.connectors.base import SpotExchangeConnector
from elysian_core.config.app_config import StrategyConfig
from elysian_core.core.enums import AssetType, MarginSideEffect, Side, Venue
import elysian_core.utils.logger as log


class IsolatedMarginExchangeConnector(SpotExchangeConnector):
    """Abstract base for Binance isolated-margin connectors.

    Inherits all feed infrastructure (kline/OB event subscriptions, ``last_price``,
    ``order_health_check``) from :class:`SpotExchangeConnector` and adds:

    * Per-symbol margin state (collateral, borrowed, interest, margin level).
    * Abstract borrow / repay / transfer_in / transfer_out methods.
    * An overridden ``order_health_check`` that skips balance sufficiency for
      ``AUTO_BORROW_REPAY`` orders — Binance handles the loan lifecycle.
    * A periodic background poll (``_margin_poll_loop``) for interest accrual,
      since Binance does not push interest events via the user-data stream.
    """

    # Seconds between margin account polls (interest accrual is not pushed).
    INTEREST_POLL_INTERVAL_S: int = 60

    def __init__(
        self,
        args: argparse.Namespace,
        api_key: str,
        api_secret: str,
        symbols: List[str],
        file_path: Optional[str] = None,
        venue: Venue = Venue.BINANCE,
        strategy_config: Optional[StrategyConfig] = None,
        max_leverage: int = 3,
        default_side_effect: MarginSideEffect = MarginSideEffect.AUTO_BORROW_REPAY,
    ):
        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols,
            file_path=file_path,
            venue=venue,
            strategy_config=strategy_config,
        )

        self._max_leverage: int = max_leverage
        self._default_side_effect: MarginSideEffect = default_side_effect

        # Per-symbol isolated margin state
        # symbol -> float (margin level = net asset / borrowed; >1 means healthy)
        self._margin_level: Dict[str, float] = {}
        # symbol -> {asset: float}  (total collateral = free + locked)
        self._collateral: Dict[str, Dict[str, float]] = {}
        # symbol -> {asset: float}  (outstanding borrowed principal)
        self._borrowed: Dict[str, Dict[str, float]] = {}
        # symbol -> {asset: float}  (accrued but not yet repaid interest)
        self._accrued_interest: Dict[str, Dict[str, float]] = {}

        # Background task for periodic interest/margin poll
        self._margin_poll_task: Optional[asyncio.Task] = None

    # ── Margin state accessors ─────────────────────────────────────────────────

    def margin_level(self, symbol: str) -> Optional[float]:
        """Current margin level for an isolated symbol (net asset / borrowed).

        Returns ``None`` if the level has not been fetched yet.
        A level below the maintenance margin triggers liquidation.
        """
        return self._margin_level.get(symbol)

    def borrowed(self, symbol: str) -> Dict[str, float]:
        """Outstanding loan amounts per asset for *symbol*.

        Example return value: ``{"ETH": 0.5}``
        """
        return dict(self._borrowed.get(symbol, {}))

    def collateral(self, symbol: str) -> Dict[str, float]:
        """Total collateral (free + locked) per asset in the isolated margin wallet."""
        return dict(self._collateral.get(symbol, {}))

    def accrued_interest(self, symbol: str) -> Dict[str, float]:
        """Accrued unpaid interest per asset for *symbol*."""
        return dict(self._accrued_interest.get(symbol, {}))

    def net_nav(
        self,
        symbol: str,
        prices: Optional[Dict[str, float]] = None,
    ) -> float:
        """Isolated margin NAV for a single symbol in quote-asset terms.

        ``NAV = Σ(collateral·price) - Σ(borrowed·price) - Σ(interest·price)``

        *prices* maps asset name → USDT price.  Stablecoins default to 1.0.
        """
        p = prices or {}
        nav = 0.0
        for asset, qty in self._collateral.get(symbol, {}).items():
            nav += qty * p.get(asset, 1.0)
        for asset, qty in self._borrowed.get(symbol, {}).items():
            nav -= qty * p.get(asset, 1.0)
        for asset, qty in self._accrued_interest.get(symbol, {}).items():
            nav -= qty * p.get(asset, 1.0)
        return nav

    # ── Order health check override ────────────────────────────────────────────

    def order_health_check(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        use_quote_order_qty: bool = False,
    ) -> bool:
        """Isolated margin order validation.

        Balance sufficiency is intentionally skipped: ``AUTO_BORROW_REPAY``
        instructs Binance to borrow the required amount automatically.
        Only the minimum notional size is enforced here.
        """
        info = self._token_infos.get(symbol, {})
        if not info:
            self.logger.error(f"[{symbol}] order_health_check: symbol info not found")
            return False

        price = self.last_price(symbol) or 0.0
        min_notional = info.get("min_notional", 0.0)
        estimated_notional = (
            price * abs(quantity) if not use_quote_order_qty else abs(quantity)
        )

        if estimated_notional < min_notional:
            self.logger.error(
                f"[{symbol}] Notional {estimated_notional:.4f} < min {min_notional}"
            )
            return False

        return True

    # ── Abstract margin operations ─────────────────────────────────────────────

    @abstractmethod
    async def borrow(self, symbol: str, asset: str, amount: float) -> bool:
        """Borrow *amount* of *asset* for the isolated margin account of *symbol*.

        Returns ``True`` if the loan was created, ``False`` on error.
        Only needed when ``side_effect=NO_SIDE_EFFECT`` or ``MARGIN_BUY``.
        With ``AUTO_BORROW_REPAY`` the exchange handles borrowing automatically.
        """
        ...

    @abstractmethod
    async def repay(self, symbol: str, asset: str, amount: float) -> bool:
        """Repay *amount* of *asset* to the isolated margin account of *symbol*.

        Returns ``True`` on success, ``False`` on error.
        """
        ...

    @abstractmethod
    async def transfer_in(self, symbol: str, asset: str, amount: float) -> bool:
        """Transfer *amount* of *asset* from the spot wallet into this isolated margin account.

        Returns ``True`` on success.
        """
        ...

    @abstractmethod
    async def transfer_out(self, symbol: str, asset: str, amount: float) -> bool:
        """Transfer *amount* of *asset* out of this isolated margin account to the spot wallet.

        Returns ``True`` on success.
        """
        ...

    @abstractmethod
    async def refresh_margin_account(self) -> None:
        """Fetch and update the full margin state for all symbols from the exchange REST API.

        Implementations must update:
          ``self._collateral[symbol]``
          ``self._borrowed[symbol]``
          ``self._accrued_interest[symbol]``
          ``self._margin_level[symbol]``
        """
        ...

    # ── Periodic margin poll ───────────────────────────────────────────────────

    async def _margin_poll_loop(self) -> None:
        """Poll the margin account every :attr:`INTEREST_POLL_INTERVAL_S` seconds.

        Binance does **not** push interest accrual events, so periodic polling
        is the only way to keep ``_accrued_interest`` current.
        """
        while True:
            try:
                await asyncio.sleep(self.INTEREST_POLL_INTERVAL_S)
                await self.refresh_margin_account()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(f"[IsolatedMarginExchangeConnector] margin poll error: {exc}")

    def _start_margin_poll(self) -> None:
        """Schedule the background margin-poll task."""
        if self._margin_poll_task is None or self._margin_poll_task.done():
            self._margin_poll_task = asyncio.create_task(self._margin_poll_loop())

    async def _stop_margin_poll(self) -> None:
        """Cancel the background margin-poll task and await its completion."""
        if self._margin_poll_task and not self._margin_poll_task.done():
            self._margin_poll_task.cancel()
            try:
                await self._margin_poll_task
            except asyncio.CancelledError:
                pass
            self._margin_poll_task = None
