import asyncio
import collections
import datetime
import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
import json
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException
from binance.helpers import round_step_size

from elysian_core.connectors.base import AbstractDataFeed, SpotExchangeConnector, AbstractClientManager, AbstractKlineFeed
from elysian_core.core.enums import OrderStatus, Side, TradeType, _BINANCE_SIDE, _BINANCE_STATUS, AssetType, Venue, OrderType
from elysian_core.core.order import Order   
from elysian_core.core.market_data import Kline, OrderBook, BinanceOrderBook
from elysian_core.db.database import DATABASE
from elysian_core.connectors.binance.BinanceDataConnectors import (
    BinanceKlineClientManager,
    BinanceOrderBookClientManager,
    BinanceKlineFeed,
    BinanceOrderBookFeed,
    BinanceUserDataClientManager,
)
from elysian_core.db.models import CexTrade
from elysian_core.config.app_config import StrategyConfig
import elysian_core.utils.logger as log
import argparse
import pylru

# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────────────────── BinanceExchange ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class BinanceSpotExchange(SpotExchangeConnector):
    """
    Authenticated Binance exchange client for multiple symbols.

    Manages:
      - Per-symbol BinanceKlineFeed and BinanceOrderBookFeed
      - Account balances (polled every second)
      - Open orders registry
      - Market and limit order placement / cancellation
      - DEX-fill hedging logic
      - Asset rebalancing and withdrawals

    Usage:
        exchange = BinanceExchange(
            api_key="...", api_secret="...",
            symbols=["ETHUSDT", "SUIUSDT"],
        )
        await exchange.run()                          # starts background tasks
        await exchange.place_market_order(...)
    """

    def __init__(
        self,
        args: argparse.Namespace = argparse.Namespace(),
        api_key: str = "",
        api_secret: str = "",
        symbols: Optional[List[str]] = [],
        file_path: Optional[str] = None,
        kline_manager: Optional[BinanceKlineClientManager] = None,
        ob_manager: Optional[BinanceOrderBookClientManager] = None,
        user_data_manager: Optional[BinanceUserDataClientManager] = None,
        event_bus=None,
        strategy_config: Optional[StrategyConfig] = None
    ):

        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols,
            file_path=file_path,
            kline_manager=kline_manager,
            ob_manager=ob_manager,
            strategy_config=strategy_config
        )
        
        self.strategy_id = strategy_config.strategy_id if strategy_config else 'MAIN'

        # user data — owns event bus publishing and event parsing
        self.user_data_manager = user_data_manager or BinanceUserDataClientManager(api_key, api_secret)
        if event_bus:
            self.user_data_manager.set_event_bus(event_bus)
    
    # ── Initialisation ────────────────────────────────────────────────────────
    async def initialize(self):
        """Create authenticated client, fetch metadata and start user-data stream."""
        
        self._client = await AsyncClient.create(self._api_key, self._api_secret)
        for sym in self._symbols:
            await self._fetch_symbol_info(sym)
            
        # Pass token info to manager so it can populate base/quote in OrderUpdateEvent
        self.user_data_manager.set_token_info(self._token_infos)

        # start user-data listener
        try:
            self.user_data_manager.register(self._handle_user_data)
            await self.user_data_manager.start()
            self.logger.info("User data stream started")
        except Exception as e:
            self.logger.error(f"Failed to start user data stream: {e}")

        self.logger.info("BinanceExchange initialised.......")
        
        
    async def _fetch_symbol_info(self, symbol: str):
        '''
        Function to fetch symbol information
        '''
        info = await self._client.get_symbol_info(symbol)
        step_size = float(
            next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        )
        min_notional = float(
            next(f["minNotional"] for f in info["filters"] if f["filterType"] == "NOTIONAL")
        )
        self._token_infos[symbol] = {"step_size": step_size, 
                                     "min_notional": min_notional, 
                                     "base_asset": info["baseAsset"], 
                                     "quote_asset": info["quoteAsset"], 
                                     "base_asset_precision": info["baseAssetPrecision"], 
                                     "quote_asset_precision": info["quoteAssetPrecision"]}
        #self.logger.info(f"[{symbol}] step={step_size} min_notional={min_notional}")


    # ── Account ───────────────────────────────────────────────────────────────
    async def refresh_balances(self):
        account = await self._client.get_account()
        #print(f"Account Fetched: {account}")
        for b in account["balances"]:
            self._balances[b["asset"]] = float(b["free"])


    def _handle_user_data(self, event: dict) -> None:
        """Callback receiving normalized events from BinanceUserDataClientManager.
        
        Only responsible for state updates (_balances, _open_orders).
        Parsing and event bus publishing are handled by the manager.
        """
        t = event["type"]
        if t == "balance_update":
            asset, delta = event["asset"], event["delta"]
            self._balances[asset] = self._balances.get(asset, 0.0) + delta
            self.logger.info(f"[BinanceExchange_{self.strategy_id}] [{asset}] balance update delta={delta:.6f} new={self._balances[asset]:.6f}")

        elif t == "account_position":
            for b in event["balances"]:
                self._balances[b["asset"]] = b["free"] + b["locked"]
            self.logger.info(f"[BinanceExchange_{self.strategy_id}] outboundAccountPosition update applied to balances")

        elif t == "execution_report":
            self.logger.info(f"[BinanceExchange_{self.strategy_id}] Received execution report @ {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))}")
            self._handle_execution_report(event["order"], event["exec_type"])
            
    def _handle_execution_report(self, order: Order, exec_type: str) -> None:
        """Update _open_orders from a parsed Order received via the user-data callback."""
        symbol, order_id = order.symbol, order.id
        symbol_orders = self._open_orders.get(symbol, {})

        # ── NEW order ────────────────────────────────────────────────────────────
        if exec_type == "NEW" and order_id not in symbol_orders:
            if order_id in self._past_orders:
                self.logger.warning(f"[{symbol}] Received NEW execution report for recently closed order {order_id}, re-adding to open orders")
                raise ValueError(f"[BinanceExchange_{self.strategy_id}] Received NEW execution report for recently closed order {order_id}")
            else:
                self._open_orders[symbol][order_id] = order
                self.logger.info(f"[BinanceExchange_{self.strategy_id}] [{symbol}] New order registered: {order}")
            return

        # ── Update existing order ─────────────────────────────────────────────────
        existing = symbol_orders.get(order_id)
        if existing is None:
            self.logger.warning(f"[{symbol}] executionReport for unknown order {order_id}, skipping")
            return

        # Use Binance's cumulative fields as the authoritative source — don't recompute
        if order.filled_qty > existing.filled_qty:
            existing.filled_qty = order.filled_qty # z  — cumulative filled qty
            existing.avg_fill_price = order.avg_fill_price # Z/z — cumulative avg price (authoritative)
            existing.last_fill_price = order.last_fill_price # L — last executed price

        # n is per-execution commission (not cumulative) — accumulate across partial fills
        existing.commission += order.commission
        existing.commission_asset = order.commission_asset or existing.commission_asset
        existing.last_updated_timestamp = order.last_updated_timestamp
        existing.transition_to(order.status)  # X — Binance is source of truth for status
        self.logger.info(f"[BinanceExchange_{self.strategy_id}] [{symbol}] Order updated: {existing}")

        if existing.is_terminal:
            closed_order = self._open_orders[symbol].pop(order_id, None)
            self._past_orders[order_id] = closed_order
            self.logger.info(f"[BinanceExchange_{self.strategy_id}] [{symbol}] Order {order_id} removed from open orders")
        
        
    async def monitor_balances(self, poll_interval = 10):
        while True:
            await self.refresh_balances()
            await asyncio.sleep(poll_interval)


    
    async def get_deposit_address(self, coin: str, network: Optional[str] = None) -> Optional[str]:
        '''
        Gets the deposit address for a given coin and network. If network is None, gets the default address.
        '''
        params = {"coin": coin, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        if network:
            params["network"] = network
        qs = urlencode(params)
        sig = hmac.new(self._api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig

        def _sync_fetch():
            return requests.get(
                "https://api.binance.com/sapi/v1/capital/deposit/address",
                headers={"X-MBX-APIKEY": self._api_key},
                params=params,
            )

        resp = await asyncio.to_thread(_sync_fetch)
        if resp.status_code == 200:
            return resp.json()["address"]
        self.logger.error(f"[BinanceExchange_{self.strategy_id}] get_deposit_address failed: {resp.status_code} {resp.json()}")
        return None
    
    
    async def deposit_asset(self, coin, amount, network = None):
        """
        Implementation to be done
        """
        pass
    

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_limit_order(
        self, symbol: str, side: Side, quantity: float, price: float, strategy_id: int
    ):
        """
        Place a GTC limit order.
        
        Quantity is denoted in BASE_ASSET we set it as such
        """
        try:
            order = await self._client.create_order(
                symbol=symbol,
                side=side.value.upper(),
                type="LIMIT",
                timeInForce="GTC",
                quantity=quantity,
                price=str(price),
                strategyId=strategy_id
            )
            order_id = str(order.get("orderId", ""))
            order_obj = Order(
                id=order_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                price=price,
                status=OrderStatus.PENDING,
                venue=Venue.BINANCE,
                strategy_id=strategy_id
            )
            self._open_orders[symbol][order_id] = order_obj
            self.logger.info(f"[BinanceExchange_{self.strategy_id}] Limit order placed: {order_id}")
            return order
        except BinanceAPIException as e:
            self.logger.error(f"[BinanceExchange_{self.strategy_id}] [{symbol}] place_limit_order API error: {e}")
        except Exception as e:
            self.logger.error(f"[BinanceExchange_{self.strategy_id}] [{symbol}] place_limit_order error: {e}")



    async def cancel_order(self, symbol: str, order_id: int):
        """Cancel an active order by id."""
        try:
            result = await self._client.cancel_order(symbol=symbol, orderId=order_id)
            self.logger.info(f"[BinanceExchange_{self.strategy_id}] [{symbol}] Order {order_id} cancelled: {result}")
        except Exception as e:
            self.logger.error(f"[BinanceExchange_{self.strategy_id}] [{symbol}] cancel_order error: {e}")
            
            
            
    async def get_open_orders(self, symbol: str):
        """Refresh open orders for a specific symbol."""
        try:
            open_orders = await self._client.get_open_orders(symbol=symbol)
            #self.logger.info(f"[BinanceExchange_{self.strategy_id}] [{symbol}] Open orders: {open_orders}")
            return open_orders
        except Exception as e:
            self.logger.error(f"[BinanceExchange_{self.strategy_id}] [{symbol}] get_open_orders error: {e}")


    async def get_all_open_orders(self):
        """Refresh open orders for all symbols."""
        all_orders = await self._client.get_open_orders()
        
        # Snapshot existing orders before repopulating to support staleness checks
        prev_orders = {sym: dict(orders) for sym, orders in self._open_orders.items()}
        self._open_orders.clear()
        for o in all_orders:
            updated_time = o.get("updateTime")
            symbol = o.get("symbol")
            order_id = str(o.get("orderId"))
            strategy_id = int(o.get('strategyId'))

            existing_order = prev_orders.get(symbol, {}).get(order_id)
            if existing_order and updated_time and existing_order.last_updated_timestamp and updated_time < existing_order.last_updated_timestamp:
                self._open_orders[symbol][order_id] = existing_order
                continue
            
            raw_status = o.get("status", "NEW")
            raw_type = o.get("type", "OTHERS")
            raw_side = o.get("side", "OTHERS")

            cum_filled_qty = float(o.get("executedQty", 0))
            cum_quote_qty  = float(o.get("cummulativeQuoteQty", 0))
            avg_price = (cum_quote_qty / cum_filled_qty) if cum_filled_qty > 0 else float(o.get("price", 0))

            order = Order(
                id = order_id,
                symbol = symbol,
                side = Side.BUY if raw_side == "BUY" else Side.SELL,
                order_type = OrderType.MARKET if raw_type == "MARKET" else OrderType.LIMIT,
                quantity = float(o.get("origQty", 0)),
                price = float(o.get("price", 0)) or None,
                status = _BINANCE_STATUS.get(raw_status, OrderStatus.OPEN),
                avg_fill_price = avg_price,
                filled_qty = cum_filled_qty,
                timestamp = datetime.datetime.fromtimestamp(
                                o.get("time", 0) / 1000,
                                tz=datetime.timezone.utc
                            ),
                venue = Venue.BINANCE,
                strategy_id=strategy_id
            )
            
            self._open_orders[symbol][order_id] = order
            #self.logger.info(f"[{symbol}] Loaded open order: {order}")

        self.logger.info(f"[BinanceExchange_{self.strategy_id}] Refreshed open orders: {sum(len(v) for v in self._open_orders.values())} orders across {len(self._open_orders)} symbols")



    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float, # Denoted in terms of base asset ie. 0.1 ETH
        strategy_id: int,
        use_quote_order_qty: bool = False,
    ):
        """
        Place a market order. amount is in base asset unless use_quote_order_qty=True.
        """
        
        if not self.order_health_check(symbol, side, quantity, use_quote_order_qty):
            self.logger.error(f"[{symbol}] Market order health check failed. Order not placed.")
            return
                
        # ------- Main loop to place market order and handle response ------- ##### 
        base_asset, quote_asset = self._token_infos.get(symbol, {}).get("base_asset"), self._token_infos.get(symbol, {}).get("quote_asset")
        price = self.last_price(symbol) or 0.0
        
        try:
            step = self._token_infos.get(symbol, {}).get("step_size", 0.0001)
            qty = round_step_size(abs(quantity), step)

            # Amount denoted in Base Asset
            if not use_quote_order_qty:
                self.logger.info(f"[{symbol}] Market {'BUY' if side == Side.BUY else 'SELL'} qty={qty} {base_asset} (~{qty * price:.2f} {quote_asset})")
                fn = self._client.order_market_buy if side == Side.BUY else self._client.order_market_sell
                
                # Create new order obj and add to the open orders registry before placing the order, so that the user-data stream can update it when execution report arrives
                resp = await fn(symbol=symbol, quantity=qty, strategyId=strategy_id)
                
            else:
                self.logger.info(f"[{symbol}] Market {'BUY' if side == Side.BUY else 'SELL'} quoteQty={qty:.2f} {quote_asset}")
                fn = self._client.order_market_buy if side == Side.BUY else self._client.order_market_sell
                resp = await fn(symbol=symbol, quoteOrderQty=qty, strategyId=strategy_id)
                

            # For market orders, log the REST response fill summary.
            # Authoritative fill recording happens in ShadowBook when the
            # order reaches FILLED via the user-data stream.
            avg_price, total_comm = self._average_fill_price(resp)
            total_base_quantity = float(resp.get("executedQty", 0))
            comm_asset = resp["fills"][0]["commissionAsset"] if resp.get("fills") else ""

            self.logger.success(
                f"[{symbol}] Filled: {side.value} {total_base_quantity} @ {avg_price:.6f} "
                f"comm={total_comm} {comm_asset}"
            )
            return resp

        except BinanceAPIException as e:
            self.logger.error(f"[{symbol}] place_market_order API error: {e}")
        except Exception as e:
            self.logger.error(f"[{symbol}] place_market_order error: {e}")
            



    # ──  Withdrawals ─────────────────────────────────────────────
    async def withdraw_asset(
        self, coin: str, amount: float, address: str, network: str = "SUI"
    ) -> bool:
        try:
            await self._client.withdraw(coin=coin, address=address, amount=amount, network=network)
            self.logger.info(f"Withdrew {amount} {coin} → {address} via {network}")
            return True
        except BinanceAPIException as e:
            self.logger.error(f"withdraw_asset API error: {e}")
        except Exception as e:
            self.logger.error(f"withdraw_asset error: {e}")
        return False

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self):
        """
        Initialise the client and start background monitoring tasks.
        Feed streams are NOT started here — call feed.__call__() separately
        or pass them to your ThreadPoolExecutor.
        """
        await self.initialize()
        asyncio.create_task(self.monitor_balances())
        asyncio.create_task(self.monitor_open_orders())
        asyncio.create_task(self.print_snapshot())
        await asyncio.sleep(0.5)   # let first balance fetch settle
        self.logger.info("BinanceExchange running................")
        
    

    async def cleanup(self):
        """
        Cleanup shared client managers and authenticated client.
        Call this before shutting down the exchange.
        """

        # Stop shared client managers
        await self.kline_manager.stop()
        await self.ob_manager.stop()

        # Close authenticated client
        if self._client:
            await self._client.close_connection()
            self._client = None

        self.logger.info("BinanceExchange cleanup completed.")

