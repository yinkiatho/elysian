import asyncio
import os
import pylru
import pandas as pd
import json
import datetime
import shiolabs.utils.logger as log 
from shiolabs.exchange.ShioBidBuilder import ShioBidBuilder
from binance.helpers import round_step_size
from shiolabs.calculators.AuctionEventFilter import AuctionEventFilter
from shiolabs.connectors.ActualAuctionListener import ActualAuctionListener
from shiolabs.pools.PoolManager import BaseCetusV3PoolManager
from shiolabs.connectors.SuiEventListener import SuiEventListener
from shiolabs.exchange.TQExchange import TQHedger
from shiolabs.connectors.VolatiltyFeed import VolatilityBarbClientLocal
from shiolabs.utils.constants import *
from shiolabs.utils.v3Functions import * 
from shiolabs.utils.data_types import *
from pathlib import Path
import collections
from shiolabs.pools.math_utils import *
from itertools import count
import time
from shiolabs.connectors.VolatiltyFeed import *



logger = log.setup_custom_logger('root')


class Processor:
    '''
    Main Processor class, listens to Binance data sockets as well as Auction Events
    '''
    def __init__(
        self,
            datafeed: dict,
            tokenlist: list,
            eventfilter: AuctionEventFilter,
            bidBuilder: ShioBidBuilder,
            poolManager: BaseCetusV3PoolManager,
            sui_event_listener: SuiEventListener,
            tq_hedger: TQHedger,
            vol_feed: VolatilityBarbClientLocal,
            timestamp: str,
            #actual_auction_listener: ActualAuctionListener = None,
    ):
        self.data_feed = datafeed
        self.last_auction_timestamp = None
        self.tokenlist = tokenlist
        self.event_filter = eventfilter
        self.token_dictionary = set(ASSET_TO_ADDRESS.keys())
        self.cex_tokens = set()
        
        self.initialize()
        self.start_timestamp = timestamp
        
        self.sui_event_listener = sui_event_listener
        self.pool_manager = poolManager
        self.bid_builder = bidBuilder
        self.tq_hedger = tq_hedger
        #self.actual_auction_listener = actual_auction_listener
        self.vol_feed = vol_feed
        
        self.trade_id = 0
        
        # JSON file to save trades sent
        self.transactions_sent = {}
        self.utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))
        
        
    
    def initialize(self):
        # Loop through each token pair in tokenList
        for token_pair in self.tokenlist:
            # Split the token pair and add each component to cex_tokens
            for token in self.token_dictionary:
                if token in token_pair:
                    self.cex_tokens.add(token)
                    
    
    def is_rebalancing(self):
        return self.tq_hedger.REBALANCING
        
        
    @staticmethod
    async def sleep(time: int = None):
        if time is None:
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(time)
            
    async def get_current_price(self, pair, side=None):
        try:
            if pair in self.data_feed['CANDLES']:
                if side == 'buy':
                    return self.data_feed['CANDLES'][pair].latest_ask_price
                elif side == 'sell':
                    return self.data_feed['CANDLES'][pair].latest_bid_price
                else:
                    return self.data_feed['CANDLES'][pair].latest_price
            
            # Identify the base and quote tokens using cex_tokens set
            base, quote = None, None
            for token in self.cex_tokens:
                if pair.startswith(token) and pair[len(token):] in self.cex_tokens:
                    base = token
                    quote = pair[len(token):]
                    break
                elif pair.endswith(token) and pair[:len(pair) - len(token)] in self.cex_tokens:
                    quote = token
                    base = pair[:len(pair) - len(token)]
                    break
                
            logger.info(f'{base}, {quote}')
            flipped = quote + base
            if flipped in self.data_feed['CANDLES']:
                logger.warning('alternate found but shouldnt be')
                if side == 'buy':
                    return 1 / self.data_feed['CANDLES'][quote + base].latest_ask_price
                elif side == 'sell':
                    return 1 / self.data_feed['CANDLES'][quote + base].latest_bid_price
                else:
                    return 1 / self.data_feed['CANDLES'][quote + base].latest_price
                
            elif flipped.endswith('USDC') and not flipped.startswith('USDT'):
                logger.warning('alternate found but shouldnt be, can construct with USDT')
                logger.warning(f"Using {quote + 'USDT'} as proxy")
                output = await self.get_current_price(quote + 'USDT', side=side)
                return 1 / output

            
            ##### SHOULD REMOVE THIS ###### USING ANOTHER STABLECOIN AS PRICE, USING FOR BYBIT TOKENS
            elif (base and quote and (quote == 'USDC' or quote == 'USDT') and not (base == 'USDC' or base == 'USDT')):
                logger.warning(f"Trying to use alternate stablecoin pair price")
                alt_quote = 'USDT' if quote == 'USDC' else 'USDC'
                #logger.info(f"Alternate: {base + alt_quote}")
                if base + alt_quote in self.data_feed['CANDLES']:
                    logger.info(f"Found alternate: {base + alt_quote}")
                    output = await self.get_current_price(base + alt_quote, side=side)
                    return output
            
            
            # Try to construct the price if base and quote are identified
            elif base and quote:
                pair1 = base + "USDT"
                pair2 = quote + "USDT"
                # Check if both related pairs are available
                if pair1 in self.data_feed['CANDLES'] and pair2 in self.data_feed['CANDLES']:
                    
                    if side is None:
                        price_base_usdt = self.data_feed['CANDLES'][pair1].latest_price
                        price_quote_usdt = self.data_feed['CANDLES'][pair2].latest_price
                        return price_base_usdt/price_quote_usdt
                    elif side == 'buy':
                        price_base_usdt = self.data_feed['CANDLES'][pair1].latest_ask_price
                        price_quote_usdt = self.data_feed['CANDLES'][pair2].latest_bid_price
                        return price_base_usdt/price_quote_usdt
                    elif side == 'sell':
                        price_base_usdt = self.data_feed['CANDLES'][pair1].latest_bid_price
                        price_quote_usdt = self.data_feed['CANDLES'][pair2].latest_ask_price
                        return price_base_usdt/price_quote_usdt
                else:
                    logger.warning(f"Could not find price for {pair} or construct it using USDT pairs.")
                    return None
            else:
                logger.warning(f"Could not identify base and quote tokens for {pair}")
        except Exception as e:
            logger.error(f"Encountered Exception in get_current_price: {e}")
            
            
    def get_current_vol_v2(self, tokenB: str, tokenA: str) -> Optional[float]:
        '''
        Getting current volatility using the Redis, we will assume to be tokenB/tokenA ie. SUI/USDC
        Returns the vol in bps
        '''
        try:
            vol = self.vol_feed.get_current_vol(base=tokenB, quote=tokenA, duration=0.8)
            return vol * 10000  
        except Exception as e:
            logger.error(f"Error in get_current_vol_v2: {e}")
            return None        
    
    async def get_to_sui_price(self, token: str):
        '''
        Gets to SUI price with an inputted token, constructs using the USDT pairs if given a non stable coin and if given stable coin directly quotes 
        '''
        try:
            # Check if there is a direct SUI pair for the token
            sui_pair = f"SUI{token}"
            if sui_pair in self.data_feed['CANDLES']:
                return 1 / self.data_feed['CANDLES'][sui_pair].latest_price

            # Check if there is a USDT pair for the token
            usdt_pair = f"{token}USDT"
            sui_usdt_pair = "SUIUSDT"
            
            if usdt_pair in self.data_feed['CANDLES'] and sui_usdt_pair in self.data_feed['CANDLES']:
                token_to_usdt = self.data_feed['CANDLES'][usdt_pair].latest_price
                sui_to_usdt = self.data_feed['CANDLES'][sui_usdt_pair].latest_price
                
                # Log the conversion path with the calculated values
                # logger.info(
                #     f"Conversion path for {token} to SUI: "
                #     f"{token} -> USDT = {token_to_usdt}, SUI -> USDT = {sui_to_usdt}, "
                #     f"{token} -> SUI = {token_to_usdt / sui_to_usdt}"
                # )
                return token_to_usdt / sui_to_usdt
            
            # Log a warning if the token cannot be directly or indirectly converted to SUI
            logger.error(f"Could not find SUI price for {token} using existing pairs.")
            return None
        
        except Exception as e:
            logger.error(f"Encountered Exception in get_to_sui_price: {e}")
            return None
        
        
    def get_last_executed_price(self, amount_in: int, amount_out: int, token_a: str, token_b: str, atob: bool):
        '''Gets last executed price based on ratios, A is USDC, B is SUI for eg. '''
        decimals_a, decimals_b = DECIMALS[token_a], DECIMALS[token_b]
        if atob:
            return (amount_in / (10**decimals_a)) / (amount_out / (10**decimals_b))
        else:
            return (amount_out / (10**decimals_a)) / (amount_in / (10**decimals_b))
        
                
    async def process_swap_events_v2(self, poolSwapEvents: dict, gasPrice: int, tx_id: str):
        '''
        Function that processes swap events and sends out orders, layers transcations on top to layer and send to ShioBidBuilder
        
        This function WIP, processes each auction asynchronously and gathers the results to send out
        '''
        try:
            #start_time = time.time()
            lock = asyncio.Lock()
            transactions_to_sent = asyncio.Queue()
            pools_touched = {}
            async def process_single_swap(asset_pair: str, poolSwap: dict, i: int, tx_id: str, gasPrice: int):
                logger.info(f"Processing Pool Swap {i}, {asset_pair}")
                
                order_id = self.trade_id

                if asset_pair == 'USDCUSDT' or asset_pair == 'USDTUSDC':
                    logger.warning(f"Skipping processing auction of {asset_pair}, double stablecoin pairs")
                    return
                
                current_price = await self.get_current_price(asset_pair)
                if current_price is None or poolSwap.get('poolAddress') in pools_touched:
                    return
            
                # 1 Cex trade is enough
                after_liquidity, after_tick_index, tick_spacing = poolSwap['liquidity'], poolSwap['current_tick_index'], poolSwap['tick_spacing']
                if asset_pair in self.data_feed['CANDLES'] or asset_pair[-4:] == 'USDC' or asset_pair[-4:] == 'USDT':
                    current_price_buy = await self.get_current_price(asset_pair,side='buy')
                    current_price_sell = await self.get_current_price(asset_pair,side='sell')
                
                    asset_1, asset_2 = poolSwap['Token A'], poolSwap['Token B']
                    
                    # Process if profitable, then make the trade
                    after_price = 1 /  MathUtil.sqrt_price_x64_to_price(str(poolSwap['after_sqrt_price']), poolSwap['decimals_a'], poolSwap['decimals_b'])
                    logger.info(f"After Sqrt Price: {after_price}, Cex Price: {current_price}, CEX Price Buy: {current_price_buy}, CEX Price Sell: {current_price_sell}, Raw After Sqrt Price : {poolSwap['after_sqrt_price']}")
                    logger.info(f"Pool After Liquidity: {after_liquidity}")
                    pool_fee, cex_fee = int(poolSwap.get('Fee Type')) / (10000 * 100), 0.00025
                    amount_a, amount_b = poolSwap['amount_a'], poolSwap['amount_b']
                    
                    VOL_THRESHOLD = self.get_current_vol_v2(tokenB=asset_2, tokenA=asset_1)
                    
                    logger.info(f"Fees, Pool: {pool_fee * 10000} bps, CEX: 2.5bps, Asset A: {asset_1}, Asset B: {asset_2}. Volatility Threshold: {VOL_THRESHOLD} BPS")
                    # 100 is 0.01% , 2500 is 0.25%
                    
                    incomingAsset = amount_a if poolSwap['a_to_b'] else amount_b
                    #logger.info(f"Incoming Swap of {incomingAsset} {asset_1 if poolSwap['a_to_b'] else asset_2} in Pool {asset_2}/{asset_1}, current pool after price: {after_price} {asset_2}/{asset_1}, current cex: {current_price} {asset_2}/{asset_1}")
                    
                    a_to_sui, b_to_sui = await self.get_to_sui_price(asset_1), await self.get_to_sui_price(asset_2)
                    
                    # We will buy on pool sell in cex, for now we just do the bid summission
                    if after_price < current_price_sell:
      
                        mev_value, max_amount_in_a, estimated_executed_price, profit_margin = self.pool_manager.get_mev_value_v3_optimize(
                                                                                                            poolSwap.get('poolAddress'),
                                                                                                            current_price_sell,
                                                                                                            a_to_sui,
                                                                                                            b_to_sui,
                                                                                                            current_price=after_price,
                                                                                                            opp_tx_digest=tx_id,
                                                                                                            liquidity=after_liquidity,
                                                                                                            vol_threshold=VOL_THRESHOLD
                                                                                                        )
                        
                        mev_params = {'pool_object_id': poolSwap.get('poolAddress'), 'ref_price': float(current_price_sell), 'a_to_sui':a_to_sui, 'b_to_sui':b_to_sui, 
                                      'current_price': float(after_price), 'cex_fees_mul': 1, 'opp_tx_digest': tx_id, 
                                      'liquidity': after_liquidity, #'amount_in_a_sim': float(amount_in_a_sim), 'amount_in_b_sim': float(amount_in_b_sim)
                                      }
                        
                        synthetic_values = {}
                        
                        logger.info(f"MEV VALUE: {mev_value} SUI, Max Amount In: {max_amount_in_a} {asset_1}")
                        logger.info(f"Estimated Profit Margin: {profit_margin}")
                        
                        if mev_value > 0 and profit_margin > VOL_THRESHOLD:
                            swap_param = await self.build_swap_params(poolSwap, a2b=True, amount=max_amount_in_a, tokenA=asset_1, tokenB=asset_2)
                            
                            transaction = {
                                'poolSwap': poolSwap, 'swap_params': swap_param, 'order_id': order_id, 'bid_amount': mev_value, 
                                'estimated_executed_price': estimated_executed_price, 'timestamp': datetime.datetime.now(self.utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S.%f'),
                                #'mev_value_sim': mev_value_sim, 'amount_in_sim': amount_in_a_sim, 'executed_price_sim': estimated_executed_price_sim,
                                'synthetic_values': synthetic_values, 'a_to_sui': a_to_sui, 'b_to_sui': b_to_sui, 'SUI Price': self.data_feed['CANDLES']['SUIUSDT'].latest_price,
                                'mev_params': mev_params, 'vol_threshold': VOL_THRESHOLD
                            }
                            
                            await transactions_to_sent.put(transaction)
                            async with lock:
                                pools_touched[poolSwap.get('poolAddress')] = True
                                self.trade_id += 1
            
                    elif after_price > current_price_buy: # and abs(after_price - current_price)/current_price > (pool_fee + cex_fee):
                        
                        mev_value, max_amount_in_b, estimated_executed_price, profit_margin = self.pool_manager.get_mev_value_v3_optimize(
                                                                                                            poolSwap.get('poolAddress'),
                                                                                                            current_price_buy,
                                                                                                            a_to_sui,
                                                                                                            b_to_sui,
                                                                                                            current_price=after_price,
                                                                                                            opp_tx_digest=tx_id,
                                                                                                            liquidity=after_liquidity,
                                                                                                            vol_threshold=VOL_THRESHOLD
                                                                                                        )
                        
                        mev_params = {'pool_object_id': poolSwap.get('poolAddress'), 'ref_price': float(current_price_buy), 'a_to_sui':a_to_sui, 'b_to_sui':b_to_sui, 
                                      'current_price': float(after_price), 'cex_fees_mul': 1, 'opp_tx_digest': tx_id, 
                                      'liquidity': after_liquidity, #'amount_in_a_sim': float(amount_in_a_sim), 'amount_in_b_sim': float(amount_in_b_sim)
                                      }
                        
                        synthetic_values = {}
                    
                        logger.info(f"MEV VALUE: {mev_value} SUI, Max Amount In: {max_amount_in_b} {asset_2}")
                        logger.info(f"Estimated Profit Margin: {profit_margin}")
                        
                        if mev_value > 0 and profit_margin > VOL_THRESHOLD:
                            swap_param = await self.build_swap_params(poolSwap, a2b=False, 
                                                                    amount=max_amount_in_b, gasPrice=gasPrice, tokenA=asset_1, tokenB=asset_2)
                            transaction = {
                                'poolSwap': poolSwap, 'swap_params': swap_param, 'order_id': order_id, 'bid_amount': mev_value, 
                                'estimated_executed_price': estimated_executed_price, 'timestamp': datetime.datetime.now(self.utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S.%f'),
                                #'mev_value_sim': mev_value_sim, 'amount_in_sim': amount_in_b_sim, 'executed_price_sim': estimated_executed_price_sim,
                                'synthetic_values': synthetic_values, 'a_to_sui': a_to_sui, 'b_to_sui': b_to_sui, 'SUI Price': self.data_feed['CANDLES']['SUIUSDT'].latest_price,
                                'mev_params': mev_params, 'vol_threshold': VOL_THRESHOLD
                            }
                            await transactions_to_sent.put(transaction)
                            async with lock:
                                pools_touched[poolSwap.get('poolAddress')] = True
                                self.trade_id += 1
                    
                    else:
                        logger.info(f"Not Enough differential")
                
                else:
                    asset_1, asset_2 = poolSwap['Token A'], poolSwap['Token B']
                    cex_1, cex_2 = asset_1 + 'USDT',  asset_2 + 'USDT'
                    #logger.info(f"Asset 1: {asset_1}, Asset 2: {asset_2}, Cex 1: {cex_1}, Cex 2: {cex_2}")
                    
                    #cex_1_price, cex_2_price = self.data_feed['CANDLES'][cex_1].latest_price, self.data_feed['CANDLES'][cex_2].latest_price
                    cex_1_price, cex_2_price = await self.get_current_price(cex_1), await self.get_current_price(cex_2)
                    cex_1_price_buy, cex_2_price_buy = await self.get_current_price(cex_1, side='buy'), await self.get_current_price(cex_2, side='buy')
                    cex_1_price_sell, cex_2_price_sell = await self.get_current_price(cex_1, side='sell'), await self.get_current_price(cex_2, side='sell')
                    
                    ref_cex_price = cex_2_price / cex_1_price
                    ref_cex_price_buy = cex_2_price_buy / cex_1_price_sell
                    ref_cex_price_sell = cex_2_price_sell / cex_1_price_buy
                    
                    VOL_THRESHOLD = self.get_current_vol_v2(tokenB=asset_2, tokenA=asset_1)
                                        
                    after_price = 1 /  MathUtil.sqrt_price_x64_to_price(str(poolSwap['after_sqrt_price']), poolSwap['decimals_a'], poolSwap['decimals_b'])
                    logger.info(f"After Sqrt Price: {after_price}, Cex Price: {current_price}, CEX Price Buy: {ref_cex_price_buy}, CEX Price Sell: {ref_cex_price_sell}, Raw After Sqrt Price : {poolSwap['after_sqrt_price']},  Volatility Threshold: {VOL_THRESHOLD} BPS")
                    
                    pool_fee, cex_fee = poolSwap.get('Fee Type') / (10000 * 100), 0.00025
                    amount_a, amount_b = poolSwap['amount_a'], poolSwap['amount_b']
                    # 100 is 0.01% , 2500 is 0.25%
                    logger.info(f"Fees, Pool: {pool_fee * 10000} bps, CEX: 2.5bps, Asset A: {asset_1}, Asset B: {asset_2}")
                    incomingAsset = amount_a if poolSwap['a_to_b'] else amount_b
                    logger.info(f"Incoming Swap of {incomingAsset} {asset_1 if poolSwap['a_to_b'] else asset_2} in Pool {asset_2}/{asset_1}, current pool after price: {after_price} {asset_2}/{asset_1}, current ref cex: {ref_cex_price} {asset_2}/{asset_1}")
                    
                    # We will need to place 2 trades, if buy on dex CETUSSUI, we will sell CETUSSUI by
                    if after_price < ref_cex_price_sell: # and abs(after_price - ref_cex_price)/ref_cex_price > (pool_fee + cex_fee * 2):                
                        
                        # We will buy the max in the opposite direction, buy B on DEX, sell B on CEX
                        a_to_sui, b_to_sui = await self.get_to_sui_price(asset_1), await self.get_to_sui_price(asset_2)
                        mev_value, max_amount_in_a, estimated_executed_price, profit_margin = self.pool_manager.get_mev_value_v3_optimize(
                                                                                                    poolSwap.get('poolAddress'),
                                                                                                    ref_cex_price_sell,
                                                                                                    a_to_sui,
                                                                                                    b_to_sui,
                                                                                                    current_price=after_price,
                                                                                                    cex_fees_mul=2,
                                                                                                    opp_tx_digest=tx_id,
                                                                                                    liquidity=after_liquidity,
                                                                                                    vol_threshold=VOL_THRESHOLD
                                                                                                )
                        
                        
                        mev_params = {'pool_object_id': poolSwap.get('poolAddress'), 'ref_price': float(ref_cex_price_sell), 'a_to_sui':a_to_sui, 'b_to_sui':b_to_sui, 
                                      'current_price': float(after_price), 'cex_fees_mul': 2, 'opp_tx_digest': tx_id, 
                                      'liquidity': after_liquidity, #'amount_in_a_sim': float(amount_in_a_sim), 'amount_in_b_sim': float(amount_in_b_sim)
                                      }
                        
                        
                        synthetic_values = {}
                        
                        logger.info(f"MEV VALUE: {mev_value} SUI, Max Amount In: {max_amount_in_a} {asset_1}")
                        logger.info(f"Estimated Profit Margin: {profit_margin}")
                        
                        if mev_value > 0 and profit_margin > VOL_THRESHOLD:
                            swap_param = await self.build_swap_params(poolSwap=poolSwap, a2b=True, 
                                                                    amount=max_amount_in_a, tokenA=asset_1, tokenB=asset_2)
                            
                            transaction = {
                                'poolSwap': poolSwap, 'swap_params': swap_param, 'order_id': order_id, 'bid_amount': mev_value, 'estimated_executed_price': estimated_executed_price,
                                #'mev_value_sim': mev_value_sim, 'amount_in_sim': amount_in_a_sim, 'executed_price_sim': estimated_executed_price_sim,  
                                'timestamp': datetime.datetime.now(self.utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S.%f'),
                                'synthetic_values': synthetic_values, 'a_to_sui': a_to_sui, 'b_to_sui': b_to_sui, 'SUI Price': self.data_feed['CANDLES']['SUIUSDT'].latest_price,
                                'mev_params': mev_params, 'vol_threshold': VOL_THRESHOLD
                        
                            }
                            
                            await transactions_to_sent.put(transaction)
                            async with lock:
                                pools_touched[poolSwap.get('poolAddress')] = True
                                self.trade_id += 1
                        
                    elif after_price > ref_cex_price_buy: # and abs(after_price - ref_cex_price)/ref_cex_price > (pool_fee + cex_fee * 2):
                        
                        a_to_sui, b_to_sui = await self.get_to_sui_price(asset_1), await self.get_to_sui_price(asset_2)

                        mev_value, max_amount_in_b, estimated_executed_price, profit_margin = self.pool_manager.get_mev_value_v3_optimize( 
                                                                                                                                        poolSwap.get('poolAddress'), ref_cex_price_buy, a_to_sui, b_to_sui, 
                                                                                                                                       current_price=after_price, cex_fees_mul=2, opp_tx_digest=tx_id, liquidity=after_liquidity,
                                                                                                                                       vol_threshold=VOL_THRESHOLD)
                        
        
                        mev_params = {'pool_object_id': poolSwap.get('poolAddress'), 'ref_price': float(ref_cex_price_buy), 'a_to_sui':a_to_sui, 'b_to_sui':b_to_sui, 'current_price': float(after_price), 'cex_fees_mul': 2, 'opp_tx_digest': tx_id, 
                                      'liquidity': float(after_liquidity), 
                                      #'amount_in_a_sim': float(amount_in_a_sim), 'amount_in_b_sim': float(amount_in_b_sim)
                                      }
                        
                        synthetic_values = {}
                        
                        logger.info(f"MEV VALUE: {mev_value} SUI, Max Amount In: {max_amount_in_b} {asset_2}")
                        logger.info(f"Estimated Profit Margin: {profit_margin}")
                        
                        if mev_value > 0 and profit_margin > VOL_THRESHOLD:
                            swap_param = await self.build_swap_params(poolSwap=poolSwap, a2b=False, amount=max_amount_in_b, tokenA=asset_1, tokenB=asset_2)
                            transaction = {
                                'poolSwap': poolSwap, 'swap_params': swap_param, 'order_id': order_id, 'bid_amount': mev_value, 'estimated_executed_price': estimated_executed_price,
                                #'mev_value_sim': mev_value_sim, 'amount_in_sim': amount_in_b_sim, 'executed_price_sim': estimated_executed_price_sim, 
                                'timestamp': datetime.datetime.now(self.utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S.%f'),
                                'synthetic_values': synthetic_values, 'a_to_sui': a_to_sui, 'b_to_sui': b_to_sui, 'SUI Price': self.data_feed['CANDLES']['SUIUSDT'].latest_price,
                                'mev_params': mev_params, 'vol_threshold': VOL_THRESHOLD
                            }
                            
                            await transactions_to_sent.put(transaction)
                            async with lock:
                                pools_touched[poolSwap.get('poolAddress')] = True
                                self.trade_id += 1
                    else:
                        logger.info(f"Not Enough differential")
                        
                        
            tasks = [
                asyncio.create_task(process_single_swap(asset_pair, poolSwap, i , tx_id, gasPrice))
                for asset_pair, poolSwaps in poolSwapEvents.items()
                for i, poolSwap in enumerate(poolSwaps)
            ]
            await asyncio.gather(*tasks)
                    
            transactions = []
            while not transactions_to_sent.empty():
                transactions.append(await transactions_to_sent.get())
            
            # end_time = time.time()
            # print(f"Time taken for processing transactions: {(end_time - start_time) * 1000} ms, processing {len(tasks)} transactions")
            asyncio.create_task(self.save_sent_bids_to_db(transactions_to_sent=transactions, tx_digest=tx_id))
            
            await self.bid_builder.build_transactions(transactions, tx_id)
            logger.info(f"Sent and bidded for {len(transactions)} transactions")
        except Exception as e:
            logger.error(f"Error at process_swap_events: {e}")
        
        
    async def save_sent_bids_to_db(self, transactions_to_sent: list, tx_digest: str):
        """
        Function saves sent bids to the database 
        """
        try:
            if len(transactions_to_sent) > 0:
                row = {'opp_tx_digest': tx_digest, 'auction_event': transactions_to_sent}
                AuctionEvents.insert(row)
                logger.info(f"Logged Auction Events and Bids to Postgres")
        except Exception as e:
            logger.error(f"Error encountered in saving sent bids and auction event to Postgres: {e}")
                
            
            
    async def build_swap_params(self, poolSwap: dict, tokenA: str, tokenB:str, a2b=True, amount: float = 0.01, gasPrice: int = 750, 
                                THRESHOLD=6):
        '''
        Makes the swap parameter that goes into the bid builder, price is number of bs to a, if 50 USDC/ETH
        THRESHOLD is denoted as USD value
        amount: human readable amount in the respective tokens , depending on a2b direction
        '''
        try:
            # We will temporary set a threshold limit of 10usdc
            mev_amount = amount
            token_a_balance, token_b_balance = self.tq_hedger.get_symbol_balance(tokenA, WalletType.WALLET), self.tq_hedger.get_symbol_balance(tokenB, WalletType.WALLET)
            
            if a2b and (tokenA == 'USDC' or tokenA == 'USDT'):
                input_amount = min(THRESHOLD, amount, token_a_balance)
                logger.info(f"Built Swap Amount of {input_amount}, Threshold Amount: {THRESHOLD}, Amount In: {amount}, Wallet Balance: {token_a_balance}")
            elif a2b:
                price = await self.get_current_price(tokenB + 'USDT', side='buy')
                input_amount = min(THRESHOLD, amount, token_a_balance)
                logger.info(f"Built Swap Amount of {input_amount}, Threshold Amount: {THRESHOLD}, Amount In: {amount}, Wallet Balance: {token_a_balance}")

            elif not a2b and (tokenA == 'USDC' or tokenA == 'USDT'):
                price = await self.get_current_price(tokenB + 'USDT', side='sell')
                input_amount = min(THRESHOLD / price, amount, token_b_balance)                
                logger.info(f"Built Swap Amount of {input_amount}, Threshold Amount: {THRESHOLD / price}, Amount In: {amount}, Wallet Balance: {token_b_balance}")

            elif not a2b:
                price = await self.get_current_price(tokenB + tokenA, side='sell')
                input_amount = min(THRESHOLD / price, amount, token_b_balance)
                logger.info(f"Built Swap Amount of {input_amount}, Threshold Amount: {THRESHOLD / price}, Amount In: {amount}, Wallet Balance: {token_b_balance}")


            swap_param = {
                'pool_id': poolSwap.get('poolAddress'),
                'coinTypeA': poolSwap.get('Token A Address'), # typically the stablecoin
                'coinTypeB': poolSwap.get('Token B Address'),
                'a2b': a2b,
                'by_amount_in': True,
                'mev_amount_in': int(mev_amount * (10 ** DECIMALS[ADDRESS_TO_ASSET[poolSwap.get('Token A Address')]])) if a2b else int(mev_amount * (10 ** DECIMALS[ADDRESS_TO_ASSET[poolSwap.get('Token B Address')]])),
                'amount': int(input_amount * (10 ** DECIMALS[ADDRESS_TO_ASSET[poolSwap.get('Token A Address')]])) if a2b else int(input_amount * (10 ** DECIMALS[ADDRESS_TO_ASSET[poolSwap.get('Token B Address')]])), 
                'amount_limit': 0,
                'swap_partner': None,
                'gasPriceToSet': gasPrice
            }
            
            # Adjusting Delta Balances we will deduct delta from the token out
            await self.tq_hedger.adjust_delta_balance(symbol=tokenA if a2b else tokenB,
                                                amount=-input_amount,
                                                wallet=WalletType.WALLET)
            return swap_param
        
        except Exception as e:
            logger.error(f"Error encountered building swap params processor: {e}")
        
        
    async def pull_trades_and_hedge(self):  
        '''
        Async function to pull trades and hedge it on CEX
        '''
        while True:
            if not self.is_rebalancing():
                trades = self.sui_event_listener.get_trades_to_hedge()
                if len(trades) != 0:
                    logger.info(f"Pulled Trades to hedge at {datetime.datetime.now(self.utc_plus_8)}")
                    await self.tq_hedger.parse_data_to_hedge(trades)
                    print()
                    print()
            await self.sleep(0.01)
            
            
            
    async def process_auctions(self):
        '''
        Async Function to process auctions
        ''' 
        while True:
            if self.last_auction_timestamp == self.data_feed['AUCTIONS'].last_timestamp or self.is_rebalancing():
                #rint(len(self.data_feed['AUCTIONS'].events)
                await self.sleep(0.0025)
                continue
                
            auction_event, auction_event_tx_digest = self.data_feed['AUCTIONS'].latest_event, self.data_feed['AUCTIONS'].latest_event_tx_digest
            logger.info(f"Received new auction event at {self.data_feed['AUCTIONS'].last_timestamp}, Tx Digest: {auction_event_tx_digest}, currently at {datetime.datetime.now(self.utc_plus_8).strftime('%Y-%m-%d_%H-%M-%S')}")
            self.last_auction_timestamp = self.data_feed['AUCTIONS'].last_timestamp
            
            poolSwapEvents, gasPrice, tx_id = await self.event_filter.decoder(auction_event)
            await self.process_swap_events_v2(poolSwapEvents, gasPrice, tx_id)
            print()
            print()
        
        
    async def run(self):
        await self.sleep(90)
        logger.info(f"Initial Sleep completed, starting main loop")
        
        # Create tasks for your async functions
        tasks = [
            asyncio.create_task(self.process_auctions()),
            asyncio.create_task(self.pull_trades_and_hedge()),
            #asyncio.create_task(self.periodic_save_sent_bids_to_file())
        ]
        
        # Use asyncio.gather to run them concurrently
        await asyncio.gather(*tasks)
        