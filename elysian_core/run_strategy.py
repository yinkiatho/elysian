"""
Single-strategy container entry point — Redis transport.

Stripped-down version of run_strategy.py for Docker container deployment.
Key differences:
  - No client managers (MarketDataService owns all WebSocket feeds)
  - shared_event_bus is RedisEventBusSubscriber
  - Loads one strategy, identified by STRATEGY_CONFIG_YAML env var

Run with:
    python elysian_core/run_strategy_redis_new.py
    STRATEGY_CONFIG_YAML=.../strategy_000.yaml python elysian_core/run_strategy_redis_new.py
"""

from typing import Dict, List, Optional, Callable, Any
import argparse
import asyncio
import datetime
import os
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

from elysian_core.connectors.binance.BinanceExchange import BinanceSpotExchange
from elysian_core.connectors.binance.BinanceFuturesExchange import BinanceFuturesExchange
from elysian_core.connectors.aster.AsterExchange import AsterSpotExchange

from elysian_core.utils.logger import setup_custom_logger, shutdown_discord, init_discord_logging
from elysian_core.core.event_bus import EventBus
from elysian_core.core.enums import OrderType, RunnerState, Venue, AssetType
from elysian_core.core.fsm import PeriodicTask
from elysian_core.execution.engine import ExecutionEngine
from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.core.portfolio import Portfolio
from elysian_core.config.app_config import load_app_config, load_strategy_class, StrategyConfig
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.shadow_book import ShadowBook
from elysian_core.db.models import create_tables
from elysian_core.transport.redis_event_bus import RedisEventBusSubscriber
from elysian_core.transport.event_serializer import strategy_channels
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _compute_channels(cfg) -> List[str]:
    """Redis channels needed by all strategies loaded in this container."""
    channels = []
    for strat_cfg in cfg.strategies.values():
        venue = Venue[strat_cfg.venue.upper()]
        asset_type = AssetType[strat_cfg.asset_type.upper()]
        channels.extend(strategy_channels(strat_cfg.symbols, venue, asset_type))
    return list(set(channels))


