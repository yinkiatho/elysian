import sys
from pathlib import Path
    
import aiohttp
import asyncio
# Add parent directory to path so imports work when running this script directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from typing import Dict, Any, List
import asyncio
import os
import datetime 
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
from elysian_core.config.app_config import AppConfig, load_app_config
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.weight_aggregator import WeightAggregator
from elysian_core.core.shadow_book import ShadowBook

import sys
    
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
logger = setup_custom_logger('root')


class StrategyRunner:
    """Main strategy runner class that orchestrates all trading components."""
    
    def __init__(self, config_yaml: str = 'elysian_core/config/config.yaml',
                       config_json: str = 'elysian_core/config/config.json'):
        """Initialize the strategy runner with configuration."""

        logger.info(f'Kickstarted StrategyRunner with config: {config_yaml} and {config_json} at PWD: {os.getcwd()}')

        env_path = os.path.join(os.getcwd(), '.env')
        self.cfg = load_app_config(
            yaml_path=config_yaml,
            json_path=config_json,
            env_path=env_path,
        )
        logger.info(f"AppConfig loaded: {self.cfg.meta.version_name} / {self.cfg.meta.strategy_name}")

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

        # Managing WeightAggregators — one per (asset_type, venue) for multi-strategy coordination
        self._aggregator_dict: Dict[AssetType, Dict[Venue, WeightAggregator]] = {AssetType.SPOT: {},
                                                                                  AssetType.PERPETUAL: {}}

        self._exchange_connector_mapping: Dict[AssetType, Dict[Venue, Any]] = {AssetType.SPOT: {},
                                                                               AssetType.PERPETUAL: {}}
    
    
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
        logger.info("Starting cleanup...")
        
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
        logger.info("Cleanup completed")
        
    
    # --------------------------- SETUP FUNCTIONS START HERE  --------------------------- #
    def _setup_config(self) -> None:
        """Initialize tokens and pools configuration from AppConfig.symbols."""
        logger.info("Setting up Strategy Configuration...")

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
        logger.info(f"Configuration setup completed with {len(self.total_token_pairs)} total token pairs: {self.total_token_pairs}")        
        
        
    def _setup_exchanges(self) -> None:
        """Initialize exchange instances for trading operations."""

        logger.info("Setting up exchange instances...")
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
            logger.info("Binance futures exchange initialized")

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
            logger.info("Aster Spot Exchange initialized")
            
            
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
        logger.info("Exchange mapping completed:")
        for asset_type, venues in self.exchange_connectors_dict.items():
            for venue, exchange in venues.items():
                if exchange:
                    logger.info(f"  - {asset_type.value} | {venue.value}: {exchange.__class__.__name__}")
                else:
                    logger.debug(f"  - {asset_type.value} | {venue.value}: Not initialized")
        
        
    # --------- Setting up Binance Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_binance_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        logger.info("Setting up Binance data feeds with multiplex architecture...")
        
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
        
        logger.info(f"Initialized {len(self.binance_kline_manager._active_feeds)} Binance kline feeds and {len(self.binance_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.binance_kline_manager.start()
        await self.binance_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.binance_kline_manager._active_feeds:
            logger.info(f"Starting Binance Kline multiplex feeds ({len(self.binance_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_kline_manager.run_multiplex_feeds()))
        
        if self.binance_ob_manager._active_feeds:
            logger.info(f"Starting Binance Order Book multiplex feeds ({len(self.binance_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(10.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Binance order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            logger.info(f"All Binance order book snapshots fetched and synced")
            
        if multiplex_tasks:
            logger.info(f"Worker process running {len(multiplex_tasks)} feed task(s)...")
            await asyncio.gather(*multiplex_tasks)
            
    
# --------- Setting up Binance Futures Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_binance_futures_data_feeds(self) -> None:
        """Initialize all futures data feeds with multiplex architecture"""
        
        logger.info("Setting up Binance futures data feeds with multiplex architecture...")
        
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
        
        logger.info(f"Initialized {len(self.binance_futures_kline_manager._active_feeds)} Binance futures kline feeds and {len(self.binance_futures_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.binance_futures_kline_manager.start()
        await self.binance_futures_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.binance_futures_kline_manager._active_feeds:
            logger.info(f"Starting Binance Futures Kline multiplex feeds ({len(self.binance_futures_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_futures_kline_manager.run_multiplex_feeds()))
        
        if self.binance_futures_ob_manager._active_feeds:
            logger.info(f"Starting Binance Futures Order Book multiplex feeds ({len(self.binance_futures_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.binance_futures_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(5.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Binance futures order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            logger.info(f"All Binance futures order book snapshots fetched and synced")
            
        if multiplex_tasks:
            logger.info(f"Worker process running {len(multiplex_tasks)} futures feed task(s)...")
            await asyncio.gather(*multiplex_tasks)
            
            




# --------- Setting up Aster Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_aster_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        logger.info("Setting up data feeds with multiplex architecture...")
        
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
        
        logger.info(f"Initialized {len(self.aster_kline_manager._active_feeds)} Aster kline feeds and {len(self.aster_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.aster_kline_manager.start()
        await self.aster_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.aster_kline_manager._active_feeds:
            logger.info(f"Starting Aster Kline multiplex feeds ({len(self.aster_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_kline_manager.run_multiplex_feeds()))
        
        if self.aster_ob_manager._active_feeds:
            logger.info(f"Starting Aster Order Book multiplex feeds ({len(self.aster_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(10.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Aster order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            logger.info(f"All Aster order book snapshots fetched and synced")
            
        if multiplex_tasks:
            logger.info(f"Worker process running {len(multiplex_tasks)} feed task(s)...")
            await asyncio.gather(*multiplex_tasks)
            

        
    # --------- Setting up Aster Futures Kline and OB Feeds with Multiplex Architecture --------- # 
    async def _setup_aster_futures_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        logger.info("Setting up data feeds with multiplex architecture...")
        
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
        
        logger.info(f"Initialized {len(self.aster_futures_kline_manager._active_feeds)} Aster kline feeds and {len(self.aster_futures_ob_manager._active_feeds)} order book feeds")
        
        # Start the multiplex managers (begins WebSocket connection and buffering)
        await self.aster_futures_kline_manager.start()
        await self.aster_futures_ob_manager.start()
        
        # Start multiplex reader/worker tasks (WebSocket now connecting and buffering events)
        multiplex_tasks = []
        if self.aster_futures_kline_manager._active_feeds:
            logger.info(f"Starting Aster Futures Kline multiplex feeds ({len(self.aster_futures_kline_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_futures_kline_manager.run_multiplex_feeds()))
        
        if self.aster_futures_ob_manager._active_feeds:
            logger.info(f"Starting Aster Futures Order Book multiplex feeds ({len(self.aster_futures_ob_manager._active_feeds)} feeds)")
            multiplex_tasks.append(asyncio.create_task(self.aster_futures_ob_manager.run_multiplex_feeds()))
        
        # Give WebSocket a moment to connect and start buffering events
        await asyncio.sleep(10.0)
        
        # NOW fetch snapshots on all order book feeds
        # This will trigger _process_buffered_events() which finds the sync point
        if ob_feeds:
            logger.info(f"Fetching initial snapshots for {len(ob_feeds)} Aster Perp order book feeds...")
            snapshot_tasks = [feed.get_initial_snapshot() for feed in ob_feeds]
            await asyncio.gather(*snapshot_tasks)
            logger.info(f"All Aster Perp order book snapshots fetched and synced")
            
        if multiplex_tasks:
            logger.info(f"Aster Perps Worker process running {len(multiplex_tasks)} feed task(s)...")
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
            logger.warning(f"[Runner] Unknown venue: {venue}, returning empty feeds")
            return {}
        
        # Collect all active feeds
        all_feeds = {}
        if kline_manager_obj and hasattr(kline_manager_obj, '_active_feeds'):
            for sym, feed in kline_manager_obj._active_feeds.items():
                all_feeds[sym] = feed
        
        logger.debug(
            f"[Runner] Collected {len(all_feeds)} feeds for {venue} {asset_type.value}"
        )
        
        return all_feeds


    async def _setup_portfolio(self, asset_type: AssetType) -> None:
        """Initialize the strategy's portfolio with full exchange state sync.

        Reads ``self.cfg.portfolio.*`` for configuration (max_history,
        snapshot_interval_s).  Constructs a fresh Portfolio, then syncs cash,
        positions, mark prices, and open orders from BinanceSpotExchange.
        
        Portfolio should not be reliant on a sepecific strategy ie. multiple strategies can rely on one portfolio in one exchange.
        We set up a portfolio for each single venue there is
        """
        list_venues = self.cfg.symbols.venues.spot if asset_type == AssetType.SPOT else self.cfg.symbols.venues.futures
        for venue in list_venues:
            # venue here is a string , which value comes from Venue.ENUM
            
            # max_history and snapshot interval should be consistent throughout all portfolios
            max_history = int(self.cfg.portfolio.get("max_history", 10_000))
            self._snapshot_interval_s = int(self.cfg.portfolio.get("snapshot_interval_s", 0)) 
            
            
            logger.info(f"[Runner] Loading {venue} {asset_type.value} Portfolio config: max_history={max_history}, snapshot_interval={self._snapshot_interval_s}s")
            
            # Full sync: cash + positions + mark prices + open orders
            if venue.lower() == Venue.BINANCE.value.lower():
                exchange_enum = Venue.BINANCE
                if asset_type == AssetType.SPOT:
                    exchange = self.binance_exchange
                else:
                    exchange = self.binance_futures_exchange
                
            elif venue.lower() == Venue.ASTER.value.lower():
                exchange_enum = Venue.ASTER
                if asset_type == AssetType.SPOT:
                    exchange = self.aster_exchange
                else:
                    exchange = self.aster_futures_exchange

            all_feeds = self._collect_all_feeds(venue=exchange_enum, asset_type=asset_type)
            
            # Initialize the Portfolio Object and start it
            portfolio = Portfolio(
                    exchange=exchange,
                    max_history=max_history,
                    cfg=self.cfg,
                    venue=exchange_enum
            )
            portfolio.sync_from_exchange(all_feeds)
            
            # Start event-driven portfolio (auto-updates weights, mark prices, cash)
            portfolio.start(self.event_bus)
            self._portfolio_dict[asset_type][exchange_enum] = portfolio
            
            logger.info(
                f"[Runner] {venue} {asset_type.value} Portfolio initialized: cash={portfolio.cash:.2f}, "
                f"{len(portfolio.active_positions)} positions, "
                f"{len(all_feeds)} feeds wired"
            )

    def _setup_risk(self, portfolio: Portfolio, asset_type: AssetType, venue: Venue) -> None:
        """Create the PortfolioOptimizer using the typed RiskConfig from AppConfig. 
        
        Current Structure:
        - Each PortfolioOptimzer will wrap around each portfolio and handle its optimization 
          , essentially a package"""
          
        risk_config = self.cfg.risk
        
        self._optimizer_dict[asset_type][venue] = PortfolioOptimizer(
                                                        risk_config=risk_config,
                                                        portfolio=portfolio,
                                                        cfg=self.cfg,
                                                    )
        #strategy._optimizer = self.optimizer
        logger.info(f"[Runner] Risk optimizer configured for Portfolio of {asset_type} and {venue}")



    def _setup_execution(self, asset_type: AssetType, venue: Venue) -> None:
        
        """Create the ExecutionEngine and inject it into the strategy."""
        all_feeds = self._collect_all_feeds(venue=venue, asset_type=asset_type)
        optimizer = self._optimizer_dict.get(asset_type, {}).get(venue)
        risk_config = optimizer.config

        # Read execution config from cfg.execution (DictConfig)
        _ORDER_TYPE_MAP = {"MARKET": OrderType.MARKET, "LIMIT": OrderType.LIMIT}
        _VENUE_MAP = {v.value: v for v in Venue}

        ot_str = self.cfg.execution.get("default_order_type", "MARKET")
        default_order_type = _ORDER_TYPE_MAP.get(str(ot_str).upper(), OrderType.MARKET)

        dv_str = self.cfg.execution.get("default_venue", "Binance")
        default_venue = _VENUE_MAP.get(str(dv_str), Venue.BINANCE)

        logger.info(f"[Runner] Execution config: venue={default_venue.value}, order_type={default_order_type.value}")

        self._execution_engine_dict[asset_type][venue] = ExecutionEngine(
                                                            exchanges=self._exchange_connector_mapping.get(asset_type), # Get the dict of venue to exchange, leave as it is first
                                                            portfolio=self._portfolio_dict.get(asset_type, {}).get(venue),
                                                            feeds=all_feeds,
                                                            risk_config=risk_config,
                                                            default_venue=default_venue,
                                                            default_order_type=default_order_type,
                                                            cfg=self.cfg,
                                                            asset_type= asset_type,
                                                            venue=venue
                                                        )
        logger.info(
            f"[Runner] {asset_type} {venue} Execution engine configured: "
            f"min_weight_delta={risk_config.min_weight_delta}"
        )


    async def _setup_pipeline(self) -> None:
        """Wire the full Stage 3-5 pipeline: Portfolio -> Risk -> Execution, centrally for all strategies"""
        
        logger.info("[Runner] ── Setting up Stage 3-5 pipeline ──")
        
        # Setup both the spot and futures portfolios
        for asset_type in [AssetType.SPOT, AssetType.PERPETUAL]:
            await self._setup_portfolio(asset_type)
            
        # Risk Optimizer instead of a strategy based optimizer per exchange/portfolio based,
        # Should take in the portfolio config risk config to do the risk 
        # Here portoflio dict is set up already
        logger.info(f"[Runner] Risk config: {self.cfg.risk}")
        for asset_type in self._portfolio_dict:
            for exchange_enum, portfolio in self._portfolio_dict.get(asset_type, {}).items():
                self._setup_risk(portfolio, asset_type, exchange_enum)

                # Set up execution layer as well, this is set by each asset and exchange_enum
                self._setup_execution(asset_type, exchange_enum)

                # Create a WeightAggregator per (asset_type, venue) for multi-strategy coordination
                self._aggregator_dict[asset_type][exchange_enum] = WeightAggregator()
                logger.info(f"[Runner] WeightAggregator created for {asset_type.value} {exchange_enum.value}")
                
                
    async def setup_strategy(self, strategy: SpotStrategy, strategy_id: int, strategy_name: str,
                             allocation: float = 1.0):
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
        logger.info(f"[Runner] Wiring Stage 3-5 + event_bus and exchanges into strategy: {strategy.__class__.__name__}")
        strat_venue, strat_asset_type = strategy.venue, strategy.asset_type

        portfolio = self._portfolio_dict.get(strat_asset_type, {}).get(strat_venue)
        strategy.portfolio = portfolio
        strategy._optimizer = self._optimizer_dict.get(strat_asset_type, {}).get(strat_venue)
        strategy._execution_engine = self._execution_engine_dict.get(strat_asset_type, {}).get(strat_venue)

        strategy._event_bus = self.event_bus
        strategy._exchanges = self.exchange_connectors_dict.get(strategy.asset_type)
        strategy.cfg = self.cfg
        strategy.strategy_id = strategy_id
        strategy.name = strategy_name

        # ── Create ShadowBook for this strategy ─────────────────────────
        shadow_book = ShadowBook(
            strategy_id=strategy_id,
            allocation=allocation,
            venue=strat_venue,
        )
        # Initialize shadow book: allocate cash proportionally, distribute
        # existing positions if the portfolio already has some (restart case)
        if portfolio is not None:
            if portfolio.active_positions:
                shadow_book.init_from_existing(
                    portfolio_cash=portfolio.cash,
                    portfolio_positions=portfolio.positions,
                    mark_prices=portfolio.mark_prices,
                )
            else:
                shadow_book.init_from_portfolio_cash(
                    portfolio_cash=portfolio.cash,
                    mark_prices=portfolio.mark_prices,
                )
        strategy._shadow_book = shadow_book

        # ── Register with WeightAggregator ──────────────────────────────
        aggregator = self._aggregator_dict.get(strat_asset_type, {}).get(strat_venue)
        if aggregator is not None:
            strategy_symbols = set(getattr(strategy, '_symbols', set()))
            aggregator.register_strategy(
                strategy_id=strategy_id,
                allocation=allocation,
                symbols=strategy_symbols,
                shadow_book=shadow_book,
            )

        # ── Start strategy (creates RebalanceFSM with aggregator) ───────
        await strategy.start(aggregator=aggregator)

        # Apply strategy-level config (max_heavy_workers)
        mhw = self.cfg.strategy.get("max_heavy_workers")
        if mhw is not None:
            strategy._executor.shutdown(wait=False)
            strategy._executor = ProcessPoolExecutor(max_workers=int(mhw))
            logger.info(f"[Runner] strategy.max_heavy_workers = {int(mhw)}")

        logger.info(
            f"[Runner] Wiring complete: {strategy.__class__.__name__} "
            f"(id={strategy_id}, alloc={allocation:.0%}) -> "
            f"ShadowBook -> Aggregator -> Optimizer -> ExecutionEngine"
        )

    def _start_snapshot_task(self, strategy: SpotStrategy) -> None:
        """Start a periodic portfolio snapshot saver using PeriodicTask.

        Replaces the old ``while True`` snapshot loop with a clean
        start/stop lifecycle.
        """
        interval = self._snapshot_interval_s
        if interval <= 0:
            return

        def _save():
            try:
                #strategy.portfolio.save_snapshot()
                strategy._shadow_book.save_snapshot()
                logger.info(f"[Runner] Strategy {strategy.strategy_id} Shadow Book snapshot saved")
            except Exception as e:
                logger.error(f"[Runner] Snapshot save failed: {e}", exc_info=True)

        self._snapshot_task_dict[strategy.strategy_id] = PeriodicTask(
            callback=_save,
            interval_s=interval,
            name=f"Strategy {strategy.strategy_id} ShadowBookSnapshot",
        )
        self._snapshot_task_dict[strategy.strategy_id].start()
        logger.info(f"[Runner] Shadow Book snapshot task started (every {interval}s) for {strategy}")


#############################################################################################################################################################
# # ────────────────────────────────────────────────────────────── Main entry point ──────────────────────────────────────────────────────
#############################################################################################################################################################  
  
    @property
    def state(self) -> RunnerState:
        """Current runner lifecycle state."""
        return self._state

    async def run(self, strategies: List[SpotStrategy] = [],
                        strategy_ids: List[int] = [],
                        strategy_names: List[str] = [],
                        allocations: List[float] = []) -> None:
        """Run the complete strategy in a single event loop.

        All feeds, exchange connectors and the optional *strategy* share one
        asyncio event loop.  Heavy compute is offloaded via
        ``strategy.run_heavy()``.
        """
        try:
            logger.info("[Runner] ════════════════════════════════════════════════════════════════════════════════════════")
            logger.info("[Runner] Starting strategy runner................................")
            logger.info("[Runner] ════════════════════════════════════════════════════════════════════════════════════════")

            # ── CONFIGURING ──
            self._state = RunnerState.CONFIGURING
            self._setup_config()

            # Create shared event bus BEFORE exchanges so it can be injected
            self.event_bus = EventBus()
            logger.info("[Runner] EventBus created")

            # ── CONNECTING ──
            self._state = RunnerState.CONNECTING
            self._setup_exchanges()

            # Inject event bus into client managers, we can manually adjust here for Aster + Binance 
            self.binance_kline_manager.set_event_bus(self.event_bus)
            self.binance_ob_manager.set_event_bus(self.event_bus)
            logger.info("[Runner] EventBus injected into Binance kline + OB client managers")

            # Run exchange + feeds in single event loop
            try:
                # Initialize the exchange first so its AsyncClient and
                    # get_exchange_info() call don't compete with the kline/OB
                    # managers creating their own AsyncClients concurrently.
                    
                # We can do async gather run all exchanges here 
                logger.info("[Runner] Binance exchange initialized")
                await self.binance_exchange.run()
                logger.info("[Runner] Binance exchange running")
                
                 # ── SYNCING ──
                self._state = RunnerState.SYNCING
                
                # We setup the Pipeline for all exchange and asset_type pairing
                await self._setup_pipeline()
                    
                # Wire runner's event bus and exchanges into strategy, then start
                # Default allocations: equal split if not provided
                if not allocations:
                    n = len(strategies)
                    allocations = [1.0 / n] * n if n > 0 else []

                logger.info(f'Initialising wiring of components for all strategies')
                await asyncio.gather(*[
                    self.setup_strategy(strategy, strategy_id, strategy_name, allocation)
                    for strategy, strategy_id, strategy_name, allocation
                    in zip(strategies, strategy_ids, strategy_names, allocations)
                ])
                logger.info("[Runner] All components and strategies initialized. Starting event loops...")

            
                # Start all event loops for all strategies and setting up of feeds
                # _setup_binance_data_feeds blocks forever (runs multiplex
                # feed tasks).  If the strategy has its own long-running
                # coroutine (e.g. periodic rebalance timer), gather them so
                # both run concurrently in the same event loop.
                    
                coros = [self._setup_binance_data_feeds()]
                logger.info(f"[Runner] Launching strategy.run_forever() alongside data feeds")
                for strategy in strategies:
                    coros.append(strategy.run_forever())

                    # Periodic portfolio snapshot via PeriodicTask (no while-True loop)
                    if self._snapshot_interval_s > 0:
                        self._start_snapshot_task(strategy)
                
                # Add the snapshot task for Portfolio
                self


                # ── RUNNING ──
                self._state = RunnerState.RUNNING
                logger.info(f"[Runner] Entering main event loop ({len(coros)} coroutines)")
                await asyncio.gather(*coros)

            finally:
                # ── STOPPING ──
                self._state = RunnerState.STOPPING
                logger.info("[Runner] Shutting down exchange and strategy...")

                for _, task in self._snapshot_task_dict.items():
                    task.stop()

                # Clean up all exchange connectors, can make adjustments here next time
                await self.binance_exchange.cleanup()                
                await asyncio.gather(*[strategy.stop() for strategy in strategies])

        except asyncio.CancelledError:
            logger.warning("[Runner] Strategy cancelled by user")
        except Exception as e:
            self._state = RunnerState.FAILED
            logger.error(f"[Runner] Fatal error: {e}", exc_info=True)
            raise
        finally:
            logger.info("Cleaning up...")
            await self._cleanup([])
            if self._state != RunnerState.FAILED:
                self._state = RunnerState.STOPPED
            logger.info(f"Strategy runner shutdown complete (state={self._state.name})")
    

    
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


async def run_test_strategy():
    """Run the EventDrivenStrategy through the full pipeline.

    Creates a StrategyRunner, loads config, and runs a single
    EventDrivenStrategy with allocation=1.0 on Binance Spot.
    """
    from elysian_core.strategy.example_weight_strategy_v2_event_driven import EventDrivenStrategy

    runner = StrategyRunner()

    # Create strategy — symbols will be loaded from cfg in on_start()
    strategy = EventDrivenStrategy(
        exchanges={},
        event_bus=EventBus(),
        rebalance_interval_s=60.0,
        asset_type=AssetType.SPOT,
        venue=Venue.BINANCE,
    )

    await runner.run(
        strategies=[strategy],
        strategy_ids=[1],
        strategy_names=["event_driven_momentum"],
        allocations=[1.0],
    )


if __name__ == "__main__":

    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    logger.info("Running in TEST-STRATEGY mode")
    asyncio.run(run_test_strategy())
