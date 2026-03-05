from shiolabs.connectors.BinanceFeed import BinanceDataFeed
from concurrent.futures import ThreadPoolExecutor
from shiolabs.connectors.BybitFeed import BybitDataFeed
from shiolabs.connectors.OkxFeed import OKXDataFeed
from shiolabs.connectors.ShioFeedConnector import ShioFeedConnector
from shiolabs.strategy.processor import Processor
from shiolabs.calculators.AuctionEventFilter import AuctionEventFilter
from shiolabs.calculators.AuctionHistoricalLogger import AuctionHistoricalLogger
from shiolabs.exchange.BinanceExchange import BinanceHedger
from shiolabs.exchange.ShioBidBuilder import ShioBidBuilder
from shiolabs.connectors.SuiEventListener import SuiEventListener
from shiolabs.connectors.ActualAuctionListener import ActualAuctionListener
from shiolabs.connectors.VolatiltyFeed import *
from shiolabs.exchange.Rebalancer import Rebalancer
from shiolabs.connectors.GasListener import SuiGasListener
from shiolabs.connectors.TradeCaptureLogger import TradeCaptureLogger
from pysui.sui import sui_types
from shiolabs.pools.PoolManager import BaseCetusV3PoolManager
from shiolabs.exchange.TQExchange import TQHedger
from dotenv import load_dotenv
import asyncio
import os
import datetime 
import shiolabs.utils.logger as log
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor


logger = log.setup_custom_logger('root')
os.environ['TQ_ENV'] = 'prod'

def run_event_loop_in_thread(tasks):
    """
    Runs a group of coroutines in a separate thread with a dedicated event loop.
    """
    loop = asyncio.new_event_loop()  # Create a new event loop for the thread
    asyncio.set_event_loop(loop)  # Set it as the active event loop in the thread
    try:
        loop.run_until_complete(asyncio.gather(*tasks))  # Run the tasks
    finally:
        loop.close()  # Ensure the loop is properly closed
        
async def cleanup(tasks_groups):
    """Cleanup function to properly shutdown all tasks"""
    for tasks in tasks_groups:
        for task in tasks:
            if hasattr(task, 'stop'):  # For objects with stop method
                await task.stop()
            elif isinstance(task, asyncio.Task):  # For raw tasks
                task.cancel()
    
    for tasks in tasks_groups:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    
