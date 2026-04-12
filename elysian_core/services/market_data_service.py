"""
Market Data Service — Elysian connector layer for Docker deployment.

Owns all WebSocket client managers (Binance Spot/Futures, Aster Spot/Futures)
and publishes KlineEvent / OrderBookUpdateEvent to Redis pub/sub.

Strategy containers subscribe to Redis instead of the in-process EventBus,
allowing each strategy to run in its own container while sharing one set of
WebSocket connections.

Run with:
    python -m elysian_core.services.market_data_service
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import List

from aiohttp import web
from dotenv import load_dotenv

# Resolve project root so relative config paths work when invoked as __main__
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

load_dotenv()

from elysian_core.connectors.binance.BinanceDataConnectors import (
    BinanceKlineFeed,
    BinanceOrderBookFeed,
    BinanceKlineClientManager,
    BinanceOrderBookClientManager,
)
from elysian_core.connectors.binance.BinanceFuturesDataConnectors import (
    BinanceFuturesKlineFeed,
    BinanceFuturesOrderBookFeed,
    BinanceFuturesKlineClientManager,
    BinanceFuturesOrderBookClientManager,
)
from elysian_core.connectors.aster.AsterDataConnectors import (
    AsterKlineFeed,
    AsterOrderBookFeed,
    AsterKlineClientManager,
    AsterOrderBookClientManager,
)
from elysian_core.connectors.aster.AsterPerpDataConnectors import (
    AsterPerpKlineFeed,
    AsterPerpOrderBookFeed,
    AsterPerpKlineClientManager,
    AsterPerpOrderBookClientManager,
)

from elysian_core.config.app_config import load_app_config
from elysian_core.transport.event_serializer import channel_prefix
from elysian_core.transport.redis_event_bus import RedisEventBusPublisher
from elysian_core.core.enums import AssetType, Venue
from elysian_core.utils.logger import setup_custom_logger

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class MarketDataService:
    """Connector layer: subscribes to exchange WebSocket feeds and
    publishes events to Redis pub/sub for strategy containers.

    Stages 1–2 only — no strategies, no sub-account exchanges.
    """

    def __init__(
        self,
        trading_config_yaml: str = "elysian_core/config/trading_config.yaml",
        config_json: str = "elysian_core/config/config.json",
        redis_url: str = "redis://localhost:6379",
    ) -> None:
        self.logger = setup_custom_logger("root", False)
        self._redis_url = redis_url
        self._start_time = time.monotonic()

        env_path = os.path.join(os.getcwd(), ".env")
        self.cfg = load_app_config(
            trading_config_yaml=trading_config_yaml,
            strategy_config_yamls=[],
            json_path=config_json,
            env_path=env_path,
        )
        self.logger.info(
            f"[MDS] Config loaded: {self.cfg.meta.version_name} | redis={redis_url}"
        )

        # ── Client managers ────────────────────────────────────────────────
        self.binance_kline_manager = BinanceKlineClientManager()
        self.binance_ob_manager = BinanceOrderBookClientManager()
        self.binance_futures_kline_manager = BinanceFuturesKlineClientManager()
        self.binance_futures_ob_manager = BinanceFuturesOrderBookClientManager()
        self.aster_kline_manager = AsterKlineClientManager()
        self.aster_ob_manager = AsterOrderBookClientManager()
        self.aster_futures_kline_manager = AsterPerpKlineClientManager()
        self.aster_futures_ob_manager = AsterPerpOrderBookClientManager()

        # Symbol lists populated by _setup_config
        self.binance_token_symbols: List[str] = []
        self.binance_futures_token_symbols: List[str] = []
        self.aster_token_symbols: List[str] = []
        self.aster_futures_token_symbols: List[str] = []

        # Running feed count for health endpoint
        self._total_feeds = 0

    # ── Configuration ─────────────────────────────────────────────────────────

    def _setup_config(self) -> None:
        sym = self.cfg.symbols
        self.binance_token_symbols = sym.symbols_for("binance", "spot")
        self.binance_futures_token_symbols = sym.symbols_for("binance", "futures")
        self.aster_token_symbols = sym.symbols_for("aster", "spot")
        self.aster_futures_token_symbols = sym.symbols_for("aster", "futures")
        self.logger.info(
            f"[MDS] Symbols — Binance Spot: {len(self.binance_token_symbols)}, "
            f"Futures: {len(self.binance_futures_token_symbols)}, "
            f"Aster Spot: {len(self.aster_token_symbols)}, "
            f"Aster Futures: {len(self.aster_futures_token_symbols)}"
        )

    # ── Publisher injection ───────────────────────────────────────────────────

    def _make_publisher(self, venue: Venue, asset_type: AssetType) -> RedisEventBusPublisher:
        prefix = channel_prefix(venue, asset_type)
        return RedisEventBusPublisher(redis_url=self._redis_url, channel_prefix=prefix)

    def _inject_publishers(self) -> None:
        """Assign one RedisEventBusPublisher per (venue, asset_type) to its managers."""
        if self.binance_token_symbols:
            pub = self._make_publisher(Venue.BINANCE, AssetType.SPOT)
            self.binance_kline_manager.set_event_bus(pub)
            self.binance_ob_manager.set_event_bus(pub)
            self.logger.info("[MDS] Publisher injected → Binance Spot managers")

        if self.binance_futures_token_symbols:
            pub = self._make_publisher(Venue.BINANCE, AssetType.PERPETUAL)
            self.binance_futures_kline_manager.set_event_bus(pub)
            self.binance_futures_ob_manager.set_event_bus(pub)
            self.logger.info("[MDS] Publisher injected → Binance Futures managers")

        if self.aster_token_symbols:
            pub = self._make_publisher(Venue.ASTER, AssetType.SPOT)
            self.aster_kline_manager.set_event_bus(pub)
            self.aster_ob_manager.set_event_bus(pub)
            self.logger.info("[MDS] Publisher injected → Aster Spot managers")

        if self.aster_futures_token_symbols:
            pub = self._make_publisher(Venue.ASTER, AssetType.PERPETUAL)
            self.aster_futures_kline_manager.set_event_bus(pub)
            self.aster_futures_ob_manager.set_event_bus(pub)
            self.logger.info("[MDS] Publisher injected → Aster Futures managers")

    # ── Feed registration ─────────────────────────────────────────────────────

    def _register_binance_spot_feeds(self) -> List:
        ob_feeds = []
        for token in self.binance_token_symbols:
            kf = BinanceKlineFeed(save_data=False)
            kf.create_new(asset=token, interval="1s")
            self.binance_kline_manager.register_feed(kf)

            obf = BinanceOrderBookFeed(save_data=False)
            obf.create_new(asset=token, interval="100ms")
            self.binance_ob_manager.register_feed(obf)
            ob_feeds.append(obf)
        self._total_feeds += len(self.binance_token_symbols) * 2
        self.logger.info(f"[MDS] Registered {len(self.binance_token_symbols)} Binance Spot kline+OB feeds")
        return ob_feeds

    def _register_binance_futures_feeds(self) -> List:
        ob_feeds = []
        for token in self.binance_futures_token_symbols:
            kf = BinanceFuturesKlineFeed(save_data=False)
            kf.create_new(asset=token, interval="1m")
            self.binance_futures_kline_manager.register_feed(kf)

            obf = BinanceFuturesOrderBookFeed(save_data=False)
            obf.create_new(asset=token, interval="100ms")
            self.binance_futures_ob_manager.register_feed(obf)
            ob_feeds.append(obf)
        self._total_feeds += len(self.binance_futures_token_symbols) * 2
        self.logger.info(f"[MDS] Registered {len(self.binance_futures_token_symbols)} Binance Futures kline+OB feeds")
        return ob_feeds

    def _register_aster_spot_feeds(self) -> List:
        ob_feeds = []
        for token in self.aster_token_symbols:
            kf = AsterKlineFeed(save_data=False)
            kf.create_new(asset=token, interval="1s")
            self.aster_kline_manager.register_feed(kf)

            obf = AsterOrderBookFeed(save_data=False)
            obf.create_new(asset=token, interval="100ms")
            self.aster_ob_manager.register_feed(obf)
            ob_feeds.append(obf)
        self._total_feeds += len(self.aster_token_symbols) * 2
        self.logger.info(f"[MDS] Registered {len(self.aster_token_symbols)} Aster Spot kline+OB feeds")
        return ob_feeds

    def _register_aster_futures_feeds(self) -> List:
        ob_feeds = []
        for token in self.aster_futures_token_symbols:
            kf = AsterPerpKlineFeed(save_data=False)
            kf.create_new(asset=token, interval="1s")
            self.aster_futures_kline_manager.register_feed(kf)

            obf = AsterPerpOrderBookFeed(save_data=False)
            obf.create_new(asset=token, interval="100ms")
            self.aster_futures_ob_manager.register_feed(obf)
            ob_feeds.append(obf)
        self._total_feeds += len(self.aster_futures_token_symbols) * 2
        self.logger.info(f"[MDS] Registered {len(self.aster_futures_token_symbols)} Aster Futures kline+OB feeds")
        return ob_feeds

    # ── Health server ─────────────────────────────────────────────────────────

    async def _health_handler(self, request: web.Request) -> web.Response:
        uptime = int(time.monotonic() - self._start_time)
        return web.json_response({
            "status": "ok",
            "feeds": self._total_feeds,
            "uptime_s": uptime,
        })

    async def _health_server(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8080)
        await site.start()
        self.logger.info("[MDS] Health server listening on :8080")
        # Run forever — the gather() in run() keeps us alive
        await asyncio.Event().wait()

    # ── Main run ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.logger.info("[MDS] ── Market Data Service starting ──────────────────")

        # 1. Resolve symbol lists from config
        self._setup_config()

        # 2. Inject Redis publishers into each manager
        self._inject_publishers()

        # 3. Register feeds per venue
        binance_ob_feeds = []
        binance_futures_ob_feeds = []
        aster_ob_feeds = []
        aster_futures_ob_feeds = []

        if self.binance_token_symbols:
            binance_ob_feeds = self._register_binance_spot_feeds()
        if self.binance_futures_token_symbols:
            binance_futures_ob_feeds = self._register_binance_futures_feeds()
        if self.aster_token_symbols:
            aster_ob_feeds = self._register_aster_spot_feeds()
        if self.aster_futures_token_symbols:
            aster_futures_ob_feeds = self._register_aster_futures_feeds()

        self.logger.info(f"[MDS] Total feeds registered: {self._total_feeds}")

        # 4. Start all managers (initiates WebSocket handshake + buffering)
        if self.binance_token_symbols:
            await self.binance_kline_manager.start()
            await self.binance_ob_manager.start()
        if self.binance_futures_token_symbols:
            await self.binance_futures_kline_manager.start()
            await self.binance_futures_ob_manager.start()
        if self.aster_token_symbols:
            await self.aster_kline_manager.start()
            await self.aster_ob_manager.start()
        if self.aster_futures_token_symbols:
            await self.aster_futures_kline_manager.start()
            await self.aster_futures_ob_manager.start()

        # 5. Launch multiplex reader/worker tasks (background — run_forever)
        multiplex_tasks = []
        if self.binance_kline_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.binance_kline_manager.run_multiplex_feeds(), name="binance-spot-kline"
            ))
        if self.binance_ob_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.binance_ob_manager.run_multiplex_feeds(), name="binance-spot-ob"
            ))
        if self.binance_futures_kline_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.binance_futures_kline_manager.run_multiplex_feeds(), name="binance-futures-kline"
            ))
        if self.binance_futures_ob_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.binance_futures_ob_manager.run_multiplex_feeds(), name="binance-futures-ob"
            ))
        if self.aster_kline_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.aster_kline_manager.run_multiplex_feeds(), name="aster-spot-kline"
            ))
        if self.aster_ob_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.aster_ob_manager.run_multiplex_feeds(), name="aster-spot-ob"
            ))
        if self.aster_futures_kline_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.aster_futures_kline_manager.run_multiplex_feeds(), name="aster-futures-kline"
            ))
        if self.aster_futures_ob_manager._active_feeds:
            multiplex_tasks.append(asyncio.create_task(
                self.aster_futures_ob_manager.run_multiplex_feeds(), name="aster-futures-ob"
            ))

        # 6. Wait for WebSocket connections to establish before fetching snapshots
        all_ob_feeds = (
            binance_ob_feeds + binance_futures_ob_feeds
            + aster_ob_feeds + aster_futures_ob_feeds
        )
        if all_ob_feeds:
            self.logger.info("[MDS] Waiting 10s for WebSocket connections to stabilise...")
            await asyncio.sleep(10.0)

            self.logger.info(f"[MDS] Fetching initial OB snapshots for {len(all_ob_feeds)} feeds...")
            await asyncio.gather(*[feed.get_initial_snapshot() for feed in all_ob_feeds])
            self.logger.info("[MDS] All OB snapshots fetched and synced")

        self.logger.info(
            f"[MDS] ── Running: {len(multiplex_tasks)} feed task(s), "
            f"{self._total_feeds} total feeds ──"
        )

        # 7. Run forever: multiplex tasks + health server
        await asyncio.gather(*multiplex_tasks, self._health_server())


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Elysian Market Data Service")
    parser.add_argument(
        "--trading-config",
        default=os.environ.get(
            "TRADING_CONFIG_YAML",
            "elysian_core/config/trading_config.yaml",
        ),
    )
    parser.add_argument(
        "--config-json",
        default=os.environ.get(
            "CONFIG_JSON",
            "elysian_core/config/config.json",
        ),
    )
    parser.add_argument(
        "--redis-url",
        default=(
            f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:"
            f"{os.environ.get('REDIS_PORT', '6379')}"
        ),
    )
    args = parser.parse_args()

    service = MarketDataService(
        trading_config_yaml=args.trading_config,
        config_json=args.config_json,
        redis_url=args.redis_url,
    )
    await service.run()


if __name__ == "__main__":
    asyncio.run(_main())
