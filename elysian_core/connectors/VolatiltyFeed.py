import asyncio
import datetime
import threading
import time
import numpy as np
from enum import Enum
from loguru import logger
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Dict, Optional
from shiolabs.utils.data_types import *
import shiolabs.utils.logger as log
from shiolabs.utils.constants import *
from redis import Redis
from pysui.sui.sui_types import bcs, SuiAddress
import math


logger = log.setup_custom_logger('root')



class CacheKeys(str, Enum):
    SUI_BALANCE = "barb:sui:balance"
    VOLATILITY = "barb:volatility"
    
def set_json(redis: Redis, prefix: str, key: str, value: any) -> bool:
    try:
        full_key = f"{prefix}:{key}" if key else prefix
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        redis.set(full_key, value)
        return True
    except Exception as e:
        logger.error(f"Error setting Redis key {full_key}: {e}")
        return False


def get_json(redis: Redis, prefix: str, key: str) -> Optional[any]:
    try:
        full_key = f"{prefix}:{key}" if key else prefix
        value = redis.get(full_key)
        if value is None:
            return None
        return json.loads(value)
    except json.JSONDecodeError:
        return value
    except Exception as e:
        logger.error(f"Error getting Redis key {full_key}: {e}")
        return None


class BarbClientBase:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.stopped = False
        logger.info(f"Initialized BarbClient Redis: {redis}")

    def publish_sui_balance(self, balances: dict[SuiAddress]):
        key = ""
        return set_json(self.redis, CacheKeys.SUI_BALANCE, key, balances)
    
class RedisConfig:
    def __init__(self, host: str = None, port: int = None):
        import os
        self.host = host or os.getenv("REDIS_HOST", "localhost")
        self.port = port or int(os.getenv("REDIS_PORT", "6379"))
    
    def local(self) -> Redis:
        return Redis(host=self.host, port=self.port, decode_responses=True)

    
class VolatilityBarbClientLocal(BarbClientBase):    
        
    @staticmethod
    def is_stable(token: str) -> bool:
        return token.upper() in {'USDT', 'USDC', 'DAI', 'BUSD'}
    
    @staticmethod
    async def new(
        redis: Optional[Redis] = None,
    ) -> "VolatilityBarbClientLocal":
        
        # TODO add log if redis is sucessfully connected 
        if redis is None:
            REDIS_CONFIG = RedisConfig()
            redis = REDIS_CONFIG.local()

        return VolatilityBarbClientLocal(redis)
    
    def get_sui_balance(self) -> dict[SuiAddress]:
        key = ""
        return get_json(self.redis, CacheKeys.SUI_BALANCE, key)
    
    
    def set_volatility_1s(self, base: str, quote: str, payload: Volatility1s):
        value = payload.to_json()
        token0, token1 = sorted([base, quote])
        set_json(
            self.redis,
            CacheKeys.VOLATILITY,
            f"{token0}.{token1}".lower(),
            value,   
        )
        
    def get_volatility_1s_raw(self, base: str, quote: str, setup: bool = False) -> Optional[float]:
        """
        get 1s volatility
        order of base/quote does not matter
        """
        if not setup:
            return 0.0005 # CURRENT FIXED VALUE 5bps
        
        if self.is_stable(base):
            base = "USDT"

        # if self.is_stable(quote):
        #     quote = "USDT"

        token0, token1 = sorted([base, quote])
        redis_key = f"{token0}.{token1}".lower()
        payload = get_json(self.redis, CacheKeys.VOLATILITY, redis_key)
        if payload is None:
            logger.warning(f"Volatility({redis_key}) not found in redis")
            return None

        vol_entry = Volatility1s.from_json(payload)
        if vol_entry is None:
            logger.error(
                f"Volatility({redis_key}) not found in redis. payload={payload}"
            )
            return None

        volatility_ts = vol_entry.timestamp_sec
        current_ts = time.time()
        if current_ts - volatility_ts > 3:
            logger.warning(
                f"Volatility({redis_key}) is stale. value={vol_entry} current_time={current_ts} delta_time={current_ts - volatility_ts}s"
            )
            return None
        return vol_entry.volatility
    
    
    def get_current_vol(self, base: str, quote: str, duration: float = 0.8) -> Optional[float]:
        '''
        Based on the current 1s vol, return the current vol scaled to the time duration. currently set to 0.8s for Shio
        '''
        try:
            scaled_vol = self.get_volatility_1s_raw(base, quote) * math.sqrt(duration)
            return scaled_vol
        except Exception as e:
            logger.error(f"Error encountered in Vol Feed getting current vol: {e}")
            return None
        
        
        
        
        
