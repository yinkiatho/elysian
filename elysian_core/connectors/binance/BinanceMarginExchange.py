"""
Binance isolated margin exchange connector.

Implements :class:`IsolatedMarginExchangeConnector` using the python-binance
``AsyncClient`` for all REST operations.

Features
---------
* Isolated margin market and limit orders with configurable ``sideEffectType``
  (defaults to ``AUTO_BORROW_REPAY`` — Binance handles the loan lifecycle).
* Manual borrow / repay / transfer_in / transfer_out for strategies that
  need explicit loan control.
* Full margin account state sync: collateral, borrowed, interest, margin level.
* Real-time order and balance events via per-symbol listen-key WebSocket streams
  (``BinanceMarginUserDataClientManager``).
* Periodic margin account poll (60 s) to capture interest accrual, which is
  not pushed via the user-data stream.

Usage::

    exchange = BinanceMarginExchange(
        api_key="...", api_secret="...",
        symbols=["ETHUSDT"],
        private_event_bus=bus,
        strategy_config=cfg,
    )
    exchange.start(shared_event_bus)   # subscribe to kline/OB events
    await exchange.run()               # initialize + start background tasks
    await exchange.place_market_order("ETHUSDT", Side.BUY, 0.1)
    await exchange.cleanup()           # stop stream + close REST client
"""

import argparse
import asyncio
import collections
import datetime
from typing import Dict, List, Optional

import pylru
from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from binance.helpers import round_step_size

from elysian_core.connectors.base_margin import IsolatedMarginExchangeConnector
from elysian_core.connectors.binance.BinanceMarginDataConnectors import (
    BinanceMarginUserDataClientManager,
)
from elysian_core.config.app_config import StrategyConfig
from elysian_core.core.enums import (
    AssetType,
    MarginSideEffect,
    OrderStatus,
    OrderType,
    Side,
    Venue,
    _BINANCE_SIDE,
    _BINANCE_STATUS,
)
from elysian_core.core.order import Order
import elysian_core.utils.logger as log


