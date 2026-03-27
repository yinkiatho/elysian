import asyncio
import collections
import datetime
import hashlib
import hmac
import math
import statistics
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import pylru
import requests
import websockets
import json
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException
from binance.helpers import round_step_size

from elysian_core.connectors.base import AbstractDataFeed, SpotExchangeConnector, AbstractClientManager, AbstractKlineFeed
from elysian_core.core.enums import OrderStatus, Side, TradeType, _BINANCE_SIDE, _BINANCE_STATUS, AssetType, Venue, OrderType
from elysian_core.core.order import Order   
from elysian_core.core.market_data import Kline, OrderBook, BinanceOrderBook
from elysian_core.db.database import DATABASE
from elysian_core.connectors.BinanceDataConnectors import (
    BinanceKlineClientManager,
    BinanceOrderBookClientManager,
    BinanceKlineFeed,
    BinanceOrderBookFeed,
    BinanceUserDataClientManager,
)
from elysian_core.db.models import CexTrade
import elysian_core.utils.logger as log
import argparse


logger = log.setup_custom_logger("root")

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

    _STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})

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
    ):

        super().__init__(
            args=args,
            api_key=api_key,
            api_secret=api_secret,
            symbols=symbols,
            file_path=file_path,
            kline_manager=kline_manager,
            ob_manager=ob_manager
        )

        # user data
        self.user_data_manager = user_data_manager or BinanceUserDataClientManager(api_key, api_secret)

        # Event bus for strategy hooks
        self._event_bus = event_bus
        if event_bus and self.user_data_manager:
            self.user_data_manager.set_event_bus(event_bus)

        # Initialize strategy id
        self.strategy_name = args.strategy_name
        self.strategy_id = args.strategy_id

    
    # ── Initialisation ────────────────────────────────────────────────────────
    async def initialize(self):
        """Create authenticated client, fetch metadata and start user-data stream."""
        
        self._client = await AsyncClient.create(self._api_key, self._api_secret)
        for sym in self._symbols:
            await self._fetch_symbol_info(sym)

        # start user-data listener
        try:
            self.user_data_manager.register(self._handle_user_data)
            await self.user_data_manager.start()
            logger.info("User data stream started")
        except Exception as e:
            logger.error(f"Failed to start user data stream: {e}")

        logger.info("BinanceExchange initialised.......")
        
        
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
        self._token_infos[symbol] = {"step_size": step_size, "min_notional": min_notional, 
                                     "base_asset": info["baseAsset"], "quote_asset": info["quoteAsset"], 
                                     "base_asset_precision": info["baseAssetPrecision"], "quote_asset_precision": info["quoteAssetPrecision"]}
        #logger.info(f"[{symbol}] step={step_size} min_notional={min_notional}")


    # ── Account ───────────────────────────────────────────────────────────────
    async def refresh_balances(self):
        account = await self._client.get_account()
        #print(f"Account Fetched: {account}")
        for b in account["balances"]:
            self._balances[b["asset"]] = float(b["free"])


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
                asset = bal.get("a")
                free = float(bal.get("f", 0))
                locked = float(bal.get("l", 0))
                self._balances[asset] = free + locked
            logger.info("outboundAccountPosition update applied to balances")
        
        
        # other event types could be handled here (executionReport, etc.)
        elif et == "executionReport":
            logger.info(f"Received execution report @ {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))}")
            self._handle_execution_report(msg)
            
    def _handle_execution_report(self, msg: dict):
        """
        Handles the execution report user data streams, mainly updates all orders.
        
        Key fields:
        i  -> Order ID
        s  -> Symbol
        X  -> Current order status (NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED)
        x  -> Execution type
        z  -> Cumulative filled quantity
        Z  -> Cumulative quote asset transacted quantity (Z/z = avg price)
        L  -> Last executed price
        l  -> Last executed quantity
        n  -> Commission amount
        N  -> Commission asset
        """
        order_id = str(msg.get("i"))
        symbol   = msg.get("s")
        status   = msg.get("X")  # current order status
        exec_type = msg.get("x") # execution type

        internal_status = _BINANCE_STATUS.get(status)
        if internal_status is None:
            logger.warning(f"[{symbol}] Unknown order status '{status}' for order {order_id}")
            return

        symbol_orders = self._open_orders.get(symbol, {})

        # ── NEW order arriving for the first time ────────────────────────────────
        if exec_type == "NEW" and order_id not in symbol_orders:
            side      = Side.BUY if msg.get("S") == "BUY" else Side.SELL
            raw_type  = msg.get("o", "LIMIT")
            if raw_type.lower() == OrderType.MARKET.value.lower():
                order_type = OrderType.MARKET
            elif raw_type.lower() == OrderType.LIMIT.value.lower():
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
                                tz=datetime.timezone.utc
                            ),
                venue       = Venue.BINANCE,
                commission      = float(msg.get("n", 0)),
                commission_asset = msg.get("N"),
                last_updated_timestamp = int(msg.get('E'))
            )
            self._open_orders[symbol][order_id] = order
            logger.info(f"[{symbol}] New order registered: {order}")
            return

        # ── Update existing order ─────────────────────────────────────────────────
        else:
            order = symbol_orders.get(order_id)
            if order is None:
                logger.warning(f"[{symbol}] executionReport for unknown order {order_id} (status={status}), skipping")
                return

            cum_filled_qty = float(msg.get("z", 0))
            cum_quote_qty  = float(msg.get("Z", 0))
            last_price     = float(msg.get("L", 0))

            # Compute avg fill price from cumulative fields (more accurate than last price)
            avg_price = (cum_quote_qty / cum_filled_qty) if cum_filled_qty > 0 else last_price

            if cum_filled_qty > order.filled_qty:
                order.update_fill(cum_filled_qty, avg_price)

            # Commission is cumulative per execution; take the latest
            order.commission += float(msg.get("n", 0))
            order.commission_asset = msg.get("N") or order.commission_asset

            # Validate and apply status from exchange (source of truth).
            # transition_to logs a warning on invalid transitions but always applies.
            # Pass the new status here and if validate correct then own order object will transition to that new status
            order.transition_to(internal_status)  
            logger.info(f"[{symbol}] Order updated: {order}")

            # ── Remove terminal orders from open-orders registry ─────────────────────
            if order.is_terminal:
                self._open_orders[symbol].pop(order_id, None)
                logger.info(f"[{symbol}] Order {order_id} removed from open orders (status={status})")
        
        
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
        resp = requests.get(
            "https://api.binance.com/sapi/v1/capital/deposit/address",
            headers={"X-MBX-APIKEY": self._api_key},
            params=params,
        )
        if resp.status_code == 200:
            return resp.json()["address"]
        logger.error(f"get_deposit_address failed: {resp.status_code} {resp.json()}")
        return None
    
    
    async def deposit_asset(self, coin, amount, network = None):
        """
        Implementation to be done
        """
        pass
    
    async def withdraw_asset(self, coin, amount, address, network=None): 
        """
        Implementation to be done
        """
        pass
    



    # ── Orders ────────────────────────────────────────────────────────────────
    
    def order_health_check(self, symbol: str, side: Side, quantity: float, use_quote_order_qty: bool = False) -> bool:
        """
        Does a health check on whether we have sufficient balances and notional filter to execute the trade
        """
        base_asset, quote_asset = self._token_infos.get(symbol, {}).get("base_asset"), self._token_infos.get(symbol, {}).get("quote_asset")
        if not base_asset or not quote_asset:
            logger.error(f"[{symbol}] place_market_order failed: symbol info not found")
            return False
        
        # Check for minimum notional requirement
        price = self.last_price(symbol) or 0.0
        estimated_notional = price * abs(quantity) if not use_quote_order_qty else abs(quantity)
        min_notional = self._token_infos.get(symbol, {}).get("min_notional", 0.0)

        if estimated_notional < min_notional:
            logger.error(f"[{symbol}] Estimated notional {estimated_notional:.4f} < min {min_notional}. Need to place order of higher notional")
            return False 
        
        # Check if we have enough notional approximately to execute trade
        if not use_quote_order_qty:   # Base asset
            if side == Side.BUY:
                if self._balances.get(quote_asset, 0.0) < price * abs(quantity):
                    logger.error(f"[{symbol}] Insufficient balance to BUY estimated {quantity} {base_asset}. Need ~{price * abs(quantity):.2f} {quote_asset}")
                    return False
            else:  # SELL
                if self._balances.get(base_asset, 0.0) < abs(quantity):
                    logger.error(f"[{symbol}] Insufficient balance to SELL estimated {quantity} {base_asset}. Available: {self._balances.get(base_asset, 0.0):.4f}")
                    return False       
            
        else:
            if side == Side.BUY:
                if self._balances.get(quote_asset, 0.0) < abs(quantity):
                    logger.error(f"[{symbol}] Insufficient balance to BUY estimated {quantity:.2f} {quote_asset} worth of {base_asset}. Available: {self._balances.get(quote_asset, 0.0):.2f}")
                    return False
            else:  # SELL
                if self._balances.get(base_asset, 0.0) < price * abs(quantity):
                    logger.error(f"[{symbol}] Insufficient balance to SELL estimated {quantity:.2f} {quote_asset} worth of {base_asset}. Need ~{price * abs(quantity):.2f} {base_asset}")
                    return False
                
        return True
                

    async def place_limit_order(
        self, symbol: str, side: Side, quantity: float, price: float
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
            )
            self._open_orders[symbol][order_id] = order_obj
            logger.info(f"Limit order placed: {order_id}")
            return order
        except BinanceAPIException as e:
            logger.error(f"[{symbol}] place_limit_order API error: {e}")
        except Exception as e:
            logger.error(f"[{symbol}] place_limit_order error: {e}")



    async def cancel_order(self, symbol: str, order_id: int):
        """Cancel an active order by id."""
        try:
            result = await self._client.cancel_order(symbol=symbol, orderId=order_id)
            logger.info(f"[{symbol}] Order {order_id} cancelled: {result}")
        except Exception as e:
            logger.error(f"[{symbol}] cancel_order error: {e}")
            
            
            
    async def get_open_orders(self, symbol: str):
        """Refresh open orders for a specific symbol."""
        try:
            open_orders = await self._client.get_open_orders(symbol=symbol)
            #logger.info(f"[{symbol}] Open orders: {open_orders}")
            return open_orders
        except Exception as e:
            logger.error(f"[{symbol}] get_open_orders error: {e}")
            
            
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
            )
            
            self._open_orders[symbol][order_id] = order
            #logger.info(f"[{symbol}] Loaded open order: {order}")

        logger.info(f"Refreshed open orders: {sum(len(v) for v in self._open_orders.values())} orders across {len(self._open_orders)} symbols")



    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float, # Denoted in terms of base asset ie. 0.1 ETH
        use_quote_order_qty: bool = False
    ):
        """
        Place a market order. amount is in base asset unless use_quote_order_qty=True.
        """
        
        if not self.order_health_check(symbol, side, quantity, use_quote_order_qty):
            logger.error(f"[{symbol}] Market order health check failed. Order not placed.")
            return
                
        # ------- Main loop to place market order and handle response ------- ##### 
        base_asset, quote_asset = self._token_infos.get(symbol, {}).get("base_asset"), self._token_infos.get(symbol, {}).get("quote_asset")
        price = self.last_price(symbol) or 0.0
        
        try:
            step = self._token_infos.get(symbol, {}).get("step_size", 0.0001)
            qty = round_step_size(abs(quantity), step)

            # Amount denoted in Base Asset
            if not use_quote_order_qty:
                logger.info(f"[{symbol}] Market {'BUY' if side == Side.BUY else 'SELL'} qty={qty} {base_asset} (~{qty * price:.2f} {quote_asset})")
                fn = self._client.order_market_buy if side == Side.BUY else self._client.order_market_sell
                
                # Create new order obj and add to the open orders registry before placing the order, so that the user-data stream can update it when execution report arrives
                resp = await fn(symbol=symbol, quantity=qty)
                
            else:
                logger.info(f"[{symbol}] Market {'BUY' if side == Side.BUY else 'SELL'} quoteQty={qty:.2f} {quote_asset}")
                fn = self._client.order_market_buy if side == Side.BUY else self._client.order_market_sell
                resp = await fn(symbol=symbol, quoteOrderQty=qty)
                

            # For market orders, we assume immediate fills and we process the market response immediately
            avg_price, total_comm = self._average_fill_price(resp)
            total_quote_quantity = float(resp["cummulativeQuoteQty"])
            total_base_quantity = float(resp.get("executedQty", 0))
            order_id = int(resp["orderId"])
            status = str(resp["status"])
            side_str = str(resp["side"])
            order_type = OrderType.MARKET
            comm_asset = resp["fills"][0]["commissionAsset"] if resp.get("fills") else ""

            logger.success(
                f"[{symbol}] Filled: {side_str} {total_base_quantity} @ {avg_price:.6f} "
                f"comm={total_comm} {comm_asset}"
            )
            asyncio.create_task(self._record_trade(
                base_asset=base_asset,
                quote_asset=quote_asset,
                base_amount_executed=total_base_quantity,
                quote_amount_executed=total_quote_quantity,
                order_type=order_type,
                price=avg_price,
                commission=total_comm,
                order_id=order_id,
                status=status,
                side_str=side_str,
                comm_asset=comm_asset
            ))
            return resp

        except BinanceAPIException as e:
            logger.error(f"[{symbol}] place_market_order API error: {e}")
        except Exception as e:
            logger.error(f"[{symbol}] place_market_order error: {e}")
            



    @staticmethod
    def _average_fill_price(resp: dict):
        '''
        Get the average fill price from a MARKET ORDER response, weighted by quantity. Also returns total commission paid for the fills.
        '''
        fills = resp["fills"]
        total_qty = sum(float(f["qty"]) for f in fills)
        total_notional = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        total_comm = sum(float(f["commission"]) for f in fills)
        if total_qty == 0:
            return 0.0, 0.0
        return total_notional / total_qty, total_comm
    
    
    async def _record_trade(self, base_asset: str, quote_asset: str, base_amount_executed: float, quote_amount_executed: float, 
                                  order_type: OrderType, price: float, commission: float, order_id: int, 
                                  status: str, side_str: str, comm_asset: str):
        """
        Record a trade in the database. This is called after a market order is filled.
        """
        ts = datetime.datetime.now(self._utc8)
        try:
            CexTrade.create(
                id=ts.strftime("%Y-%m-%d_%H-%M-%S") + "_" + self.cfg.meta.version_name,
                datetime=ts,
                strategy_id=self.strategy_id,
                strategy_name=self.strategy_name,
                venue=Venue.BINANCE,
                asset_type=AssetType.SPOT,
                symbol=base_asset + quote_asset,
                side=Side.BUY if side_str.lower() == "buy" else Side.SELL,
                base_amount=base_amount_executed,
                quote_amount=quote_amount_executed,
                price=price,
                commission_asset=comm_asset,
                total_commission=commission,
                total_commission_quote=commission * price if comm_asset.upper() not in self._STABLECOINS else commission,
                order_id=str(order_id),
                status=_BINANCE_STATUS.get(status, OrderStatus.OPEN),
                order_side=_BINANCE_SIDE.get(side_str, Side.BUY),
                order_type=order_type
            )
                
            logger.info(f"Trade recorded at {ts.strftime('%Y-%m-%d_%H-%M-%S')}")
        except Exception as e:
            logger.error(f"_record_trade DB error: {e}")
    
    
    

    # ──  Withdrawals ─────────────────────────────────────────────
    async def withdraw_asset(
        self, coin: str, amount: float, address: str, network: str = "SUI"
    ) -> bool:
        try:
            await self._client.withdraw(coin=coin, address=address, amount=amount, network=network)
            logger.info(f"Withdrew {amount} {coin} → {address} via {network}")
            return True
        except BinanceAPIException as e:
            logger.error(f"withdraw_asset API error: {e}")
        except Exception as e:
            logger.error(f"withdraw_asset error: {e}")
        return False

    async def withdraw_half(self, coins: List[str], address: str, network: str = "SUI"):
        for coin in coins:
            bal = self._balances.get(coin, 0.0)
            await self.withdraw_asset(coin, round(bal / 2, 2), address, network)



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
        logger.info("BinanceExchange running................")


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

        logger.info("BinanceExchange cleanup completed.")

