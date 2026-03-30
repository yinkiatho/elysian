import asyncio
import datetime
import json
import math
import statistics
import time
from datetime import datetime as dt
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode
import hashlib
import hmac

import requests
import websockets

from elysian_core.connectors.base import AbstractDataFeed
from elysian_core.core.market_data import Kline, OrderBook, AsterOrderBook
import elysian_core.utils.logger as log
from elysian_core.connectors.base import AbstractClientManager, KlineClientManager, OrderBookClientManager


logger = log.setup_custom_logger("root")

SPOT_BASE_ENDPOINT     = "https://sapi.asterdex.com"
WEBSOCKET_SPOT_STREAM  = "wss://sstream.asterdex.com/ws"          # /ws/<listenKey> or subscription endpoint
WEBSOCKET_STREAM_BASE  = "wss://sstream.asterdex.com/stream"      # combined stream endpoint


# ──────────────────────────────────────────────────────────────────────────────
# AsterKlineClientManager
# ──────────────────────────────────────────────────────────────────────────────

class AsterKlineClientManager(KlineClientManager):
    """Shared WebSocket client for multiplex kline streams with queue-based processing."""

    def __init__(self):
        self._websocket: Optional[Any]             = None
        self._queue: asyncio.Queue                 = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, "AsterKlineFeed"] = {}
        self._running                              = False
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task]    = []
        self._subscription_id                     = 1

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("AsterKlineClientManager: Started shared kline client")

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

        for task in self._worker_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._websocket:
            try:
                await self._websocket.close()
            except Exception:
                pass
            self._websocket = None

        logger.info("AsterKlineClientManager: Stopped shared kline client")

    # ── Feed registry ─────────────────────────────────────────────────────────

    def register_feed(self, feed: "AsterKlineFeed"):
        self._active_feeds[feed._name] = feed

    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)

    def get_feed(self, symbol: str) -> Optional["AsterKlineFeed"]:
        return self._active_feeds.get(symbol)

    # ── Reader ────────────────────────────────────────────────────────────────

    async def _reader_coroutine(self):
        """Network reader: connects to the combined stream and reads messages."""
        if not self._active_feeds:
            logger.warning("AsterKlineClientManager: No feeds registered for reader")
            return

        streams = [f"{symbol.lower()}@kline_1s" for symbol in self._active_feeds.keys()]

        reconnect_delay = 1
        while self._running:
            try:
                async with websockets.connect(WEBSOCKET_SPOT_STREAM) as websocket:
                    self._websocket = websocket
                    logger.info(
                        f"AsterKlineClientManager: WebSocket connected for {len(streams)} streams"
                    )
                    reconnect_delay = 1

                    subscription_msg = {
                        "method": "SUBSCRIBE",
                        "params": streams,
                        "id": self._subscription_id,
                    }
                    await websocket.send(json.dumps(subscription_msg))
                    self._subscription_id += 1

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(websocket.recv(), timeout=30)
                            await self._queue.put(json.loads(msg))
                        except asyncio.TimeoutError:
                            try:
                                await websocket.pong()
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"AsterKlineClientManager: Reader error: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(
                    f"AsterKlineClientManager: WebSocket connection error: {e}", exc_info=False
                )

            if self._running:
                logger.warning(
                    f"AsterKlineClientManager: Reconnecting in {reconnect_delay}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker_coroutine(self, worker_id: int):
        while self._running:
            try:
                msg = await self._queue.get()

                # Aster uses {"stream": "...", "data": {...}} envelope or direct event
                if "data" in msg and "stream" in msg:
                    data   = msg["data"]
                    stream = msg["stream"]
                elif msg.get("e") == "kline":
                    data   = msg
                    stream = f"{msg.get('s', '').lower()}@kline_1s"
                else:
                    self._queue.task_done()
                    continue

                if "@kline" in stream:
                    symbol = stream.split("@")[0].upper()
                    feed   = self._active_feeds.get(symbol)
                    if feed:
                        kline = feed._process_kline_event(data)
                        if kline is not None:
                            feed._kline = kline
                            feed._historical.append(kline.close)

                            if len(feed._historical) == 60:
                                returns = [
                                    (feed._historical[i] - feed._historical[i - 1])
                                    / feed._historical[i - 1]
                                    for i in range(1, len(feed._historical))
                                ]
                                feed._vol = statistics.stdev(returns) * math.sqrt(20)

                self._queue.task_done()

            except Exception as e:
                logger.error(f"AsterKlineClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run_multiplex_feeds(self):
        '''
        Call to activate the multiplex feeds
        '''
        if not self._active_feeds:
            logger.warning("AsterKlineClientManager: No feeds registered")
            return

        self._reader_task = asyncio.create_task(self._reader_coroutine())

        num_workers = min(8, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i)) for i in range(num_workers)
        ]

        logger.info(f"AsterKlineClientManager: Started with {num_workers} workers")
        await self._reader_task