class StrategyRunner:

    def __init__(
        self,
        trading_config_yaml: str = 'elysian_core/config/trading_config.yaml',
        strategy_config_yaml: str = None,
        config_json: str = 'elysian_core/config/config.json',
        redis_url: str = 'redis://localhost:6379',  
    ):
        self._redis_url = redis_url
        self._state = RunnerState.CREATED

        env_path = os.path.join(os.getcwd(), '.env')
        self.cfg = load_app_config(
            trading_config_yaml=trading_config_yaml,
            strategy_config_yamls=[strategy_config_yaml],
            json_path=config_json,
            env_path=env_path,
        )
        
        self.strat_cfg = next(iter(self.cfg.strategies.values()))
        self.logger = setup_custom_logger(f"{self.strat_cfg.strategy_name}_{self.strat_cfg.strategy_id}")
        self.logger.info(
            f'[RedisRunner] config={trading_config_yaml}, '
            f'yamls={strategy_config_yaml}, redis={self._redis_url}'
        )
        
        self._portfolio_dict: Dict[AssetType, Dict[Venue, Portfolio]] = {
            AssetType.SPOT: {}, AssetType.PERPETUAL: {},
        }
        self._snapshot_interval_s = 0
        self._snapshot_task_dict: Dict[Any, PeriodicTask] = {}
        self._sub_account_exchange_dict: Dict[int, Any] = {}
        self.shared_event_bus = None

        self._exchange_connector_callables = {
            (AssetType.SPOT, Venue.BINANCE): BinanceSpotExchange,
            (AssetType.PERPETUAL, Venue.BINANCE): BinanceFuturesExchange,
            (AssetType.SPOT, Venue.ASTER): AsterSpotExchange,
        }

        create_tables(safe=True)

    @staticmethod
    def _get_timestamp() -> str:
        utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.now(utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S')


    # ── Portfolio (aggregate NAV monitor only) ────────────────────────────────

    async def _setup_portfolio(self, asset_type: AssetType) -> None:
        venues = (
            self.cfg.meta.spot_venues if asset_type == AssetType.SPOT
            else self.cfg.meta.futures_venues
        )
        for venue_str in venues:
            self._snapshot_interval_s = int(self.cfg.portfolio.get("snapshot_interval_s", 0))
            venue_enum = next(
                (v for v in Venue if v.value.lower() == venue_str.lower()), None
            )
            if venue_enum is None:
                continue
            portfolio = Portfolio(venue=venue_enum, cfg=self.cfg)
            self._portfolio_dict[asset_type][venue_enum] = portfolio
            self._start_snapshot_task(portfolio.save_snapshot, portfolio.name)

    async def _setup_pipeline(self) -> None:
        for asset_type in [AssetType.SPOT, AssetType.PERPETUAL]:
            await self._setup_portfolio(asset_type)

    # ── Strategy wiring (Stage 3-5) ───────────────────────────────────────────

    async def setup_strategy(
        self, strategy: SpotStrategy, strategy_id: int, strategy_name: str
    ) -> None:
        strat_cfg = self.cfg.strategies[strategy_id]
        asset_type = AssetType[strat_cfg.asset_type.upper()]
        venue = Venue[strat_cfg.venue.upper()]
        
        
        # Set up sub-account exchange and private event bus for strategy
        private_exchange, private_bus = await self._create_sub_account_exchange(
            strat_cfg, venue, asset_type
        )
        self._sub_account_exchange_dict[strategy_id] = private_exchange

        # Set up ShadowBook for strategy and register to portfolio
        shadow_book = ShadowBook(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            venue=venue,
            strategy_config=strat_cfg,
        )
        shadow_book.sync_from_exchange(private_exchange, feeds={})
        shadow_book.start(self.shared_event_bus, private_event_bus=private_bus)
        self._start_snapshot_task(shadow_book.save_snapshot, f"shadow_book_{strategy_id}")

        # Register ShadowBook to portfolio for NAV aggregation and snapshotting
        self._portfolio_dict[asset_type][venue].register_shadow_book(shadow_book)

        
        # Set up optimizer with strategy-specific risk config, and wire to ShadowBook and execution engine
        risk_cfg = self.cfg.effective_risk_for(
            asset_type=strat_cfg.asset_type.lower(),
            venue=strat_cfg.venue.lower(),
            strategy_id=strategy_id,
        )
        optimizer = PortfolioOptimizer(risk_config=risk_cfg, portfolio=shadow_book, cfg=self.cfg)

        # Set up execution engine with exchange connector, and wire to strategy and optimizer
        execution_engine = ExecutionEngine(
            exchanges={venue: private_exchange},
            portfolio=shadow_book,
            default_venue=venue,
            cfg=self.cfg,
            asset_type=asset_type,
        )
        
        execution_engine.start(self.shared_event_bus)

        # Wire everything together by injecting dependencies into the strategy instance
        strategy.strategy_id = strategy_id
        strategy.strategy_name = strategy_name
        strategy._shadow_book = shadow_book
        strategy.portfolio = shadow_book
        strategy._optimizer = optimizer
        strategy._execution_engine = execution_engine
        strategy._shared_event_bus = self.shared_event_bus
        strategy._private_event_bus = private_bus
        strategy._exchanges = {venue: private_exchange}
        strategy.cfg = self.cfg
        strategy.strategy_config = strat_cfg

        await strategy.start()

        self.logger.info(
            f"[Runner] Wired: {strategy.__class__.__name__} (id={strategy_id}) "
            f"→ ShadowBook → Optimizer → ExecutionEngine [{venue.value} {asset_type.value}]"
        )

    async def _create_sub_account_exchange(
        self,
        strategy_config: StrategyConfig,
        venue: Venue,
        asset_type: AssetType = AssetType.SPOT,
    ):
        private_bus = EventBus()
        exchange_cls = self._exchange_connector_callables[(asset_type, venue)]
        exchange = exchange_cls(
            args=self.cfg,
            api_key=strategy_config.sub_account_api_key,
            api_secret=strategy_config.sub_account_api_secret,
            symbols=strategy_config.symbols,
            private_event_bus=private_bus,
            strategy_config=strategy_config,
        )
        
        # Wire in the Redis Event bus for Kline and OB updates
        exchange.start(self.shared_event_bus)
        
        # Similarly we wire in the Kline and OB Feeds from RedisEventBusSubscriber to the exchange's feed managers, so that the strategy can subscribe to them via the private event bus. This is done in the exchange connector's run() method, where it subscribes to the relevant Redis channels and dispatches events to the private_bus.
        await exchange.run()
        
        self.logger.info(
            f"[Runner] Sub-account exchange ready: strategy {strategy_config.strategy_id} "
            f"({venue.value})"
        )
        return exchange, private_bus

    def _start_snapshot_task(self, callable: Callable, tag: str) -> None:
        if self._snapshot_interval_s <= 0:
            return
        self._snapshot_task_dict[tag] = PeriodicTask(
            callback=callable,
            interval_s=self._snapshot_interval_s,
            name=f"{tag} Snapshot Task",
        )
        self._snapshot_task_dict[tag].start()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        await shutdown_discord()
        
        for exchange in self._sub_account_exchange_dict.values():
            await exchange.cleanup()
            
        for task in self._snapshot_task_dict.values():
            task.stop()

        if self.shared_event_bus and hasattr(self.shared_event_bus, 'close'):
            await self.shared_event_bus.close()
        self.logger.info("[Runner] Cleanup complete")

    @property
    def state(self) -> RunnerState:
        return self._state

    # ── Main run ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        enable_discord = self.cfg.extra.get('logging', {}).get('enable_discord', False)
        if enable_discord:
            token = os.getenv('DISCORD_TOKEN')
            guild_id_str = os.getenv('DISCORD_GUILD_ID')
            if token and guild_id_str:
                await init_discord_logging(token=token, guild_id=int(guild_id_str))

        strategy = load_strategy_class(self.strat_cfg.class_name)(
            strategy_name=self.strat_cfg.strategy_name,
            strategy_id=self.strat_cfg.strategy_id,
        )

        try:
            self.logger.info("[Runner] ── Redis strategy runner starting ──")

            self._state = RunnerState.CONFIGURING
            self.shared_event_bus = RedisEventBusSubscriber(redis_url=self._redis_url)
            self.logger.info(f"[Runner] RedisEventBusSubscriber ready ({self._redis_url})")

            self._state = RunnerState.CONNECTING
            
            try:
                self._state = RunnerState.SYNCING
                await self._setup_pipeline()
                await self.setup_strategy(strategy, strategy_id=self.strat_cfg.strategy_id, strategy_name=self.strat_cfg.strategy_name)

                # Start Redis listener for this strategy's symbols
                channels = _compute_channels(self.cfg)
                await self.shared_event_bus.start_listener(channels)
                self.logger.info(
                    f"[Runner] Subscribed to {len(channels)} Redis channels"
                )

                coros: List[Any] = []
                if self.shared_event_bus._listener_task:
                    coros.append(self.shared_event_bus._listener_task)
                coros.append(strategy.run_forever())

                self._state = RunnerState.RUNNING
                self.logger.info(f"[Runner] Main loop: {len(coros)} coroutine(s)")
                await asyncio.gather(*coros)

            finally:
                self._state = RunnerState.STOPPING
                await strategy.stop()

        except asyncio.CancelledError:
            self.logger.warning("[Runner] Cancelled")
        except Exception as e:
            self._state = RunnerState.FAILED
            self.logger.error(f"[Runner] Fatal: {e}", exc_info=True)
            raise
        finally:
            await self._cleanup()
            if self._state != RunnerState.FAILED:
                self._state = RunnerState.STOPPED
            self.logger.info(f"[Runner] Stopped (state={self._state.name})")


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_strategy(
    trading_config_yaml: str,
    strategy_config_yaml: str,
    redis_url: str,
) -> None:
    runner = StrategyRunner(
        trading_config_yaml=trading_config_yaml,
        strategy_config_yaml=strategy_config_yaml,
        redis_url=redis_url,
    )
    await runner.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Elysian Redis Strategy Runner")
    parser.add_argument(
        "--trading-config",
        default="elysian_core/config/trading_config.yaml",
    )
    parser.add_argument(
        "--strategy-yaml",
        default=os.environ.get(
            "STRATEGY_CONFIG_YAML",
            "elysian_core/config/strategies/strategy_000_event_driven.yaml",
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

    asyncio.run(run_strategy(
        trading_config_yaml=args.trading_config,
        strategy_config_yaml=args.strategy_yaml,
        redis_url=args.redis_url,
    ))
