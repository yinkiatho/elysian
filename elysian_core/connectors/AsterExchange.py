import asyncio
import collections
import datetime
import hashlib
import hmac
import math
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
import argparse
import requests

from elysian_core.connectors.base import SpotExchangeConnector
from elysian_core.connectors.AsterDataConnectors import (
    AsterKlineClientManager,
    AsterOrderBookClientManager,
    AsterUserDataClientManager,
)
from elysian_core.core.enums import (
    OrderStatus, Side, AssetType, Venue, OrderType,
    _BINANCE_SIDE, _BINANCE_STATUS,
)
from elysian_core.core.order import Order
from elysian_core.core.market_data import Kline, OrderBook
from elysian_core.db.models import CexTrade
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

SPOT_BASE_ENDPOINT = "https://sapi.asterdex.com"


# ──────────────────────────────────────────────────────────────────────────────
# AsterSpotExchange
# ──────────────────────────────────────────────────────────────────────────────

class AsterSpotExchange(SpotExchangeConnector):
    """
    Authenticated Aster spot exchange client.

    Manages:
      - Per-symbol AsterKlineFeed and AsterOrderBookFeed
      - Account balances (polled periodically)
      - Open orders registry
      - Market and limit order placement / cancellation
      - User data stream (executions, balance updates)

    Usage:
        exchange = AsterSpotExchange(
            api_key="...", api_secret="...",
            symbols=["ETHUSDT", "SUIUSDT"],
        )
        await exchange.run()
        await exchange.place_market_order(...)
    """

    _STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})

    def __init__(
        self,
        args: argparse.Namespace = argparse.Namespace(),
        api_key: str = "",
        api_secret: str = "",
        symbols: Optional[List[str]] = None,
        file_path: Optional[str] = None,
        kline_manager: Optional[AsterKlineClientManager] = None,
        ob_manager: Optional[AsterOrderBookClientManager] = None,
        user_data_manager: Optional[AsterUserDataClientManager] = None,
    ):
        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols or [],
            file_path=file_path,
            kline_manager=kline_manager,
            ob_manager=ob_manager,
        )

        self.user_data_manager = user_data_manager or AsterUserDataClientManager(api_key, api_secret)
        self._session: Optional[aiohttp.ClientSession] = None

        self.strategy_name = getattr(args, "strategy_name", "")
        self.strategy_id   = getattr(args, "strategy_id", "")

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        """Add timestamp + HMAC SHA256 signature to a params dict (in-place)."""
        params["timestamp"] = int(time.time() * 1000)
        qs  = urlencode(params)
        sig = hmac.new(self._api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self._api_key}

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        signed: bool = False,
    ) -> Any:
        """
        Async HTTP request against the Aster REST API.
        Returns the parsed JSON body; raises on non-2xx responses.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        url = SPOT_BASE_ENDPOINT + path
        p   = dict(params or {})
        if signed:
            self._sign(p)

        async with self._session.request(
            method, url, params=p if method.upper() == "GET" else None,
            data=p if method.upper() != "GET" else None,
            headers=self._headers(),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                logger.error(f"Aster API error {resp.status}: {body}")
                resp.raise_for_status()
            return body

    # ── Initialisation ────────────────────────────────────────────────────────

    async def initialize(self):
        """Ping the exchange, fetch symbol metadata, refresh balances and start user-data stream."""
        self._session = aiohttp.ClientSession()

        # Health check
        try:
            await self._request("GET", "/api/v1/ping")
            logger.info("AsterSpotExchange: ping OK")
        except Exception as e:
            logger.warning(f"AsterSpotExchange: ping failed: {e}")

        for sym in self._symbols:
            await self._fetch_symbol_info(sym)

        await self.refresh_balances()

        try:
            self.user_data_manager.register(self._handle_user_data)
            await self.user_data_manager.start()
            logger.info("AsterSpotExchange: user data stream started")
        except Exception as e:
            logger.error(f"AsterSpotExchange: failed to start user data stream: {e}")

        logger.info("AsterSpotExchange initialised.")


    async def _fetch_symbol_info(self, symbol: str):
        """Fetch symbol filters (step size, min notional) from /api/v1/exchangeInfo."""
        try:
            data = await self._request("GET", "/api/v1/exchangeInfo", params={"symbol": symbol})
            symbols = data.get("symbols", [])
            info = next((s for s in symbols if s["symbol"] == symbol), None)
            if not info:
                logger.warning(f"[{symbol}] exchangeInfo: symbol not found")
                return

            filters = info.get("filters", [])

            step_size = float(
                next((f["stepSize"] for f in filters if f["filterType"] == "LOT_SIZE"), "0.0001")
            )
            min_notional = float(
                next(
                    (f.get("minNotional", f.get("minQty", 0))
                     for f in filters
                     if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL")),
                    0.0,
                )
            )

            self._token_infos[symbol] = {
                "step_size":             step_size,
                "min_notional":          min_notional,
                "base_asset":            info.get("baseAsset", ""),
                "quote_asset":           info.get("quoteAsset", ""),
                "base_asset_precision":  info.get("baseAssetPrecision", 8),
                "quote_asset_precision": info.get("quoteAssetPrecision", 8),
            }
        except Exception as e:
            logger.error(f"[{symbol}] _fetch_symbol_info error: {e}")

    # ── Account ───────────────────────────────────────────────────────────────

    async def refresh_balances(self):
        """Fetch account balances from /api/v1/account."""
        try:
            account = await self._request("GET", "/api/v1/account", signed=True)
            for b in account.get("balances", []):
                self._balances[b["asset"]] = float(b["free"])
        except Exception as e:
            logger.error(f"AsterSpotExchange: refresh_balances error: {e}")

    def _handle_user_data(self, msg: dict):
        """Callback invoked by the user-data manager when an event arrives."""
        et = msg.get("e")

        if et == "balanceUpdate":
            asset = msg.get("a")
            delta = float(msg.get("d", 0))
            self._balances[asset] = self._balances.get(asset, 0.0) + delta
            logger.info(f"[{asset}] balance update delta={delta:.6f} new={self._balances[asset]:.6f}")

        elif et == "outboundAccountPosition":
            for bal in msg.get("B", []):
                asset  = bal.get("a")
                free   = float(bal.get("f", 0))
                locked = float(bal.get("l", 0))
                self._balances[asset] = free + locked
            logger.info("outboundAccountPosition applied to balances")

        elif et == "executionReport":
            logger.info(
                f"Execution report @ "
                f"{datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))}"
            )
            self._handle_execution_report(msg)

    def _handle_execution_report(self, msg: dict):
        """
        Update open-orders registry from a user-data executionReport.

        Key fields (Binance-compatible):
          i  → Order ID
          s  → Symbol
          X  → Current order status
          x  → Execution type
          z  → Cumulative filled quantity
          Z  → Cumulative quote asset transacted quantity
          L  → Last executed price
          n  → Commission amount
          N  → Commission asset
        """
        order_id  = str(msg.get("i"))
        symbol    = msg.get("s")
        status    = msg.get("X")
        exec_type = msg.get("x")

        internal_status = _BINANCE_STATUS.get(status)
        if internal_status is None:
            logger.warning(f"[{symbol}] Unknown order status '{status}' for order {order_id}")
            return

        symbol_orders = self._open_orders.get(symbol, {})

        # ── New order ─────────────────────────────────────────────────────────
        if exec_type == "NEW" and order_id not in symbol_orders:
            side      = Side.BUY if msg.get("S") == "BUY" else Side.SELL
            raw_type  = msg.get("o", "LIMIT")
            if raw_type.upper() == "MARKET":
                order_type = OrderType.MARKET
            elif raw_type.upper() == "LIMIT":
                order_type = OrderType.LIMIT
            else:
                order_type = OrderType.OTHERS

            order = Order(
                id          = order_id,
                symbol      = symbol,
                side        = side,
                order_type  = order_type,
                quantity    = float(msg.get("q", 0)),
                price       = float(msg.get("p", 0)) or None,
                status      = internal_status,
                timestamp   = datetime.datetime.fromtimestamp(
                                  msg.get("O", 0) / 1000,
                                  tz=datetime.timezone.utc,
                              ),
                venue                  = Venue.ASTER,
                commission             = float(msg.get("n", 0)),
                commission_asset       = msg.get("N"),
                last_updated_timestamp = int(msg.get("E", 0)),
            )
            self._open_orders[symbol][order_id] = order
            logger.info(f"[{symbol}] New order registered: {order}")
            return

        # ── Update existing order ─────────────────────────────────────────────
        order = symbol_orders.get(order_id)
        if order is None:
            logger.warning(
                f"[{symbol}] executionReport for unknown order {order_id} (status={status}), skipping"
            )
            return

        cum_filled_qty = float(msg.get("z", 0))
        cum_quote_qty  = float(msg.get("Z", 0))
        last_price     = float(msg.get("L", 0))
        avg_price      = (cum_quote_qty / cum_filled_qty) if cum_filled_qty > 0 else last_price

        if cum_filled_qty > order.filled_qty:
            order.update_fill(cum_filled_qty, avg_price)

        order.commission      += float(msg.get("n", 0))
        order.commission_asset = msg.get("N") or order.commission_asset
        order.status           = internal_status
        logger.info(f"[{symbol}] Order updated: {order}")

        if not order.is_active:
            self._open_orders[symbol].pop(order_id, None)
            logger.info(f"[{symbol}] Order {order_id} removed from open orders (status={status})")

    # ── Deposit / Withdraw (stubs) ────────────────────────────────────────────

    async def get_deposit_address(self, coin: str, network: Optional[str] = None) -> Optional[str]:
        """Not supported by Aster spot API — returns None."""
        logger.warning("get_deposit_address: not supported on Aster spot")
        return None

    async def deposit_asset(self, coin: str, amount: float, network: Optional[str] = None) -> bool:
        """Not applicable for CEX deposit flow — returns False."""
        logger.warning("deposit_asset: not supported on Aster spot")
        return False

    async def withdraw_asset(
        self, coin: str, amount: float, address: str, network: Optional[str] = None
    ) -> bool:
        """Submit a withdrawal via /api/v1/withdraw (signed)."""
        params: dict = {"coin": coin, "address": address, "amount": str(amount)}
        if network:
            params["network"] = network
        try:
            resp = await self._request("POST", "/api/v1/withdraw", params=params, signed=True)
            logger.info(f"Withdrew {amount} {coin} → {address}  resp={resp}")
            return True
        except Exception as e:
            logger.error(f"withdraw_asset error: {e}")
            return False

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_limit_order(
        self, symbol: str, side: Side, price: float, quantity: float, strategy_id: int = 0
    ):
        """Place a GTC limit order via POST /api/v1/order."""
        params = {
            "symbol":      symbol,
            "side":        side.value.upper(),
            "type":        "LIMIT",
            "timeInForce": "GTC",
            "quantity":    self._round_step(symbol, quantity),
            "price":       str(price),
        }
        try:
            resp = await self._request("POST", "/api/v1/order", params=params, signed=True)
            logger.info(f"[{symbol}] Limit order placed: {resp}")
            return resp
        except Exception as e:
            logger.error(f"[{symbol}] place_limit_order error: {e}")

    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        use_quote_order_qty: bool = False,
    ):
        """Place a market order via POST /api/v1/order."""
        if not self.order_health_check(symbol, side, quantity, use_quote_order_qty):
            logger.error(f"[{symbol}] Market order health check failed.")
            return

        info        = self._token_infos.get(symbol, {})
        base_asset  = info.get("base_asset", "")
        quote_asset = info.get("quote_asset", "")
        price       = self.last_price(symbol) or 0.0

        params: dict = {
            "symbol": symbol,
            "side":   side.value.upper(),
            "type":   "MARKET",
        }

        if not use_quote_order_qty:
            qty = self._round_step(symbol, abs(quantity))
            params["quantity"] = qty
            logger.info(
                f"[{symbol}] Market {'BUY' if side == Side.BUY else 'SELL'} "
                f"qty={qty} {base_asset} (~{qty * price:.2f} {quote_asset})"
            )
        else:
            qty = round(abs(quantity), 2)
            params["quoteOrderQty"] = qty
            logger.info(
                f"[{symbol}] Market {'BUY' if side == Side.BUY else 'SELL'} "
                f"quoteQty={qty:.2f} {quote_asset}"
            )

        try:
            resp = await self._request("POST", "/api/v1/order", params=params, signed=True)

            avg_price, total_comm = self._average_fill_price(resp)
            total_quote_qty  = float(resp.get("cummulativeQuoteQty", 0))
            total_base_qty   = total_quote_qty / avg_price if avg_price else 0.0
            order_id         = int(resp.get("orderId", 0))
            status           = str(resp.get("status", "FILLED"))
            side_str         = str(resp.get("side", side.value.upper()))
            comm_asset       = (resp.get("fills") or [{}])[0].get("commissionAsset", quote_asset)

            logger.success(
                f"[{symbol}] Filled: {side_str} {total_base_qty:.6f} @ {avg_price:.6f} "
                f"comm={total_comm} {comm_asset}"
            )
            asyncio.create_task(self._record_trade(
                base_asset            = base_asset,
                quote_asset           = quote_asset,
                base_amount_executed  = total_base_qty,
                quote_amount_executed = total_quote_qty,
                order_type            = OrderType.MARKET,
                price                 = avg_price,
                commission            = total_comm,
                order_id              = order_id,
                status                = status,
                side_str              = side_str,
                comm_asset            = comm_asset,
            ))
            return resp

        except Exception as e:
            logger.error(f"[{symbol}] place_market_order error: {e}")

    async def cancel_order(self, symbol: str, order_id: str):
        """Cancel an active order via DELETE /api/v1/order."""
        try:
            resp = await self._request(
                "DELETE", "/api/v1/order",
                params={"symbol": symbol, "orderId": order_id},
                signed=True,
            )
            logger.info(f"[{symbol}] Order {order_id} cancelled: {resp}")
        except Exception as e:
            logger.error(f"[{symbol}] cancel_order error: {e}")

    async def get_open_orders(self, symbol: str):
        """Refresh open orders for a specific symbol from GET /api/v1/openOrders."""
        try:
            orders = await self._request(
                "GET", "/api/v1/openOrders",
                params={"symbol": symbol},
                signed=True,
            )
            logger.info(f"[{symbol}] Open orders: {len(orders)}")
            return orders
        except Exception as e:
            logger.error(f"[{symbol}] get_open_orders error: {e}")

    async def get_all_open_orders(self):
        """Refresh open orders for all symbols from GET /api/v1/openOrders (no symbol filter)."""
        try:
            all_orders = await self._request("GET", "/api/v1/openOrders", signed=True)
        except Exception as e:
            logger.error(f"get_all_open_orders error: {e}")
            return

        self._open_orders.clear()
        for o in all_orders:
            symbol      = o.get("symbol")
            order_id    = str(o.get("orderId"))
            updated_time = o.get("updateTime")

            existing = self._open_orders[symbol].get(order_id)
            if (
                existing
                and updated_time
                and existing.last_updated_timestamp
                and updated_time < existing.last_updated_timestamp
            ):
                continue

            cum_filled_qty = float(o.get("executedQty", 0))
            cum_quote_qty  = float(o.get("cummulativeQuoteQty", 0))
            avg_price      = (
                cum_quote_qty / cum_filled_qty
            ) if cum_filled_qty > 0 else float(o.get("price", 0))

            order = Order(
                id          = order_id,
                symbol      = symbol,
                side        = Side.BUY if o.get("side") == "BUY" else Side.SELL,
                order_type  = (
                    OrderType.MARKET if o.get("type") == "MARKET" else OrderType.LIMIT
                ),
                quantity    = float(o.get("origQty", 0)),
                price       = float(o.get("price", 0)) or None,
                status      = _BINANCE_STATUS.get(o.get("status", "NEW"), OrderStatus.OPEN),
                avg_fill_price = avg_price,
                filled_qty  = cum_filled_qty,
                timestamp   = datetime.datetime.fromtimestamp(
                                  o.get("time", 0) / 1000,
                                  tz=datetime.timezone.utc,
                              ),
                venue       = Venue.ASTER,
            )
            self._open_orders[symbol][order_id] = order

        logger.info(
            f"Refreshed open orders: "
            f"{sum(len(v) for v in self._open_orders.values())} orders "
            f"across {len(self._open_orders)} symbols"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _round_step(self, symbol: str, qty: float) -> float:
        """Round quantity down to the exchange's step size."""
        step = self._token_infos.get(symbol, {}).get("step_size", 0.0001)
        if step <= 0:
            return qty
        precision = max(0, round(-math.log10(step)))
        return round(math.floor(qty / step) * step, precision)

    async def _record_trade(
        self,
        base_asset: str,
        quote_asset: str,
        base_amount_executed: float,
        quote_amount_executed: float,
        order_type: OrderType,
        price: float,
        commission: float,
        order_id: int,
        status: str,
        side_str: str,
        comm_asset: str,
    ):
        """Record a completed trade to the database."""
        ts = datetime.datetime.now(self._utc8)
        try:
            CexTrade.create(
                id                     = ts.strftime("%Y-%m-%d_%H-%M-%S") + "_aster_" + self.strategy_name,
                datetime               = ts,
                strategy_id            = self.strategy_id,
                strategy_name          = self.strategy_name,
                venue                  = Venue.ASTER,
                asset_type             = AssetType.SPOT,
                symbol                 = base_asset + quote_asset,
                side                   = Side.BUY if side_str.upper() == "BUY" else Side.SELL,
                base_amount            = base_amount_executed,
                quote_amount           = quote_amount_executed,
                price                  = price,
                commission_asset       = comm_asset,
                total_commission       = commission,
                total_commission_quote = (
                    commission * price
                    if comm_asset.upper() not in self._STABLECOINS
                    else commission
                ),
                order_id    = str(order_id),
                status      = _BINANCE_STATUS.get(status, OrderStatus.OPEN),
                order_side  = _BINANCE_SIDE.get(side_str.upper(), Side.BUY),
                order_type  = order_type,
            )
            logger.info(f"Trade recorded at {ts.strftime('%Y-%m-%d_%H-%M-%S')}")
        except Exception as e:
            logger.error(f"_record_trade DB error: {e}")

    # ── Run / Cleanup ─────────────────────────────────────────────────────────

    async def run(self):
        """
        Initialise the exchange client and start background monitoring tasks.
        Data feeds are NOT started here — pass them to your ThreadPoolExecutor separately.
        """
        await self.initialize()
        asyncio.create_task(self.monitor_balances())
        asyncio.create_task(self.monitor_open_orders())
        asyncio.create_task(self.print_snapshot())
        await asyncio.sleep(0.5)
        logger.info("AsterSpotExchange running.")

    async def cleanup(self):
        """
        Stop data managers and close the HTTP session.
        Call this before shutting down.
        """
        if self.kline_manager:
            await self.kline_manager.stop()
        if self.ob_manager:
            await self.ob_manager.stop()
        if self.user_data_manager:
            await self.user_data_manager.stop()

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("AsterSpotExchange cleanup completed.")
