from typing import Dict, List, Optional, Callable, Any
import asyncio
import os
import datetime
import sys
from elysian_core.connectors.BinanceExchange import (
    BinanceSpotExchange
)

from elysian_core.connectors.BinanceDataConnectors import (
    BinanceKlineFeed,
    BinanceOrderBookFeed,
    BinanceKlineClientManager,
    BinanceOrderBookClientManager
)

from elysian_core.connectors.BinanceFuturesDataConnectors import (
    BinanceFuturesOrderBookFeed,
    BinanceFuturesKlineFeed,
    BinanceFuturesKlineClientManager,
    BinanceFuturesOrderBookClientManager,
)
from elysian_core.connectors.BinanceFuturesExchange import BinanceFuturesExchange

from elysian_core.connectors.AsterDataConnectors import (
    AsterKlineFeed,
    AsterOrderBookFeed,
    AsterKlineClientManager,
    AsterOrderBookClientManager
)

from elysian_core.connectors.AsterExchange import AsterSpotExchange

from elysian_core.connectors.AsterPerpDataConnectors import (
    AsterPerpKlineClientManager,
    AsterPerpOrderBookClientManager,
    AsterPerpKlineFeed,
    AsterPerpOrderBookFeed,
)
# TODO: AsterPerpExchange source was deleted — re-add when perp connector is rebuilt
# from elysian_core.connectors.AsterExchange import AsterPerpExchange
# from elysian_core.connectors.VolatilityBarbClient import VolatilityBarbClientLocal, RedisConfig
from elysian_core.utils.logger import setup_custom_logger
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from elysian_core.core.event_bus import EventBus
from elysian_core.core.enums import OrderType, RunnerState, Venue, AssetType
from elysian_core.core.fsm import PeriodicTask
from elysian_core.execution.engine import ExecutionEngine
from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.core.portfolio import Portfolio
from elysian_core.risk.risk_config import RiskConfig
from elysian_core.config.app_config import AppConfig, load_app_config, load_strategy_class, StrategyConfig
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.shadow_book import ShadowBook
from elysian_core.db.models import create_tables


# Add parent directory to path so imports work when running this script directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from elysian_core.strategy.example_weight_strategy_v2_event_driven import EventDrivenStrategy

    
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
logger = setup_custom_logger('root')

