import sys
from pathlib import Path

# Add parent directory to path so imports work when running this script directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
import asyncio
import os
import datetime 
from elysian_core.connectors.BinanceExchange import (
    BinanceOrderBookFeed, 
    BinanceKlineFeed,
    _kline_client_manager,
    _ob_client_manager
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
        self.total_tokens = None
        self.okx_tokens = None
        self.okx_token_symbols = None
        self.total_token_pairs = []
        
        self.candles = {}
        self.data_feed = {}
        self.vol_feed = None
        self.processor = None
        
        
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

    
    def _setup_config(self) -> None:
        """Initialize tokens and pools configuration. Testing, we just add the Binance Tokens"""
        logger.info("Setting up Strategy Configuration...")
        
        self.binance_tokens = self.config_json.get('Trading Pairs', {}).get('Binance Pairs', [])
        self.binance_token_symbols = self.config_json.get('Trading Pairs', {}).get('Binance Symbols', [])
        self.total_token_pairs.extend(self.binance_tokens)
        
        self.total_tokens = self.config_json.get('Total Tokens', [])
        logger.info(f"Loaded pools, {len(self.total_tokens)} tokens")
        
        
        
        
    
    async def _setup_data_feeds(self) -> None:
        """Initialize all data feeds with multiplex architecture"""
        
        logger.info("Setting up data feeds with multiplex architecture...")
        
        # Create kline feeds that register with shared client manager
        for i, token in enumerate(self.binance_token_symbols):
            token_feed = BinanceKlineFeed(save_data=False)
            token_feed.create_new(asset=token, interval="1s")
            self.candles[self.binance_tokens[i]] = token_feed
        
        # Create order book feeds if needed (for exchange operations)
        self.order_books = {}
        for i, token in enumerate(self.binance_token_symbols):
            ob_feed = BinanceOrderBookFeed(save_data=False)
            ob_feed.create_new(asset=token, interval="100ms")
            self.order_books[self.binance_tokens[i]] = ob_feed
        
        self.data_feed = {
            'CANDLES': self.candles,
            'ORDER_BOOKS': self.order_books,
            'AUCTIONS': self.auction_listener,
        }
        logger.info(f"Initialized {len(self.candles)} Binance kline feeds and {len(self.order_books)} order book feeds")
        
        # Start the multiplex managers
        await _kline_client_manager.start()
        await _ob_client_manager.start()
        
        logger.info("Multiplex client managers started")
    
    
        
    # TO DO
    async def _setup_volatility_feed(self) -> None:
        """Initialize volatility feed from Redis."""
        logger.info("Setting up volatility feed...")
        
        redis_port = int(os.getenv('REDIS_PORT'))
        redis_host = os.getenv('REDIS_HOST')
        
        redis = RedisConfig(host=redis_host, port=redis_port).local()
        self.vol_feed = await VolatilityBarbClientLocal.new(redis=redis)
        
        
    async def _setup_processor(self) -> None:
        """Initialize main processor."""
        logger.info("Setting up processor...")
        
        self.processor = Processor(
            datafeed=self.data_feed,
            tokenlist=self.total_token_pairs,
            eventfilter=self.event_filter,
            bidBuilder=self.shio_bid_builder,
            poolManager=self.pool_state,
            sui_event_listener=self.sui_event_listener,
            tq_hedger=self.tq_hedger,
            vol_feed=self.vol_feed,
            timestamp=self.timestamp,
        )
    
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
        await _kline_client_manager.stop()
        await _ob_client_manager.stop()
        
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
    
    async def run(self) -> None:
        """Run the complete strategy. Main Async loop that kick starts all asynchronous components"""
        
        
        try:
            logger.info("Starting strategy runner...")
            
            # Setup phase
            self._setup_config()
            await self._setup_data_feeds()
            await self._setup_volatility_feed()
            await self._setup_processor()
            
            logger.info("All components initialized. Starting event loops...")
            
            # Start multiplex readers and workers
            multiplex_tasks = []
            if _kline_client_manager._active_feeds:
                multiplex_tasks.append(asyncio.create_task(_kline_client_manager.run_multiplex_reader()))
                multiplex_tasks.extend(_kline_client_manager.start_workers(num_workers=4))
            
            if _ob_client_manager._active_feeds:
                multiplex_tasks.append(asyncio.create_task(_ob_client_manager.run_multiplex_reader()))
                multiplex_tasks.extend(_ob_client_manager.start_workers(num_workers=4))
            
            # Create task groups (feeds are now handled by multiplex managers)
            light_tasks_1 = [
                self.auction_listener(),
                self.auction_bid_logger()
            ]
            light_tasks_2 = [
                self.sui_event_listener(),
                self.trade_capture_logger()
            ]
            heavy_task_1 = [
                self.processor.run(),
                self.tq_hedger.run()
            ]
            heavy_task_2 = [self.pool_state()]
            
            threaded_tasks = [heavy_task_2, light_tasks_1, light_tasks_2, multiplex_tasks]
            
            # Run all tasks concurrently
            with ProcessPoolExecutor(max_workers=4) as executor:
                thread_futures = [
                    executor.submit(self._run_event_loop_in_thread, task_group)
                    for task_group in threaded_tasks if task_group
                ]
                
                await asyncio.gather(*heavy_task_1)
                
                for future in thread_futures:
                    future.result()
        
        except asyncio.CancelledError:
            logger.warning("Tasks cancelled")
            await asyncio.sleep(6)
        except Exception as e:
            logger.error(f"Error in strategy runner: {e}", exc_info=True)
            raise
        finally:
            all_tasks = [heavy_task_1] + threaded_tasks
            await self._cleanup(all_tasks)
            logger.info("Strategy runner shutdown complete")

    
    
    async def debug_binance_feed(self, pair: str = "SUIUSDC", feed_type: str = "kline", duration: int = 30) -> None:
        """
        Debug a single Binance feed with detailed logging using multiplex architecture.
        
        Args:
            pair: Trading pair to test
            feed_type: "kline" or "orderbook"
            duration: Seconds to run
        """
        logger.info(f"DEBUG: Testing single Binance {feed_type} feed for {pair} with multiplex architecture")
        
        try:
            # Create feed and register with appropriate client manager
            if feed_type == "kline":
                client_manager = _kline_client_manager
                FeedClass = BinanceKlineFeed
            else:
                client_manager = _ob_client_manager
                FeedClass = BinanceOrderBookFeed
            
            feed = FeedClass(save_data=False)
            if feed_type == "kline":
                feed.create_new(asset=pair, interval="1s")
            else:
                feed.create_new(asset=pair, interval="100ms")
            
            # Register feed with client manager
            client_manager.register_feed(feed)
            
            logger.info(f"DEBUG: Created feed object, starting multiplex manager...")
            
            # Start the client manager
            await client_manager.start()
            
            # Start multiplex reader and workers
            multiplex_task = asyncio.create_task(client_manager.run_multiplex_feeds())
            
            # Run for specified duration
            logger.info(f"DEBUG: Running multiplex {feed_type} feed for {duration} seconds...")
            all_tasks = [multiplex_task]
            
            # Monitor the tasks
            start_time = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= duration:
                    logger.info(f"DEBUG: Timeout reached after {elapsed:.1f}s")
                    break
                
                # Check if any task failed
                for i, task in enumerate(all_tasks):
                    if task.done():
                        try:
                            result = task.result()
                            if isinstance(result, Exception):
                                logger.error(f"DEBUG: Task {i} failed with error: {result}")
                            else:
                                logger.info(f"DEBUG: Task {i} completed normally")
                        except Exception as e:
                            logger.error(f"DEBUG: Task {i} failed: {e}", exc_info=True)
                        break
                else:
                    logger.info(f"DEBUG: Multiplex feed running ({elapsed:.1f}s elapsed)")
                    await asyncio.sleep(5)
                    continue
                break
            
            # Cancel tasks if still running
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            
            try:
                await asyncio.gather(*all_tasks, return_exceptions=True)
                logger.info("DEBUG: All tasks cancelled successfully")
            except asyncio.CancelledError:
                logger.info("DEBUG: Tasks cancelled successfully")
        
        except Exception as e:
            logger.error(f"DEBUG: Error in debug_binance_feed: {e}", exc_info=True)
        finally:
            # Cleanup
            logger.info("DEBUG: Cleaning up debug feed...")
            if 'client_manager' in locals():
                await client_manager.stop()
            logger.info("DEBUG: Binance feed debug completed")
    
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
                client_manager = _kline_client_manager
                FeedClass = BinanceKlineFeed
            else:
                client_manager = _ob_client_manager
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
            
            # Start the client manager
            await client_manager.start()
            
            # Start multiplex reader and workers
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
    import sys
    
    # Fix for aiodns on Windows
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    import aiohttp
    import asyncio

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
    
    
    
    
    
    
