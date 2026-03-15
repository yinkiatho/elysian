import asyncio
import collections
import datetime
import hashlib
import hmac
import math
import time
import argparse
from typing import Any, List, Optional
from urllib.parse import urlencode

import aiohttp

from elysian_core.connectors.base import FuturesExchangeConnector
from elysian_core.connectors.AsterPerpDataConnectors import (
    AsterPerpKlineClientManager,
    AsterPerpOrderBookClientManager,
    AsterPerpUserDataClientManager,
)
from elysian_core.core.enums import (
    OrderStatus, Side, OrderType, AssetType, Venue,
    _BINANCE_SIDE, _BINANCE_STATUS,
)
from elysian_core.core.order import Order
from elysian_core.db.models import CexTrade
import elysian_core.utils.logger as log


logger = log.setup_custom_logger("root")

FUTURES_BASE_ENDPOINT = "https://fapi.asterdex.com"

_STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})

# Map raw Aster futures order type strings -> OrderType enum
_ASTER_ORDER_TYPE = {
    "LIMIT": OrderType.LIMIT,
    "MARKET": OrderType.MARKET,
    "STOP_MARKET": OrderType.STOP_MARKET,
    "TAKE_PROFIT_MARKET": OrderType.TAKE_PROFIT_MARKET,
    "TRAILING_STOP_MARKET": OrderType.TRAILING_STOP_MARKET,
}


# ──────────────────────────────────────────────────────────────────────────────
# AsterPerpExchange
# ──────────────────────────────────────────────────────────────────────────────