class StrategyRunner:
    """Main strategy runner class that orchestrates all trading components."""
    
    def __init__(
        self,
        trading_config_yaml: str = 'elysian_core/config/trading_config.yaml',
        strategy_config_yamls: Optional[List[str]] = None,
        config_json: str = 'elysian_core/config/config.json',
        config_yaml: Optional[str] = None,
    ):
        """Initialize the strategy runner with configuration.

        Parameters
        ----------
        trading_config_yaml:
            Path to the system-level YAML (risk, portfolio, execution, venue_configs).
        strategy_config_yamls:
            List of per-strategy YAML file paths.  Each is loaded as a
            :class:`StrategyConfig` and merged into ``cfg.strategies``.
        config_json:
            Path to the JSON symbols/venues file.
        config_yaml:
            Backward-compat alias for ``trading_config_yaml``.
        """
        if config_yaml is not None:
            trading_config_yaml = config_yaml

        self.logger = setup_custom_logger('root')
        self.logger.info(
            f'Kickstarted StrategyRunner with trading_config={trading_config_yaml}, '
            f'strategy_yamls={strategy_config_yamls}, json={config_json} at PWD: {os.getcwd()}'
        )

        env_path = os.path.join(os.getcwd(), '.env')
        self.cfg = load_app_config(
            trading_config_yaml=trading_config_yaml,
            strategy_config_yamls=strategy_config_yamls,
            json_path=config_json,
            env_path=env_path,
        )
        self.logger.info(f"AppConfig loaded: {self.cfg.meta.version_name} / {self.cfg.meta.strategy_name}")

        self.timestamp = self._get_timestamp()

        # Components (initialized as None)
        self.pool_ids = None
        self.static_object_ids = None
        self.package_targets = None
        self.spot_total_tokens = None
        self.futures_total_tokens = None
        self.okx_tokens = None
        self.okx_token_symbols = None
        self.total_token_pairs = []
        
        self.data_feed = {}
        self.vol_feed = None
        self.processor = None
        self._snapshot_interval_s = 0
        self._snapshot_task_dict: Dict[int, PeriodicTask] = {}
        self._state = RunnerState.CREATED
        
        
        # Token Symbols
        self.binance_token_symbols = None
        self.binance_futures_token_symbols = None
        self.aster_token_symbols = None
        self.aster_futures_token_symbols = None
        
        
        # Binance Client Managers
        self.binance_kline_manager = BinanceKlineClientManager()
        self.binance_ob_manager = BinanceOrderBookClientManager()
        
        # Binance Futures Client Managers
        self.binance_futures_kline_manager = BinanceFuturesKlineClientManager()
        self.binance_futures_ob_manager = BinanceFuturesOrderBookClientManager()

        # Aster Client Managers
        self.aster_kline_manager = AsterKlineClientManager()
        self.aster_ob_manager = AsterOrderBookClientManager()
            
        # Aster Futures Client Managers
        self.aster_futures_kline_manager = AsterPerpKlineClientManager()
        self.aster_futures_ob_manager = AsterPerpOrderBookClientManager()

        # Managing Portfolios
        self._portfolio_dict: Dict[AssetType, Dict[Venue, Portfolio]] = {AssetType.SPOT: {},
                                                                         AssetType.PERPETUAL: {}}
        # Managing List of Strategies, for now just spot strategy
        self._strategy_dict: Dict[int, SpotStrategy] = {}
        
        # Managing dicts of optimizers
        self._optimizer_dict: Dict[AssetType, Dict[Venue, PortfolioOptimizer]] = {AssetType.SPOT: {}, 
                                                                                  AssetType.PERPETUAL: {}}

        # Managing Execution Engines
        self._execution_engine_dict: Dict[AssetType, Dict[Venue, ExecutionEngine]] = {AssetType.SPOT: {},
                                                                                      AssetType.PERPETUAL: {}}

        # Managing Exchange Connectors
        self.exchange_connectors_dict = {
            AssetType.SPOT: {
                Venue.BINANCE: None,
                Venue.ASTER: None,
            },
            AssetType.PERPETUAL: {
                Venue.BINANCE: None,
                Venue.ASTER: None,
            }
        }
        
        self._exchange_connector_callables = {
            (AssetType.SPOT, Venue.BINANCE): BinanceSpotExchange,
            (AssetType.PERPETUAL, Venue.BINANCE): BinanceFuturesExchange,
            (AssetType.SPOT, Venue.ASTER): AsterSpotExchange,
            #(AssetType.PERPETUAL, Venue.ASTER): AsterPerpExchange,
        }
        
        self.shared_event_bus = None

        # Sub-account exchanges: strategy_id -> exchange (per-strategy, dedicated API keys)
        self._sub_account_exchange_dict: Dict[int, Any] = {}
        
        # Create the Database
        create_tables(safe=True)

    @staticmethod
    def _get_timestamp() -> str:
        """Get current timestamp in UTC+8 timezone."""
        utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.now(utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S')
    
    
    
    # --------- Setup functions for different components and async --------- #
    async def _run_event_loop_in_thread(self, tasks: list) -> None:
        """Run a group of coroutines in a separate thread with dedicated event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.gather(*tasks))
        finally:
            loop.close()
    
    
    
    async def _cleanup(self, tasks_groups: list) -> None:
        """Cleanup function to properly shutdown all tasks and client managers."""
        self.logger.info("Starting cleanup...")
        
        # Stop shared client managers first
        await self.binance_kline_manager.stop()
        await self.binance_ob_manager.stop()
        await self.binance_futures_kline_manager.stop()
        await self.binance_futures_ob_manager.stop()
        await self.aster_kline_manager.stop()
        await self.aster_ob_manager.stop()
        await self.aster_futures_kline_manager.stop()
        await self.aster_futures_ob_manager.stop()
        
        for tasks in tasks_groups:
            for task in tasks:
                if hasattr(task, 'stop'):
                    await task.stop()
                elif isinstance(task, asyncio.Task):
                    task.cancel()
        
        for tasks in tasks_groups:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        self.logger.info("Cleanup completed")
        
    
    # --------------------------- SETUP FUNCTIONS START HERE  --------------------------- #
    def _setup_config(self) -> None:
        """Initialize tokens and pools configuration from AppConfig.symbols."""
        self.logger.info("Setting up Strategy Configuration...")

        sym = self.cfg.symbols
        self.total_token_pairs = []

        # Per-venue symbol accessors (backward-compat properties)
        self.binance_token_symbols = sym.symbols_for("binance", "spot")
        self.binance_futures_token_symbols = sym.symbols_for("binance", "futures")
        self.aster_token_symbols = sym.symbols_for("aster", "spot")
        self.aster_futures_token_symbols = sym.symbols_for("aster", "futures")

        self.total_token_pairs.extend(self.binance_token_symbols)
        self.total_token_pairs.extend(self.binance_futures_token_symbols)
        self.total_token_pairs.extend(self.aster_token_symbols)
        self.total_token_pairs.extend(self.aster_futures_token_symbols)

        self.spot_total_tokens = sym.spot_tokens
        self.futures_total_tokens = sym.futures_tokens
        self.logger.info(f"Configuration setup completed with {len(self.total_token_pairs)} total token pairs: {self.total_token_pairs}")        
        
        
    def _setup_exchanges(self) -> None:
        """Initialize Exchange Connectors instances for trading operations. 
                This is the MAIN EXCHANGE CONNECTORS"""

        self.logger.info("Setting up exchange instances...")
        # Initialize Binance spot exchange
        if self.binance_token_symbols and 'Binance' in self.cfg.meta.spot_venues:
            self.binance_exchange = BinanceSpotExchange(
                args=self.cfg,
                api_key=self.cfg.secrets.binance.api_key,
                api_secret=self.cfg.secrets.binance.api_secret,
                symbols=self.binance_token_symbols,
                kline_manager=self.binance_kline_manager,
                ob_manager=self.binance_ob_manager,
                event_bus=getattr(self, 'event_bus', None),
            )

        # Initialize Binance futures exchange
        if self.binance_futures_token_symbols and 'Binance' in self.cfg.meta.futures_venues:
            self.binance_futures_exchange = BinanceFuturesExchange(
                args=self.cfg,
                api_key=self.cfg.secrets.binance.api_key,
                api_secret=self.cfg.secrets.binance.api_secret,
                symbols=self.binance_futures_token_symbols,
                kline_manager=self.binance_futures_kline_manager,
                ob_manager=self.binance_futures_ob_manager,
            )
            self.logger.info("Binance futures exchange initialized")

        # Initialize Aster futures exchange
        # TODO: AsterPerpExchange source was deleted — re-add when perp connector is rebuilt
        # if self.aster_futures_token_symbols and 'Aster' in self.cfg.meta.futures_venues:
        #     self.aster_futures_exchange = AsterPerpExchange(...)
        #     logger.info("Aster futures exchange initialized")
            
        if self.aster_token_symbols and 'Aster' in self.cfg.meta.spot_venues:
            self.aster_exchange = AsterSpotExchange(
                args=self.cfg,
                api_key=self.cfg.secrets.aster.api_key,
                api_secret=self.cfg.secrets.aster.api_secret,
                symbols=self.aster_token_symbols,
                kline_manager=self.aster_kline_manager,
                ob_manager=self.aster_ob_manager,
            )
            self.logger.info("Aster Spot Exchange initialized")
            
            
        # Make the exchange mapping
        # Initialize exchange mapping dictionary
        self.exchange_connectors_dict = {
            AssetType.SPOT: {
                Venue.BINANCE: self.binance_exchange if hasattr(self, 'binance_exchange') else None,
                Venue.ASTER: self.aster_exchange if hasattr(self, 'aster_exchange') else None,
            },
            AssetType.PERPETUAL: {
                Venue.BINANCE: self.binance_futures_exchange if hasattr(self, 'binance_futures_exchange') else None,
                Venue.ASTER: self.aster_futures_exchange if hasattr(self, 'aster_futures_exchange') else None,
            }
        }
        # Log the initialized exchanges
        self.logger.info("Exchange mapping completed:")
                
        
    # --------- Setting up Binance Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_binance_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        self.logger.info("Setting up Binance data feeds with multiplex architecture...")
        
        self.binance_kline_manager.set_event_bus(self.shared_event_bus)
        self.binance_ob_manager.set_event_bus(self.shared_event_bus)
        
        # Create kline feeds that register with shared client manager
        for i, token in enumerate(self.binance_token_symbols):
            token_feed = BinanceKlineFeed(save_data=False)
            token_feed.create_new(asset=token, interval="1s")
            
            # Register the feed in the client manager
            self.binance_kline_manager.register_feed(token_feed)

                    
        # Create order book feeds if needed (for exchange operations)
        ob_feeds = []
        for i, token in enumerate(self.binance_token_symbols):
            ob_feed = BinanceOrderBookFeed(save_data=False)
            ob_feed.create_new(asset=token, interval="100ms")
            
            # Register the feed in the client manager
            self.binance_ob_manager.register_feed(ob_feed)
            ob_feeds.append(ob_feed)
        
        self.logger.info(f"Initialized {len(self.binance_kline_manager._active_feeds)} Binance kline feeds and {len(self.binance_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.binance_kline_manager.start()
        await self.binance_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.binance_kline_manager._active_feeds:
            self.logger.info(f"Starting Binance Kline multiplex feeds ({len(self.binance_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_kline_manager.run_multiplex_feeds()))
        
        if self.binance_ob_manager._active_feeds:
            self.logger.info(f"Starting Binance Order Book multiplex feeds ({len(self.binance_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(10.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            self.logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Binance order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            self.logger.info(f"All Binance order book snapshots fetched and synced")
            
        if multiplex_tasks:
            self.logger.info(f"Worker process running {len(multiplex_tasks)} feed task(s)...")
            await asyncio.gather(*multiplex_tasks)
            
    
# --------- Setting up Binance Futures Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_binance_futures_data_feeds(self) -> None:
        """Initialize all futures data feeds with multiplex architecture"""
        
        self.binance_futures_kline_manager.set_event_bus(self.shared_event_bus)
        self.binance_futures_ob_manager.set_event_bus(self.shared_event_bus)
        
        self.logger.info("Setting up Binance futures data feeds with multiplex architecture...")
        
        # Create kline feeds that register with shared client manager
        for i, token in enumerate(self.binance_futures_token_symbols):
            token_feed = BinanceFuturesKlineFeed(save_data=False)
            token_feed.create_new(asset=token, interval="1m")
            
            # Register the feed in the client manager
            self.binance_futures_kline_manager.register_feed(token_feed)

                    
        # Create order book feeds if needed (for exchange operations)
        ob_feeds = []
        for i, token in enumerate(self.binance_futures_token_symbols):
            ob_feed = BinanceFuturesOrderBookFeed(save_data=False)
            ob_feed.create_new(asset=token, interval="100ms")
            
            # Register the feed in the client manager
            self.binance_futures_ob_manager.register_feed(ob_feed)
            ob_feeds.append(ob_feed)
        
        self.logger.info(f"Initialized {len(self.binance_futures_kline_manager._active_feeds)} Binance futures kline feeds and {len(self.binance_futures_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.binance_futures_kline_manager.start()
        await self.binance_futures_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.binance_futures_kline_manager._active_feeds:
            self.logger.info(f"Starting Binance Futures Kline multiplex feeds ({len(self.binance_futures_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_futures_kline_manager.run_multiplex_feeds()))
        
        if self.binance_futures_ob_manager._active_feeds:
            self.logger.info(f"Starting Binance Futures Order Book multiplex feeds ({len(self.binance_futures_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_futures_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(5.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            self.logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Binance futures order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            self.logger.info(f"All Binance futures order book snapshots fetched and synced")
            
        if multiplex_tasks:
            self.logger.info(f"Worker process running {len(multiplex_tasks)} futures feed task(s)...")
            await asyncio.gather(*multiplex_tasks)


# --------- Setting up Aster Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_aster_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        self.aster_kline_manager.set_event_bus(self.shared_event_bus)
        self.aster_ob_manager.set_event_bus(self.shared_event_bus)
        
        self.logger.info("Setting up data feeds with multiplex architecture...")
        
        # Create kline feeds that register with shared client manager
        for i, token in enumerate(self.aster_token_symbols):
            token_feed = AsterKlineFeed(save_data=False)
            token_feed.create_new(asset=token, interval="1s")
            
            # Register the feed in the client manager
            self.aster_kline_manager.register_feed(token_feed)

                    
        # Create order book feeds if needed (for exchange operations)
        ob_feeds = []
        for i, token in enumerate(self.aster_token_symbols):
            ob_feed = AsterOrderBookFeed(save_data=False)
            ob_feed.create_new(asset=token, interval="100ms")
            
            # Register the feed in the client manager
            self.aster_ob_manager.register_feed(ob_feed)
            ob_feeds.append(ob_feed)
        
        self.logger.info(f"Initialized {len(self.aster_kline_manager._active_feeds)} Aster kline feeds and {len(self.aster_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.aster_kline_manager.start()
        await self.aster_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.aster_kline_manager._active_feeds:
            self.logger.info(f"Starting Aster Kline multiplex feeds ({len(self.aster_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_kline_manager.run_multiplex_feeds()))
        
        if self.aster_ob_manager._active_feeds:
            self.logger.info(f"Starting Aster Order Book multiplex feeds ({len(self.aster_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(10.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            self.logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Aster order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            self.logger.info(f"All Aster order book snapshots fetched and synced")
            
        if multiplex_tasks:
            self.logger.info(f"Worker process running {len(multiplex_tasks)} feed task(s)...")
            await asyncio.gather(*multiplex_tasks)
            

        
    # --------- Setting up Aster Futures Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_aster_futures_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        self.aster_futures_kline_manager.set_event_bus(self.shared_event_bus)
        self.aster_futures_ob_manager.set_event_bus(self.shared_event_bus)
        
        
        self.logger.info("Setting up data feeds with multiplex architecture...")
        
        # Create kline feeds that register with shared client manager
        for i, token in enumerate(self.aster_futures_token_symbols):
            token_feed = AsterPerpKlineFeed(save_data=False)
            token_feed.create_new(asset=token, interval="1s")
            
            # Register the feed in the client manager
            self.aster_futures_kline_manager.register_feed(token_feed)

                    
        # Create order book feeds if needed (for exchange operations)
        ob_feeds = []
        for i, token in enumerate(self.aster_futures_token_symbols):
            ob_feed = AsterPerpOrderBookFeed(save_data=False)
            ob_feed.create_new(asset=token, interval="100ms")
            
            # Register the feed in the client manager
            self.aster_futures_ob_manager.register_feed(ob_feed)
            ob_feeds.append(ob_feed)
        
        self.logger.info(f"Initialized {len(self.aster_futures_kline_manager._active_feeds)} Aster kline feeds and {len(self.aster_futures_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.aster_futures_kline_manager.start()
        await self.aster_futures_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.aster_futures_kline_manager._active_feeds:
            self.logger.info(f"Starting Aster Futures Kline multiplex feeds ({len(self.aster_futures_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_futures_kline_manager.run_multiplex_feeds()))
        
        if self.aster_futures_ob_manager._active_feeds:
            self.logger.info(f"Starting Aster Futures Order Book multiplex feeds ({len(self.aster_futures_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_futures_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(10.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            self.logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Aster Perp order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            self.logger.info(f"All Aster Perp order book snapshots fetched and synced")
            
        if multiplex_tasks:
            self.logger.info(f"Aster Perps Worker process running {len(multiplex_tasks)} feed task(s)...")
            await asyncio.gather(*multiplex_tasks)
            
    
    # ── Stage 3-5 setup helpers ────────────────────────────────────────────────────────────────────────────── # 

    def _collect_all_feeds(self, venue: Venue, asset_type: AssetType) -> dict:
        """Gather all registered kline feeds for a specific venue and asset type.
        
        Args:
            venue: The exchange venue (e.g., "binance", "aster")
            asset_type: The asset type (SPOT or FUTURES)
        
        Returns:
            Dictionary of symbol -> feed for the specified venue and asset type
        """
        venue_lower = venue.value.lower()
        
        # Determine which kline manager to use based on venue and asset type
        if venue_lower == Venue.BINANCE.value.lower():
            if asset_type == AssetType.SPOT:
                kline_manager_obj = self.binance_kline_manager
            else:
                kline_manager_obj = self.binance_futures_kline_manager
                
        elif venue_lower == Venue.ASTER.value.lower():
            if asset_type == AssetType.SPOT:
                kline_manager_obj = self.aster_kline_manager
            else:
                kline_manager_obj = self.aster_futures_kline_manager
        else:
            self.logger.warning(f"[Runner] Unknown venue: {venue}, returning empty feeds")
            return {}
        
        # Collect all active feeds
        all_feeds = {}
        if kline_manager_obj and hasattr(kline_manager_obj, '_active_feeds'):
            for sym, feed in kline_manager_obj._active_feeds.items():
                all_feeds[sym] = feed
        
        self.logger.debug(
            f"[Runner] Collected {len(all_feeds)} feeds for {venue} {asset_type.value}"
        )
        
        return all_feeds


    async def _setup_portfolio(self, asset_type: AssetType) -> None:
        """Initialize one thin Portfolio aggregator per venue.

        Portfolio is now a lightweight NAV monitor across ShadowBooks — no
        exchange sync, no event subscriptions.  One Portfolio is created per
        (asset_type, venue) pair; ShadowBooks are registered into it later
        via setup_strategy().
        """
        list_venues = (
            self.cfg.meta.spot_venues if asset_type == AssetType.SPOT
            else self.cfg.meta.futures_venues
        )
        for venue in list_venues:
            self._snapshot_interval_s = int(self.cfg.portfolio.get("snapshot_interval_s", 0))

            exchange_enum = None
            if venue.lower() == Venue.BINANCE.value.lower():
                exchange_enum = Venue.BINANCE
            elif venue.lower() == Venue.ASTER.value.lower():
                exchange_enum = Venue.ASTER

            portfolio = Portfolio(venue=exchange_enum, cfg=self.cfg)
            self._portfolio_dict[asset_type][exchange_enum] = portfolio
            self._start_snapshot_task(portfolio.save_snapshot, portfolio.name)
            self.logger.info(
                f"[Runner] {venue} {asset_type.value} Portfolio aggregator initialized"
            )

    def _setup_risk(self, portfolio: Portfolio, asset_type: AssetType, venue: Venue) -> None:
        """Create the PortfolioOptimizer using the effective RiskConfig for this (asset_type, venue).

        Effective risk priority (highest wins):
          strategy risk_overrides > venue_configs["{asset_type}_{venue}"] > global risk
        """
        risk_config = self.cfg.effective_risk_for(
            asset_type=asset_type.value.lower(),
            venue=venue.value.lower(),
        )
        self._optimizer_dict[asset_type][venue] = PortfolioOptimizer(
            risk_config=risk_config,
            portfolio=portfolio,
            cfg=self.cfg,
        )
        self.logger.info(f"[Runner] Risk optimizer configured for {asset_type.value} {venue.value}")



    def _setup_execution(self, asset_type: AssetType, venue: Venue) -> None:
        """Create the ExecutionEngine using the effective execution config for this (asset_type, venue).

        Effective execution priority (highest wins):
          strategy execution_overrides > venue_configs["{asset_type}_{venue}"] > global execution
        """
        all_feeds = self._collect_all_feeds(venue=venue, asset_type=asset_type)

        # Resolve execution config with venue-level overrides applied
        exec_cfg = self.cfg.effective_execution_for(
            asset_type=asset_type.value.lower(),
            venue=venue.value.lower(),
        )
        _ORDER_TYPE_MAP = {"MARKET": OrderType.MARKET, "LIMIT": OrderType.LIMIT}
        _VENUE_MAP = {v.value: v for v in Venue}

        ot_str = exec_cfg.get("default_order_type", "MARKET")
        default_order_type = _ORDER_TYPE_MAP.get(str(ot_str).upper(), OrderType.MARKET)

        dv_str = exec_cfg.get("default_venue", "Binance")
        default_venue = _VENUE_MAP.get(str(dv_str), Venue.BINANCE)

        self.logger.info(
            f"[Runner] Execution config for {asset_type.value} {venue.value}: "
            f"venue={default_venue.value}, order_type={default_order_type.value}"
        )

        self._execution_engine_dict[asset_type][venue] = ExecutionEngine(
            exchanges=self.exchange_connectors_dict.get(asset_type),
            portfolio=self._portfolio_dict.get(asset_type, {}).get(venue),
            feeds=all_feeds,
            default_venue=default_venue,
            default_order_type=default_order_type,
            cfg=self.cfg,
            asset_type=asset_type,
            venue=venue,
        )
        self.logger.info(
            f"[Runner] {asset_type.value} {venue.value} Execution engine configured: "
        )


    async def _setup_pipeline(self) -> None:
        """Wire the full Stage 3-5 pipeline: Portfolio -> Risk -> Execution, centrally for all strategies"""
        
        self.logger.info("[Runner] -- Setting up Stage 3-5 pipeline --")
        
        # Setup both the spot and futures portfolios
        for asset_type in [AssetType.SPOT, AssetType.PERPETUAL]:
            await self._setup_portfolio(asset_type)
            
        # Risk Optimizer instead of a strategy based optimizer per exchange/portfolio based,
        # Should take in the portfolio config risk config to do the risk 
        # Here portoflio dict is set up already
        self.logger.info(f"[Runner] Risk config: {self.cfg.risk}")
        for asset_type in self._portfolio_dict:
            for exchange_enum, portfolio in self._portfolio_dict.get(asset_type, {}).items():
                
                # Setup Risk Layer per Portfolio (which is per (asset_type, venue)) - Deprecated for now as we not yet implemented PortfolioLevelOptimzer only ShadowBook level optimizers, but we keep the code here for when we want to implement the PortfolioLevelOptimizer in the future
                # self._setup_risk(portfolio, asset_type, exchange_enum)

                # Set up execution layer as well, this is set by each asset and exchange_enum
                self._setup_execution(asset_type, exchange_enum)
                
                
    async def setup_strategy(self, strategy: SpotStrategy, strategy_id: int, strategy_name: str):
        '''
        We wire Steps 3-5 of the pipeline into the strategy as per required of the strategy
        Wire in the portfolio, risk, executor, shadow book, and aggregator.

        Constraints:
        - Strategy can only have live in one exchange and asset_type

        Parameters
        ----------
        allocation:
            Capital allocation fraction for this strategy [0, 1].
            When running a single strategy, use 1.0 (default).
            For multi-strategy, each strategy's allocation should sum to <= 1.0
            across all strategies sharing the same (asset_type, venue) pipeline.
        '''
        # ── Sub-account mode branch ──────────────────────────────────────────
        strat_cfg = self.cfg.strategies.get(strategy_id)
        self.logger.info(
            f"[Runner] Setting up Sub-account mode for strategy {strategy_id} ({strategy_name})"
        )
        asset_type = AssetType[strat_cfg.asset_type.upper()]
        venue = Venue[strat_cfg.venue.upper()]

        # 1. Per-strategy exchange + private event bus
        private_exchange, private_bus = await self._create_sub_account_exchange(strat_cfg, venue)
        self._sub_account_exchange_dict[strategy_id] = private_exchange

        # 2. Shared market data feeds (may be empty here; self-correct on first KLINE)
        feeds = self._collect_all_feeds(venue, asset_type)

        # 3. ShadowBook as real ledger (owns 100% of sub-account capital)
        shadow_book = ShadowBook(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            venue=venue,
        )
        shadow_book.sync_from_exchange(private_exchange, feeds)
        shadow_book.start(
            self.shared_event_bus,
            private_event_bus=private_bus,
        )

        # 4. Per-strategy risk optimizer
        risk_cfg = self.cfg.effective_risk_for(
            asset_type=strat_cfg.asset_type.lower(),
            venue=strat_cfg.venue.lower(),
            strategy_id=strategy_id,
        )
        optimizer = PortfolioOptimizer(risk_config=risk_cfg, portfolio=shadow_book, cfg=self.cfg)

        # 5. Per-strategy execution engine
        execution_engine = ExecutionEngine(
            exchanges={venue: private_exchange},
            portfolio=shadow_book,
            feeds=feeds,
            default_venue=venue,
            cfg=self.cfg,
            asset_type=asset_type,
            venue=venue,
        )

        # 6. Wire into strategy
        strategy.strategy_id = strategy_id
        strategy.strategy_name = strategy_name
        strategy._shadow_book = shadow_book
        strategy.portfolio = shadow_book      # backward compat: satisfies duck-type interface
        strategy._optimizer = optimizer
        strategy._execution_engine = execution_engine
        strategy._shared_event_bus = self.shared_event_bus
        strategy._private_event_bus = private_bus
        strategy._exchanges = {venue: private_exchange}
        strategy.cfg = self.cfg
        strategy.strategy_config = strat_cfg

        # 7. Register shadow book with aggregate portfolio for monitoring
        if asset_type in self._portfolio_dict and venue in self._portfolio_dict[asset_type]:
            self._portfolio_dict[asset_type][venue].register_shadow_book(shadow_book)

        # 8. Start strategy
        await strategy.start()

        # 9. Periodic snapshot
        self._start_snapshot_task(shadow_book.save_snapshot, f"shadow_book_{strategy_id}")
        self._strategy_dict[strategy_id] = strategy

        self.logger.info(
            f"[Runner] Sub-account wiring complete: {strategy.__class__.__name__} "
            f"(id={strategy_id}) -> ShadowBook (real ledger) -> Optimizer -> ExecutionEngine"
        )
        return
        
    def _has_sub_account(self, strategy_config) -> bool:
        """Return True if the strategy config has dedicated sub-account credentials."""
        return bool(getattr(strategy_config, 'sub_account_api_key', ''))
    

    async def _create_sub_account_exchange(self, strategy_config: StrategyConfig, 
                                                 venue: Venue, 
                                                 asset_type: AssetType = AssetType.SPOT):
        """Create a per-strategy exchange connector with its own API keys and private EventBus.

        Returns (exchange, private_bus). The private bus receives only this strategy's
        user data events (ORDER_UPDATE, BALANCE_UPDATE). Market data (KLINE) continues
        to flow through the shared EventBus via the shared kline/ob managers.
        """
        
        api_key = strategy_config.sub_account_api_key
        api_secret = strategy_config.sub_account_api_secret

        # Private event bus: receives only this strategy's user data events
        private_bus = EventBus()
        exchange_connector_cls = self._exchange_connector_callables.get((asset_type, venue))
        exchange = exchange_connector_cls(
            args=self.cfg,
            api_key=api_key,
            api_secret=api_secret,
            symbols=strategy_config.symbols,
            kline_manager=self.binance_kline_manager,    # shared, unauthenticated
            ob_manager=self.binance_ob_manager,          # shared, unauthenticated
            event_bus=private_bus,                       # private bus for account events
        )
        await exchange.run()
        self.logger.info(
            f"[Runner] Sub-account exchange created for strategy {strategy_config.strategy_id} "
            f"({venue.value})"
        )
        return exchange, private_bus

    def _start_snapshot_task(self, callable: Callable, tag: str) -> None:
        """Start a periodic portfolio snapshot saver using PeriodicTask.

        Replaces the old ``while True`` snapshot loop with a clean
        start/stop lifecycle.
        """
        interval = self._snapshot_interval_s
        if interval <= 0:
            return

        self._snapshot_task_dict[tag] = PeriodicTask(
            callback=callable,
            interval_s=interval,
            name=f"{tag} Snapshot Task",
        )
        self._snapshot_task_dict[tag].start()
        self.logger.info(f"[Runner] {tag} snapshot task started (every {interval}s)")


#############################################################################################################################################################
# # ────────────────────────────────────────────────────────────── Main entry point ──────────────────────────────────────────────────────
#############################################################################################################################################################  
  
    @property
    def state(self) -> RunnerState:
        """Current runner lifecycle state."""
        return self._state

    async def run(self, **kwargs) -> None:
        """Run the complete strategy in a single event loop.

        All feeds, exchange connectors and the optional *strategy* share one
        asyncio event loop.  Heavy compute is offloaded via
        ``strategy.run_heavy()``.
        """
        
        # Premake the Strategies, ids and strategy_names
        strategies, strategy_ids, strategy_names = [], [], []
        for strategy_id, strat_cfg in self.cfg.strategies.items():
            strategy_name = strat_cfg.strategy_name
            strategy_ids.append(strategy_id)
            strategy_names.append(strategy_name)
            strategy = load_strategy_class(strat_cfg.class_name)(strategy_name=strategy_name, strategy_id=strategy_id)
            strategies.append(strategy)
        
        try:
            self.logger.info("[Runner] ----------------------------------------------------------------------------------------")
            self.logger.info("[Runner] Starting strategy runner................................")
            self.logger.info("[Runner] ----------------------------------------------------------------------------------------")
            # ── CONFIGURING ──
            self._state = RunnerState.CONFIGURING
            self._setup_config()

            # Create shared event bus BEFORE exchanges so it can be injected
            self.shared_event_bus = EventBus() # EventBus for all Klines and OBs
            self.logger.info("[Runner] EventBus created")

            # ── CONNECTING ──
            self._state = RunnerState.CONNECTING
            self._setup_exchanges()

            # Inject event bus into client managers, we can manually adjust here for Aster + Binance 
            self.logger.info("[Runner] EventBus injected into Binance kline + OB client managers")

            # Run exchange + feeds in single event loop
            try:
                    
                # We can do async gather run all exchanges here 
                for asset_type, venues in self.exchange_connectors_dict.items():
                    for venue, exchange in venues.items():
                        if exchange:
                            self.logger.info(f"  - {asset_type.value} | {venue.value}: {exchange.__class__.__name__} initializing run()")
                            await exchange.run()
                        else:
                            self.logger.debug(f" - {asset_type.value} | {venue.value}: Not initialized")
        
                 # ── SYNCING ──
                self._state = RunnerState.SYNCING
                
                # We setup the Pipeline for all exchange and asset_type pairing
                await self._setup_pipeline()
                    
                self.logger.info(f'Initialising wiring of components for all strategies. Strategies loaded: {list(self.cfg.strategies.keys())}')
                await asyncio.gather(*[
                    self.setup_strategy(strategy, strategy_id, strategy_name)
                    for strategy, strategy_id, strategy_name, 
                    in zip(strategies, strategy_ids, strategy_names)
                ])
                self.logger.info("[Runner] All components and strategies initialized. Starting event loops...")

                # Initializing of DataFeeds
                coros = [self._setup_binance_data_feeds()]
                
                self.logger.info(f"[Runner] Launching strategy.run_forever() alongside data feeds")
                for strategy in strategies:
                    coros.append(strategy.run_forever())
                        
                # ── RUNNING ──
                self._state = RunnerState.RUNNING
                self.logger.info(f"[Runner] Entering main event loop ({len(coros)} coroutines)")
                await asyncio.gather(*coros)

            finally:
                # ── STOPPING ──
                self._state = RunnerState.STOPPING
                self.logger.info("[Runner] Shutting down exchange and strategy...")

                for _, task in self._snapshot_task_dict.items():
                    task.stop()

                # Clean up all exchange connectors, can make adjustments here next time
                # for asset_type, venues in self.exchange_connectors_dict.items():
                #     for venue, exchange in venues.items():
                #         if exchange:
                #             self.logger.info(f"Stopping exchange c onnector for {asset_type.value} {venue.value}...")
                #             try:
                #                 await exchange.stop()
                #             except Exception as e:
                #                 self.logger.error(f"Error stopping exchange connector for {asset_type.value} {venue.value}: {e}", exc_info=True)
                                
                await asyncio.gather(*[strategy.stop() for strategy in strategies])

        except asyncio.CancelledError:
            self.logger.warning("[Runner] Strategy cancelled by user")
        except Exception as e:
            self._state = RunnerState.FAILED
            self.logger.error(f"[Runner] Fatal error: {e}", exc_info=True)
            raise
        finally:
            self.logger.info("Cleaning up...")
            await self._cleanup([])
            if self._state != RunnerState.FAILED:
                self._state = RunnerState.STOPPED
            self.logger.info(f"Strategy runner shutdown complete (state={self._state.name})")
    

    
#############################################################################################################################################################
######## Testing functions for individual components in isolation   #########################################################################################
#############################################################################################################################################################
async def cleanup(tasks_groups):
    """Cleanup function to properly shutdown all tasks"""
    for tasks in tasks_groups:
        for task in tasks:
            if hasattr(task, 'stop'):
                await task.stop()
            elif isinstance(task, asyncio.Task):
                task.cancel()
    
    for tasks in tasks_groups:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)




    
async def run_test_strategy(
    trading_config_yaml: str = 'elysian_core/config/trading_config.yaml',
    strategy_config_yamls: Optional[List[str]] = None,
):
    """Run the EventDrivenStrategy through the full pipeline.

    Creates a StrategyRunner from the two-tier config layout and runs a single
    EventDrivenStrategy.  Strategy parameters (id, name, allocation,
    rebalance_interval_s) are loaded from the strategy YAML rather than
    hardcoded here.

    Parameters
    ----------
    trading_config_yaml:
        Path to the system-level config YAML.
    strategy_config_yamls:
        Per-strategy YAML file paths.  Defaults to the bundled example strategy.
    """
    runner = StrategyRunner(
        trading_config_yaml=trading_config_yaml,
        strategy_config_yamls=strategy_config_yamls,
    )
    await runner.run()


if __name__ == "__main__":

    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    logger.info("Running in TEST-STRATEGY mode")
    
    trading_config_yaml = 'elysian_core/config/trading_config.yaml'
    strategy_config_yamls = [
        'elysian_core/config/strategies/strategy_000_event_driven.yaml',
        'elysian_core/config/strategies/strategy_001_test_print.yaml'
    ]
    asyncio.run(run_test_strategy(
        trading_config_yaml=trading_config_yaml,
        strategy_config_yamls=strategy_config_yamls,
    ))
