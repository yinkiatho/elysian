import asyncio
import collections
import datetime
import hashlib
import hmac
import math
import statistics
import time
from datetime import datetime as dt
from typing import Any, Dict, List, Optional, Callable
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
from elysian_core.core.events import KlineEvent, OrderBookUpdateEvent, OrderUpdateEvent, BalanceUpdateEvent
from elysian_core.db.database import DATABASE
from elysian_core.db.models import CexTrade
import elysian_core.utils.logger as log
import argparse


logger = log.setup_custom_logger("root")


# ──────────────────────────────────────────────────────────────────────────────
# Shared Client Managers
# ──────────────────────────────────────────────────────────────────────────────

class BinanceKlineClientManager:
    """Shared AsyncClient for multiplex kline streams with queue-based processing."""

    def __init__(self):
        self._client: Optional[AsyncClient] = None
        self._manager: Optional[BinanceSocketManager] = None
        self._socket: Optional[Any] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100000)
        self._active_feeds: Dict[str, BinanceKlineFeed] = {}
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._event_bus = None

    def set_event_bus(self, event_bus):
        """Inject an EventBus so kline updates can be published."""
        self._event_bus = event_bus

    async def _create_client(self, retries: int = 5) -> AsyncClient:
        for i in range(retries):
            try:
                # Pre-resolve DNS to avoid async DNS issues
                loop = asyncio.get_event_loop()
                await loop.getaddrinfo("api.binance.com", 443)
                logger.debug("BinanceKlineClientManager: DNS pre-resolved successfully")

                return await AsyncClient.create()
            except (TimeoutError, BinanceAPIException) as e:
                logger.warning(f"BinanceKlineClientManager: Client creation attempt {i+1}/{retries} failed: {e}")
                if i == retries - 1:
                    raise
                await asyncio.sleep(2 ** i)

    async def start(self):
        if self._running:
            return

        self._client = await self._create_client()
        self._manager = BinanceSocketManager(self._client, max_queue_size=10000)
        self._running = True
        logger.info("BinanceKlineClientManager: Started shared kline client")

    async def stop(self):
        if not self._running:
            return

        self._running = False

        # Cancel reader and worker tasks
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        for task in self._worker_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close socket and client
        if self._socket:
            await self._socket.__aexit__(None, None, None)
            self._socket = None

        if self._client:
            await self._client.close_connection()
            self._client = None

        logger.info("BinanceKlineClientManager: Stopped shared kline client")


    def register_feed(self, feed: 'BinanceKlineFeed'):
        self._active_feeds[feed._name] = feed


    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)


    async def _reader_coroutine(self):
        """Network reader: only reads messages from multiplex socket."""
        if not self._active_feeds:
            logger.warning("BinanceKlineClientManager: No feeds registered for reader")
            return

        # Create multiplex socket for all symbols
        streams = [f"{symbol.lower()}@kline_1s" for symbol in self._active_feeds.keys()]
        self._socket = self._manager.multiplex_socket(streams)

        reconnect_delay = 1
        while self._running:
            try:
                async with self._socket:
                    logger.info(f"BinanceKlineClientManager: Multiplex socket opened for {len(streams)} streams")
                    reconnect_delay = 1  # Reset delay on successful connection

                    while self._running:
                        try:
                            msg = await self._socket.recv()
                            await self._queue.put(msg)
                        except Exception as e:
                            logger.error(f"BinanceKlineClientManager: Error in reader: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"BinanceKlineClientManager: Socket connection error: {e}", exc_info=False)

            if self._running:
                logger.warning(f"BinanceKlineClientManager: Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _worker_coroutine(self, worker_id: int):
        """Worker coroutine: processes messages from queue."""
        logger.debug(f"BinanceKlineClientManager: Worker {worker_id} started")

        while self._running:
            try:
                msg = await self._queue.get()

                stream = msg.get("stream", "")
                data = msg.get("data", {})

                # Extract symbol from stream name (e.g., "ethusdt@kline_1s" -> "ETHUSDT")
                if "@kline" in stream:
                    symbol = stream.split("@")[0].upper()
                    feed = self._active_feeds.get(symbol)
                    if feed:
                        kline = feed._process_kline_event(data)
                        if kline is not None:
                            feed._kline = kline
                            feed._historical.append(kline.close)

                            if len(feed._historical) == 60:
                                returns = [
                                    (feed._historical[i] - feed._historical[i - 1]) / feed._historical[i - 1]
                                    for i in range(1, len(feed._historical))
                                ]
                                feed._vol = statistics.stdev(returns) * math.sqrt(20)

                            # Emit event to strategy via EventBus
                            if self._event_bus is not None:
                                await self._event_bus.publish(KlineEvent(
                                    symbol=symbol,
                                    venue=Venue.BINANCE,
                                    kline=kline,
                                    timestamp=int(time.time() * 1000),
                                ))

                self._queue.task_done()

            except Exception as e:
                logger.error(f"BinanceKlineClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

        logger.debug(f"BinanceKlineClientManager: Worker {worker_id} stopped")

    async def run_multiplex_feeds(self):
        """Run multiplex socket with reader and worker pool."""
        if not self._active_feeds:
            logger.warning("BinanceKlineClientManager: No feeds registered")
            return

        # Start reader coroutine
        self._reader_task = asyncio.create_task(self._reader_coroutine())

        # Start worker pool (4 workers for parallel processing)
        num_workers = min(8, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i))
            for i in range(num_workers)
        ]

        logger.info(f"BinanceKlineClientManager: Started with {num_workers} workers")

        # Wait for reader to complete (it runs until stopped)
        await self._reader_task


class BinanceOrderBookClientManager:
    """Shared AsyncClient for multiplex orderbook streams with queue-based processing."""

    def __init__(self):
        self._client: Optional[AsyncClient] = None
        self._manager: Optional[BinanceSocketManager] = None
        self._socket: Optional[Any] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, BinanceOrderBookFeed] = {}
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._event_bus = None

    def set_event_bus(self, event_bus):
        """Inject an EventBus so order book updates can be published."""
        self._event_bus = event_bus

    async def _create_client(self, retries: int = 5) -> AsyncClient:
        for i in range(retries):
            try:
                # Pre-resolve DNS to avoid async DNS issues
                loop = asyncio.get_event_loop()
                await loop.getaddrinfo("api.binance.com", 443)
                logger.debug("BinanceOrderBookClientManager: DNS pre-resolved successfully")

                return await AsyncClient.create()
            except (TimeoutError, BinanceAPIException) as e:
                logger.warning(f"BinanceOrderBookClientManager: Client creation attempt {i+1}/{retries} failed: {e}")
                if i == retries - 1:
                    raise
                await asyncio.sleep(2 ** i)

    async def start(self):
        if self._running:
            return

        self._client = await self._create_client()
        self._manager = BinanceSocketManager(self._client, max_queue_size=10000)
        self._running = True
        logger.info("BinanceOrderBookClientManager: Started shared orderbook client")


    async def stop(self):
        if not self._running:
            return

        self._running = False

        # Cancel reader and worker tasks
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        for task in self._worker_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close socket and client
        if self._socket:
            await self._socket.__aexit__(None, None, None)
            self._socket = None

        if self._client:
            await self._client.close_connection()
            self._client = None

        logger.info("BinanceOrderBookClientManager: Stopped shared orderbook client")

    def register_feed(self, feed: 'BinanceOrderBookFeed'):
        self._active_feeds[feed._name] = feed

    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)

    async def _reader_coroutine(self):
        """Network reader: only reads messages from multiplex socket."""
        if not self._active_feeds:
            logger.warning("BinanceOrderBookClientManager: No feeds registered for reader")
            return

        # Create multiplex socket for all symbols
        streams = [f"{symbol.lower()}@depth@100ms" for symbol in self._active_feeds.keys()]
        self._socket = self._manager.multiplex_socket(streams)

        reconnect_delay = 1
        while self._running:
            try:
                async with self._socket:
                    logger.info(f"BinanceOrderBookClientManager: Multiplex socket opened for {len(streams)} streams")
                    reconnect_delay = 1  # Reset delay on successful connection

                    while self._running:
                        try:
                            msg = await self._socket.recv()
                            await self._queue.put(msg)
                        except Exception as e:
                            logger.error(f"BinanceOrderBookClientManager: Error in reader: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"BinanceOrderBookClientManager: Socket connection error: {e}", exc_info=False)

            if self._running:
                logger.warning(f"BinanceOrderBookClientManager: Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _worker_coroutine(self, worker_id: int):
        """Worker coroutine: processes messages from queue."""
        logger.debug(f"BinanceOrderBookClientManager: Worker {worker_id} started")

        while self._running:
            try:
                msg = await self._queue.get()

                stream = msg.get("stream", "")
                data = msg.get("data", {})

                # Extract symbol from stream name (e.g., "ethusdt@depth20@100ms" -> "ETHUSDT")
                if "@depth" in stream:
                    symbol = stream.split("@")[0].upper()
                    feed = self._active_feeds.get(symbol)
                    if feed:
                        feed._data = await feed._process_depth_event(data)

                        if feed.save_data:
                            asyncio.create_task(feed._append_to_df(feed._data))

                        # Emit event to strategy via EventBus
                        if self._event_bus is not None and feed._data is not None:
                            await self._event_bus.publish(OrderBookUpdateEvent(
                                symbol=symbol,
                                venue=Venue.BINANCE,
                                orderbook=feed._data,
                                timestamp=int(time.time() * 1000),
                            ))

                self._queue.task_done()

            except Exception as e:
                logger.error(f"BinanceOrderBookClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

        logger.debug(f"BinanceOrderBookClientManager: Worker {worker_id} stopped")

    async def run_multiplex_feeds(self):
        """Run multiplex socket with reader and worker pool."""
        if not self._active_feeds:
            logger.warning("BinanceOrderBookClientManager: No feeds registered")
            return

        # Start reader coroutine
        self._reader_task = asyncio.create_task(self._reader_coroutine())

        # Start worker pool (4 workers for parallel processing)
        num_workers = min(8, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i))
            for i in range(num_workers)
        ]

        logger.info(f"BinanceOrderBookClientManager: Started with {num_workers} workers")

        # Wait for reader to complete (it runs until stopped)
        await self._reader_task


# Global client managers
# binance_spot_kline_client_manager = BinanceKlineClientManager()
# binance_spot_ob_client_manager = BinanceOrderBookClientManager()

class BinanceKlineFeed(AbstractDataFeed):
    """
    Real-time closed kline (candle) feed for a single Binance symbol.
    Produces Kline objects and maintains a 60-candle rolling close-price window
    for realised volatility.

    Usage:
        feed = BinanceKlineFeed()
        feed.create_new(asset="ETHUSDT", interval="1s")
        await feed()
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        self._kline: Optional[Kline] = None   # latest closed kline

    def create_new(self, asset: str, interval: str = "1s"):
        self._name = asset
        self._interval = interval

    @property
    def latest_kline(self) -> Optional[Kline]:
        return self._kline

    @property
    def latest_close(self) -> Optional[float]:
        return self._historical[-1] if self._historical else None

    # rolling vol from close prices — override base which uses mid-price
    async def update_current_stats(self):
        pass  # klines update _historical inline on each closed candle

    def _process_kline_event(self, raw: dict) -> Optional[Kline]:
        k = raw.get("k", {})
        if not k.get("x"):         # only process closed candles
            return None
        
        kline = Kline(
            ticker=k["s"],
            interval=k["i"],
            start_time=dt.fromtimestamp(k["t"] / 1000),
            end_time=dt.fromtimestamp(k["T"] / 1000),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
        )
        #logger.info(f"[{kline.ticker}] Closed kline: {kline}")
        return kline

    async def __call__(self):
        """Register with shared client manager and wait for multiplex events."""
        global binance_spot_kline_client_manager

        # Register this feed with the shared manager
        binance_spot_kline_client_manager.register_feed(self)

        # Start the shared manager if not already running
        if not binance_spot_kline_client_manager._running:
            await binance_spot_kline_client_manager.start()
            # Start the multiplex feed runner in background
            asyncio.create_task(binance_spot_kline_client_manager.run_multiplex_feeds())

        # Keep the feed alive
        while True:
            await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────────────────────
# BinanceOrderBookFeed
# ──────────────────────────────────────────────────────────────────────────────

class BinanceOrderBookFeed(AbstractDataFeed):
    """
    Real-time depth (order book) feed for a single Binance symbol.
    Produces OrderBook snapshots updated at 100ms intervals.

    Usage:
        feed = BinanceOrderBookFeed()
        feed.create_new(asset="ETHUSDT")
        await feed()
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        
        # Binance Specific Parameters
        self.last_update_id: Optional[int] = None
        self.snapshot_ready: bool = False
        self.event_buffer: List[dict] = []  # Buffer events while waiting for snapshot
        self.first_update_processed: bool = False  # Track if we've synced with snapshot

    def create_new(self, asset: str, interval: str = "100ms"):
        self._name = asset
        self._interval = interval

    def _fetch_rest_snapshot(self, limit: int = 100) -> dict:
        resp = requests.get(
            "https://api.binance.com/api/v3/depth",
            params={"symbol": self._name, "limit": min(limit, 5000)},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_initial_snapshot(self):
        """Fetch snapshot and process any buffered events that cover it."""
        
        raw = self._fetch_rest_snapshot(100)
        ts = int(time.time() * 1000)
        bids = [[float(b[0]), float(b[1])] for b in raw["bids"]]
        asks = [[float(a[0]), float(a[1])] for a in raw["asks"]]

        self._data = BinanceOrderBook.from_lists(
            last_update_id=raw["lastUpdateId"],
            last_timestamp=ts,
            ticker=self._name,
            interval=self._interval,
            bid_levels=bids,
            ask_levels=asks,
        )
        self.last_update_id = raw["lastUpdateId"]
        self.snapshot_ready = True
        self.first_update_processed = False  # Reset for new snapshot sync
        #logger.info(f"Fetched Binance [{self._name}] OB snapshot id={raw['lastUpdateId']}")
        
        # Process any buffered events now that snapshot is ready
        if self.event_buffer:
            #logger.info(f"[{self._name}] Processing {len(self.event_buffer)} buffered depth events")
            await self._process_buffered_events()

    async def _process_buffered_events(self):
        """Process buffered events after snapshot is ready."""
        buffered_events = self.event_buffer[:]
        self.event_buffer.clear()
        
        for buffered_event in buffered_events:
            #print(buffered_event)
            # Skip events that are too old (u < lastUpdateId)
            if buffered_event['u'] < self._data.last_update_id:
                logger.debug(f"[{self._name}] Skipping old buffered event u={buffered_event['u']}")
                continue
            
            # Find first event that covers the snapshot
            elif buffered_event['U'] <= self._data.last_update_id and buffered_event['u'] >= self._data.last_update_id:
                #logger.info(f"[{self._name}] Found sync point: buffered event u={buffered_event['u']} covers snapshot id={self._data.last_update_id}")
                await self._apply_depth_update(buffered_event)
                self.first_update_processed = True
                

            elif buffered_event['U'] > self._data.last_update_id:
                # This event is ahead of snapshot with gap - re-fetch snapshot
                #logger.warning(f"[{self._name}] Gap detected: buffered event U={buffered_event['U']} > snapshot id={self._data.last_update_id}")
                self.snapshot_ready = False
                asyncio.create_task(self.get_initial_snapshot())
                return
            
            elif self.first_update_processed:
                await self._apply_depth_update(buffered_event)
            
            
    
    async def _process_depth_event(self, event: dict) -> OrderBook:
        """Process depth updates following Binance API spec."""
        
        # Phase 1: Buffer events while snapshot is being fetched
        if not self.snapshot_ready:
            self.event_buffer.append(event)
            #logger.warning(f"[{self._name}] Buffering depth event (snapshot not ready). Buffer size: {len(self.event_buffer)}")
            return self._data if self._data else None
        
        # Validate U and u according to Binance spec
        if event['u'] < self._data.last_update_id:
            #logger.debug(f"[{self._name}] Skipping old event u={event['u']} < last_update_id={self._data.last_update_id}")
            return self._data
        
        if event['U'] > self._data.last_update_id + 1:
            logger.error(f"[{self._name}] Gap detected: event U={event['U']} > last_update_id+1={self._data.last_update_id+1}. Re-fetching snapshot...")
            self.snapshot_ready = False
            asyncio.create_task(self.get_initial_snapshot())
            raise ValueError("Gap in event sequence")
        
        else:
            await self._apply_depth_update(event)
        return self._data
    
    
    async def _apply_depth_update(self, event: dict):
        """Apply depth update to order book."""
        ts = int(time.time() * 1000)
        await self._data.apply_both_updates(ts, event['u'], 
                                            bid_levels=event.get("bids", []), ask_levels=event.get("asks", []))
        #logger.success(f"[{self._name}] OB update id={self._data.last_update_id} best_bid={self._data.best_bid_price:.5f} best_ask={self._data.best_ask_price:.5f}") 


    async def __call__(self):
        """Register with shared client manager and wait for multiplex events."""
        global binance_spot_ob_client_manager

        # Register this feed with the shared manager (WebSocket will start buffering)
        binance_spot_ob_client_manager.register_feed(self)

        # Start the shared manager if not already running
        if not binance_spot_ob_client_manager._running:
            await binance_spot_ob_client_manager.start()
            
            # Start the multiplex feed runner in background (WebSocket reader/workers)
            asyncio.create_task(binance_spot_ob_client_manager.run_multiplex_feeds())

        # Give WebSocket a moment to start buffering events
        await asyncio.sleep(0.1)
        
        # NOW fetch the snapshot while events are being buffered
        await self.get_initial_snapshot()

        # Keep the feed alive
        while True:
            await asyncio.sleep(1)



# ──────────────────────────────────────────────────────────────────────────────
# User data stream manager for balance/order updates
# ──────────────────────────────────────────────────────────────────────────────
class BinanceUserDataClientManager:
    """Maintain a single user-data websocket and dispatch events.

    Uses the new Binance WebSocket API (wss://ws-api.binance.com) with
    userDataStream.subscribe.signature, replacing the deprecated listenKey
    approach (/api/v3/userDataStream) which was retired 2026-02-20.

    HMAC-SHA256 keys are supported via userDataStream.subscribe.signature
    which does not require an authenticated session (session.logon), unlike
    userDataStream.subscribe which requires Ed25519 keys only.
    """

    WS_API_URL = "wss://ws-api.binance.com:443/ws-api/v3"

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._running: bool = False
        self._ws: Optional[Any] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._subscribers: List[Callable[[dict], None]] = []
        self._event_bus = None  # Optional EventBus — set via set_event_bus()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("BinanceUserDataClientManager: started")

    async def stop(self):
        if not self._running:
            return
        self._running = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("BinanceUserDataClientManager: stopped")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, payload: str) -> str:
        """HMAC-SHA256 hex digest of payload using api_secret as key."""
        return hmac.new(
            self._api_secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    async def _subscribe(self, ws) -> bool:
        """Send userDataStream.subscribe.signature.
        
        Works with HMAC keys without requiring session.logon.
        Params must be sorted alphabetically before signing.
        """
        timestamp = int(time.time() * 1000)

        # Params sorted alphabetically as required by Binance signing spec
        payload = f"apiKey={self._api_key}&timestamp={timestamp}"
        signature = self._sign(payload)

        await ws.send(json.dumps({
            "id": "user-data-sub",
            "method": "userDataStream.subscribe.signature",
            "params": {
                "apiKey": self._api_key,
                "timestamp": timestamp,
                "signature": signature,
            }
        }))

        resp = json.loads(await ws.recv())
        if resp.get("status") != 200:
            logger.error(f"userDataStream.subscribe.signature failed: {resp}")
            return False

        logger.info("BinanceUserDataClientManager: subscribed to user data stream")
        return True

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _reader_loop(self):
        """Connect, subscribe, and dispatch events. Reconnects with exponential backoff on failure."""
        reconnect_delay = 1
        while self._running:
            try:
                async with websockets.connect(
                    self.WS_API_URL,
                    additional_headers={"X-MBX-APIKEY": self._api_key},
                    ping_interval=180,  # server pings every 3 min
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws

                    if not await self._subscribe(ws):
                        logger.error("BinanceUserDataClientManager: subscribe failed, stopping")
                        break

                    reconnect_delay = 1  # reset backoff on successful connection
                    logger.info("BinanceUserDataClientManager: connected and listening")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning(f"BinanceUserDataClientManager: failed to decode message: {raw}")
                            continue

                        # New WS API envelope: {"subscriptionId": 0, "event": {"e": ..., ...}}
                        # Skip non-event messages (e.g. heartbeats, subscription confirmations)
                        event = msg.get("event")
                        if event is None:
                            continue

                        # Existing sync callbacks (e.g. BinanceSpotExchange._handle_user_data)
                        for cb in self._subscribers:
                            try:
                                cb(event)
                            except Exception as e:
                                logger.error(f"BinanceUserDataClientManager: callback error: {e}")

                        # Async event bus dispatch for strategy hooks
                        if self._event_bus is not None:
                            et = event.get("e")
                            ts = int(event.get("E", time.time() * 1000))
                            if et == "executionReport":
                                order = self._parse_execution_to_order(event)
                                if order:
                                    await self._event_bus.publish(OrderUpdateEvent(
                                        symbol=event.get("s"),
                                        venue=Venue.BINANCE,
                                        order=order,
                                        timestamp=ts,
                                    ))
                            elif et == "balanceUpdate":
                                await self._event_bus.publish(BalanceUpdateEvent(
                                    asset=event.get("a"),
                                    venue=Venue.BINANCE,
                                    delta=float(event.get("d", 0)),
                                    new_balance=0.0,
                                    timestamp=ts,
                                ))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"BinanceUserDataClientManager: connection error: {e}")
                if self._running:
                    logger.info(f"BinanceUserDataClientManager: reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)

        self._ws = None
        logger.info("BinanceUserDataClientManager: reader loop exited")

    # ── Subscriber registry ───────────────────────────────────────────────────

    def register(self, callback: Callable[[dict], None]):
        """Register a callback to receive raw event dicts from the user data stream."""
        self._subscribers.append(callback)
        logger.debug(f"BinanceUserDataClientManager: registered callback {callback.__name__}")

    def unregister(self, callback: Callable[[dict], None]):
        """Unregister a previously registered callback."""
        try:
            self._subscribers.remove(callback)
            logger.debug(f"BinanceUserDataClientManager: unregistered callback {callback.__name__}")
        except ValueError:
            pass

    def set_event_bus(self, event_bus):
        """Inject an EventBus for pushing typed events to strategy hooks."""
        self._event_bus = event_bus

    # ── Event parsing helpers ──────────────────────────────────────────────────

    @staticmethod
    def _parse_execution_to_order(msg: dict) -> Optional[Order]:
        """Parse a raw executionReport dict into an Order dataclass for event emission.

        Field mappings mirror BinanceSpotExchange._handle_execution_report.
        """
        status = _BINANCE_STATUS.get(msg.get("X"))
        if status is None:
            return None

        side = Side.BUY if msg.get("S") == "BUY" else Side.SELL
        raw_type = msg.get("o", "LIMIT")
        if raw_type.upper() == "MARKET":
            order_type = OrderType.MARKET
        elif raw_type.upper() == "LIMIT":
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.OTHERS

        cum_filled = float(msg.get("z", 0))
        cum_quote = float(msg.get("Z", 0))
        avg_price = (cum_quote / cum_filled) if cum_filled > 0 else float(msg.get("L", 0))

        return Order(
            id=str(msg.get("i")),
            symbol=msg.get("s"),
            side=side,
            order_type=order_type,
            quantity=float(msg.get("q", 0)),
            price=float(msg.get("p", 0)) or None,
            status=status,
            avg_fill_price=avg_price,
            filled_qty=cum_filled,
            timestamp=datetime.datetime.fromtimestamp(
                msg.get("O", 0) / 1000, tz=datetime.timezone.utc
            ),
            venue=Venue.BINANCE,
            commission=float(msg.get("n", 0)),
            commission_asset=msg.get("N"),
            last_updated_timestamp=int(msg.get("E", 0)),
        )