class BinanceMarginExchange(IsolatedMarginExchangeConnector):
    """Authenticated Binance isolated margin exchange client.

    Manages:
      - Per-symbol isolated margin accounts (collateral, borrowed, interest, margin level).
      - Market and limit order placement using ``AUTO_BORROW_REPAY`` by default.
      - Manual borrow / repay / transfer_in / transfer_out for explicit loan control.
      - Real-time account events via per-symbol listen-key WebSocket streams.
      - Periodic margin account polling for interest accrual (every 60 s).
    """

    def __init__(
        self,
        args: argparse.Namespace = argparse.Namespace(),
        api_key: str = "",
        api_secret: str = "",
        symbols: Optional[List[str]] = None,
        file_path: Optional[str] = None,
        user_data_manager: Optional[BinanceMarginUserDataClientManager] = None,
        private_event_bus=None,
        strategy_config: Optional[StrategyConfig] = None,
        max_leverage: int = 3,
        default_side_effect: MarginSideEffect = MarginSideEffect.AUTO_BORROW_REPAY,
    ):
        symbols = symbols or []
        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols,
            file_path=file_path,
            venue=Venue.BINANCE,
            strategy_config=strategy_config,
            max_leverage=max_leverage,
            default_side_effect=default_side_effect,
        )

        self.strategy_id = strategy_config.strategy_id if strategy_config else "MAIN"

        # Per-symbol open orders (inherits _open_orders from base, added here for clarity)
        self._open_orders: Dict[str, collections.OrderedDict] = collections.defaultdict(
            collections.OrderedDict
        )
        self._past_orders = pylru.lrucache(1000)

        # REST client (created in initialize())
        self._client: Optional[AsyncClient] = None

        # User-data stream manager (one WebSocket per symbol)
        self.user_data_manager = user_data_manager or BinanceMarginUserDataClientManager(
            api_key, api_secret, symbols
        )
        if private_event_bus:
            self.user_data_manager.set_event_bus(private_event_bus)

        # Register our state-update callback so internal state stays in sync
        # with exchange events arriving before the event bus publish.
        self.user_data_manager.register(self._handle_user_data)

    # ── Initialisation ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Create the REST client, fetch symbol metadata, sync margin state, start stream."""
        self._client = await AsyncClient.create(self._api_key, self._api_secret)

        for sym in self._symbols:
            await self._fetch_symbol_info(sym)

        # Populate token info in manager so OrderUpdateEvents carry correct base/quote
        self.user_data_manager.set_token_info(self._token_infos)

        # Initial full margin account sync
        await self.refresh_margin_account()

        # Start isolated margin user-data stream
        try:
            await self.user_data_manager.start()
            self.logger.info("BinanceMarginExchange: isolated margin user-data stream started")
        except Exception as e:
            self.logger.error(f"BinanceMarginExchange: failed to start user-data stream: {e}")

        self.logger.info(f"BinanceMarginExchange initialised for {self._symbols}")

    async def _fetch_symbol_info(self, symbol: str) -> None:
        """Fetch LOT_SIZE, NOTIONAL filter, and asset names for *symbol*."""
        info = await self._client.get_symbol_info(symbol)
        filters = {f["filterType"]: f for f in info.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))

        self._token_infos[symbol] = {
            "step_size": float(lot.get("stepSize", "0.0001")),
            "min_notional": float(notional.get("minNotional", "10.0")),
            "base_asset": info.get("baseAsset", ""),
            "quote_asset": info.get("quoteAsset", ""),
            "base_asset_precision": int(info.get("baseAssetPrecision", 8)),
            "quote_asset_precision": int(info.get("quoteAssetPrecision", 8)),
        }
        self.logger.info(f"[{symbol}] Margin symbol info fetched")

    # ── Account state ─────────────────────────────────────────────────────────

    async def refresh_balances(self) -> None:
        """Refresh free balances for all isolated margin symbols."""
        for symbol in self._symbols:
            try:
                account = await self._client.get_isolated_margin_account(symbols=symbol)
                for asset_data in account.get("assets", []):
                    if asset_data.get("symbol") != symbol:
                        continue
                    for side_key in ("baseAsset", "quoteAsset"):
                        ai = asset_data.get(side_key, {})
                        asset_name = ai.get("asset", "")
                        if asset_name:
                            self._balances[asset_name] = float(ai.get("free", 0))
            except Exception as e:
                self.logger.error(f"[{symbol}] Failed to refresh margin balance: {e}")

    async def refresh_margin_account(self) -> None:
        """Fetch and update full margin state for all symbols.

        Updates collateral, borrowed, accrued interest, margin level, and
        free balances from the Binance isolated margin account endpoint.
        """
        for symbol in self._symbols:
            try:
                account = await self._client.get_isolated_margin_account(symbols=symbol)
                for asset_data in account.get("assets", []):
                    if asset_data.get("symbol") != symbol:
                        continue

                    self._margin_level[symbol] = float(asset_data.get("marginLevel", 0))

                    collateral: Dict[str, float] = {}
                    borrowed: Dict[str, float] = {}
                    interest: Dict[str, float] = {}

                    for side_key in ("baseAsset", "quoteAsset"):
                        ai = asset_data.get(side_key, {})
                        asset_name = ai.get("asset", "")
                        if not asset_name:
                            continue

                        free = float(ai.get("free", 0))
                        locked = float(ai.get("locked", 0))
                        collateral[asset_name] = free + locked
                        self._balances[asset_name] = free

                        borrow_amt = float(ai.get("borrowed", 0))
                        if borrow_amt > 0:
                            borrowed[asset_name] = borrow_amt

                        interest_amt = float(ai.get("interest", 0))
                        if interest_amt > 0:
                            interest[asset_name] = interest_amt

                    self._collateral[symbol] = collateral
                    self._borrowed[symbol] = borrowed
                    self._accrued_interest[symbol] = interest

                    self.logger.debug(
                        f"[{symbol}] Margin level={self._margin_level[symbol]:.4f} "
                        f"collateral={collateral} borrowed={borrowed} interest={interest}"
                    )
            except Exception as e:
                self.logger.error(f"[{symbol}] Failed to refresh margin account: {e}")

    # ── User-data stream callback ─────────────────────────────────────────────

    def _handle_user_data(self, event: dict) -> None:
        """Synchronous callback receiving raw WS events before the event bus publish.

        Keeps ``_balances`` and ``_open_orders`` in sync with exchange state.
        The manager handles typed event publishing to the private bus.
        """
        et = event.get("e")

        if et == "balanceUpdate":
            asset = event.get("a")
            delta = float(event.get("d", 0))
            self._balances[asset] = self._balances.get(asset, 0.0) + delta
            self.logger.info(
                f"[BinanceMarginExchange_{self.strategy_id}] "
                f"[{asset}] balance delta={delta:+.6f} new={self._balances[asset]:.6f}"
            )

        elif et == "outboundAccountPosition":
            for b in event.get("B", []):
                asset = b.get("a", "")
                self._balances[asset] = float(b.get("f", 0)) + float(b.get("l", 0))
            self.logger.info(
                f"[BinanceMarginExchange_{self.strategy_id}] outboundAccountPosition applied"
            )

        elif et == "executionReport":
            order = BinanceMarginUserDataClientManager._parse_execution_report(event)
            if order is not None:
                exec_type = event.get("x")
                self.logger.info(
                    f"[BinanceMarginExchange_{self.strategy_id}] "
                    f"Execution report @ {datetime.datetime.now(datetime.timezone.utc)}"
                )
                self._handle_execution_report(order, exec_type)

    def _handle_execution_report(self, order: Order, exec_type: str) -> None:
        """Update ``_open_orders`` from a received ``Order``."""
        symbol, order_id = order.symbol, order.id
        symbol_orders = self._open_orders[symbol]

        if exec_type == "NEW" and order_id not in symbol_orders:
            symbol_orders[order_id] = order
            self.logger.info(
                f"[BinanceMarginExchange_{self.strategy_id}] [{symbol}] New order: {order_id}"
            )
            return

        existing = symbol_orders.get(order_id)
        if existing is None:
            self.logger.warning(
                f"[{symbol}] executionReport for unknown order {order_id}, skipping"
            )
            return

        if order.filled_qty > existing.filled_qty:
            existing.filled_qty = order.filled_qty
            existing.avg_fill_price = order.avg_fill_price
            existing.last_fill_price = order.last_fill_price

        existing.commission += order.commission
        existing.commission_asset = order.commission_asset or existing.commission_asset
        existing.last_updated_timestamp = order.last_updated_timestamp
        existing.transition_to(order.status)

        self.logger.info(
            f"[BinanceMarginExchange_{self.strategy_id}] [{symbol}] Order updated: {existing}"
        )

        if existing.is_terminal:
            closed = symbol_orders.pop(order_id, None)
            self._past_orders[order_id] = closed

    # ── Loan management ───────────────────────────────────────────────────────

    async def borrow(self, symbol: str, asset: str, amount: float) -> bool:
        """Borrow *amount* of *asset* in the isolated margin account of *symbol*."""
        try:
            await self._client.create_margin_loan(
                asset=asset,
                amount=str(amount),
                isIsolated="TRUE",
                symbol=symbol,
            )
            self.logger.info(f"[{symbol}] Borrowed {amount} {asset}")
            return True
        except Exception as e:
            self.logger.error(f"[{symbol}] borrow {amount} {asset} failed: {e}")
            return False

    async def repay(self, symbol: str, asset: str, amount: float) -> bool:
        """Repay *amount* of *asset* to the isolated margin account of *symbol*."""
        try:
            await self._client.repay_margin_loan(
                asset=asset,
                amount=str(amount),
                isIsolated="TRUE",
                symbol=symbol,
            )
            self.logger.info(f"[{symbol}] Repaid {amount} {asset}")
            return True
        except Exception as e:
            self.logger.error(f"[{symbol}] repay {amount} {asset} failed: {e}")
            return False

    async def transfer_in(self, symbol: str, asset: str, amount: float) -> bool:
        """Transfer *amount* of *asset* from spot wallet into the isolated margin account."""
        try:
            await self._client.transfer_spot_to_isolated_margin(
                asset=asset,
                symbol=symbol,
                amount=str(amount),
            )
            self.logger.info(f"[{symbol}] Transferred in {amount} {asset}")
            return True
        except Exception as e:
            self.logger.error(f"[{symbol}] transfer_in {amount} {asset} failed: {e}")
            return False

    async def transfer_out(self, symbol: str, asset: str, amount: float) -> bool:
        """Transfer *amount* of *asset* from the isolated margin account back to spot."""
        try:
            await self._client.transfer_isolated_margin_to_spot(
                asset=asset,
                symbol=symbol,
                amount=str(amount),
            )
            self.logger.info(f"[{symbol}] Transferred out {amount} {asset}")
            return True
        except Exception as e:
            self.logger.error(f"[{symbol}] transfer_out {amount} {asset} failed: {e}")
            return False

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        strategy_id: int = 0,
        use_quote_order_qty: bool = False,
        side_effect: Optional[MarginSideEffect] = None,
    ) -> Optional[dict]:
        """Place an isolated margin market order.

        *side_effect* defaults to the connector's ``default_side_effect``
        (``AUTO_BORROW_REPAY``).  Binance will automatically borrow on entry
        and repay on exit when this mode is active.

        Returns the raw Binance REST response on success, ``None`` on failure.
        """
        if not self.order_health_check(symbol, side, quantity, use_quote_order_qty):
            self.logger.error(
                f"[{symbol}] Margin market order health check failed — not placed"
            )
            return None

        info = self._token_infos.get(symbol, {})
        step_size = info.get("step_size", 0.001)
        side_str = "BUY" if side == Side.BUY else "SELL"
        side_effect_str = (side_effect or self._default_side_effect).value

        try:
            qty = round_step_size(abs(quantity), step_size)
            kwargs = dict(
                symbol=symbol,
                side=side_str,
                type="MARKET",
                isIsolated="TRUE",
                sideEffectType=side_effect_str,
                newOrderRespType="FULL",
                strategyId=strategy_id or self.strategy_id,
            )
            if use_quote_order_qty:
                kwargs["quoteOrderQty"] = qty
            else:
                kwargs["quantity"] = qty

            resp = await self._client.create_margin_order(**kwargs)

            avg_price, total_comm = self._average_fill_price(resp)
            total_qty = float(resp.get("executedQty", qty))
            base_asset = info.get("base_asset", "")
            quote_asset = info.get("quote_asset", "")
            comm_asset = (resp.get("fills") or [{}])[0].get("commissionAsset", "")

            self.logger.success(
                f"[{symbol}] Margin {side_str} {total_qty} {base_asset} "
                f"@ {avg_price:.6f} {quote_asset} "
                f"comm={total_comm:.8f} {comm_asset} "
                f"sideEffect={side_effect_str}"
            )
            return resp

        except BinanceAPIException as e:
            self.logger.error(
                f"[{symbol}] Margin market order API error ({e.code}): {e.message}"
            )
            return None
        except Exception as e:
            self.logger.error(f"[{symbol}] Margin market order failed: {e}")
            return None

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        price: float,
        strategy_id: int = 0,
        side_effect: Optional[MarginSideEffect] = None,
    ) -> Optional[Order]:
        """Place an isolated margin GTC limit order.

        Returns an :class:`Order` tracking object on success, ``None`` on failure.
        The order is registered in ``_open_orders`` immediately so that
        execution reports arriving via the user-data stream can update it.
        """
        if not self.order_health_check(symbol, side, quantity):
            self.logger.error(
                f"[{symbol}] Margin limit order health check failed — not placed"
            )
            return None

        info = self._token_infos.get(symbol, {})
        step_size = info.get("step_size", 0.001)
        side_str = "BUY" if side == Side.BUY else "SELL"
        side_effect_str = (side_effect or self._default_side_effect).value

        try:
            qty = round_step_size(abs(quantity), step_size)
            resp = await self._client.create_margin_order(
                symbol=symbol,
                side=side_str,
                type="LIMIT",
                timeInForce="GTC",
                quantity=qty,
                price=str(price),
                isIsolated="TRUE",
                sideEffectType=side_effect_str,
                strategyId=strategy_id or self.strategy_id,
            )

            order_id = str(resp["orderId"])
            order = Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=price,
                status=_BINANCE_STATUS.get(resp.get("status"), OrderStatus.OPEN),
                venue=Venue.BINANCE,
                strategy_id=strategy_id or self.strategy_id,
            )
            self._open_orders[symbol][order_id] = order
            self._past_orders[order_id] = order
            self.logger.info(
                f"[{symbol}] Margin limit {side_str} {qty} @ {price} placed — id={order_id}"
            )
            return order

        except BinanceAPIException as e:
            self.logger.error(
                f"[{symbol}] Margin limit order API error ({e.code}): {e.message}"
            )
            return None
        except Exception as e:
            self.logger.error(f"[{symbol}] Margin limit order failed: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open isolated margin order."""
        try:
            await self._client.cancel_margin_order(
                symbol=symbol,
                orderId=order_id,
                isIsolated="TRUE",
            )
            self._open_orders[symbol].pop(order_id, None)
            self.logger.info(f"[{symbol}] Order {order_id} cancelled")
            return True
        except BinanceAPIException as e:
            self.logger.error(
                f"[{symbol}] Cancel margin order {order_id} API error ({e.code}): {e.message}"
            )
            return False
        except Exception as e:
            self.logger.error(f"[{symbol}] Cancel margin order {order_id} failed: {e}")
            return False

    async def get_open_orders(self, symbol: str) -> List[Order]:
        """Fetch and reconcile open isolated margin orders for *symbol*."""
        try:
            raw_orders = await self._client.get_open_margin_orders(
                symbol=symbol,
                isIsolated="TRUE",
            )
            orders = []
            for o in raw_orders:
                cum_filled = float(o.get("executedQty", 0))
                cum_quote = float(o.get("cummulativeQuoteQty", 0))
                avg_price = (cum_quote / cum_filled) if cum_filled > 0 else 0.0

                order = Order(
                    id=str(o["orderId"]),
                    symbol=symbol,
                    side=_BINANCE_SIDE.get(o.get("side", "BUY"), Side.BUY),
                    order_type=(
                        OrderType.LIMIT if o.get("type") == "LIMIT" else OrderType.MARKET
                    ),
                    quantity=float(o.get("origQty", 0)),
                    price=float(o.get("price", 0)) or None,
                    status=_BINANCE_STATUS.get(o.get("status"), OrderStatus.OPEN),
                    avg_fill_price=avg_price,
                    filled_qty=cum_filled,
                    venue=Venue.BINANCE,
                    strategy_id=self.strategy_id,
                )
                self._open_orders[symbol][order.id] = order
                orders.append(order)
            return orders
        except Exception as e:
            self.logger.error(f"[{symbol}] get_open_orders failed: {e}")
            return []

    # ── Unsupported spot-only operations ──────────────────────────────────────

    async def get_deposit_address(self, coin: str, network: Optional[str] = None):
        """Not applicable to isolated margin accounts."""
        raise NotImplementedError("Isolated margin accounts do not support direct deposits")

    async def deposit_asset(self, coin: str, amount: float, network: Optional[str] = None):
        """Not applicable to isolated margin accounts — use transfer_in() instead."""
        raise NotImplementedError("Use transfer_in() to fund an isolated margin account")

    async def withdraw_asset(self, coin: str, amount: float, address: str, network: str = ""):
        """Not applicable to isolated margin accounts — use transfer_out() instead."""
        raise NotImplementedError("Use transfer_out() to withdraw from an isolated margin account")

    # ── Background monitoring ─────────────────────────────────────────────────

    async def _monitor_balances(self) -> None:
        """Refresh balances every 10 seconds as a safety net against missed events."""
        while True:
            try:
                await asyncio.sleep(10)
                await self.refresh_balances()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"BinanceMarginExchange: balance monitor error: {e}")

    async def _monitor_open_orders(self) -> None:
        """Reconcile open orders every 30 seconds to catch any missed execution reports."""
        while True:
            try:
                await asyncio.sleep(30)
                for symbol in self._symbols:
                    await self.get_open_orders(symbol)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"BinanceMarginExchange: open order monitor error: {e}")

    # ── Run / Cleanup ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Initialise the connector and start all background tasks.

        Call ``exchange.start(shared_event_bus)`` before ``run()`` to wire up
        the kline and order book event subscriptions.
        """
        await self.initialize()
        asyncio.create_task(self._monitor_balances())
        asyncio.create_task(self._monitor_open_orders())
        self._start_margin_poll()
        await asyncio.sleep(0.5)  # let first balance fetch settle
        self.logger.info("BinanceMarginExchange running")

    async def cleanup(self) -> None:
        """Stop all background tasks and close connections cleanly."""
        self.stop()  # unsubscribe from shared event bus

        await self._stop_margin_poll()

        if self.user_data_manager:
            await self.user_data_manager.stop()

        if self._client:
            await self._client.close_connection()
            self._client = None

        self.logger.info("BinanceMarginExchange cleanup completed")
