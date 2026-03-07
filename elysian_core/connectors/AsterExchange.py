import asyncio
import collections
import datetime
import hashlib
import hmac
import json
import math
import statistics
import time
from datetime import datetime as dt
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import pylru
import requests
import websockets

from elysian_core.connectors.base import AbstractDataFeed
from elysian_core.core.enums import OrderStatus, Side, TradeType
from elysian_core.core.market_data import Kline, OrderBook, AsterOrderBook
from elysian_core.db.database import DATABASE
from elysian_core.db.models import CexTrade
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

FUTURES_BASE_ENDPOINT = "https://fapi.asterdex.com"
SPOT_BASE_ENDPOINT = "https://sapi.asterdex.com"
WEBSOCKET_SPOT_ENDPOINT = "wss://sstream.asterdex.com/ws"


# ──────────────────────────────────────────────────────────────────────────────
# Shared Client Managers
# ──────────────────────────────────────────────────────────────────────────────

class AsterKlineClientManager:
    """Shared WebSocket client for multiplex kline streams with queue-based processing."""

    def __init__(self):
        self._websocket: Optional[Any] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, 'AsterKlineFeed'] = {}
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._subscription_id = 1

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("AsterKlineClientManager: Started shared kline client")

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

        # Close websocket
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

        logger.info("AsterKlineClientManager: Stopped shared kline client")

    def register_feed(self, feed: 'AsterKlineFeed'):
        self._active_feeds[feed._name] = feed

    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)

    async def _reader_coroutine(self):
        """Network reader: connects WebSocket and reads messages."""
        if not self._active_feeds:
            logger.warning("AsterKlineClientManager: No feeds registered for reader")
            return

        # Create stream params for all symbols
        streams = [f"{symbol.lower()}@kline_1m" for symbol in self._active_feeds.keys()]
        
        reconnect_delay = 1
        while self._running:
            try:
                async with websockets.connect(WEBSOCKET_SPOT_ENDPOINT) as websocket:
                    self._websocket = websocket
                    logger.info(f"AsterKlineClientManager: WebSocket connected for {len(streams)} streams")
                    reconnect_delay = 1  # Reset delay on successful connection

                    # Subscribe to streams
                    subscription_msg = {
                        "method": "SUBSCRIBE",
                        "params": streams,
                        "id": self._subscription_id
                    }
                    await websocket.send(json.dumps(subscription_msg))
                    logger.debug(f"AsterKlineClientManager: Subscribed to {len(streams)} kline streams")
                    self._subscription_id += 1

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(websocket.recv(), timeout=30)
                            await self._queue.put(json.loads(msg))
                        except asyncio.TimeoutError:
                            # Send pong to keep connection alive
                            try:
                                await websocket.pong()
                            except:
                                pass
                        except Exception as e:
                            logger.error(f"AsterKlineClientManager: Error in reader: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"AsterKlineClientManager: WebSocket connection error: {e}", exc_info=False)

            if self._running:
                logger.warning(f"AsterKlineClientManager: Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _worker_coroutine(self, worker_id: int):
        """Worker coroutine: processes messages from queue."""
        logger.debug(f"AsterKlineClientManager: Worker {worker_id} started")

        while self._running:
            try:
                msg = await self._queue.get()

                # Aster wraps data in {"stream": "...", "data": {...}} or direct kline event
                if "data" in msg and "stream" in msg:
                    data = msg.get("data", {})
                    stream = msg.get("stream", "")
                elif "e" in msg and msg["e"] == "kline":
                    data = msg
                    stream = f"{msg.get('s', '').lower()}@kline_1m"
                else:
                    self._queue.task_done()
                    continue

                # Extract symbol from stream name
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
                                feed._vol = statistics.stdev(returns) * math.sqrt(12)
                                logger.info(f"[{symbol}] Volatility: {feed._vol * 1e4:.2f} bps (60-candle window)")

                self._queue.task_done()

            except Exception as e:
                logger.error(f"AsterKlineClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

        logger.debug(f"AsterKlineClientManager: Worker {worker_id} stopped")

    async def run_multiplex_feeds(self):
        """Run multiplex socket with reader and worker pool."""
        if not self._active_feeds:
            logger.warning("AsterKlineClientManager: No feeds registered")
            return

        # Start reader coroutine
        self._reader_task = asyncio.create_task(self._reader_coroutine())

        # Start worker pool
        num_workers = min(4, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i))
            for i in range(num_workers)
        ]

        logger.info(f"AsterKlineClientManager: Started with {num_workers} workers")
        await self._reader_task


class AsterOrderBookClientManager:
    """Shared WebSocket client for multiplex orderbook streams with queue-based processing."""

    def __init__(self):
        self._websocket: Optional[Any] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, 'AsterOrderBookFeed'] = {}
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._subscription_id = 1

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("AsterOrderBookClientManager: Started shared orderbook client")


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

        # Close websocket
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

        logger.info("AsterOrderBookClientManager: Stopped shared orderbook client")


    def register_feed(self, feed: 'AsterOrderBookFeed'):
        self._active_feeds[feed._name] = feed


    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)


    async def _reader_coroutine(self):
        """Network reader: connects WebSocket and reads messages."""
        if not self._active_feeds:
            logger.warning("AsterOrderBookClientManager: No feeds registered for reader")
            return

        # Create stream params for all symbols (20-level depth updates)
        streams = [f"{symbol.lower()}@depth20@100ms" for symbol in self._active_feeds.keys()]
        
        reconnect_delay = 1
        while self._running:
            try:
                async with websockets.connect(WEBSOCKET_SPOT_ENDPOINT) as websocket:
                    self._websocket = websocket
                    logger.info(f"AsterOrderBookClientManager: WebSocket connected for {len(streams)} streams")
                    reconnect_delay = 1

                    # Subscribe to streams
                    subscription_msg = {
                        "method": "SUBSCRIBE",
                        "params": streams,
                        "id": self._subscription_id
                    }
                    await websocket.send(json.dumps(subscription_msg))
                    logger.debug(f"AsterOrderBookClientManager: Subscribed to {len(streams)} depth streams")
                    self._subscription_id += 1

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(websocket.recv(), timeout=30)
                            await self._queue.put(json.loads(msg))
                        except asyncio.TimeoutError:
                            # Send pong to keep connection alive
                            try:
                                await websocket.pong()
                            except:
                                pass
                        except Exception as e:
                            logger.error(f"AsterOrderBookClientManager: Error in reader: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"AsterOrderBookClientManager: WebSocket connection error: {e}", exc_info=False)

            if self._running:
                logger.warning(f"AsterOrderBookClientManager: Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)


    async def _worker_coroutine(self, worker_id: int):
        """Worker coroutine: processes messages from queue."""
        logger.debug(f"AsterOrderBookClientManager: Worker {worker_id} started")

        while self._running:
            try:
                msg = await self._queue.get()

                # Aster wraps data in {"stream": "...", "data": {...}} or direct depth event
                if "e" in msg and msg["e"] == "depthUpdate":
                    data = msg
                    stream = f"{msg.get('s', '').lower()}@depth"
                else:
                    self._queue.task_done()
                    continue

                # Extract symbol from stream name
                if "@depth" in stream:
                    symbol = stream.split("@")[0].upper()
                    feed = self._active_feeds.get(symbol)
                    if feed:
                        feed._data = await feed._process_depth_event(data)

                        if feed.save_data:
                            asyncio.create_task(feed._append_to_df(feed._data))

                self._queue.task_done()

            except Exception as e:
                logger.error(f"AsterOrderBookClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

        logger.debug(f"AsterOrderBookClientManager: Worker {worker_id} stopped")


    async def run_multiplex_feeds(self):
        """Run multiplex socket with reader and worker pool."""
        if not self._active_feeds:
            logger.warning("AsterOrderBookClientManager: No feeds registered")
            return

        # Start reader coroutine
        self._reader_task = asyncio.create_task(self._reader_coroutine())

        # Start worker pool
        num_workers = min(4, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i))
            for i in range(num_workers)
        ]

        logger.info(f"AsterOrderBookClientManager: Started with {num_workers} workers")
        await self._reader_task


# Global client managers
aster_spot_kline_client_manager = AsterKlineClientManager()
aster_spot_ob_client_manager = AsterOrderBookClientManager()


# ──────────────────────────────────────────────────────────────────────────────
# AsterKlineFeed
# ──────────────────────────────────────────────────────────────────────────────

class AsterKlineFeed(AbstractDataFeed):
    """
    Real-time closed kline (candle) feed for a single Aster DEX symbol.
    Produces Kline objects and maintains a 60-candle rolling close-price window
    for realised volatility.

    Usage:
        feed = AsterKlineFeed()
        feed.create_new(asset="BTCUSDT", interval="1m")
        await feed()
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        self._kline: Optional[Kline] = None   # latest closed kline

    def create_new(self, asset: str, interval: str = "1m"):
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
        global aster_spot_kline_client_manager

        # Register this feed with the shared manager
        aster_spot_kline_client_manager.register_feed(self)

        # Start the shared manager if not already running
        if not aster_spot_kline_client_manager._running:
            await aster_spot_kline_client_manager.start()
            # Start the multiplex feed runner in background
            asyncio.create_task(aster_spot_kline_client_manager.run_multiplex_feeds())

        # Keep the feed alive
        while True:
            await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────────────────────
# AsterOrderBookFeed
# ──────────────────────────────────────────────────────────────────────────────

class AsterOrderBookFeed(AbstractDataFeed):
    """
    Real-time depth (order book) feed for a single Aster DEX symbol.
    Produces OrderBook snapshots updated at 100ms intervals.

    Usage:
        feed = AsterOrderBookFeed()
        feed.create_new(asset="BTCUSDT")
        await feed()
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        
        # Aster Specific Parameters
        self.last_update_id: Optional[int] = None
        self.snapshot_ready: bool = False
        self.event_buffer: List[dict] = []  # Buffer events while waiting for snapshot
        self.first_update_processed: bool = False  # Track if we've synced with snapshot

    def create_new(self, asset: str, interval: str = "100ms"):
        self._name = asset
        self._interval = interval

    def _fetch_rest_snapshot(self, limit: int = 100) -> dict:
        resp = requests.get(
            f"{SPOT_BASE_ENDPOINT}/api/v1/depth",
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
        
        self._data = AsterOrderBook.from_lists(
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
        logger.info(f"Fetched initial snapshot for [{self._name}] OB snapshot id={raw['lastUpdateId']}")
        
        # Process any buffered events now that snapshot is ready
        if self.event_buffer:
            logger.info(f"[{self._name}] Processing {len(self.event_buffer)} buffered depth events")
            await self._process_buffered_events()

    async def _process_buffered_events(self):
        """Process buffered events after snapshot is ready."""
        buffered_events = self.event_buffer[:]
        self.event_buffer.clear()
        
        logger.info(f"[{self._name}] Processing {len(buffered_events)} buffered events for snapshot sync")
        for buffered_event in buffered_events:
            # Skip events that are too old (u < lastUpdateId)
            if buffered_event['u'] < self.last_update_id:
                logger.info(f"[{self._name}] Skipping old buffered event u={buffered_event['u']}")
                continue
            
            # Find first event that covers the snapshot
            if buffered_event['U'] <= self.last_update_id and buffered_event['u'] >= self.last_update_id:
                logger.info(f"[{self._name}] Found sync point: buffered event u={buffered_event['u']} covers snapshot id={self.last_update_id}")
                await self._apply_depth_update(buffered_event)
                self.first_update_processed = True
                self.last_update_id = buffered_event['u']
                
            elif buffered_event['U'] > self.last_update_id:
                # This event is ahead of snapshot with gap - re-fetch snapshot
                logger.warning(f"[{self._name}] Gap detected: buffered event U={buffered_event['U']} > snapshot id={self.last_update_id}")
                self.snapshot_ready = False
                asyncio.create_task(self.get_initial_snapshot())
                return
    
    async def _process_depth_event(self, event: dict) -> OrderBook:
        """Process depth updates following Binance/Aster API spec."""
        
        # Phase 1: Buffer events while snapshot is being fetched
        if not self.snapshot_ready:
            self.event_buffer.append(event)
            logger.warning(f"[{self._name}] Buffering depth event (snapshot not ready). Buffer size: {len(self.event_buffer)}")
            return self._data if self._data else None
        
        # Phase 2: Normal sequential event processing
        # Validate pu (previous update id)
        if event.get('pu') != self.last_update_id:
            logger.error(f"[{self._name}] Sequence break: event pu={event.get('pu')} != last_update_id={self.last_update_id}")
            self.snapshot_ready = False
            await asyncio.sleep(3.0)
            asyncio.create_task(self.get_initial_snapshot())
            raise ValueError("Invalid event sequence")
        else:
            logger.info(f"[{self._name}] Processing depth event: U={event['U']} u={event['u']} (last_update_id={self.last_update_id})")
            self.last_update_id = event['u']
            await self._apply_depth_update(event)
        return self._data
    
    
    async def _apply_depth_update(self, event: dict):
        """Apply depth update to order book."""
        ts = int(time.time() * 1000)
        await self._data.apply_both_updates(ts, event['u'], 
                                            bid_levels=event.get("b", []), ask_levels=event.get("a", []))
        logger.info(f"[{self._name}] OB update id={self._data.last_update_id} best_bid={self._data.best_bid_price:.5f} best_ask={self._data.best_ask_price:.5f}")


    async def __call__(self):
        """Register with shared client manager and wait for multiplex events."""
        global aster_spot_ob_client_manager

        # Register this feed with the shared manager (WebSocket will start buffering)
        aster_spot_ob_client_manager.register_feed(self)

        # Start the shared manager if not already running
        if not aster_spot_ob_client_manager._running:
            await aster_spot_ob_client_manager.start()
            
            # Start the multiplex feed runner in background (WebSocket reader/workers)
            asyncio.create_task(aster_spot_ob_client_manager.run_multiplex_feeds())

        # Give WebSocket a moment to start buffering events
        await asyncio.sleep(0.1)
        
        # NOW fetch the snapshot while events are being buffered
        await self.get_initial_snapshot()

        # Keep the feed alive
        while True:
            await asyncio.sleep(1)
