import asyncio
import collections
import time
from typing import Optional

import requests
from binance import AsyncClient, BinanceSocketManager

from elysian_core.connectors.base import AbstractDataFeed
from elysian_core.core.market_data import OrderBook
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class BinanceFeed(AbstractDataFeed):
    """
    Real-time order-book feed from Binance using the official python-binance library.

    Usage:
        feed = BinanceFeed()
        feed.create_new(asset="ETHUSDT")
        await feed()
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        super().__init__(save_data=save_data, file_dir=file_dir)
        self._client: AsyncClient = None
        self._manager: BinanceSocketManager = None
        self._last_update_id: Optional[int] = None

    # ── Configuration ─────────────────────────────────────────────────────────

    def create_new(self, asset: str, interval: str = "100ms"):
        self._name = asset
        self._interval = interval

    # ── Snapshot / REST ───────────────────────────────────────────────────────

    def _fetch_snapshot(self, limit: int = 100) -> dict:
        url = "https://api1.binance.com/api/v3/depth"
        resp = requests.get(url, params={"symbol": self._name, "limit": min(limit, 5000)})
        resp.raise_for_status()
        return resp.json()

    async def get_initial_snapshot(self):
        raw = self._fetch_snapshot(100)
        ts = int(time.time() * 1000)
        bids = [[float(b[0]), float(b[1])] for b in raw["bids"]]
        asks = [[float(a[0]), float(a[1])] for a in raw["asks"]]
        self._data = OrderBook(
            last_update_id=raw["lastUpdateId"],
            last_timestamp=ts,
            ticker=self._name,
            interval=self._interval,
            best_bid_price=bids[0][0],
            best_bid_amount=bids[0][1],
            best_ask_price=asks[0][0],
            best_ask_amount=asks[0][1],
            bid_orders=bids,
            ask_orders=asks,
        )
        self._last_update_id = raw["lastUpdateId"]
        logger.info(f"[{self._name}] Initial snapshot at update_id={self._last_update_id}")

    # ── Event processing ──────────────────────────────────────────────────────

    def _process_depth_event(self, event: dict) -> OrderBook:
        ts = int(time.time() * 1000)
        bids = [[float(b[0]), float(b[1])] for b in event["bids"]]
        asks = [[float(a[0]), float(a[1])] for a in event["asks"]]
        return OrderBook(
            last_update_id=ts,
            last_timestamp=ts,
            ticker=self._name,
            interval=self._interval,
            best_bid_price=bids[0][0],
            best_bid_amount=bids[0][1],
            best_ask_price=asks[0][0],
            best_ask_amount=asks[0][1],
            bid_orders=bids,
            ask_orders=asks,
        )

    # ── Client helpers ────────────────────────────────────────────────────────

    async def _create_client(self, retries: int = 10) -> AsyncClient:
        for i in range(retries):
            try:
                return await AsyncClient.create()
            except (TimeoutError) as e:
                if i == retries - 1:
                    raise
                await asyncio.sleep(2 ** i)

    # ── Main feed loop ────────────────────────────────────────────────────────

    async def __call__(self):
        self._client = await self._create_client()
        self._manager = BinanceSocketManager(self._client)
        socket = self._manager.depth_socket(
            self._name,
            interval=100,
            depth=BinanceSocketManager.WEBSOCKET_DEPTH_20,
        )

        stats_task = asyncio.create_task(self.update_current_stats())
        if self.save_data:
            asyncio.create_task(self.periodically_log_data())

        try:
            async with socket:
                while True:
                    try:
                        event = await socket.recv()
                        self._data = self._process_depth_event(event)
                        if self.save_data:
                            asyncio.create_task(self._append_to_df(self._data))
                    except Exception as e:
                        logger.error(f"[{self._name}] depth event error: {e}")
        finally:
            stats_task.cancel()
            await self._client.close_connection()


# Backwards-compatible alias
BinanceDataFeed = BinanceFeed
