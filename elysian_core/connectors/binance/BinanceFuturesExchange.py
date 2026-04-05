import asyncio
import collections
import datetime
import argparse
from typing import List, Optional

import pylru
from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from binance.helpers import round_step_size

from elysian_core.connectors.base import FuturesExchangeConnector
from elysian_core.connectors.binance.BinanceFuturesDataConnectors import (
    BinanceFuturesKlineClientManager,
    BinanceFuturesOrderBookClientManager,
    BinanceFuturesUserDataClientManager,
)
from elysian_core.core.enums import (
    OrderStatus, Side, OrderType, AssetType, Venue,
    _BINANCE_SIDE, _BINANCE_STATUS,
)
from elysian_core.core.order import Order
from elysian_core.db.models import CexTrade
import elysian_core.utils.logger as log


_STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})

# Map raw Binance futures order type strings → OrderType enum
_BINANCE_ORDER_TYPE = {
    "LIMIT": OrderType.LIMIT,
    "MARKET": OrderType.MARKET,
    "STOP_MARKET": OrderType.STOP_MARKET,
    "TAKE_PROFIT_MARKET": OrderType.TAKE_PROFIT_MARKET,
    "TRAILING_STOP_MARKET": OrderType.TRAILING_STOP_MARKET,
}


# ──────────────────────────────────────────────────────────────────────────────
# BinanceFuturesExchange
# ──────────────────────────────────────────────────────────────────────────────

