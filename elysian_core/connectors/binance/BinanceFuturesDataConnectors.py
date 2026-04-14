import asyncio
import collections
import datetime
import json
import math
import statistics
import time
from datetime import datetime as dt
from typing import Any, Dict, List, Optional, Callable
from urllib.parse import urlencode

import requests
import websockets
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException

from elysian_core.connectors.base import AbstractDataFeed
from elysian_core.core.market_data import Kline, OrderBook, BinanceOrderBook
from elysian_core.core.events import KlineEvent, OrderBookUpdateEvent
from elysian_core.core.enums import Venue

from elysian_core.connectors.base import AbstractClientManager, KlineClientManager, OrderBookClientManager
import elysian_core.utils.logger as log
from elysian_core.utils.async_helpers import cancel_tasks

# ──────────────────────────────────────────────────────────────────────────────
# Shared Client Managers
# ──────────────────────────────────────────────────────────────────────────────

class BinanceFuturesKlineClientManager(KlineClientManager):
    """Shared AsyncClient for multiplex futures kline streams with queue-based processing."""

    def __init__(self):
        self.logger = log.setup_custom_logger("root")
        self._client: Optional[AsyncClient] = None
        self._manager: Optional[BinanceSocketManager] = None
        self._socket: Optional[Any] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, 'BinanceFuturesKlineFeed'] = {}
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._event_bus = None


    async def _create_client(self, retries: int = 5) -> AsyncClient:
        for i in range(retries):
            try:
                # Pre-resolve DNS to avoid async DNS issues
                loop = asyncio.get_running_loop()
                await loop.getaddrinfo("fapi.binance.com", 443)
                self.logger.debug("BinanceFuturesKlineClientManager: DNS pre-resolved successfully")

                return await AsyncClient.create()
            except (TimeoutError, BinanceAPIException) as e:
                self.logger.warning(f"BinanceFuturesKlineClientManager: Client creation attempt {i+1}/{retries} failed: {e}")
                if i == retries - 1:
                    raise
                await asyncio.sleep(2 ** i)


    async def start(self):
        if self._running:
            return

        self._client = await self._create_client()
        self._manager = BinanceSocketManager(self._client, max_queue_size=1000000)
        self._running = True
        self.logger.info("BinanceFuturesKlineClientManager: Started shared futures kline client")


    async def stop(self):
        if not self._running:
            return

        self._running = False
        await cancel_tasks(self._reader_task, self._worker_tasks)

        if self._socket:
            await self._socket.__aexit__(None, None, None)
            self._socket = None

        if self._client:
            await self._client.close_connection()
            self._client = None

        self.logger.info("BinanceFuturesKlineClientManager: Stopped shared futures kline client")

    def register_feed(self, feed: 'BinanceFuturesKlineFeed'):
        self._active_feeds[feed._name] = feed

    def get_feed(self, symbol: str):
        return self._active_feeds.get(symbol)

    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)

    async def _reader_coroutine(self):
        """Network reader: only reads messages from multiplex socket."""
        if not self._active_feeds:
            self.logger.warning("BinanceFuturesKlineClientManager: No feeds registered for reader")
            return

        # Create multiplex socket for all symbols (futures streams)
        streams = [f"{symbol.lower()}@kline_1m" for symbol in self._active_feeds.keys()]
        self._socket = self._manager.multiplex_socket(streams)

        reconnect_delay = 1
        while self._running:
            try:
                async with self._socket:
                    self.logger.info(f"BinanceFuturesKlineClientManager: Multiplex socket opened for {len(streams)} streams")
                    reconnect_delay = 1  # Reset delay on successful connection

                    while self._running:
                        try:
                            msg = await self._socket.recv()
                            await self._queue.put(msg)
                        except Exception as e:
                            self.logger.error(f"BinanceFuturesKlineClientManager: Error in reader: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                self.logger.error(f"BinanceFuturesKlineClientManager: Socket connection error: {e}", exc_info=False)

            if self._running:
                self.logger.warning(f"BinanceFuturesKlineClientManager: Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _worker_coroutine(self, worker_id: int):
        """Worker coroutine: processes messages from queue."""
        self.logger.debug(f"BinanceFuturesKlineClientManager: Worker {worker_id} started")

        while self._running:
            try:
                msg = await self._queue.get()

                stream = msg.get("stream", "")
                data = msg.get("data", {})

                # Extract symbol from stream name (e.g., "ethusdt@kline_1m" -> "ETHUSDT")
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
                                self.logger.info(f"[{symbol}] Futures Volatility: {feed._vol * 1e4:.2f} bps (60-candle window)")

                            if self._event_bus is not None:
                                await self._event_bus.publish(KlineEvent(
                                    symbol=symbol,
                                    venue=Venue.BINANCE,
                                    kline=kline,
                                    timestamp=int(time.time() * 1000),
                                ))

                self._queue.task_done()

            except Exception as e:
                self.logger.error(f"BinanceFuturesKlineClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

        self.logger.debug(f"BinanceFuturesKlineClientManager: Worker {worker_id} stopped")

    async def run_multiplex_feeds(self):
        """Run multiplex socket with reader and worker pool."""
        if not self._active_feeds:
            self.logger.warning("BinanceFuturesKlineClientManager: No feeds registered")
            return

        # Start reader coroutine
        self._reader_task = asyncio.create_task(self._reader_coroutine())

        # Start worker pool (4 workers for parallel processing)
        num_workers = min(8, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i))
            for i in range(num_workers)
        ]

        self.logger.info(f"BinanceFuturesKlineClientManager: Started with {num_workers} workers")

        # Wait for reader to complete (it runs until stopped)
        await self._reader_task


class BinanceFuturesOrderBookClientManager(OrderBookClientManager):
    """Shared AsyncClient for multiplex futures orderbook streams with queue-based processing."""

    def __init__(self):
        self.logger = log.setup_custom_logger("root")
        self._client: Optional[AsyncClient] = None
        self._manager: Optional[BinanceSocketManager] = None
        self._socket: Optional[Any] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, 'BinanceFuturesOrderBookFeed'] = {}
        self._running = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._event_bus = None

    async def _create_client(self, retries: int = 5) -> AsyncClient:
        for i in range(retries):
            try:
                # Pre-resolve DNS to avoid async DNS issues
                loop = asyncio.get_running_loop()
                await loop.getaddrinfo("fapi.binance.com", 443)
                self.logger.debug("BinanceFuturesOrderBookClientManager: DNS pre-resolved successfully")

                return await AsyncClient.create()
            except (TimeoutError, BinanceAPIException) as e:
                self.logger.warning(f"BinanceFuturesOrderBookClientManager: Client creation attempt {i+1}/{retries} failed: {e}")
                if i == retries - 1:
                    raise
                await asyncio.sleep(2 ** i)

    async def start(self):
        if self._running:
            return

        self._client = await self._create_client()
        self._manager = BinanceSocketManager(self._client, max_queue_size=1000000)
        self._running = True
        self.logger.info("BinanceFuturesOrderBookClientManager: Started shared futures orderbook client")

    async def stop(self):
        if not self._running:
            return

        self._running = False
        await cancel_tasks(self._reader_task, self._worker_tasks)

        if self._socket:
            await self._socket.__aexit__(None, None, None)
            self._socket = None

        if self._client:
            await self._client.close_connection()
            self._client = None

        self.logger.info("BinanceFuturesOrderBookClientManager: Stopped shared futures orderbook client")

    def register_feed(self, feed: 'BinanceFuturesOrderBookFeed'):
        self._active_feeds[feed._name] = feed

    def get_feed(self, symbol: str):
        return self._active_feeds.get(symbol)

    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)

    async def _reader_coroutine(self):
        """Network reader: only reads messages from multiplex socket."""
        if not self._active_feeds:
            self.logger.warning("BinanceFuturesOrderBookClientManager: No feeds registered for reader")
            return

        # Create multiplex socket for all symbols (futures depth streams)
        streams = [f"{symbol.lower()}@depth@100ms" for symbol in self._active_feeds.keys()]
        self._socket = self._manager.futures_multiplex_socket(streams)

        reconnect_delay = 1
        while self._running:
            try:
                async with self._socket:
                    self.logger.info(f"BinanceFuturesOrderBookClientManager: Multiplex socket opened for {len(streams)} streams")
                    reconnect_delay = 1  # Reset delay on successful connection

                    while self._running:
                        try:
                            msg = await self._socket.recv()
                            await self._queue.put(msg)
                        except Exception as e:
                            self.logger.error(f"BinanceFuturesOrderBookClientManager: Error in reader: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                self.logger.error(f"BinanceFuturesOrderBookClientManager: Socket connection error: {e}", exc_info=False)

            if self._running:
                self.logger.warning(f"BinanceFuturesOrderBookClientManager: Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _worker_coroutine(self, worker_id: int):
        """Worker coroutine: processes messages from queue."""
        self.logger.debug(f"BinanceFuturesOrderBookClientManager: Worker {worker_id} started")

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

                        if self._event_bus is not None and feed._data is not None:
                            await self._event_bus.publish(OrderBookUpdateEvent(
                                symbol=symbol,
                                venue=Venue.BINANCE,
                                orderbook=feed._data,
                                timestamp=int(time.time() * 1000),
                            ))

                self._queue.task_done()

            except Exception as e:
                self.logger.error(f"BinanceFuturesOrderBookClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

        self.logger.debug(f"BinanceFuturesOrderBookClientManager: Worker {worker_id} stopped")

    async def run_multiplex_feeds(self):
        """Run multiplex socket with reader and worker pool."""
        if not self._active_feeds:
            self.logger.warning("BinanceFuturesOrderBookClientManager: No feeds registered")
            return

        # Start reader coroutine
        self._reader_task = asyncio.create_task(self._reader_coroutine())

        # Start worker pool (4 workers for parallel processing)
        num_workers = min(8, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i))
            for i in range(num_workers)
        ]

        self.logger.info(f"BinanceFuturesOrderBookClientManager: Started with {num_workers} workers")

        # Wait for reader to complete (it runs until stopped)
        await self._reader_task


# ──────────────────────────────────────────────────────────────────────────────
# Futures Feeds
# ──────────────────────────────────────────────────────────────────────────────

class BinanceFuturesKlineFeed(AbstractDataFeed):
    """
    Real-time closed kline (candle) feed for a single Binance futures symbol.
    Produces Kline objects and maintains a 60-candle rolling close-price window
    for realised volatility.

    Usage:
        feed = BinanceFuturesKlineFeed()
        feed.create_new(asset="ETHUSDT", interval="1m")
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
        return kline

    async def __call__(self):
        """Keep the feed alive. Registration with manager is done externally via run_strategy."""
        while True:
            await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────────────────────
# BinanceFuturesOrderBookFeed
# ──────────────────────────────────────────────────────────────────────────────

class BinanceFuturesOrderBookFeed(AbstractDataFeed):
    """
    Real-time depth (order book) feed for a single Binance futures symbol.
    Produces OrderBook snapshots updated at 100ms intervals.

    Usage:
        feed = BinanceFuturesOrderBookFeed()
        feed.create_new(asset="ETHUSDT")
        await feed()
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)

        # Binance Futures Specific Parameters
        self.snapshot_ready: bool = False
        self.event_buffer: List[dict] = []  # Buffer events while waiting for snapshot
        self.first_update_processed: bool = False  # Track if we've synced with snapshot

    def create_new(self, asset: str, interval: str = "100ms"):
        self._name = asset
        self._interval = interval

    async def _fetch_rest_snapshot(self, limit: int = 100) -> dict:
        def _sync_fetch():
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/depth",
                params={"symbol": self._name, "limit": min(limit, 1000)},
            )
            resp.raise_for_status()
            return resp.json()
        return await asyncio.to_thread(_sync_fetch)

    async def get_initial_snapshot(self):
        """Fetch snapshot and process any buffered events that cover it."""
        raw = await self._fetch_rest_snapshot(100)
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
        self.snapshot_ready = True
        self.first_update_processed = False  # Reset for new snapshot sync
        self.logger.info(f"[{self._name}] Futures OB snapshot id={raw['lastUpdateId']}")

        # Process any buffered events now that snapshot is ready
        if self.event_buffer:
            self.logger.info(f"[{self._name}] Processing {len(self.event_buffer)} buffered depth events")
            await self._process_buffered_events()
            self.logger.info(f"[{self._name}] Finished processing buffered events. Buffer cleared.")



    async def _process_buffered_events(self):
        """Process buffered events after snapshot is ready."""
        buffered_events = self.event_buffer[:]
        self.event_buffer.clear()

        for buffered_event in buffered_events:

            # Skip events that are too old (u < lastUpdateId)
            if buffered_event['u'] < self._data.last_update_id:
                self.logger.debug(f"[{self._name}] Skipping old buffered event u={buffered_event['u']}")
                continue

            # Find first event that covers the snapshot
            if buffered_event['U'] <= self._data.last_update_id and buffered_event['u'] == self._data.last_update_id:
                self.logger.info(f"[{self._name}] Found sync point: buffered event u={buffered_event['u']} covers snapshot id={self._data.last_update_id}")
                await self._apply_depth_update(buffered_event)
                self.first_update_processed = True

            elif self.first_update_processed and buffered_event['U'] <= self._data.last_update_id + 1:
                # This event is in sequence after the snapshot - apply it
                await self._apply_depth_update(buffered_event)

            elif buffered_event['U'] > self._data.last_update_id:
                # This event is ahead of snapshot with gap - re-fetch snapshot
                self.logger.warning(f"[{self._name}] Gap detected: buffered event U={buffered_event['U']} > snapshot id={self._data.last_update_id}")
                self.snapshot_ready = False
                asyncio.create_task(self.get_initial_snapshot())
                return



    async def _process_depth_event(self, event: dict) -> OrderBook:
        """Process depth updates following Binance Futures API spec."""

        # Buffer events while snapshot is being fetched
        if not self.snapshot_ready:
            self.event_buffer.append(event)
            self.logger.warning(f"[{self._name}] Buffering depth event (snapshot not ready). Buffer size: {len(self.event_buffer)}")
            return self._data if self._data else None

        # Validate U and u according to Binance spec
        elif event['pu'] < self._data.last_update_id:
            return self._data


        elif event['pu'] != self._data.last_update_id:
            self.logger.error(f"[{self._name}] Gap detected: event pu={event['pu']} != last_update_id={self._data.last_update_id}. Re-fetching snapshot...")
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
                                            bid_levels=event.get("b", []), ask_levels=event.get("a", []))
        self.logger.success(f"[{self._name}] Futures OB update id={self._data.last_update_id} best_bid={self._data.best_bid_price:.5f} best_ask={self._data.best_ask_price:.5f}")


    async def __call__(self):
        """Keep the feed alive. Registration with manager is done externally via run_strategy."""
        # # Give WebSocket a moment to start buffering events
        # await asyncio.sleep(0.1)

        # # Fetch the snapshot while events are being buffered
        # await self.get_initial_snapshot()

        # # Keep the feed alive
        # while True:
        #     await asyncio.sleep(1)
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Futures User Data Stream Manager
# ──────────────────────────────────────────────────────────────────────────────

