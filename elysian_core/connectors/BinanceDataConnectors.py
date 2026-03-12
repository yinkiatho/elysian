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
                                logger.info(f"[{symbol}] Volatility: {feed._vol * 1e4:.2f} bps (60-kline window)")

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
binance_spot_kline_client_manager = BinanceKlineClientManager()
binance_spot_ob_client_manager = BinanceOrderBookClientManager()

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
        logger.info(f"Fetched Binance [{self._name}] OB snapshot id={raw['lastUpdateId']}")
        
        # Process any buffered events now that snapshot is ready
        if self.event_buffer:
            logger.info(f"[{self._name}] Processing {len(self.event_buffer)} buffered depth events")
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
                logger.info(f"[{self._name}] Found sync point: buffered event u={buffered_event['u']} covers snapshot id={self._data.last_update_id}")
                await self._apply_depth_update(buffered_event)
                self.first_update_processed = True
                

            elif buffered_event['U'] > self._data.last_update_id:
                # This event is ahead of snapshot with gap - re-fetch snapshot
                logger.warning(f"[{self._name}] Gap detected: buffered event U={buffered_event['U']} > snapshot id={self._data.last_update_id}")
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
            logger.warning(f"[{self._name}] Buffering depth event (snapshot not ready). Buffer size: {len(self.event_buffer)}")
            return self._data if self._data else None
        
        # Validate U and u according to Binance spec
        if event['u'] < self._data.last_update_id:
            logger.debug(f"[{self._name}] Skipping old event u={event['u']} < last_update_id={self._data.last_update_id}")
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
        logger.success(f"[{self._name}] OB update id={self._data.last_update_id} best_bid={self._data.best_bid_price:.5f} best_ask={self._data.best_ask_price:.5f}") 


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

    Other parts of the code (e.g. the exchange class) can register
    callbacks to receive raw event dictionaries.  This manager handles
    listen-key creation, keepalive, and reconnection.
    """

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._client: Optional[AsyncClient] = None
        self._listen_key: Optional[str] = None
        self._socket: Optional[Any] = None
        self._running: bool = False
        self._reader_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._subscribers: List[Callable[[dict], None]] = []

    async def start(self):
        if self._running:
            return
        # create authenticated client and initial listen key
        self._client = await AsyncClient.create(self._api_key, self._api_secret)
        self._listen_key = await self._client.stream_get_listen_key()
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
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
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        if self._socket:
            await self._socket.close()
            self._socket = None
        if self._client:
            try:
                if self._listen_key:
                    await self._client.stream_close(self._listen_key)
            except Exception:
                pass
            await self._client.close_connection()
            self._client = None
        logger.info("BinanceUserDataClientManager: stopped")


    async def _reader_loop(self):
        url = f"wss://stream.binance.com:9443/ws/{self._listen_key}"
        reconnect_delay = 1
        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    self._socket = ws
                    logger.info("BinanceUserDataClientManager: socket connected")
                    reconnect_delay = 1
                    while self._running:
                        msg = await ws.recv()
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue
                        for cb in self._subscribers:
                            try:
                                cb(data)
                            except Exception as e:
                                logger.error(f"user data callback error: {e}")
            except Exception as e:
                logger.error(f"BinanceUserDataClientManager connection error: {e}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)


    async def _keepalive_loop(self):
        # ping every 25 minutes to keep the key alive
        while self._running:
            try:
                await asyncio.sleep(25 * 60)
                if self._client and self._listen_key:
                    await self._client.stream_keepalive(self._listen_key)
                    logger.debug("BinanceUserDataClientManager: keepalive")
            except Exception as e:
                logger.error(f"keepalive error: {e}")
                break

    def register(self, callback: Callable[[dict], None]):
        self._subscribers.append(callback)

    def unregister(self, callback: Callable[[dict], None]):
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass