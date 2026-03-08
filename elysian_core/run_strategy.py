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
    BinanceOrderBookFeed, 
    BinanceKlineFeed,
    binance_spot_kline_client_manager,
    binance_spot_ob_client_manager,
    BinanceSpotExchange
)

from elysian_core.connectors.BinanceFuturesExchange import (
    BinanceFuturesOrderBookFeed,
    BinanceFuturesKlineFeed,
    binance_futures_kline_client_manager,
    binance_futures_ob_client_manager,
    BinanceFuturesExchange
)

from elysian_core.connectors.AsterExchange import (
    AsterOrderBookFeed,
    AsterKlineFeed,
    aster_spot_kline_client_manager,
    aster_spot_ob_client_manager
)


# from elysian_core.connectors.OKXFeed import OKXDataFeed
# from elysian_core.connectors.VolatilityBarbClient import VolatilityBarbClientLocal, RedisConfig
from elysian_core.utils.logger import setup_custom_logger
from elysian_core.utils.utils import load_config, replace_placeholders, config_to_args
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor


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
        self.config = load_config(config_yaml)
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
        self.binance_kline_manager = binance_spot_kline_client_manager
        self.binance_ob_manager = binance_spot_ob_client_manager
        
        # Binance Futures Client Managers
        self.binance_futures_kline_manager = binance_futures_kline_client_manager
        self.binance_futures_ob_manager = binance_futures_ob_client_manager
        
        # Aster Client Managers
        self.aster_kline_manager = aster_spot_kline_client_manager
        self.aster_ob_manager = aster_spot_ob_client_manager
        
        
        
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
        
        self.spot_total_tokens = self.config_json.get('Spot Total Tokens', [])
        self.futures_total_tokens = self.config_json.get('Futures Total Tokens', [])
        logger.info(f"Configuration setup completed with {len(self.total_token_pairs)} total token pairs: {self.total_token_pairs}")        
        
        
        
        
        
    # TO DO # -----------------------------------
    def _setup_exchanges(self) -> None:
        """Initialize exchange instances for trading operations."""
        
        logger.info("Setting up exchange instances...")
        
        # Initialize Binance spot exchange
        if hasattr(self, 'binance_tokens') and self.binance_tokens:
            self.binance_exchange = BinanceSpotExchange(
                api_key=self._get_env('BINANCE_API_KEY', required=True),
                api_secret=self._get_env('BINANCE_API_SECRET', required=True),
                testnet=self.config.get('binance', {}).get('testnet', True)
            )
            logger.info("Binance spot exchange initialized")
        
        # Initialize Binance futures exchange
        if hasattr(self, 'binance_futures_tokens') and self.binance_futures_tokens:
            self.binance_futures_exchange = BinanceFuturesExchange(
                api_key=self._get_env('BINANCE_API_KEY', required=True),
                api_secret=self._get_env('BINANCE_API_SECRET', required=True),
                testnet=self.config.get('binance', {}).get('testnet', True)
            )
            logger.info("Binance futures exchange initialized")
        
        # # Initialize Aster exchange
        # if hasattr(self, 'aster_tokens') and self.aster_tokens:
        #     from elysian_core.connectors.AsterExchange import AsterExchange
        #     self.aster_exchange = AsterExchange(
        #         api_key=self._get_env('ASTER_API_KEY', required=True),
        #         api_secret=self._get_env('ASTER_API_SECRET', required=True),
        #         testnet=self.config.get('aster', {}).get('testnet', True)
        #     )
        #     logger.info("Aster exchange initialized")
        
        
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
            
            
    def setup_and_run_binance_feeds(self):
        '''Helper function to setup Binance feeds and return the multiplex tasks. 
           Wraps around _setup_data_feeds for better modularity and testing.'''
        
        asyncio.run(self._setup_binance_data_feeds())
    
    
    
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
            
            
    def setup_and_run_binance_futures_feeds(self):
        '''Helper function to setup Binance futures feeds and return the multiplex tasks. 
           Wraps around _setup_binance_futures_data_feeds for better modularity and testing.'''
        
        asyncio.run(self._setup_binance_futures_data_feeds())




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
            
    
    def setup_and_run_aster_feeds(self):
        '''Helper function to setup Aster feeds and return the multiplex tasks. 
           Wraps around _setup_data_feeds for better modularity and testing.'''
        
        asyncio.run(self._setup_aster_data_feeds())
        

    
    
    # # TO DO
    # async def _setup_volatility_feed(self) -> None:
    #     """Initialize volatility feed from Redis."""
    #     logger.info("Setting up volatility feed...")
        
    #     redis_port = int(os.getenv('REDIS_PORT'))
    #     redis_host = os.getenv('REDIS_HOST')
        
    #     redis = RedisConfig(host=redis_host, port=redis_port).local()
    #     self.vol_feed = await VolatilityBarbClientLocal.new(redis=redis)
        
        
    # async def _setup_processor(self) -> None:
    #     """Initialize main processor."""
    #     logger.info("Setting up processor...")
        
    #     self.processor = Processor(
    #         datafeed=self.data_feed,
    #         tokenlist=self.total_token_pairs,
    #         eventfilter=self.event_filter,
    #         bidBuilder=self.shio_bid_builder,
    #         poolManager=self.pool_state,
    #         sui_event_listener=self.sui_event_listener,
    #         tq_hedger=self.tq_hedger,
    #         vol_feed=self.vol_feed,
    #         timestamp=self.timestamp,
    #     )
    
    
    async def run(self) -> None:
        """Run the complete strategy. Main Async loop that kick starts all asynchronous components"""
        try:
            logger.info("Starting strategy runner...")
            
            # Setup phase
            self._setup_config()
            #self._setup_exchanges()
            #await self._setup_processor()
            
            logger.info("All components initialized. Starting event loops...")
            
            # Compile Process Jobs
            #process_jobs = [(self.setup_and_run_binance_feeds, [], "binance_feeds")]
            process_jobs = [
                #(self.setup_and_run_binance_feeds, [], "binance_feeds"),
                #(self.setup_and_run_aster_feeds, [], "aster_feeds")
                (self.setup_and_run_binance_futures_feeds, [], "binance_futures_feeds")
            ]
            
            # Run all task groups concurrently using ProcessPoolExecutor
            loop = asyncio.get_running_loop()
            logger.info(f"Running {len(process_jobs)} task group(s) with ProcessPoolExecutor...")
            with ProcessPoolExecutor(max_workers=4) as executor:
                futures = [
                    loop.run_in_executor(executor, func, *args)
                    for func, args, process_name in process_jobs
                ]
                await asyncio.gather(*futures)

        
        except asyncio.CancelledError:
            logger.warning("Strategy cancelled")
        except Exception as e:
            logger.error(f"Error in strategy runner: {e}", exc_info=True)
            raise
        finally:
            logger.info("Cleaning up...")
            await self._cleanup(process_jobs)
            logger.info("Strategy runner shutdown complete")

    
    
    

    
    #############################################################################################################################################################
    ######## Testing functions for individual components in isolation   #########################################################################################
    #############################################################################################################################################################
    
    
    
    async def test_binance_feed(self, pairs: list = None, duration: int = 60, feed_type: str = "kline") -> None:
        """
        Test Binance data feeds in isolation using multiplex architecture.
        
        Args:
            pairs: List of trading pairs to test (defaults from config)
            duration: Seconds to run the test
            feed_type: "kline" for candlestick data or "orderbook" for depth data
        """
        logger.info(f"Testing Binance {feed_type} feeds with multiplex architecture...")
        
        if not pairs:
            pairs = self.config_json.get('Trading Pairs', {}).get('Binance Pairs', [])
        
        try:
            # Create feeds that register with appropriate client manager
            binance_feeds = {}
            if feed_type == "kline":
                client_manager = binance_spot_kline_client_manager
                FeedClass = BinanceKlineFeed
            else:
                client_manager = binance_spot_ob_client_manager
                FeedClass = BinanceOrderBookFeed
            
            for pair in pairs:
                logger.info(f"Initializing Binance {feed_type} feed for {pair}...")
                feed = FeedClass(save_data=False)
                if feed_type == "kline":
                    feed.create_new(asset=pair, interval="1s")
                else:
                    feed.create_new(asset=pair, interval="100ms")
                    
                # Register feed with client manager
                client_manager.register_feed(feed)
                binance_feeds[pair] = feed
            
            await client_manager.start()
            multiplex_task = asyncio.create_task(client_manager.run_multiplex_feeds())
            
            # Run for specified duration
            logger.info(f"Running multiplex {feed_type} feeds for {duration} seconds...")
            all_tasks = [multiplex_task]
            
            # Wait for timeout
            await asyncio.wait_for(
                asyncio.gather(*all_tasks, return_exceptions=True),
                timeout=duration
            )
            
        except asyncio.TimeoutError:
            logger.info(f"Test completed after {duration} seconds (timeout as expected)")
        except Exception as e:
            logger.error(f"Error testing Binance {feed_type} feeds: {e}", exc_info=True)
            raise
        finally:
            # Cleanup
            logger.info("Cleaning up test feeds...")
            if 'client_manager' in locals():
                await client_manager.stop()
            logger.info(f"Binance {feed_type} feed test completed")