async def run_strategy():
    load_dotenv()
    SUI_ADDRESS = os.getenv('SUI_ADDRESS')
    # SUI_MNEMONIC = os.getenv('SUI_MNEMONIC')
    # SUI_PRIVATE_KEY = os.getenv('SUI_PRIVATE_KEY')
    
    # Load configurations
    config_file_path = 'shiolabs/config/configuration.json'
    with open(config_file_path, 'r') as file:
        config = json.load(file)
    
    # Define UTC+8 timezone
    utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))
    timestamp = datetime.datetime.now(utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S')
    
    #current_directory = os.getcwd()
    #data_dir = Path(current_directory, "shiolabs", "data", f"data_{timestamp}")
    # if not data_dir.exists():
    #     data_dir.mkdir(parents=True)
        
    # ------------------------------------SETTING UP Tokens and Pools Configuration --------------------------------------------------------------#
        
    okx_tokens = config.get('Trading Pairs', {}).get('OKX Pairs', [])
    okx_token_symbols = config.get('Trading Pairs', {}).get('OKX Symbols', [])
    
    total_token_pairs = []
    total_token_pairs.extend(okx_tokens)
    
    #total_tokens = ['SUI', 'CETUS', 'USDC', 'USDT']
    total_tokens = config.get('Total Tokens', [])
    
    pool_ids = [(pool[0], sui_types.ObjectID(pool[1])) for pool in config.get('Pools', [])]
    
    static_object_ids = [(obj[0], sui_types.ObjectID(obj[1])) for obj in config.get('Static Object IDs', [])]
    package_targets = config.get('Package Targets', [])
    
    # ---------------------------------------------------------------------------------------------------------------------------------#  
    
    # -------------------------------------------------------- SETTING UP Data Feeds --------------------------------------------------------------# 
    shio_rpc = config.get("Shio Addresses", {}).get("RPC URL", "")
    shio_package = config.get("Shio Addresses", {}).get("Package Address", "")
    shio_feed_url = config.get("Shio Addresses", {}).get("Feed URL", "")
    

    auctionListener = ShioFeedConnector(timestamp=timestamp, feed_url=shio_feed_url)
    
    candles = {}
    for i, token in enumerate(okx_token_symbols):
        token_candle = OKXDataFeed(save_data=False)
        token_candle.create_new(asset=token)
        candles[okx_tokens[i]] = token_candle


    # -----------------------------------------  SETTING UP TQ :(((((((((((((((  ------------------------------------------------------------#
    # Loading TQ Config
    OMS_CHANNEL, ACCOUNT_ID, NATS_TIMEOUT, NATS_URL, TQ_ENV = (os.getenv("OMS_CHANNEL"),  int(os.getenv("ACCOUNT_ID")),  int(os.getenv("NATS_TIMEOUT")),
                                                                os.getenv('NATS_URL'),  os.getenv('TQ_ENV'))
    
    
    
    tq_hedger = await TQHedger.new(nats_url=NATS_URL, NATS_TIMEOUT=NATS_TIMEOUT, 
                                   OMS_CHANNEL=OMS_CHANNEL, ACCOUNT_ID=ACCOUNT_ID, 
                                   sui_address=SUI_ADDRESS, data_feed=data_feed)
    await tq_hedger.run()
    # ---------------------------------------------------------------------------------------------------------------------------------#
    
    
    # ------------------------------------SETTING UP auctionBidLogger ShioBidBuilder --------------------------------------------------------------#
    
    auctionBidLogger = AuctionHistoricalLogger()
    #await auctionBidLogger.start()
    
    shio_bid_builder = ShioBidBuilder(wallet_mnemonic_1='', 
                                      sui_address=SUI_ADDRESS, 
                                      sui_private_key='', 
                                      pool_ids=pool_ids, 
                                      total_tokens=total_tokens,
                                      static_object_ids = static_object_ids, 
                                      package_targets = package_targets, 
                                      shio_rpc=shio_rpc, 
                                      shio_package=shio_package,
                                      tq_hedger=tq_hedger,
                                      auction_bid_logger=auctionBidLogger)
    shio_bid_builder.start_initialization()
    
    event_filter = AuctionEventFilter(tokenList=total_token_pairs)
    
    data_feed = {
        'CANDLES': candles,
        'AUCTIONS': auctionListener,
    }
    
    # ---------------------------------------------------------------------------------------------------------------------------------#   

    
    
    # ------------------- SETTING UP VOLATILITY REDIS PUBLISHER -----------------------------------------------------------------------#
    REDIS_PORT, REDIS_HOST = int(os.getenv('REDIS_PORT')), os.getenv('REDIS_HOST')
    
    redis = RedisConfig(host=REDIS_HOST, port=REDIS_PORT).local()
    vol_feed = await VolatilityBarbClientLocal.new(redis=redis)
     
    #---------------------------------------------------------------------------------------------------------------------------------#   
    
    
    #-----------------------------------------SETTING UP SuiEventListener and TradeCaptureLogger ------------------------------------------#
    #Initializing TOC Trade Capture Logger
    toc_url, t2x_account_id = config.get("TOC Config").get("URL"), config.get("TOC Config").get("T2X Account ID")
    trade_capture_logger = TradeCaptureLogger(toc_url=toc_url, t2x_account_id=t2x_account_id,)

    
    sui_event_listener = SuiEventListener(trade_capture_logger=trade_capture_logger, 
                                          sui_address=SUI_ADDRESS, 
                                          pool_addresses=pool_ids)
    await sui_event_listener.initialize()
    
    # ---------------------------------------------------------------------------------------------------------------------------------#
    
    
    
    
    # -----------------------------------------  SETTING UP PoolQuoter  ------------------------------------------------------------#
    
    pool_state = BaseCetusV3PoolManager(pool_ids=pool_ids, 
                                        price_feed=data_feed, 
                                        tokens_list=total_token_pairs)
    
    # #First call to initialize the pool states
    await pool_state.auto_update()
    # ---------------------------------------------------------------------------------------------------------------------------------#
    
    
    # -----------------------------------------  Main Processor Engine  ------------------------------------------------------------#
    processor = Processor(
        datafeed=data_feed,
        tokenlist=total_token_pairs,
        eventfilter=event_filter,
        bidBuilder=shio_bid_builder,
        poolManager=pool_state,
        sui_event_listener=sui_event_listener,
        tq_hedger=tq_hedger,
        vol_feed=vol_feed,
        timestamp=timestamp,
    )
    
    # ---------------------------------------------------------------------------------------------------------------------------------#


    # -----------------------------------------  Starting all processes in their threads  ------------------------------------------------------------#

    # Create tasks for both feeds and the processor
    candles_tasks, light_tasks_1, light_tasks_2, heavy_task_1, heavy_task_2 = [], [], [], [], []
    for token in candles:
        candles_tasks.append(candles[token]())# Await each Binance data feed task
        
    light_tasks_1.append(auctionListener())  # Await auction listener task
    light_tasks_1.append(auctionBidLogger())
    
    light_tasks_2.append(sui_event_listener())
    light_tasks_2.append(trade_capture_logger())
    
    heavy_task_1.append(processor.run())
    heavy_task_1.append(tq_hedger.run())
    
    heavy_task_2.append(pool_state())      
    
    threaded_tasks = [heavy_task_2, light_tasks_1, light_tasks_2, candles_tasks]    
    
    try:
        # Run all tasks concurrently
        with ThreadPoolExecutor(max_workers=10) as executor:  # Adjust max_workers as needed
            thread_futures = [
                executor.submit(run_event_loop_in_thread, task_group)
                for task_group in threaded_tasks if task_group  # Only submit non-empty groups
            ]
            
            await asyncio.gather(*heavy_task_1)

            # Wait for all threads to complete
            for future in thread_futures:
                future.result()

    except asyncio.CancelledError:
        print("Tasks cancelled. Starting cleanup.")
        await asyncio.sleep(6)
        print("Cleanup completed.")
    finally:
        all_tasks = [heavy_task_1] + threaded_tasks
        await cleanup(all_tasks)
        print("Finalizing shutdown.")
        

    
    
    
    
    
    