# ──────────────────────────────────────────────────────────────────────────────
# AsterOrderBookClientManager
# ──────────────────────────────────────────────────────────────────────────────

class AsterOrderBookClientManager(OrderBookClientManager):
    """Shared WebSocket client for multiplex order-book streams with queue-based processing."""

    def __init__(self):
        self._websocket: Optional[Any]                  = None
        self._queue: asyncio.Queue                      = asyncio.Queue(maxsize=10000)
        self._active_feeds: Dict[str, "AsterOrderBookFeed"] = {}
        self._running                                   = False
        self._reader_task: Optional[asyncio.Task]       = None
        self._worker_tasks: List[asyncio.Task]          = []
        self._subscription_id                           = 1

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        logger.info("AsterOrderBookClientManager: Started shared orderbook client")

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

        for task in self._worker_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._websocket:
            try:
                await self._websocket.close()
            except Exception:
                pass
            self._websocket = None

        logger.info("AsterOrderBookClientManager: Stopped shared orderbook client")

    # ── Feed registry ─────────────────────────────────────────────────────────

    def register_feed(self, feed: "AsterOrderBookFeed"):
        self._active_feeds[feed._name] = feed

    def unregister_feed(self, symbol: str):
        self._active_feeds.pop(symbol, None)

    def get_feed(self, symbol: str) -> Optional["AsterOrderBookFeed"]:
        return self._active_feeds.get(symbol)

    # ── Reader ────────────────────────────────────────────────────────────────

    async def _reader_coroutine(self):
        if not self._active_feeds:
            logger.warning("AsterOrderBookClientManager: No feeds registered for reader")
            return

        # 20-level depth with 100ms updates
        streams = [
            f"{symbol.lower()}@depth@100ms" for symbol in self._active_feeds.keys()
        ]

        reconnect_delay = 1
        while self._running:
            try:
                async with websockets.connect(WEBSOCKET_SPOT_STREAM) as websocket:
                    self._websocket = websocket
                    logger.info(
                        f"AsterOrderBookClientManager: WebSocket connected for {len(streams)} streams"
                    )
                    reconnect_delay = 1

                    subscription_msg = {
                        "method": "SUBSCRIBE",
                        "params": streams,
                        "id": self._subscription_id,
                    }
                    await websocket.send(json.dumps(subscription_msg))
                    self._subscription_id += 1

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(websocket.recv(), timeout=30)
                            await self._queue.put(json.loads(msg))
                        except asyncio.TimeoutError:
                            try:
                                await websocket.pong()
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"AsterOrderBookClientManager: Reader error: {e}")
                            await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(
                    f"AsterOrderBookClientManager: WebSocket connection error: {e}", exc_info=False
                )

            if self._running:
                logger.warning(
                    f"AsterOrderBookClientManager: Reconnecting in {reconnect_delay}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker_coroutine(self, worker_id: int):
        while self._running:
            try:
                msg = await self._queue.get()

                # Unwrap combined-stream envelope if present
                if "data" in msg and "stream" in msg:
                    data   = msg["data"]
                    stream = msg["stream"]
                elif msg.get("e") == "depthUpdate":
                    data   = msg
                    stream = f"{msg.get('s', '').lower()}@depth"
                else:
                    self._queue.task_done()
                    continue

                if "@depth" in stream:
                    symbol = stream.split("@")[0].upper()
                    feed   = self._active_feeds.get(symbol)
                    if feed:
                        feed._data = await feed._process_depth_event(data)
                        if feed.save_data:
                            asyncio.create_task(feed._append_to_df(feed._data))

                self._queue.task_done()

            except Exception as e:
                logger.error(f"AsterOrderBookClientManager: Worker {worker_id} error: {e}")
                await asyncio.sleep(0.1)

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run_multiplex_feeds(self):
        if not self._active_feeds:
            logger.warning("AsterOrderBookClientManager: No feeds registered")
            return

        self._reader_task = asyncio.create_task(self._reader_coroutine())

        num_workers        = min(8, len(self._active_feeds))
        self._worker_tasks = [
            asyncio.create_task(self._worker_coroutine(i)) for i in range(num_workers)
        ]

        logger.info(f"AsterOrderBookClientManager: Started with {num_workers} workers")
        await self._reader_task


# ──────────────────────────────────────────────────────────────────────────────
# AsterUserDataClientManager
# Manages the listen-key lifecycle and dispatches account/order events.
# Docs: POST /api/v1/listenKey  PUT /api/v1/listenKey  DELETE /api/v1/listenKey
# Stream: wss://sstream.asterdex.com/ws/<listenKey>
# ──────────────────────────────────────────────────────────────────────────────

class AsterUserDataClientManager:
    """
    Manages the Aster user-data WebSocket stream.

    Lifecycle:
      1. POST /api/v1/listenKey  → obtain listen key
      2. Connect to wss://sstream.asterdex.com/ws/<listenKey>
      3. PUT /api/v1/listenKey every 30 min to keep it alive
      4. Dispatch inbound events to registered callbacks
      5. DELETE /api/v1/listenKey on stop
    """

    _KEEPALIVE_INTERVAL = 30 * 60   # 30 minutes in seconds

    def __init__(self, api_key: str, api_secret: str):
        self._api_key    = api_key
        self._api_secret = api_secret
        self._listen_key: Optional[str]            = None
        self._running: bool                        = False
        self._ws: Optional[Any]                    = None
        self._reader_task: Optional[asyncio.Task]  = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._subscribers: List[Callable[[dict], None]] = []

    # ── Subscription ──────────────────────────────────────────────────────────

    def register(self, callback: Callable[[dict], None]):
        """Register a callback that will receive every user-data event."""
        self._subscribers.append(callback)

    # ── Listen-key REST helpers ────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self._api_key}

    def _get_listen_key(self) -> str:
        resp = requests.post(
            f"{SPOT_BASE_ENDPOINT}/api/v1/listenKey",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["listenKey"]

    def _keepalive_listen_key(self) -> None:
        resp = requests.put(
            f"{SPOT_BASE_ENDPOINT}/api/v1/listenKey",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()

    def _delete_listen_key(self) -> None:
        try:
            requests.delete(
                f"{SPOT_BASE_ENDPOINT}/api/v1/listenKey",
                headers=self._headers(),
                timeout=10,
            )
        except Exception:
            pass

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return

        self._listen_key = await asyncio.to_thread(self._get_listen_key)
        logger.info("AsterUserDataClientManager: Obtained listen key")

        self._running        = True
        self._reader_task    = asyncio.create_task(self._reader_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info("AsterUserDataClientManager: Started")

    async def stop(self):
        if not self._running:
            return
        self._running = False

        for task in (self._reader_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._listen_key:
            await asyncio.to_thread(self._delete_listen_key)
            self._listen_key = None

        logger.info("AsterUserDataClientManager: Stopped")

    # ── Reader loop ───────────────────────────────────────────────────────────

    async def _reader_loop(self):
        reconnect_delay = 1
        while self._running:
            if not self._listen_key:
                await asyncio.sleep(1)
                continue

            ws_url = f"{WEBSOCKET_SPOT_STREAM}/{self._listen_key}"
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    logger.info("AsterUserDataClientManager: WebSocket connected")
                    reconnect_delay = 1

                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                            msg = json.loads(raw)
                            for cb in self._subscribers:
                                try:
                                    cb(msg)
                                except Exception as e:
                                    logger.error(
                                        f"AsterUserDataClientManager: Subscriber error: {e}"
                                    )
                        except asyncio.TimeoutError:
                            try:
                                await ws.pong()
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"AsterUserDataClientManager: Reader error: {e}")
                            break

            except Exception as e:
                logger.error(
                    f"AsterUserDataClientManager: Connection error: {e}", exc_info=False
                )

            if self._running:
                logger.warning(
                    f"AsterUserDataClientManager: Reconnecting in {reconnect_delay}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

                # Refresh listen key on reconnect
                try:
                    self._listen_key = await asyncio.to_thread(self._get_listen_key)
                except Exception as e:
                    logger.error(
                        f"AsterUserDataClientManager: Failed to refresh listen key: {e}"
                    )

    # ── Keepalive loop ────────────────────────────────────────────────────────

    async def _keepalive_loop(self):
        while self._running:
            await asyncio.sleep(self._KEEPALIVE_INTERVAL)
            if not self._running:
                break
            try:
                await asyncio.to_thread(self._keepalive_listen_key)
                logger.debug("AsterUserDataClientManager: Listen key extended")
            except Exception as e:
                logger.error(f"AsterUserDataClientManager: Keepalive failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# AsterKlineFeed
# ──────────────────────────────────────────────────────────────────────────────

class AsterKlineFeed(AbstractDataFeed):
    """
    Real-time closed kline (candle) feed for a single Aster spot symbol.
    Produces Kline objects and maintains a 60-candle rolling window for
    realised volatility (annualised via sqrt(20) for ~1-second candles).
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        self._kline: Optional[Kline] = None

    def create_new(self, asset: str, interval: str = "1s"):
        self._name     = asset
        self._interval = interval

    @property
    def latest_kline(self) -> Optional[Kline]:
        return self._kline

    @property
    def latest_close(self) -> Optional[float]:
        return self._historical[-1] if self._historical else None

    async def update_current_stats(self):
        pass  # klines update _historical inline on each closed candle

    def _process_kline_event(self, raw: dict) -> Optional[Kline]:
        k = raw.get("k", {})
        if not k.get("x"):   # only closed candles
            return None
        return Kline(
            ticker     = k["s"],
            interval   = k["i"],
            start_time = dt.fromtimestamp(k["t"] / 1000),
            end_time   = dt.fromtimestamp(k["T"] / 1000),
            open       = float(k["o"]),
            high       = float(k["h"]),
            low        = float(k["l"]),
            close      = float(k["c"]),
            volume     = float(k["v"]),
        )

    async def __call__(self):
        """Register with shared manager and keep alive."""
        from elysian_core.connectors.AsterDataConnectors import aster_spot_kline_client_manager
        aster_spot_kline_client_manager.register_feed(self)
        if not aster_spot_kline_client_manager._running:
            await aster_spot_kline_client_manager.start()
            asyncio.create_task(aster_spot_kline_client_manager.run_multiplex_feeds())
        while True:
            await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────────────────────
# AsterOrderBookFeed
# ──────────────────────────────────────────────────────────────────────────────

class AsterOrderBookFeed(AbstractDataFeed):
    """
    Real-time incremental depth feed for a single Aster spot symbol.
    Maintains a full order book via snapshot + delta updates, following the
    Aster/Binance order-book synchronisation protocol.
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        self.last_update_id: Optional[int]   = None
        self.snapshot_ready: bool            = False
        self.event_buffer: List[dict]        = []
        self.first_update_processed: bool    = False

    def create_new(self, asset: str, interval: str = "100ms"):
        self._name     = asset
        self._interval = interval

    # ── REST snapshot ─────────────────────────────────────────────────────────

    def _fetch_rest_snapshot(self, limit: int = 100) -> dict:
        resp = requests.get(
            f"{SPOT_BASE_ENDPOINT}/api/v1/depth",
            params={"symbol": self._name, "limit": min(limit, 5000)},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_initial_snapshot(self):
        """Fetch REST snapshot and replay any buffered delta events."""
        raw = await asyncio.to_thread(self._fetch_rest_snapshot, 100)
        ts  = int(time.time() * 1000)

        bids = [[float(b[0]), float(b[1])] for b in raw["bids"]]
        asks = [[float(a[0]), float(a[1])] for a in raw["asks"]]

        self._data = AsterOrderBook.from_lists(
            last_update_id = raw["lastUpdateId"],
            last_timestamp = ts,
            ticker         = self._name,
            interval       = self._interval,
            bid_levels     = bids,
            ask_levels     = asks,
        )

        self.snapshot_ready          = True
        self.first_update_processed  = False
        logger.info(
            f"[{self._name}] OB snapshot fetched, lastUpdateId={raw['lastUpdateId']}"
        )

        if self.event_buffer:
            logger.info(
                f"[{self._name}] Replaying {len(self.event_buffer)} buffered depth events"
            )
            await self._process_buffered_events()

    # ── Buffered event replay ─────────────────────────────────────────────────

    async def _process_buffered_events(self):
        buffered = self.event_buffer[:]
        self.event_buffer.clear()

        for ev in buffered:
            if ev["u"] < self._data.last_update_id:
                continue

            if ev["U"] <= self._data.last_update_id <= ev["u"]:
                await self._apply_depth_update(ev)
                self.first_update_processed  = True
                self._data.last_update_id    = ev["u"]

            elif self.first_update_processed and ev.get("pu") == self._data.last_update_id:
                await self._apply_depth_update(ev)

            elif self.first_update_processed and ev.get("pu") != self._data.last_update_id:
                logger.warning(f"[{self._name}] Gap in buffered events — re-fetching snapshot")
                self.snapshot_ready = False
                asyncio.create_task(self.get_initial_snapshot())
                return

    # ── Live event processing ─────────────────────────────────────────────────

    async def _process_depth_event(self, event: dict) -> OrderBook:
        if not self.snapshot_ready:
            self.event_buffer.append(event)
            return self._data if self._data else None

        pu = event.get("pu")
        if pu != self._data.last_update_id:
            if pu < self._data.last_update_id:
                return self._data   # stale, ignore
            else:
                logger.error(
                    f"[{self._name}] Sequence break: pu={pu} > last_update_id={self._data.last_update_id}"
                )
                self.snapshot_ready = False
                await asyncio.sleep(3.0)
                asyncio.create_task(self.get_initial_snapshot())
                raise ValueError("Sequence break — re-syncing order book")

        await self._apply_depth_update(event)
        return self._data

    async def _apply_depth_update(self, event: dict):
        ts = int(time.time() * 1000)
        await self._data.apply_both_updates(
            ts,
            event["u"],
            bid_levels = event.get("b", []),
            ask_levels = event.get("a", []),
        )
        logger.success(
            f"[{self._name}] OB id={self._data.last_update_id} "
            f"bid={self._data.best_bid_price:.5f} ask={self._data.best_ask_price:.5f}"
        )

    async def __call__(self):
        """Register with shared manager, start buffering, then fetch snapshot."""
        from elysian_core.connectors.AsterDataConnectors import aster_spot_ob_client_manager
        aster_spot_ob_client_manager.register_feed(self)
        if not aster_spot_ob_client_manager._running:
            await aster_spot_ob_client_manager.start()
            asyncio.create_task(aster_spot_ob_client_manager.run_multiplex_feeds())
        await asyncio.sleep(0.1)
        await self.get_initial_snapshot()
        while True:
            await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton managers
# ──────────────────────────────────────────────────────────────────────────────

aster_spot_kline_client_manager = AsterKlineClientManager()
aster_spot_ob_client_manager    = AsterOrderBookClientManager()
