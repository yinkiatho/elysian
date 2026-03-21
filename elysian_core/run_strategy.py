import sys
from pathlib import Path
    
import aiohttp
import asyncio
# Add parent directory to path so imports work when running this script directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
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
from elysian_core.connectors.AsterPerpExchange import AsterPerpExchange
    

# from elysian_core.connectors.VolatilityBarbClient import VolatilityBarbClientLocal, RedisConfig
from elysian_core.utils.logger import setup_custom_logger
from elysian_core.utils.utils import load_config, replace_placeholders, config_to_args
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor
from elysian_core.core.event_bus import EventBus
from elysian_core.core.enums import Venue
from elysian_core.core.events import EventType, BalanceUpdateEvent
from elysian_core.execution.engine import ExecutionEngine
from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.risk.risk_config import RiskConfig
from elysian_core.strategy.base_strategy import SpotStrategy

import sys


# Strategy Imports
from elysian_core.strategy.test_strategy import TestPrintStrategy
    
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
logger = setup_custom_logger('root')


class StrategyRunner:
    """Main strategy runner class that orchestrates all trading components."""
    
    def __init__(self, config_yaml: str = 'elysian_core/config/config.yaml', 
                       config_json: str = 'elysian_core/config/config.json'):
        
        
        """Initialize the strategy runner with configuration."""
        
        logger.info(f'Kickstarted StrategyRunner with config: {config_yaml} and {config_json} at PWD: {os.getcwd()}')
        
        # Load .env file if it exists
        env_path = os.path.join(os.getcwd(), '.env')
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path, verbose=True)
            logger.info(f"Loaded environment variables from {env_path}")
        else:
            logger.warning(f".env file not found at {env_path}. Using system environment variables.")
            load_dotenv()
                    
        self.config_json = self._load_config(config_json)
        self.args = config_to_args(load_config(config_yaml))
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



    @staticmethod
    def _load_config(config_path: str) -> dict:
        """Load configuration from JSON file."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(config_path, 'r') as f:
            return json.load(f)
    
    
    
    @staticmethod
    def _get_env(key: str, default: str = None, required: bool = False) -> str:
        """Get environment variable with optional default and required check."""
        value = os.getenv(key, default)
        if required and not value:
            logger.warning(f"Required environment variable '{key}' not set")
        return value
    
    
    
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
        
        
    # --------- CORE FUNCTIONS STARTS HERE --------- #
    def _setup_config(self) -> None:
        
        """Initialize tokens and pools configuration. Testing, we just add the Binance Tokens"""
        logger.info("Setting up Strategy Configuration...")
        
        
        # Setting up Binance tokens 
        self.binance_tokens = self.config_json.get('Spot Trading Pairs', {}).get('Binance Pairs', [])
        self.binance_token_symbols = self.config_json.get('Spot Trading Pairs', {}).get('Binance Symbols', [])
        self.total_token_pairs.extend(self.binance_tokens)
        
        
        # Setting up Binance Futures tokens
        self.binance_futures_tokens = self.config_json.get('Futures Trading Pairs', {}).get('Binance Futures Pairs', [])
        self.binance_futures_token_symbols = self.config_json.get('Futures Trading Pairs', {}).get('Binance Futures Symbols', [])
        self.total_token_pairs.extend(self.binance_futures_tokens)


        # Setting up Aster tokens
        self.aster_tokens = self.config_json.get('Spot Trading Pairs', {}).get('Aster Pairs', [])
        self.aster_token_symbols = self.config_json.get('Spot Trading Pairs', {}).get('Aster Symbols', [])
        self.total_token_pairs.extend(self.aster_tokens)
        
        
        # Setting up Aster Futures tokens
        self.aster_futures_tokens = self.config_json.get('Futures Trading Pairs', {}).get('Aster Futures Pairs', [])
        self.aster_futures_token_symbols = self.config_json.get('Futures Trading Pairs', {}).get('Aster Futures Symbols', [])
        self.total_token_pairs.extend(self.aster_futures_tokens)
        
        self.spot_total_tokens = self.config_json.get('Spot Total Tokens', [])
        self.futures_total_tokens = self.config_json.get('Futures Total Tokens', [])
        logger.info(f"Configuration setup completed with {len(self.total_token_pairs)} total token pairs: {self.total_token_pairs}")        
        
        
        
        
        
    # TO DO # -----------------------------------
    def _setup_exchanges(self) -> None:
        """Initialize exchange instances for trading operations."""
        
        logger.info("Setting up exchange instances...")
        # Initialize Binance spot exchange
        if hasattr(self, 'binance_tokens') and self.binance_tokens and 'Binance' in self.args.spot.venues:
            self.binance_exchange = BinanceSpotExchange(
                args=self.args,
                api_key=self._get_env('BINANCE_API_KEY', required=True),
                api_secret=self._get_env('BINANCE_API_SECRET', required=True),
                symbols=self.binance_token_symbols,
                kline_manager=self.binance_kline_manager,
                ob_manager=self.binance_ob_manager,
                event_bus=getattr(self, 'event_bus', None),
            )
        
        # Initialize Binance futures exchange
        if hasattr(self, 'binance_futures_tokens') and self.binance_futures_tokens and 'Binance' in self.args.futures.venues:
            self.binance_futures_exchange = BinanceFuturesExchange(
                args=self.args,
                api_key=self._get_env('BINANCE_API_KEY', required=True),
                api_secret=self._get_env('BINANCE_API_SECRET', required=True),
                symbols=self.binance_futures_token_symbols,
                kline_manager=self.binance_futures_kline_manager,
                ob_manager=self.binance_futures_ob_manager,
            )
            logger.info("Binance futures exchange initialized")
        
        # Initialize Aster futures exchange
        if hasattr(self, 'aster_futures_tokens') and self.aster_futures_tokens and 'Aster' in self.args.spot.venues:
            self.aster_futures_exchange = AsterPerpExchange(
                args=self.args,
                api_key=self._get_env('ASTER_API_KEY', required=True),
                api_secret=self._get_env('ASTER_API_SECRET', required=True),
                symbols=self.aster_futures_token_symbols,
                kline_manager=self.aster_futures_kline_manager,
                ob_manager=self.aster_futures_ob_manager,
            )
            logger.info("Aster futures exchange initialized")
        
        
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
            
    
    # Legacy sync wrapper removed — see run().
        
        
        
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
            
    
    async def run(self, strategy: SpotStrategy = None) -> None:
        """Run the complete strategy in a single event loop.

        All feeds, exchange connectors and the optional *strategy* share one
        asyncio event loop.  Heavy compute is offloaded via
        ``strategy.run_heavy()``.
        """
        try:
            logger.info("Starting strategy runner...")

            # Setup phase
            self._setup_config()

            # Create shared event bus BEFORE exchanges so it can be injected
            self.event_bus = EventBus()

            self._setup_exchanges()

            # Inject event bus into client managers
            self.binance_kline_manager.set_event_bus(self.event_bus)
            self.binance_ob_manager.set_event_bus(self.event_bus)

            # Wire runner's event bus and exchanges into strategy, then start
            if strategy is not None:
                strategy._event_bus = self.event_bus
                strategy._exchanges = {Venue.BINANCE: self.binance_exchange}
                await strategy.start()

            logger.info("All components initialized. Starting event loops...")

            # Run exchange + feeds in single event loop
            try:
                # Initialize the exchange first so its AsyncClient and
                # get_exchange_info() call don't compete with the kline/OB
                # managers creating their own AsyncClients concurrently.
                await self.binance_exchange.run()

                # ── Stage 3-5 pipeline: Portfolio, Optimizer, ExecutionEngine ──
                if strategy is not None:
                    # Strategy owns the Portfolio — sync initial cash from exchange
                    usdt_balance = self.binance_exchange.get_balance("USDT")
                    strategy.portfolio.set_cash(usdt_balance)
                    logger.info(f"Portfolio initialized with cash={usdt_balance:.2f} USDT")

                    # Keep cash synced via EventBus
                    async def _sync_portfolio_cash(event: BalanceUpdateEvent):
                        if event.asset in ("USDT", "USDC", "BUSD"):
                            strategy.portfolio.set_cash(event.new_balance)
                    self.event_bus.subscribe(EventType.BALANCE_UPDATE, _sync_portfolio_cash)

                    # Collect all registered feeds from kline managers
                    all_feeds = {}
                    for sym, feed in self.binance_kline_manager._active_feeds.items():
                        all_feeds[sym] = feed

                    risk_config = RiskConfig()
                    self.optimizer = PortfolioOptimizer(risk_config=risk_config, portfolio=strategy.portfolio)
                    self.execution_engine = ExecutionEngine(
                        exchanges={Venue.BINANCE: self.binance_exchange},
                        portfolio=strategy.portfolio,
                        feeds=all_feeds,
                        default_venue=Venue.BINANCE,
                    )

                    # Inject into strategy
                    strategy._optimizer = self.optimizer
                    strategy._execution_engine = self.execution_engine
                    logger.info("Pipeline wired: Strategy.portfolio -> Optimizer -> ExecutionEngine")

                # _setup_binance_data_feeds blocks forever (runs multiplex feed tasks).  If the strategy has its own long-running coroutine (e.g. periodic rebalance timer), gather them
                # so both run concurrently in the same event loop.
                coros = [self._setup_binance_data_feeds()]
                
                if strategy is not None:
                    
                    # Entry corou for a strategy
                    coros.append(strategy.run_forever())
                await asyncio.gather(*coros)
                
            finally:
                await self.binance_exchange.cleanup()
                if strategy is not None:
                    await strategy.stop()

        except asyncio.CancelledError:
            logger.warning("Strategy cancelled")
        except Exception as e:
            logger.error(f"Error in strategy runner: {e}", exc_info=True)
            raise
        finally:
            logger.info("Cleaning up...")
            await self._cleanup([])
            logger.info("Strategy runner shutdown complete")
    

    
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

    runner = StrategyRunner()
    strategy = TestPrintStrategy(exchanges={}, event_bus=EventBus())
    await runner.run(strategy=strategy)


if __name__ == "__main__":

    # Fix for aiodns on Windows
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Parse command line arguments
    
    logger.info("Running in TEST-STRATEGY mode")
    asyncio.run(run_test_strategy())
    
    
    
    
    
