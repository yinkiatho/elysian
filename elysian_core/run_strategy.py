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
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from elysian_core.core.event_bus import EventBus
from elysian_core.core.enums import OrderType, RunnerState, Venue
from elysian_core.core.fsm import PeriodicTask
from elysian_core.execution.engine import ExecutionEngine
from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.core.portfolio import Portfolio
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
        self._snapshot_interval_s = 0
        self._snapshot_task: PeriodicTask | None = None
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
            
    
    # ── Stage 3-5 setup helpers ────────────────────────────────────────────────────────────────────────────── # 

    def _collect_all_feeds(self) -> dict:
        """Gather all registered kline feeds across venues."""
        all_feeds = {}
        for sym, feed in self.binance_kline_manager._active_feeds.items():
            all_feeds[sym] = feed
        return all_feeds


    async def _setup_portfolio(self, strategy: SpotStrategy) -> None:
        """Initialize the strategy's portfolio with full exchange state sync.

        Reads ``self.args.portfolio.*`` for configuration (max_history,
        snapshot_interval_s).  Constructs a fresh Portfolio with these params,
        then syncs cash, positions, mark prices, and open orders from
        BinanceSpotExchange.  Finally subscribes to EventBus so balance
        deltas, kline prices, and weights stay in sync automatically.

        For multi-venue expansion: call ``sync_from_exchange()`` once per
        exchange — each venue's state accumulates into the portfolio.
        """
        # Read portfolio config from args
        portfolio_section = getattr(self.args, "portfolio", None)
        max_history = 10_000
        self._snapshot_interval_s = 0

        if portfolio_section is not None:
            logger.info("[Runner] Reading portfolio config from args.portfolio")
            mh = getattr(portfolio_section, "max_history", None)
            if mh is not None:
                max_history = int(mh)
                logger.info(f"[Runner]   portfolio.max_history = {max_history}")

            si = getattr(portfolio_section, "snapshot_interval_s", None)
            if si is not None:
                self._snapshot_interval_s = int(si)
                logger.info(f"[Runner]   portfolio.snapshot_interval_s = {self._snapshot_interval_s}")
        else:
            logger.info("[Runner] No args.portfolio section — using Portfolio defaults")

        
        strategy.portfolio = Portfolio(
            initial_cash=0.0,
            max_history=max_history,
            args=self.args,
            config_json=self.config_json,
        )

        all_feeds = self._collect_all_feeds()

        # Full sync: cash + positions + mark prices + open orders
        strategy.portfolio.sync_from_exchange(
            self.binance_exchange, venue=Venue.BINANCE, feeds=all_feeds
        )

        # Start event-driven portfolio (auto-updates weights, mark prices, cash)
        strategy.portfolio.start(self.event_bus, feeds=all_feeds)

        logger.info(
            f"[Runner] Portfolio initialized: cash={strategy.portfolio.cash:.2f}, "
            f"{len(strategy.portfolio.active_positions)} positions, "
            f"{len(all_feeds)} feeds wired, "
            f"max_history={max_history}, snapshot_interval={self._snapshot_interval_s}s"
        )

    def _setup_risk(self, strategy: SpotStrategy) -> None:
        """Build a RiskConfig from ``self.args.risk`` and create the PortfolioOptimizer.

        Reads every RiskConfig field from the YAML config.  Falls back to
        RiskConfig defaults for any field not present in the config.

        Type conversions handled:
          - ``allowed_symbols``: YAML list/null → ``Optional[FrozenSet[str]]``
          - ``blocked_symbols``: YAML list → ``FrozenSet[str]``
        """
        risk_kwargs = {}
        risk_section = getattr(self.args, "risk", None)
        if risk_section is not None:
            logger.info("[Runner] Reading risk config from args.risk")

            # Scalar fields — pass through directly
            _SCALAR_FIELDS = {
                "max_weight_per_asset", "min_weight_per_asset",
                "max_total_exposure", "min_cash_weight",
                "max_turnover_per_rebalance", "max_leverage",
                "max_short_weight", "min_order_notional",
                "max_order_notional", "min_rebalance_interval_ms",
                "min_weight_delta",
            }
            for field_name in _SCALAR_FIELDS:
                val = getattr(risk_section, field_name, None)
                if val is not None:
                    risk_kwargs[field_name] = val
                    logger.info(f"[Runner]   risk.{field_name} = {val}")

            # Set fields — YAML lists/null → frozenset
            allowed = getattr(risk_section, "allowed_symbols", None)
            if allowed is not None and isinstance(allowed, list):
                risk_kwargs["allowed_symbols"] = frozenset(allowed)
                logger.info(f"[Runner]   risk.allowed_symbols = {risk_kwargs['allowed_symbols']}")

            blocked = getattr(risk_section, "blocked_symbols", None)
            if blocked is not None and isinstance(blocked, list) and blocked:
                risk_kwargs["blocked_symbols"] = frozenset(blocked)
                logger.info(f"[Runner]   risk.blocked_symbols = {risk_kwargs['blocked_symbols']}")
        else:
            logger.info("[Runner] No args.risk section — using RiskConfig defaults")

        risk_config = RiskConfig(**risk_kwargs)
        self.optimizer = PortfolioOptimizer(
            risk_config=risk_config,
            portfolio=strategy.portfolio,
            args=self.args,
            config_json=self.config_json,
        )
        strategy._optimizer = self.optimizer
        logger.info(f"[Runner] Risk optimizer configured: {risk_config}")



    def _setup_execution(self, strategy: SpotStrategy) -> None:
        """Create the ExecutionEngine and inject it into the strategy.

        Reads ``self.args.execution.*`` for configuration (default_order_type,
        default_venue).  Shares the same RiskConfig as the optimizer so the
        weight-delta threshold is consistent between risk and execution stages.

        Type conversions handled:
          - ``default_order_type``: YAML string → :class:`OrderType` enum
          - ``default_venue``: YAML string → :class:`Venue` enum
        """
        all_feeds = self._collect_all_feeds()
        risk_config = self.optimizer.config if hasattr(self, "optimizer") else RiskConfig()

        # Read execution config from args
        exec_section = getattr(self.args, "execution", None)
        default_order_type = OrderType.MARKET
        default_venue = Venue.BINANCE

        if exec_section is not None:
            logger.info("[Runner] Reading execution config from args.execution")

            ot = getattr(exec_section, "default_order_type", None)
            if ot is not None:
                _ORDER_TYPE_MAP = {
                    "MARKET": OrderType.MARKET,
                    "LIMIT": OrderType.LIMIT,
                }
                default_order_type = _ORDER_TYPE_MAP.get(str(ot).upper(), OrderType.MARKET)
                logger.info(f"[Runner]   execution.default_order_type = {default_order_type.value}")

            dv = getattr(exec_section, "default_venue", None)
            if dv is not None:
                _VENUE_MAP = {v.value: v for v in Venue}
                default_venue = _VENUE_MAP.get(str(dv), Venue.BINANCE)
                logger.info(f"[Runner]   execution.default_venue = {default_venue.value}")
        else:
            logger.info("[Runner] No args.execution section — using ExecutionEngine defaults")

        self.execution_engine = ExecutionEngine(
            exchanges={default_venue: self.binance_exchange},
            portfolio=strategy.portfolio,
            feeds=all_feeds,
            risk_config=risk_config,
            default_venue=default_venue,
            default_order_type=default_order_type,
            args=self.args,
            config_json=self.config_json,
        )
        strategy._execution_engine = self.execution_engine
        logger.info(
            f"[Runner] Execution engine configured: "
            f"venue={default_venue.value}, "
            f"order_type={default_order_type.value}, "
            f"min_weight_delta={risk_config.min_weight_delta}"
        )


    async def _setup_pipeline(self, strategy: SpotStrategy) -> None:
        """Wire the full Stage 3-5 pipeline: Portfolio -> Risk -> Execution.

        Also applies ``args.strategy.max_heavy_workers`` if present (replaces
        the ProcessPoolExecutor created during SpotStrategy.__init__).
        """
        logger.info("[Runner] ── Setting up Stage 3-5 pipeline ──")
        await self._setup_portfolio(strategy)
        self._setup_risk(strategy)
        self._setup_execution(strategy)

        # Apply strategy-level config (max_heavy_workers)
        strat_section = getattr(self.args, "strategy", None)
        if strat_section is not None:
            mhw = getattr(strat_section, "max_heavy_workers", None)
            if mhw is not None:
                strategy._executor.shutdown(wait=False)
                strategy._executor = ProcessPoolExecutor(max_workers=int(mhw))
                logger.info(f"[Runner]   strategy.max_heavy_workers = {int(mhw)}")

        logger.info(
            f"[Runner] Pipeline complete: {strategy.__class__.__name__}.portfolio "
            f"-> Optimizer -> ExecutionEngine"
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
                strategy.portfolio.save_snapshot()
                logger.info("[Runner] Portfolio snapshot saved")
            except Exception as e:
                logger.error(f"[Runner] Snapshot save failed: {e}", exc_info=True)

        self._snapshot_task = PeriodicTask(
            callback=_save,
            interval_s=interval,
            name="PortfolioSnapshot",
        )
        self._snapshot_task.start()
        logger.info(f"[Runner] Portfolio snapshot task started (every {interval}s)")

    # ── Main entry point ──────────────────────────────────────────────────────
    @property
    def state(self) -> RunnerState:
        """Current runner lifecycle state."""
        return self._state

    async def run(self, strategy: SpotStrategy = None) -> None:
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

            # Inject event bus into client managers
            self.binance_kline_manager.set_event_bus(self.event_bus)
            self.binance_ob_manager.set_event_bus(self.event_bus)
            logger.info("[Runner] EventBus injected into kline + OB client managers")

            # Wire runner's event bus and exchanges into strategy, then start
            if strategy is not None:
                logger.info(f"[Runner] Wiring strategy: {strategy.__class__.__name__}")
                strategy._event_bus = self.event_bus
                strategy._exchanges = {Venue.BINANCE: self.binance_exchange}
                strategy.args = self.args
                strategy.config_json = self.config_json
                await strategy.start()

            logger.info("[Runner] All components initialized. Starting event loops...")

            # Run exchange + feeds in single event loop
            try:
                # Initialize the exchange first so its AsyncClient and
                # get_exchange_info() call don't compete with the kline/OB
                # managers creating their own AsyncClients concurrently.
                logger.info("[Runner] Binance exchange initialized")
                await self.binance_exchange.run()
                logger.info("[Runner] Binance exchange running")

                # ── SYNCING ──
                self._state = RunnerState.SYNCING
                if strategy is not None:
                    await self._setup_pipeline(strategy)

                # _setup_binance_data_feeds blocks forever (runs multiplex
                # feed tasks).  If the strategy has its own long-running
                # coroutine (e.g. periodic rebalance timer), gather them so
                # both run concurrently in the same event loop.
                coros = [self._setup_binance_data_feeds()]

                if strategy is not None:
                    logger.info(f"[Runner] Launching strategy.run_forever() alongside data feeds")
                    coros.append(strategy.run_forever())

                    # Periodic portfolio snapshot via PeriodicTask (no while-True loop)
                    if self._snapshot_interval_s > 0:
                        self._start_snapshot_task(strategy)

                # ── RUNNING ──
                self._state = RunnerState.RUNNING
                logger.info(f"[Runner] Entering main event loop ({len(coros)} coroutines)")
                await asyncio.gather(*coros)

            finally:
                # ── STOPPING ──
                self._state = RunnerState.STOPPING
                logger.info("[Runner] Shutting down exchange and strategy...")

                if self._snapshot_task is not None:
                    self._snapshot_task.stop()

                await self.binance_exchange.cleanup()
                if strategy is not None:
                    strategy.portfolio.stop()
                    await strategy.stop()

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
    
    
    
    
    