class AsterPerpExchange(FuturesExchangeConnector):
    """
    Authenticated Aster USDS-M futures exchange client for multiple symbols.

    Manages:
      - Account balances and positions (via polling + user data stream)
      - Per-symbol leverage and margin type
      - Open orders registry using Order dataclass
      - Market, limit, and stop order placement / cancellation
      - Position management (close, adjust leverage)
      - Real-time order updates via futures user data stream

    Usage:
        exchange = AsterPerpExchange(
            args=args,
            api_key="...", api_secret="...",
            symbols=["ETHUSDT", "BTCUSDT"],
        )
        await exchange.run()
        await exchange.place_market_order("ETHUSDT", Side.BUY, 0.1)
    """

    def __init__(
        self,
        args: argparse.Namespace = argparse.Namespace(),
        api_key: str = "",
        api_secret: str = "",
        symbols: Optional[List[str]] = None,
        file_path: Optional[str] = None,
        kline_manager: Optional[AsterPerpKlineClientManager] = None,
        ob_manager: Optional[AsterPerpOrderBookClientManager] = None,
        user_data_manager: Optional[AsterPerpUserDataClientManager] = None,
        default_leverage: int = 1,
    ):
        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols or [],
            file_path=file_path,
            kline_manager=kline_manager,
            ob_manager=ob_manager,
            default_leverage=default_leverage,
        )

        self.user_data_manager = user_data_manager or AsterPerpUserDataClientManager(api_key, api_secret)
        self._session: Optional[aiohttp.ClientSession] = None

        self.strategy_name = getattr(args, "strategy_name", "")
        self.strategy_id = getattr(args, "strategy_id", "")

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        """Add timestamp + HMAC SHA256 signature to a params dict (in-place)."""
        params["timestamp"] = int(time.time() * 1000)
        qs = urlencode(params)
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
        """Async HTTP request against the Aster Futures REST API."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        url = FUTURES_BASE_ENDPOINT + path
        p = dict(params or {})
        if signed:
            self._sign(p)

        async with self._session.request(
            method, url, params=p if method.upper() == "GET" else None,
            data=p if method.upper() != "GET" else None,
            headers=self._headers(),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                logger.error(f"Aster Futures API error {resp.status}: {body}")
                resp.raise_for_status()
            return body

    # ── Initialisation ────────────────────────────────────────────────────────

    async def initialize(self):
        """Ping the exchange, fetch symbol metadata, set leverage, refresh balances and start user-data stream."""
        self._session = aiohttp.ClientSession()

        # Health check
        try:
            await self._request("GET", "/fapi/v1/ping")
            logger.info("AsterPerpExchange: ping OK")
        except Exception as e:
            logger.warning(f"AsterPerpExchange: ping failed: {e}")

        for sym in self._symbols:
            await self._fetch_symbol_info(sym)
            await self.set_leverage(sym, self._default_leverage)

        await self.refresh_balances()

        # Start user data listener
        try:
            self.user_data_manager.register(self._handle_user_data)
            await self.user_data_manager.start()
            logger.info("AsterPerpExchange: user data stream started")
        except Exception as e:
            logger.error(f"AsterPerpExchange: failed to start user data stream: {e}")

        logger.info("AsterPerpExchange initialised.")

    async def _fetch_symbol_info(self, symbol: str):
        """Fetch symbol filters (step size, min notional, tick size) from /fapi/v1/exchangeInfo."""
        try:
            data = await self._request("GET", "/fapi/v1/exchangeInfo", params={"symbol": symbol})
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
            tick_size = float(
                next((f["tickSize"] for f in filters if f["filterType"] == "PRICE_FILTER"), "0.01")
            )

            self._token_infos[symbol] = {
                "step_size": step_size,
                "min_notional": min_notional,
                "tick_size": tick_size,
                "base_asset": info.get("baseAsset", ""),
                "quote_asset": info.get("quoteAsset", ""),
                "base_asset_precision": info.get("baseAssetPrecision", 8),
                "quote_asset_precision": info.get("quoteAssetPrecision", 8),
            }
            logger.info(f"[{symbol}] Futures step={step_size} min_notional={min_notional} tick={tick_size}")
        except Exception as e:
            logger.error(f"[{symbol}] _fetch_symbol_info error: {e}")

    # ── Leverage & Margin ─────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int):
        try:
            await self._request(
                "POST", "/fapi/v1/leverage",
                params={"symbol": symbol, "leverage": leverage},
                signed=True,
            )
            self._leverages[symbol] = leverage
            logger.info(f"[{symbol}] Leverage set to {leverage}x")
        except Exception as e:
            logger.error(f"[{symbol}] Failed to set leverage: {e}")

    async def set_margin_type(self, symbol: str, margin_type: str):
        """Set margin type: 'ISOLATED' or 'CROSSED'."""
        try:
            await self._request(
                "POST", "/fapi/v1/marginType",
                params={"symbol": symbol, "marginType": margin_type},
                signed=True,
            )
            self._margin_types[symbol] = margin_type
            logger.info(f"[{symbol}] Margin type set to {margin_type}")
        except aiohttp.ClientResponseError as e:
            # Handle "margin type already set" gracefully
            if e.status == 400:
                self._margin_types[symbol] = margin_type
                logger.debug(f"[{symbol}] Margin type already {margin_type}")
            else:
                logger.error(f"[{symbol}] Failed to set margin type: {e}")
        except Exception as e:
            logger.error(f"[{symbol}] Failed to set margin type: {e}")

    # ── Account ───────────────────────────────────────────────────────────────

    async def refresh_balances(self):
        """Refresh balances and positions from the futures account endpoint."""
        try:
            account = await self._request("GET", "/fapi/v4/account", signed=True)
            for asset in account.get("assets", []):
                self._balances[asset["asset"]] = float(asset.get("walletBalance", 0))
            # Also update positions while we have the data
            self._update_positions_from_account(account)
        except Exception as e:
            logger.error(f"AsterPerpExchange: refresh_balances error: {e}")

    async def refresh_positions(self):
        """Refresh positions only (uses same endpoint as balances)."""
        try:
            account = await self._request("GET", "/fapi/v4/account", signed=True)
            self._update_positions_from_account(account)
        except Exception as e:
            logger.error(f"AsterPerpExchange: refresh_positions error: {e}")

    def _update_positions_from_account(self, account: dict):
        """Extract position data from a futures account response."""
        for position in account.get("positions", []):
            symbol = position["symbol"]
            amt = float(position.get("positionAmt", 0))
            if amt != 0:
                self._positions[symbol] = {
                    "amount": amt,
                    "entry_price": float(position.get("entryPrice", 0)),
                    "unrealized_pnl": float(position.get("unrealizedProfit", 0)),
                    "leverage": int(position.get("leverage", self._default_leverage)),
                }
            else:
                self._positions.pop(symbol, None)

    # ── User Data Stream Handling ─────────────────────────────────────────────

    def _handle_user_data(self, msg: dict):
        """Callback invoked by the user-data manager when an event arrives."""
        et = msg.get("e")
        if et == "ACCOUNT_UPDATE":
            self._handle_account_update(msg)
        elif et == "ORDER_TRADE_UPDATE":
            self._handle_order_trade_update(msg)
        elif et == "MARGIN_CALL":
            logger.warning(f"MARGIN CALL received: {msg}")
        elif et == "ACCOUNT_CONFIG_UPDATE":
            self._handle_account_config_update(msg)

    def _handle_account_update(self, msg: dict):
        """
        Handle ACCOUNT_UPDATE events — balance + position changes.

        Structure:
          msg["a"]["B"] -> list of {"a": asset, "wb": wallet_balance, "cw": cross_wallet_balance}
          msg["a"]["P"] -> list of {"s": symbol, "pa": position_amt, "ep": entry_price, "up": unrealized_pnl}
        """
        account_data = msg.get("a", {})

        # Update balances
        for bal in account_data.get("B", []):
            asset = bal.get("a")
            wallet_balance = float(bal.get("wb", 0))
            self._balances[asset] = wallet_balance

        # Update positions
        for pos in account_data.get("P", []):
            symbol = pos.get("s")
            amt = float(pos.get("pa", 0))
            if amt != 0:
                self._positions[symbol] = {
                    "amount": amt,
                    "entry_price": float(pos.get("ep", 0)),
                    "unrealized_pnl": float(pos.get("up", 0)),
                    "leverage": self._leverages.get(symbol, self._default_leverage),
                }
            else:
                self._positions.pop(symbol, None)

        logger.info(f"ACCOUNT_UPDATE applied: {len(account_data.get('B', []))} balance(s), {len(account_data.get('P', []))} position(s)")

    def _handle_order_trade_update(self, msg: dict):
        """
        Handle ORDER_TRADE_UPDATE events — order status changes and fills.

        Key fields in msg["o"]:
          s  -> Symbol
          i  -> Order ID
          S  -> Side (BUY/SELL)
          o  -> Order type (LIMIT, MARKET, STOP_MARKET, etc.)
          X  -> Order status (NEW, PARTIALLY_FILLED, FILLED, CANCELED, etc.)
          x  -> Execution type
          q  -> Original quantity
          p  -> Original price
          ap -> Average price (cumulative)
          z  -> Cumulative filled quantity
          n  -> Commission of this trade
          N  -> Commission asset
          rp -> Realized profit of this trade
        """
        o = msg.get("o", {})
        order_id = str(o.get("i"))
        symbol = o.get("s")
        exec_type = o.get("x")
        raw_status = o.get("X")

        internal_status = _BINANCE_STATUS.get(raw_status)
        if internal_status is None:
            logger.warning(f"[{symbol}] Unknown futures order status '{raw_status}' for order {order_id}")
            return

        symbol_orders = self._open_orders.get(symbol, {})

        # ── NEW order arriving for the first time ────────────────────────────
        if exec_type == "NEW" and order_id not in symbol_orders:
            side = Side.BUY if o.get("S") == "BUY" else Side.SELL
            order_type = _ASTER_ORDER_TYPE.get(o.get("o", "LIMIT"), OrderType.OTHERS)

            order = Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=float(o.get("q", 0)),
                price=float(o.get("p", 0)) or None,
                status=internal_status,
                timestamp=datetime.datetime.fromtimestamp(
                    msg.get("T", 0) / 1000, tz=datetime.timezone.utc
                ),
                venue=Venue.ASTER,
                commission=float(o.get("n", 0)),
                commission_asset=o.get("N"),
                last_updated_timestamp=int(msg.get("E", 0)),
            )
            self._open_orders[symbol][order_id] = order
            logger.info(f"[{symbol}] New futures order registered: {order}")
            return

        # ── Update existing order ────────────────────────────────────────────
        order = symbol_orders.get(order_id)
        if order is None:
            logger.warning(f"[{symbol}] ORDER_TRADE_UPDATE for unknown order {order_id} (status={raw_status}), skipping")
            return

        cum_filled_qty = float(o.get("z", 0))
        avg_price = float(o.get("ap", 0))

        if cum_filled_qty > order.filled_qty:
            order.update_fill(cum_filled_qty, avg_price)

        order.commission += float(o.get("n", 0))
        order.commission_asset = o.get("N") or order.commission_asset
        order.status = internal_status

        logger.info(f"[{symbol}] Futures order updated: {order}")

        # Remove terminal orders from open-orders registry
        if not order.is_active:
            self._open_orders[symbol].pop(order_id, None)
            logger.info(f"[{symbol}] Futures order {order_id} removed from open orders (status={raw_status})")

    def _handle_account_config_update(self, msg: dict):
        """Handle ACCOUNT_CONFIG_UPDATE — leverage or multi-asset mode changes."""
        ac = msg.get("ac", {})
        if ac:
            symbol = ac.get("s")
            leverage = int(ac.get("l", self._default_leverage))
            self._leverages[symbol] = leverage
            logger.info(f"[{symbol}] Leverage updated to {leverage}x via stream")

    # ── Price helper ──────────────────────────────────────────────────────────

    def last_price(self, symbol: str) -> Optional[float]:
        """Best available mid-price: OB mid -> last kline close -> None."""
        if self.ob_manager:
            feed = self.ob_manager.get_feed(symbol)
            if feed and feed.data:
                return feed.data.mid_price

        if self.kline_manager:
            feed = self.kline_manager.get_feed(symbol)
            if feed and feed.latest_close:
                return feed.latest_close

        return None

    # ── Order Health Check ────────────────────────────────────────────────────

    def order_health_check(self, symbol: str, side: Side, quantity: float) -> bool:
        """Validate minimum notional and approximate balance sufficiency for a futures order."""
        price = self.last_price(symbol) or 0.0
        notional = price * abs(quantity)
        min_notional = self._token_infos.get(symbol, {}).get("min_notional", 0.0)

        if notional < min_notional:
            logger.error(f"[{symbol}] Estimated notional {notional:.4f} < min {min_notional}")
            return False

        # Check approximate margin availability (wallet balance in quote)
        quote_asset = self._token_infos.get(symbol, {}).get("quote_asset", "USDT")
        leverage = self.get_leverage(symbol)
        required_margin = notional / leverage
        available = self._balances.get(quote_asset, 0.0)

        if available < required_margin:
            logger.error(
                f"[{symbol}] Insufficient margin for {side.value} {quantity}. "
                f"Need ~{required_margin:.2f} {quote_asset} (at {leverage}x), have {available:.2f}"
            )
            return False

        return True

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_limit_order(
        self, symbol: str, side: Side, price: float, quantity: float,
        reduce_only: bool = False, time_in_force: str = "GTC"
    ):
        """Place a GTC limit order on Aster Futures."""
        params = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": self._round_step(symbol, quantity),
            "price": str(price),
            "reduceOnly": str(reduce_only).lower(),
        }
        try:
            resp = await self._request("POST", "/fapi/v1/order", params=params, signed=True)

            order_id = str(resp["orderId"])
            order = Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                quantity=float(resp.get("origQty", quantity)),
                price=float(resp.get("price", price)),
                status=_BINANCE_STATUS.get(resp.get("status", "NEW"), OrderStatus.OPEN),
                timestamp=datetime.datetime.fromtimestamp(
                    resp.get("updateTime", 0) / 1000, tz=datetime.timezone.utc
                ),
                venue=Venue.ASTER,
                last_updated_timestamp=resp.get("updateTime"),
            )
            self._open_orders[symbol][order_id] = order
            logger.info(f"Futures limit order placed: {order}")
            return order

        except Exception as e:
            logger.error(f"[{symbol}] place_limit_order error: {e}")

    async def place_stop_order(
        self, symbol: str, side: Side, quantity: float, stop_price: float,
        reduce_only: bool = True
    ):
        """Place a stop-market order on Aster Futures."""
        params = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "STOP_MARKET",
            "stopPrice": str(stop_price),
            "quantity": self._round_step(symbol, quantity),
            "reduceOnly": str(reduce_only).lower(),
        }
        try:
            resp = await self._request("POST", "/fapi/v1/order", params=params, signed=True)

            order_id = str(resp["orderId"])
            order = Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.STOP_MARKET,
                quantity=float(resp.get("origQty", quantity)),
                price=stop_price,
                status=_BINANCE_STATUS.get(resp.get("status", "NEW"), OrderStatus.OPEN),
                timestamp=datetime.datetime.fromtimestamp(
                    resp.get("updateTime", 0) / 1000, tz=datetime.timezone.utc
                ),
                venue=Venue.ASTER,
                last_updated_timestamp=resp.get("updateTime"),
            )
            self._open_orders[symbol][order_id] = order
            logger.info(f"Futures stop order placed: {order}")
            return order

        except Exception as e:
            logger.error(f"[{symbol}] place_stop_order error: {e}")

    async def place_take_profit_order(
        self, symbol: str, side: Side, quantity: float, stop_price: float,
        reduce_only: bool = True
    ):
        """Place a take-profit market order on Aster Futures."""
        params = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": str(stop_price),
            "quantity": self._round_step(symbol, quantity),
            "reduceOnly": str(reduce_only).lower(),
        }
        try:
            resp = await self._request("POST", "/fapi/v1/order", params=params, signed=True)

            order_id = str(resp["orderId"])
            order = Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.TAKE_PROFIT_MARKET,
                quantity=float(resp.get("origQty", quantity)),
                price=stop_price,
                status=_BINANCE_STATUS.get(resp.get("status", "NEW"), OrderStatus.OPEN),
                timestamp=datetime.datetime.fromtimestamp(
                    resp.get("updateTime", 0) / 1000, tz=datetime.timezone.utc
                ),
                venue=Venue.ASTER,
                last_updated_timestamp=resp.get("updateTime"),
            )
            self._open_orders[symbol][order_id] = order
            logger.info(f"Futures take-profit order placed: {order}")
            return order

        except Exception as e:
            logger.error(f"[{symbol}] place_take_profit_order error: {e}")

    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        reduce_only: bool = False,
    ):
        """Place a futures market order."""
        if not self.order_health_check(symbol, side, quantity):
            logger.error(f"[{symbol}] Futures market order health check failed. Order not placed.")
            return

        price = self.last_price(symbol) or 0.0
        base_asset = self._token_infos.get(symbol, {}).get("base_asset", "")
        quote_asset = self._token_infos.get(symbol, {}).get("quote_asset", "USDT")

        qty = self._round_step(symbol, abs(quantity))

        logger.info(f"[{symbol}] Futures Market {side.value.upper()} qty={qty} (~{qty * price:.2f} {quote_asset})")

        params = {
            "symbol": symbol,
            "side": side.value.upper(),
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": str(reduce_only).lower(),
        }

        try:
            resp = await self._request("POST", "/fapi/v1/order", params=params, signed=True)

            avg_price, total_comm = self._average_fill_price(resp)
            total_base_quantity = float(resp.get("executedQty", 0))
            total_quote_quantity = float(resp.get("cumQuote", resp.get("cummulativeQuoteQty", 0)))
            order_id = int(resp.get("orderId", 0))
            status = str(resp.get("status", "FILLED"))
            side_str = str(resp.get("side", side.value.upper()))
            comm_asset = (resp.get("fills") or [{}])[0].get("commissionAsset", quote_asset)

            logger.success(
                f"[{symbol}] Futures Filled: {side_str} {total_base_quantity} @ {avg_price:.6f} "
                f"comm={total_comm} {comm_asset}"
            )

            asyncio.create_task(self._record_trade(
                base_asset=base_asset,
                quote_asset=quote_asset,
                base_amount_executed=total_base_quantity,
                quote_amount_executed=total_quote_quantity,
                order_type=OrderType.MARKET,
                price=avg_price,
                commission=total_comm,
                order_id=order_id,
                status=status,
                side_str=side_str,
                comm_asset=comm_asset,
            ))
            return resp

        except Exception as e:
            logger.error(f"[{symbol}] place_market_order error: {e}")

    async def cancel_order(self, symbol: str, order_id: str):
        """Cancel an active futures order by id."""
        try:
            resp = await self._request(
                "DELETE", "/fapi/v1/order",
                params={"symbol": symbol, "orderId": order_id},
                signed=True,
            )
            self._open_orders[symbol].pop(order_id, None)
            logger.info(f"[{symbol}] Futures order {order_id} cancelled: {resp}")
        except Exception as e:
            logger.error(f"[{symbol}] cancel_order error: {e}")

    async def get_open_orders(self, symbol: str):
        """Fetch and reconcile open orders for a specific symbol."""
        try:
            raw_orders = await self._request(
                "GET", "/fapi/v1/openOrders",
                params={"symbol": symbol},
                signed=True,
            )

            # Build new OrderedDict from exchange response
            refreshed = collections.OrderedDict()
            for o in raw_orders:
                order_id = str(o.get("orderId"))
                updated_time = o.get("updateTime")

                # Keep local version if it's more recent
                existing = self._open_orders[symbol].get(order_id)
                if existing and updated_time and existing.last_updated_timestamp and updated_time < existing.last_updated_timestamp:
                    refreshed[order_id] = existing
                    continue

                raw_status = o.get("status", "NEW")
                raw_type = o.get("type", "LIMIT")
                raw_side = o.get("side", "BUY")

                cum_filled_qty = float(o.get("executedQty", 0))
                avg_price = float(o.get("avgPrice", 0)) or float(o.get("price", 0))

                order = Order(
                    id=order_id,
                    symbol=symbol,
                    side=Side.BUY if raw_side == "BUY" else Side.SELL,
                    order_type=_ASTER_ORDER_TYPE.get(raw_type, OrderType.OTHERS),
                    quantity=float(o.get("origQty", 0)),
                    price=float(o.get("price", 0)) or None,
                    status=_BINANCE_STATUS.get(raw_status, OrderStatus.OPEN),
                    avg_fill_price=avg_price,
                    filled_qty=cum_filled_qty,
                    timestamp=datetime.datetime.fromtimestamp(
                        o.get("time", 0) / 1000, tz=datetime.timezone.utc
                    ),
                    venue=Venue.ASTER,
                    last_updated_timestamp=updated_time,
                )
                refreshed[order_id] = order

            self._open_orders[symbol] = refreshed

        except Exception as e:
            logger.error(f"[{symbol}] get_open_orders error: {e}")

    # ── Position Management ───────────────────────────────────────────────────

    async def close_position(self, symbol: str):
        """Close entire position for a symbol via market order."""
        position = self.get_position(symbol)
        if not position:
            logger.warning(f"[{symbol}] No position to close")
            return

        amount = position["amount"]
        close_side = Side.SELL if amount > 0 else Side.BUY
        qty = abs(amount)

        params = {
            "symbol": symbol,
            "side": close_side.value.upper(),
            "type": "MARKET",
            "quantity": self._round_step(symbol, qty),
            "reduceOnly": "true",
        }
        try:
            resp = await self._request("POST", "/fapi/v1/order", params=params, signed=True)
            logger.info(f"[{symbol}] Position closed: {resp}")
        except Exception as e:
            logger.error(f"[{symbol}] Failed to close position: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _round_step(self, symbol: str, qty: float) -> float:
        """Round quantity down to the exchange's step size."""
        step = self._token_infos.get(symbol, {}).get("step_size", 0.0001)
        if step <= 0:
            return qty
        precision = max(0, round(-math.log10(step)))
        return round(math.floor(qty / step) * step, precision)

    @staticmethod
    def _average_fill_price(resp: dict):
        """
        Compute VWAP from a market order fill response.
        Falls back to cummulativeQuoteQty / executedQty if fills list is absent.
        """
        fills = resp.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            total_notional = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            total_comm = sum(float(f["commission"]) for f in fills)
            avg = total_notional / total_qty if total_qty else 0.0
            return avg, total_comm

        # Fallback for exchanges that omit fills
        exec_qty = float(resp.get("executedQty", 0))
        quote_qty = float(resp.get("cummulativeQuoteQty", resp.get("cumQuote", 0)))
        avg = quote_qty / exec_qty if exec_qty else 0.0
        return avg, 0.0

    # ── Trade Recording ───────────────────────────────────────────────────────

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
        ts = datetime.datetime.now(self._utc8)
        try:
            CexTrade.create(
                id=ts.strftime("%Y-%m-%d_%H-%M-%S") + "_aster_perp_" + self.strategy_name,
                datetime=ts,
                strategy_id=self.strategy_id,
                strategy_name=self.strategy_name,
                venue=Venue.ASTER,
                asset_type=AssetType.PERPETUAL,
                symbol=base_asset + quote_asset,
                side=Side.BUY if side_str.upper() == "BUY" else Side.SELL,
                base_amount=base_amount_executed,
                quote_amount=quote_amount_executed,
                price=price,
                commission_asset=comm_asset,
                total_commission=commission,
                total_commission_quote=(
                    commission * price
                    if comm_asset.upper() not in _STABLECOINS
                    else commission
                ),
                order_id=str(order_id),
                status=_BINANCE_STATUS.get(status, OrderStatus.OPEN),
                order_side=_BINANCE_SIDE.get(side_str.upper(), Side.BUY),
                order_type=order_type,
            )
            logger.info(f"Futures trade recorded at {ts.strftime('%Y-%m-%d_%H-%M-%S')}")
        except Exception as e:
            logger.error(f"_record_trade DB error: {e}")

    # ── Run / Cleanup ─────────────────────────────────────────────────────────

    async def run(self):
        """
        Initialise the futures client and start background monitoring tasks.
        Data feeds are NOT started here — pass them to your ThreadPoolExecutor separately.
        """
        await self.initialize()
        asyncio.create_task(self.monitor_balances(interval_secs=10, name="AsterPerp"))
        asyncio.create_task(self.monitor_open_orders())
        asyncio.create_task(self.print_snapshot())
        await asyncio.sleep(0.5)
        logger.info("AsterPerpExchange running.")

    async def cleanup(self):
        """Stop data managers and close the HTTP session."""
        await self.user_data_manager.stop()

        if self.kline_manager:
            await self.kline_manager.stop()
        if self.ob_manager:
            await self.ob_manager.stop()

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("AsterPerpExchange cleanup completed.")