class BinanceFuturesUserDataClientManager:
    """Maintain a single futures user-data websocket and dispatch events.

    Uses the listenKey approach for Binance USDS-M Futures:
      1. POST /fapi/v1/listenKey to obtain a listenKey
      2. Connect to wss://fstream.binance.com/ws/<listenKey>
      3. PUT /fapi/v1/listenKey every 30 minutes to keep alive
      4. Dispatch ACCOUNT_UPDATE and ORDER_TRADE_UPDATE events to subscribers

    Futures user data stream event types:
      - ACCOUNT_UPDATE:      balance and position changes
      - ORDER_TRADE_UPDATE:  order status changes and fills
      - MARGIN_CALL:         margin call warnings
      - ACCOUNT_CONFIG_UPDATE: leverage or multi-asset mode changes
    """

    FAPI_BASE = "https://fapi.binance.com"
    WS_BASE = "wss://fstream.binance.com/ws"
    KEEPALIVE_INTERVAL = 30 * 60  # 30 minutes

    def __init__(self, api_key: str, api_secret: str):
        self.logger = log.setup_custom_logger("root")
        self._api_key = api_key
        self._api_secret = api_secret
        self._running: bool = False
        self._ws: Optional[Any] = None
        self._listen_key: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._subscribers: List[Callable[[dict], None]] = []

    # ── listenKey management ─────────────────────────────────────────────────

    async def _get_listen_key(self) -> str:
        """POST /fapi/v1/listenKey to create or renew a listenKey."""
        def _sync():
            resp = requests.post(
                f"{self.FAPI_BASE}/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": self._api_key},
            )
            resp.raise_for_status()
            return resp.json()["listenKey"]
        return await asyncio.to_thread(_sync)

    async def _keepalive_listen_key(self):
        """PUT /fapi/v1/listenKey to extend the listenKey validity."""
        def _sync():
            resp = requests.put(
                f"{self.FAPI_BASE}/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": self._api_key},
            )
            resp.raise_for_status()
        await asyncio.to_thread(_sync)

    async def _close_listen_key(self):
        """DELETE /fapi/v1/listenKey to close the stream."""
        def _sync():
            try:
                requests.delete(
                    f"{self.FAPI_BASE}/fapi/v1/listenKey",
                    headers={"X-MBX-APIKEY": self._api_key},
                )
            except Exception:
                pass
        await asyncio.to_thread(_sync)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        self.logger.info("BinanceFuturesUserDataClientManager: started")

    async def stop(self):
        if not self._running:
            return
        self._running = False

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

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

        # Close the listenKey on the server
        await self._close_listen_key()
        self.logger.info("BinanceFuturesUserDataClientManager: stopped")

    # ── Keepalive loop ────────────────────────────────────────────────────────

    async def _keepalive_loop(self):
        """Send PUT keepalive every 30 minutes to keep the listenKey active."""
        while self._running:
            try:
                await asyncio.sleep(self.KEEPALIVE_INTERVAL)
                await self._keepalive_listen_key()
                self.logger.debug("BinanceFuturesUserDataClientManager: listenKey keepalive sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"BinanceFuturesUserDataClientManager: keepalive error: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _reader_loop(self):
        """Connect, subscribe, and dispatch events. Reconnects with exponential backoff."""
        reconnect_delay = 1
        while self._running:
            try:
                # Obtain a fresh listenKey
                self._listen_key = await self._get_listen_key()
                ws_url = f"{self.WS_BASE}/{self._listen_key}"

                async with websockets.connect(
                    ws_url,
                    ping_interval=180,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws

                    # Start keepalive task
                    self._keepalive_task = asyncio.create_task(self._keepalive_loop())

                    reconnect_delay = 1
                    self.logger.info("BinanceFuturesUserDataClientManager: connected and listening")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            self.logger.warning(f"BinanceFuturesUserDataClientManager: failed to decode: {raw}")
                            continue

                        # Dispatch to subscribers
                        for cb in self._subscribers:
                            try:
                                cb(msg)
                            except Exception as e:
                                self.logger.error(f"BinanceFuturesUserDataClientManager: callback error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"BinanceFuturesUserDataClientManager: connection error: {e}")
                # Cancel keepalive if it was started
                if self._keepalive_task and not self._keepalive_task.done():
                    self._keepalive_task.cancel()
                if self._running:
                    self.logger.info(f"BinanceFuturesUserDataClientManager: reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)

        self._ws = None
        self.logger.info("BinanceFuturesUserDataClientManager: reader loop exited")

    # ── Subscriber registry ───────────────────────────────────────────────────

    def register(self, callback: Callable[[dict], None]):
        """Register a callback to receive raw event dicts from the futures user data stream."""
        self._subscribers.append(callback)
        self.logger.debug(f"BinanceFuturesUserDataClientManager: registered callback {callback.__name__}")

    def unregister(self, callback: Callable[[dict], None]):
        """Unregister a previously registered callback."""
        try:
            self._subscribers.remove(callback)
            self.logger.debug(f"BinanceFuturesUserDataClientManager: unregistered callback {callback.__name__}")
        except ValueError:
            pass
