"""
Market data service entry point.

Owns all WebSocket feed managers (Binance spot/futures, Aster spot).
Publishes KlineEvent / OrderBookUpdateEvent to Redis pub/sub so that
per-strategy containers can subscribe independently.

Run with:
    python elysian_core/run_market_data.py
    python elysian_core/run_market_data.py --redis-url redis://localhost:6379
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from elysian_core.services.market_data_service import MarketDataService

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Elysian Market Data Service")
    parser.add_argument(
        "--trading-config",
        default="elysian_core/config/trading_config.yaml",
    )
    parser.add_argument(
        "--config-json",
        default="elysian_core/config/config.json",
    )
    parser.add_argument(
        "--redis-url",
        default=(
            f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:"
            f"{os.environ.get('REDIS_PORT', '6379')}"
        ),
    )
    args = parser.parse_args()

    asyncio.run(
        MarketDataService(
            trading_config_yaml=args.trading_config,
            config_json=args.config_json,
            redis_url=args.redis_url,
        ).run()
    )