class BinanceFuturesExchange(FuturesExchangeConnector):
    """
    Authenticated Binance USDS-M futures exchange client for multiple symbols.

    Manages:
      - Account balances and positions (via polling + user data stream)
      - Per-symbol leverage and margin type
      - Open orders registry using Order dataclass
      - Market, limit, and stop order placement / cancellation
      - Position management (close, adjust leverage)
      - Real-time order updates via futures user data stream

    Usage:
        exchange = BinanceFuturesExchange(
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
        symbols: Optional[List[str]] = [],
        file_path: Optional[str] = None,
        kline_manager: Optional[BinanceFuturesKlineClientManager] = None,
        ob_manager: Optional[BinanceFuturesOrderBookClientManager] = None,
        user_data_manager: Optional[BinanceFuturesUserDataClientManager] = None,
        default_leverage: int = 1,
    ):
        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols,
            file_path=file_path,
            kline_manager=kline_manager,
            ob_manager=ob_manager,
            default_leverage=default_leverage,
        )

        # User data stream
        self.user_data_manager = user_data_manager or BinanceFuturesUserDataClientManager(api_key, api_secret)

        # Strategy identifiers
        self.strategy_name = getattr(args, 'strategy_name', None)
        self.strategy_id = getattr(args, 'strategy_id', None)

        # LRU cache for dedup
        self._swap_cache: pylru.lrucache = pylru.lrucache(size=50)

    # ── Initialisation ────────────────────────────────────────────────────────
    async def initialize(self):
        """Create authenticated futures client, fetch symbol metadata, set leverage, start user data stream."""
        self._client = await AsyncClient.create(self._api_key, self._api_secret, testnet=False)

        for sym in self._symbols:
            await self._fetch_symbol_info(sym)
            await self.set_leverage(sym, self._default_leverage)

        # Start user data listener
        try:
            self.user_data_manager.register(self._handle_user_data)
            await self.user_data_manager.start()
            self.logger.info("Futures user data stream started")
        except Exception as e:
            self.logger.error(f"Failed to start futures user data stream: {e}")

        self.logger.info("BinanceFuturesExchange initialised.")

    async def _fetch_symbol_info(self, symbol: str):
        info = await self._client.futures_symbol_info(symbol)
        step_size = float(
            next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        )
        min_notional = float(
            next(f["minNotional"] for f in info["filters"] if f["filterType"] == "MIN_NOTIONAL")
        )
        tick_size = float(
            next(f["tickSize"] for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        )
        self._token_infos[symbol] = {
            "step_size": step_size,
            "min_notional": min_notional,
            "tick_size": tick_size,
            "base_asset": info.get("baseAsset", ""),
            "quote_asset": info.get("quoteAsset", ""),
        }
        self.logger.info(f"[{symbol}] Futures step={step_size} min_notional={min_notional} tick={tick_size}")

    # ── Leverage & Margin ─────────────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        try:
            await self._client.futures_change_leverage(symbol=symbol, leverage=leverage)
            self._leverages[symbol] = leverage
            self.logger.info(f"[{symbol}] Leverage set to {leverage}x")
        except Exception as e:
            self.logger.error(f"[{symbol}] Failed to set leverage: {e}")

    async def set_margin_type(self, symbol: str, margin_type: str):
        """Set margin type: 'ISOLATED' or 'CROSSED'."""
        try:
            await self._client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            self._margin_types[symbol] = margin_type
            self.logger.info(f"[{symbol}] Margin type set to {margin_type}")
        except BinanceAPIException as e:
            # -4046 means margin type already set — not an error
            if e.code == -4046:
                self._margin_types[symbol] = margin_type
                self.logger.debug(f"[{symbol}] Margin type already {margin_type}")
            else:
                self.logger.error(f"[{symbol}] Failed to set margin type: {e}")
        except Exception as e:
            self.logger.error(f"[{symbol}] Failed to set margin type: {e}")

    # ── Account ───────────────────────────────────────────────────────────────
    async def refresh_balances(self):
        """Refresh balances and positions from the futures account endpoint."""
        try:
            account = await self._client.futures_account()
            for asset in account["assets"]:
                self._balances[asset["asset"]] = float(asset["walletBalance"])
            # Also update positions while we have the data
            self._update_positions_from_account(account)
        except Exception as e:
            self.logger.error(f"Failed to refresh futures account: {e}")

    async def refresh_positions(self):
        """Refresh positions only (uses same endpoint as balances)."""
        try:
            account = await self._client.futures_account()
            self._update_positions_from_account(account)
        except Exception as e:
            self.logger.error(f"Failed to refresh futures positions: {e}")

    def _update_positions_from_account(self, account: dict):
        """Extract position data from a futures_account() response."""
        for position in account.get("positions", []):
            symbol = position["symbol"]
            amt = float(position["positionAmt"])
            if amt != 0:
                self._positions[symbol] = {
                    "amount": amt,
                    "entry_price": float(position["entryPrice"]),
                    "unrealized_pnl": float(position["unrealizedProfit"]),
                    "leverage": int(position["leverage"]),
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
            self.logger.warning(f"MARGIN CALL received: {msg}")
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

        self.logger.info(f"ACCOUNT_UPDATE applied: {len(account_data.get('B', []))} balance(s), {len(account_data.get('P', []))} position(s)")

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
            self.logger.warning(f"[{symbol}] Unknown futures order status '{raw_status}' for order {order_id}")
            return

        symbol_orders = self._open_orders.get(symbol, {})

        # ── NEW order arriving for the first time ────────────────────────────
        if exec_type == "NEW" and order_id not in symbol_orders:
            side = Side.BUY if o.get("S") == "BUY" else Side.SELL
            order_type = _BINANCE_ORDER_TYPE.get(o.get("o", "LIMIT"), OrderType.OTHERS)

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
                venue=Venue.BINANCE,
                commission=float(o.get("n", 0)),
                commission_asset=o.get("N"),
                last_updated_timestamp=int(msg.get("E", 0)),
            )
            self._open_orders[symbol][order_id] = order
            self.logger.info(f"[{symbol}] New futures order registered: {order}")
            return

        # ── Update existing order ────────────────────────────────────────────
        order = symbol_orders.get(order_id)
        if order is None:
            self.logger.warning(f"[{symbol}] ORDER_TRADE_UPDATE for unknown order {order_id} (status={raw_status}), skipping")
            return

        cum_filled_qty = float(o.get("z", 0))
        avg_price = float(o.get("ap", 0))

        if cum_filled_qty > order.filled_qty:
            order.update_fill(cum_filled_qty, avg_price)

        order.commission += float(o.get("n", 0))
        order.commission_asset = o.get("N") or order.commission_asset
        order.status = internal_status

        self.logger.info(f"[{symbol}] Futures order updated: {order}")

        # Remove terminal orders from open-orders registry
        if not order.is_active:
            self._open_orders[symbol].pop(order_id, None)
            self.logger.info(f"[{symbol}] Futures order {order_id} removed from open orders (status={raw_status})")

    def _handle_account_config_update(self, msg: dict):
        """Handle ACCOUNT_CONFIG_UPDATE — leverage or multi-asset mode changes."""
        ac = msg.get("ac", {})
        if ac:
            symbol = ac.get("s")
            leverage = int(ac.get("l", self._default_leverage))
            self._leverages[symbol] = leverage
            self.logger.info(f"[{symbol}] Leverage updated to {leverage}x via stream")

    # ── Order Health Check ────────────────────────────────────────────────────
    def order_health_check(self, symbol: str, side: Side, quantity: float) -> bool:
        """Validate minimum notional and approximate balance sufficiency for a futures order."""
        price = self.last_price(symbol) or 0.0
        notional = price * abs(quantity)
        min_notional = self._token_infos.get(symbol, {}).get("min_notional", 0.0)

        if notional < min_notional:
            self.logger.error(f"[{symbol}] Estimated notional {notional:.4f} < min {min_notional}")
            return False

        # Check approximate margin availability (wallet balance in quote)
        quote_asset = self._token_infos.get(symbol, {}).get("quote_asset", "USDT")
        leverage = self.get_leverage(symbol)
        required_margin = notional / leverage
        available = self._balances.get(quote_asset, 0.0)

        if available < required_margin:
            self.logger.error(
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
        """Place a GTC limit order on Binance Futures."""
        try:
            resp = await self._client.futures_create_order(
                symbol=symbol,
                side=side.value.upper(),
                type="LIMIT",
                timeInForce=time_in_force,
                quantity=quantity,
                price=str(price),
                reduceOnly=reduce_only,
            )

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
                venue=Venue.BINANCE,
                last_updated_timestamp=resp.get("updateTime"),
            )
            self._open_orders[symbol][order_id] = order
            self.logger.info(f"Futures limit order placed: {order}")
            return order

        except BinanceAPIException as e:
            self.logger.error(f"[{symbol}] place_limit_order API error: {e}")
        except Exception as e:
            self.logger.error(f"[{symbol}] place_limit_order error: {e}")

    async def place_stop_order(
        self, symbol: str, side: Side, quantity: float, stop_price: float,
        reduce_only: bool = True
    ):
        """Place a stop-market order on Binance Futures."""
        try:
            resp = await self._client.futures_create_order(
                symbol=symbol,
                side=side.value.upper(),
                type="STOP_MARKET",
                stopPrice=str(stop_price),
                quantity=quantity,
                reduceOnly=reduce_only,
            )

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
                venue=Venue.BINANCE,
                last_updated_timestamp=resp.get("updateTime"),
            )
            self._open_orders[symbol][order_id] = order
            self.logger.info(f"Futures stop order placed: {order}")
            return order

        except BinanceAPIException as e:
            self.logger.error(f"[{symbol}] place_stop_order API error: {e}")
        except Exception as e:
            self.logger.error(f"[{symbol}] place_stop_order error: {e}")

    async def place_take_profit_order(
        self, symbol: str, side: Side, quantity: float, stop_price: float,
        reduce_only: bool = True
    ):
        """Place a take-profit market order on Binance Futures."""
        try:
            resp = await self._client.futures_create_order(
                symbol=symbol,
                side=side.value.upper(),
                type="TAKE_PROFIT_MARKET",
                stopPrice=str(stop_price),
                quantity=quantity,
                reduceOnly=reduce_only,
            )

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
                venue=Venue.BINANCE,
                last_updated_timestamp=resp.get("updateTime"),
            )
            self._open_orders[symbol][order_id] = order
            self.logger.info(f"Futures take-profit order placed: {order}")
            return order

        except BinanceAPIException as e:
            self.logger.error(f"[{symbol}] place_take_profit_order API error: {e}")
        except Exception as e:
            self.logger.error(f"[{symbol}] place_take_profit_order error: {e}")

    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        reduce_only: bool = False,
    ):
        """
        Place a futures market order.

        Args:
            symbol: Trading pair (e.g. "ETHUSDT")
            side: Side.BUY or Side.SELL
            quantity: Amount in base asset
            reduce_only: If True, only reduce an existing position
        """
        if not self.order_health_check(symbol, side, quantity):
            self.logger.error(f"[{symbol}] Futures market order health check failed. Order not placed.")
            return

        price = self.last_price(symbol) or 0.0
        base_asset = self._token_infos.get(symbol, {}).get("base_asset", "")
        quote_asset = self._token_infos.get(symbol, {}).get("quote_asset", "USDT")

        try:
            step = self._token_infos.get(symbol, {}).get("step_size", 0.001)
            qty = round_step_size(abs(quantity), step)

            self.logger.info(f"[{symbol}] Futures Market {side.value.upper()} qty={qty} (~{qty * price:.2f} {quote_asset})")

            resp = await self._client.futures_create_order(
                symbol=symbol,
                side=side.value.upper(),
                type="MARKET",
                quantity=qty,
                reduceOnly=reduce_only,
            )

            avg_price, total_comm = self._average_fill_price(resp)
            total_base_quantity = float(resp.get("executedQty", 0))
            total_quote_quantity = float(resp.get("cumQuote", 0))
            order_id = int(resp["orderId"])
            status = str(resp["status"])
            side_str = str(resp["side"])
            comm_asset = resp["fills"][0]["commissionAsset"] if resp.get("fills") else quote_asset

            self.logger.success(
                f"[{symbol}] Futures Filled: {side_str} {total_base_quantity} @ {avg_price:.6f} "
                f"comm={total_comm} {comm_asset}"
            )

        except BinanceAPIException as e:
            self.logger.error(f"[{symbol}] place_market_order API error: {e}")
        except Exception as e:
            self.logger.error(f"[{symbol}] place_market_order error: {e}")

    async def cancel_order(self, symbol: str, order_id: str):
        """Cancel an active futures order by id."""
        try:
            result = await self._client.futures_cancel_order(symbol=symbol, orderId=int(order_id))
            self._open_orders[symbol].pop(order_id, None)
            self.logger.info(f"[{symbol}] Futures order {order_id} cancelled: {result}")
        except Exception as e:
            self.logger.error(f"[{symbol}] cancel_order error: {e}")

    async def get_open_orders(self, symbol: str):
        """Fetch and reconcile open orders for a specific symbol."""
        try:
            raw_orders = await self._client.futures_get_open_orders(symbol=symbol)

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
                    order_type=_BINANCE_ORDER_TYPE.get(raw_type, OrderType.OTHERS),
                    quantity=float(o.get("origQty", 0)),
                    price=float(o.get("price", 0)) or None,
                    status=_BINANCE_STATUS.get(raw_status, OrderStatus.OPEN),
                    avg_fill_price=avg_price,
                    filled_qty=cum_filled_qty,
                    timestamp=datetime.datetime.fromtimestamp(
                        o.get("time", 0) / 1000, tz=datetime.timezone.utc
                    ),
                    venue=Venue.BINANCE,
                    last_updated_timestamp=updated_time,
                )
                refreshed[order_id] = order

            self._open_orders[symbol] = refreshed

        except Exception as e:
            self.logger.error(f"[{symbol}] get_open_orders error: {e}")

    # ── Position Management ───────────────────────────────────────────────────
    async def close_position(self, symbol: str):
        """Close entire position for a symbol via market order."""
        position = self.get_position(symbol)
        if not position:
            self.logger.warning(f"[{symbol}] No position to close")
            return

        amount = position["amount"]
        close_side = Side.SELL if amount > 0 else Side.BUY
        qty = abs(amount)

        try:
            resp = await self._client.futures_create_order(
                symbol=symbol,
                side=close_side.value.upper(),
                type="MARKET",
                quantity=qty,
                reduceOnly=True,
            )
            self.logger.info(f"[{symbol}] Position closed: {resp}")
        except Exception as e:
            self.logger.error(f"[{symbol}] Failed to close position: {e}")

    # ── Run ───────────────────────────────────────────────────────────────────
    async def run(self):
        """
        Initialise the futures client and start background monitoring tasks.
        Feed streams are NOT started here — call feed.__call__() separately
        or use the data feed managers in run_strategy.
        """
        await self.initialize()
        asyncio.create_task(self.monitor_balances(interval_secs=10, name="BinanceFutures"))
        asyncio.create_task(self.monitor_open_orders())
        asyncio.create_task(self.print_snapshot())
        await asyncio.sleep(0.5)   # let first account fetch settle
        self.logger.info("BinanceFuturesExchange running.")

    async def cleanup(self):
        """Cleanup client managers and authenticated client."""
        # Stop user data stream
        await self.user_data_manager.stop()

        # Stop shared client managers
        if self.kline_manager:
            await self.kline_manager.stop()
        if self.ob_manager:
            await self.ob_manager.stop()

        # Close authenticated client
        if self._client:
            await self._client.close_connection()
            self._client = None

        self.logger.info("BinanceFuturesExchange cleanup completed.")