# Standalone functions for backward compatibility
def run_event_loop_in_thread(tasks):
    """Runs a group of coroutines in a separate thread with a dedicated event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(asyncio.gather(*tasks))
    finally:
        loop.close()


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


async def run_strategy():
    """Main entry point for running the strategy."""
    runner = StrategyRunner()
    await runner.run()


async def test_binance_feed(pairs: list = None, duration: int = 60, feed_type: str = "kline"):
    """Test Binance feeds in isolation."""
    runner = StrategyRunner()
    await runner.test_binance_feed(pairs=pairs, duration=duration, feed_type=feed_type)
        

if __name__ == "__main__":
    
    # Fix for aiodns on Windows
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


    async def test():
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.binance.com/api/v3/ping") as r:
                print(await r.text())

    asyncio.run(test())
        
    # Parse command line arguments
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Test mode: run Binance feed test
        feed_type = sys.argv[2] if len(sys.argv) > 2 else "kline"
        duration = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        logger.info(f"Running in TEST mode: {feed_type} feed for {duration}s")
        asyncio.run(test_binance_feed(feed_type=feed_type, duration=duration))
    else:
        # Production mode: run full strategy
        logger.info("Running in PRODUCTION mode")
        asyncio.run(run_strategy())
    
    
    
    
    
    
