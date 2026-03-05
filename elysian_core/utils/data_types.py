import datetime
import pandas as pd
from typing import Optional, List
from peewee import Model, PostgresqlDatabase, CharField, DateTimeField, FloatField, IntegerField, AutoField, TextField, BooleanField
from playhouse.shortcuts import ReconnectMixin
from playhouse.postgres_ext import *
import os
import shiolabs.utils.logger as log
from dotenv import load_dotenv
from enum import Enum
import betterproto
from dataclasses import asdict, dataclass
import requests
from shiolabs.utils.constants import *


load_dotenv()
logger = log.setup_custom_logger('root')
utc_plus_8 = datetime.timezone(datetime.timedelta(hours=8))


@dataclass
class BinanceKline:
    start_time: datetime.datetime
    end_time: datetime.datetime
    ticker: str
    interval: str
    open: float = None
    close: float = None
    high: float = None
    low: float = None
    volume: float = None

    def __str__(self):
        return f'Binance Candle - start_time: {self.start_time}, end_time: {self.end_time}, ticker: {self.ticker}, ' \
               f'interval: {self.interval}, open: {self.open}, close: {self.close}, ' \
               f'high: {self.high}, low: {self.low},' \
               f'volume: {self.volume}'
               
               
               
@dataclass
class BinanceOrderBook:
    lastUpdateID: int
    lastTimestamp: int
    ticker: str
    interval: str
    best_bid_price: float
    best_bid_amount: float
    best_ask_price: float
    best_ask_amount:float
    bid_orders: list
    ask_orders: list
    
    def __str__(self):
        return (f"Binance Order Book - ticker: {self.ticker}, lastUpdateID: {self.lastUpdateID}"
                f"interval: {self.interval}, best_bid_price: {self.best_bid_price}, "
                f"best_ask_price: {self.best_ask_price}, "
                f"bid_orders: {self.bid_orders}, ask_orders: {self.ask_orders}")
        
        
    def convert_to_pd(self):
        # Convert the order book instance into a single-row Pandas DataFrame
        data = {
            "lastUpdateID": self.lastUpdateID,
            "ticker": self.ticker,
            "interval": self.interval,
            "best_bid_price": self.best_bid_price,
            "best_bid_amount": self.best_bid_amount,
            "best_ask_price": self.best_ask_price,
            "best_ask_amount": self.best_ask_amount,
            "bid_orders": [self.bid_orders],  # Stored as a list to keep as a single DataFrame cell
            "ask_orders": [self.ask_orders],  # Stored as a list to keep as a single DataFrame cell
        }
        return pd.DataFrame([data])
    
    
class APICommands(Enum):
    Empty = 0
    Register = 1
    LiquidityDeposit = 2
    RegisterWithdrawalAddress = 3
    WithdrawAsset = 4
    AssetBalances = 5
    SetRangeOrderByLiquidity = 8
    SetRangeOrderByAmounts = 9
    UpdateLimitOrder = 10
    SetLimitOrder = 11
    GetOpenSwapChannels = 12
    CancelAllOrders = 13


class RPCCommands(Enum):
    Empty = 0
    AccountInfo = 1
    RequiredRatioForRangeOrder = 3
    PoolInfo = 4
    PoolDepth = 5
    PoolLiquidity = 6
    PoolOrders = 7
    PoolRangeOrdersLiquidityValue = 8
    PoolScheduledSwaps = 9  # New
    AccountAssetBalances = 10

class StreamCommands(Enum):
    Empty = 0
    SubscribePoolPrice = 1
    SubscribePreWitnessedSwaps = 2


class NetworkStatus(Enum):
    STOPPED = 0
    NOT_CONNECTED = 1
    CONNECTED = 2


class Chains(Enum):
    Ethereum = 'Ethereum'
    Bitcoin = 'Bitcoin'
    Polkadot = 'Polkadot'


ASSETS = {
    'USDC': 'USDC',
    'ETH': 'ETH',
    'BTC': 'BTC',
    'DOT': 'DOT'
}


class Side(Enum):
    BUY = 'Buy',
    SELL = 'Sell',
    NONE = 'None',


class IncreaseOrDecreaseOrder(Enum):
    INCREASE = "Increase",
    DECREASE = "Decrease"


class WaitForOption(Enum):
    NO_WAIT = "NoWait"
    IN_BLOCK = "InBlock"
    FINALIZED = "Finalized"


class RangeOrderType(Enum):
    LIQUIDITY = 1
    ASSET = 2


DECIMALS = {
    'DOT': 10,
    'ETH': 18,
    'FLIP': 18,
    'BTC': 8,
    'USDC': 6
}

UNIT_CONVERTER = {
    'USDC': 10 ** 6,
    'ETH': 10 ** 18,
    'BTC': 10 ** 8,
    'DOT': 10 ** 10,
    'FLIP': 10 ** 18,
    'USDT': 10 ** 6,
    'SOL': 10 ** 9
}

TICK_SIZE = 2

BLOCK_TIMINGS = {
    'Chainflip': 6,
    'Bitcoin': 600,
    'BTC': 600,
    'Ethereum': 6,
    'ETH': 6,
    'Polkadot': 6,
    'DOT': 6,
    'USDC': 6
}

CHAINFLIP_BLOCK_CONFIRMATIONS = {
    'Ethereum': 8,  # i.e. 8 blocks at 6 secs a block
    'ETH': 8,
    'Bitcoin': 3,  # i.e. 3 blocks at 600 secs (10 mins) a block - on mainnet btc = 3 blocks
    'BTC': 3,
}

    
    
# ----------------------------------- PROTO MESSAGING IMPLEMENTATION DATA TYPES ---------------------------------------------------------------------#
class SwapType(betterproto.Enum):
    UNK = 0
    BLUEFIN = 1
    CETUS = 2
    FLOW_X = 3
    TURBOS = 4


@dataclass(eq=False, repr=False)
class TxParameters(betterproto.Message):
    coin_gas: str = betterproto.string_field(1)
    gas_budget: str = betterproto.string_field(2)


@dataclass(eq=False, repr=False)
class SignSwapResponseSuccess(betterproto.Message):
    tx_bytes: str = betterproto.string_field(1)
    signatures: List[str] = betterproto.string_field(2)


@dataclass(eq=False, repr=False)
class SignSwapResponseError(betterproto.Message):
    reason: str = betterproto.string_field(1)


@dataclass(eq=False, repr=False)
class SignSwapResponse(betterproto.Message):
    success: bool = betterproto.bool_field(1)
    signer_version: str = betterproto.string_field(2)
    response: "SignSwapResponseSuccess" = betterproto.message_field(
        3, group="responses"
    )
    error: "SignSwapResponseError" = betterproto.message_field(4, group="responses")

    
class Volatility1s:
    def __init__(self, timestamp_sec, volatility) -> None:
        self.timestamp_sec: float = timestamp_sec
        self.volatility: float = volatility
        
        
    def repr_cls(self, obj):
        """
        Generates a string representation of an object, including its class name and attributes.
        """
        cls_name = obj.__class__.__name__
        attrs = ", ".join(f"{key}={value!r}" for key, value in vars(obj).items())
        return f"{cls_name}({attrs})"
    

    def __repr__(self) -> str:
        return self.repr_cls(self)
    

    def to_json(self):
        return self.__dict__

    @classmethod
    def from_json(cls, json):
        try:
            return Volatility1s(
                    json["timestamp_sec"],
                    json["volatility"],
                )
        except Exception:
            return None
        
        
class WalletType(str, Enum):
    WALLET = 'sui_wallet'
    CEX = 'cex'
    
    
# ------------------------------------ DATA-TYPES FOR TRADE CAPTURE ----------------------------------------------------------------###
config_file_path = 'shiolabs/config/configuration.json'
with open(config_file_path, 'r') as file:
    config = json.load(file)
    
        
class Venue(Enum):
    TURBOS = "TURBOS"
    DEEPBOOK = "deepbook"
    CETUS = "CETUS PROTOCOL"
    
    def to_dict(self):
        return self.value
    
    @classmethod
    def from_value(cls, value):
        for member in cls:
            if member.value == value:
                return member
        raise ValueError(f"{value} is not a valid value for {cls.__name__}")
    
        
@dataclass
class Pool:
    address: str
    venue: Venue
    
    def to_dict(self):
        return {"address": self.address, "venue": self.venue.to_dict()}



class SuiReference:
    def __init__(self, pools: dict ={}):
        self._pools = pools  # address -> Pool
    
    def pool_by_address(self, address: str) -> Pool:
        return self._pools.get(address)


##### ----- LOADING ALL THE SUI REFERENCES -------- #####
TOTAL_POOLS = {}
for pool_name, address, venue_value in config.get('Pools'):
    pool = Pool(address=address, venue=Venue.from_value(venue_value))
    TOTAL_POOLS[address] = pool
    

sui_ref = SuiReference(TOTAL_POOLS)


# Add enums and base classes first
class Chain(Enum):
    TON = "ton"
    SUI = "sui"

    def account_id(self, id: int) -> int:
        return id
    
    def to_dict(self) -> dict:
        return {
            "chain": self.value,  # Enum member name (e.g., "TON")
        }
    
    

class TradeSubtype(Enum):
    SWAP = "Swap"
    
    def to_dict(self):
        return self.value
    

class CashflowSubType(Enum):
    SWAP_IN = "TransferIn"
    SWAP_OUT = "TransferOut"
    GAS_FEE = "GasFee"
    FEE_REBATE = "FeeRebate"
    
    def to_dict(self):
        return self.value
    

class Counterparty:
    def __init__(self, name: str, venue: str):
        self.name = name
        self.venue = venue

    @staticmethod
    def from_venue(venue: Venue) -> "Counterparty":
        return Counterparty(name=venue.value, venue=venue.value)
    
    
    def to_dict(self):
        return self.name

@dataclass
class Coin:
    symbol: str
    address: str
    decimals: int
    
    def to_dict(self):
        return {
            "symbol": self.symbol,
            "address": self.address,
            "decimals": self.decimals,
        }



@dataclass
class SuiTrade:
    transaction_hash: str
    pool_address: str
    sell_address: str
    buy_address: str
    sell_wei: str
    buy_wei: str
    
    def to_dict(self):
        return {
            "transaction_hash": self.transaction_hash,
            "pool_address": self.pool_address,
            "sell_address": self.sell_address,
            "buy_address": self.buy_address,
            "sell_wei": self.sell_wei,
            "buy_wei": self.buy_wei,
        }


SUI_COIN = Coin(
    symbol="SUI",
    address="SUI_ADDRESS",  # Replace with actual address
    decimals=9
)

def coin_by_address(address: str) -> Coin:
    symbol = ADDRESS_TO_ASSET[address]
    decimals = DECIMALS[address]
    return Coin(symbol, address, decimals)
    #return _coin_registry.get(address)


def pool_by_address(address: str) -> Pool:    
    return sui_ref.get(address)
    #return _pool_registry.get(address)


# Your existing code continues below...
def isotimestamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def format_amount(a: int) -> str:
    if a >= 0:
        h = hex(a)
    else:
        nbits = 256
        h = hex((a + (1 << nbits)) % (1 << nbits))
    return h



@dataclass
class Trade:

    trade_identifier: str
    ref_id: Optional[str]
    trade_subtype: TradeSubtype
    in_amount: str
    in_symbol: str
    in_address: str
    in_decimals: int
    out_amount: str
    out_symbol: str
    out_address: str
    out_decimals: int

        
    @staticmethod
    def from_sui_trade(trade: SuiTrade):
        trade_subtype = TradeSubtype.SWAP
        coin_sell = coin_by_address(trade.sell_address)
        coin_buy = coin_by_address(trade.buy_address)

        return Trade(
            trade_identifier=f"{trade.transaction_hash}_{trade_subtype.value}",
            ref_id=None,
            trade_subtype=trade_subtype,
            in_amount=format_amount(abs(int(trade.sell_wei))),
            in_symbol=coin_sell.symbol,
            in_address=coin_sell.address,
            in_decimals=coin_sell.decimals,
            out_amount=format_amount(abs(int(trade.buy_wei))),
            out_symbol=coin_buy.symbol,
            out_address=coin_buy.address,
            out_decimals=coin_buy.decimals,
        )
        
    @staticmethod
    def from_trade_json(trade: dict):
        trade_subtype = TradeSubtype.SWAP
        in_symbol = trade.get('token_b') if trade.get('a_to_b') else trade.get('token_a')
        out_symbol = trade.get('token_a') if trade.get('a_to_b') else trade.get('token_b')
        
        return Trade(
            trade_identifier=f"{trade.get('tx_digest')}_{trade_subtype.value}",
            #transaction_hash=trade.get('tx_digest'),
            ref_id=None,
            trade_subtype=trade_subtype,
            in_amount=format_amount(trade.get('amount_in')),
            in_symbol=in_symbol,
            in_address=trade.get('token_b_address') if trade.get('a_to_b') else trade.get('token_a_address'),
            in_decimals=DECIMALS[in_symbol],
            out_amount=format_amount(trade.get('amount_out')),
            out_symbol=out_symbol,
            out_address=trade.get('token_a_address') if trade.get('a_to_b') else trade.get('token_b_address'),
            out_decimals=DECIMALS[out_symbol],
        )
        
    def to_dict(self):
        return {
            "trade_identifier": self.trade_identifier,
            "ref_id": self.ref_id,
            "trade_subtype": self.trade_subtype.to_dict(),
            "in_amount": self.in_amount,
            "in_symbol": self.in_symbol,
            "in_address": self.in_address,
            "in_decimals": self.in_decimals,
            "out_amount": self.out_amount,
            "out_symbol": self.out_symbol,
            "out_address": self.out_address,
            "out_decimals": self.out_decimals,
        }
        


@dataclass
class Cashflow:
    cashflow_identifier: str
    ref_id: Optional[str]
    cashflow_subtype: CashflowSubType
    token_amount: str
    token_symbol: str
    token_address: str
    token_decimals: int
    trade_identifier: Optional[str]

    @staticmethod
    def gas_fee_from_sui(trade: SuiTrade, toc_trade: Trade):

        cashflow_subtype = CashflowSubType.GAS_FEE
        coin = SUI_COIN
        amount = 0
        
        return Cashflow(
            cashflow_identifier=f"{trade.transaction_hash}_{cashflow_subtype.value}",
            ref_id=trade.transaction_hash,
            cashflow_subtype=cashflow_subtype,
            token_amount=format_amount(abs(amount)),
            token_symbol=coin.symbol,
            token_address=coin.address,
            token_decimals=coin.decimals,
            trade_identifier=toc_trade.trade_identifier,
        )
        
    @staticmethod 
    def gas_fee_from_trade(trade: Trade, gas_fee: float):
        '''
        Directly inputs gas_fee from a Trade on SUI Chain
        '''
        cashflow_subtype = CashflowSubType.GAS_FEE
        coin = SUI_COIN
        amount = gas_fee
        transaction_hash = trade.trade_identifier.split("_")[0]
        return Cashflow(
            cashflow_identifier=f"{transaction_hash}_{cashflow_subtype.value}",
            ref_id=transaction_hash,
            cashflow_subtype=cashflow_subtype,
            token_amount=format_amount(abs(int(amount * (10**coin.decimals)))),
            token_symbol=coin.symbol,
            token_address=coin.address,
            token_decimals=coin.decimals,
            trade_identifier=trade.trade_identifier,
        )
        
    def to_dict(self):
        return {
            "cashflow_identifier": self.cashflow_identifier,
            "ref_id": self.ref_id,
            "cashflow_subtype": self.cashflow_subtype.to_dict(),
            "token_amount": self.token_amount,
            "token_symbol": self.token_symbol,
            "token_address": self.token_address,
            "token_decimals": self.token_decimals,
            "trade_identifier": self.trade_identifier,
        }

@dataclass
class Payload:

    chain: Chain
    counterparty: Counterparty
    account_id: int
    transaction_hash: str
    block_timestamp: str
    block_hash: str
    trades: list[Trade]
    cashflows: list[Cashflow]
    
    
    @staticmethod
    def from_general_trade(
        trade: Trade,
        t2x_account_id: int,
        pool_address: str,
        block_hash: str, 
        block_dt: datetime.datetime,
        gas_fee: float = None,
    ):
        chain = Chain.SUI
        pool = sui_ref.pool_by_address(pool_address)
        print(pool)
        account_id = chain.account_id(t2x_account_id)
        cf_fee = Cashflow.gas_fee_from_trade(trade=trade, gas_fee=gas_fee)
        
        transaction_hash = trade.trade_identifier.split("_")[0]
        payload = Payload(
            chain=chain,
            counterparty=Counterparty.from_venue(pool.venue),
            account_id=account_id,
            transaction_hash=transaction_hash,
            block_timestamp=isotimestamp(block_dt),
            block_hash=block_hash,
            trades=[trade],
            cashflows=[cf_fee],
        )
        
        return payload

    @staticmethod
    def from_sui_trade(
        trade: SuiTrade,
        t2x_account_id: int,
        block_hash: str,
        block_dt: datetime.datetime,
    ):
        chain = Chain.SUI

        pool = sui_ref.pool_by_address(trade.pool_address)
        account_id = chain.account_id(t2x_account_id)

        toc_trade = Trade.from_sui_trade(trade)
        cf_fee = Cashflow.gas_fee_from_sui(trade, toc_trade)

        payload = Payload(
            chain=chain,
            counterparty=Counterparty.from_venue(pool.venue),
            account_id=account_id,
            transaction_hash=trade.transaction_hash,
            block_timestamp=isotimestamp(block_dt),
            block_hash=block_hash,
            trades=[toc_trade],
            cashflows=[cf_fee],
        )

        return payload

    def to_dict(self):
        return {
            "chain": self.chain.value if hasattr(self.chain, 'value') else str(self.chain),
            "counterparty": self.counterparty.to_dict() if hasattr(self.counterparty, 'to_dict') else vars(self.counterparty),
            "account_id": self.account_id,
            "transaction_hash": self.transaction_hash,
            "block_timestamp": self.block_timestamp,
            "block_hash": self.block_hash,
            "trades": [trade.to_dict() if hasattr(trade, 'to_dict') else vars(trade) for trade in self.trades],
            "cashflows": [cashflow.to_dict() if hasattr(cashflow, 'to_dict') else vars(cashflow) for cashflow in self.cashflows],
        }

    # def publish(self):
    #     base_url = TOC_CONFIG.url
    #     url = f"{base_url}/trade"
    #     json = self.to_json()

    #     logger.info(f"send TOC {self.transaction_hash}: json={json} -> url={url}")
    #     resp = requests.post(url, json=json, timeout=5.0)
    #     logger.info(f"recv TOC {self.transaction_hash}: resp={resp.text}")

    #     return resp
    
    
### ----------------------------------- DATABASE IMPLEMENTATION --------------------------------------------------------------------####

# Load Postgres Database
DATABASE_CONFIG = {"DB": os.getenv("POSTGRES_DATABASE"), 
                   "USER": os.getenv("POSTGRES_USER"), 
                   "PWD": os.getenv("POSTGRES_PASSWORD"), 
                   "HOST": os.getenv("POSTGRES_HOST"),
                   "PORT": os.getenv("POSTGRES_PORT")}




class ReconnectPostgresqlDatabase(ReconnectMixin, PostgresqlDatabase):
    pass
        
DATABASE = ReconnectPostgresqlDatabase(
    DATABASE_CONFIG['DB'],
    user=DATABASE_CONFIG['USER'],
    password=DATABASE_CONFIG['PWD'],
    host=DATABASE_CONFIG['HOST'],
    port=DATABASE_CONFIG['PORT'],
    autoconnect=False,
)

class BaseModel(Model):
    class Meta:
        database = DATABASE
        
        
class TQTrades(BaseModel):
    order_id=AutoField(primary_key=True)
    datetime = DateTimeField(default=datetime.datetime.now(utc_plus_8))
    tx_digest=TextField(null=False)
    symbol=TextField(null=False)
    side=TextField(null=False)
    amount=FloatField(null=False)
    executedQty=FloatField(null=False)
    priceSold=FloatField(null=False)
    commission_asset=TextField(null=True)
    total_commission_quote=FloatField(null=False)
    
    class Meta:
        table_name = "TQ_Trades"

    
class DexTrades(BaseModel):
    tx_digest=TextField(primary_key=True)
    datetime=DateTimeField(default=datetime.datetime.now(utc_plus_8))
    a_to_b = BooleanField(null=False)
    sender=TextField(null=False)
    pool_address=TextField(null=False)
    token_a=TextField(null=False)
    token_b=TextField(null=False)
    token_a_address=TextField(null=False)
    token_b_address=TextField(null=False)
    amount_in=IntegerField(null=False)
    amount_out=IntegerField(null=False)
    amount_a=FloatField(null=False)
    amount_b=FloatField(null=False)
    before_sqrt_price= IntegerField(null=False),
    after_sqrt_price= IntegerField(null=False),
    sqrt_price= IntegerField(null=False),
    type= TextField(null=False)
    
    
    class Meta:
        table_name = "DEX_Trades"
        
        
class AuctionEvents(BaseModel):
    opp_tx_digest=TextField(primary_key=True)
    auction_event=JSONField(null=False)
    
    class Meta:
        table_name = "Auction_Events_with_Bids"
        
        
class AuctionEventsRaw(BaseModel):
    opp_tx_digest=TextField(primary_key=True)
    auction_event=JSONField(null=False)
    
    class Meta:
        table_name = "Auction_Events_Raw_Full"
    
    
    
class AuctionOutcomes(BaseModel):
    opp_tx_digest=TextField(primary_key=True)
    auction_outcomes=JSONField(null=False)
    
    class Meta:
        table_name = "Auction_Outcomes"
        
        
class AuctionsBidded(BaseModel):
    datetime=DateTimeField(default=datetime.datetime.now(utc_plus_8))
    opp_tx_digest=TextField(primary_key=True)
    tx_digest=TextField()
    bidded_amount=FloatField(null=False)
    mev_opp=FloatField(null=False)
    
    class Meta:
        table_name = "Auctions_Bidded_Digests"
        
        
# def create_tables():
#     try:
#         if DATABASE.is_closed():
#             connected = DATABASE.connect()
#             if connected:
#                 print("Connected to the database.")
#             else:
#                 logger.error(f"Not connected to database")
                
#         with DATABASE:
#             DATABASE.create_tables([TQTrades, DexTrades, AuctionEvents, AuctionEventsRaw, AuctionOutcomes, AuctionsBidded])
#         print("Tables created successfully.")
#     except Exception as e:
#         print(f"Error during table creation: {e}")


# create_tables()
# logger.info(f"Postgres Connection Info: {DATABASE.connection()}")
    


    

    
    


               


import datetime

from dataclasses import dataclass
from typing import List, Union, Optional, Dict, Tuple, Literal

import chainflip.utils.constants as CONSTANTS


@dataclass
class PrewitnessedSwap:
    base_asset: str
    quote_asset: str
    amount: int
    end_time: datetime.datetime
    side: str
    block_number: int
    block_hash: str
    swap_request_id: int

    def __lt__(self, other):
        return self.end_time < other.end_time

    def __str__(self):
        return f'Witnessed - {self.base_asset} to {self.quote_asset}, amount={self.amount}, side={self.side}, encountered_at:{self.block_hash}'


@dataclass
class LimitOrder:
    amount: float
    price: float
    base_asset: str
    quote_asset: str
    id: hex
    side: CONSTANTS.Side
    timestamp: Optional[datetime.datetime] = None
    lp_account: Optional[str] = None
    volatility: Optional[float] = 0.0

    def __str__(self):
        return f'Limit Order - {self.base_asset}{self.quote_asset}: ' \
               f'price = {self.price}, amount = {self.amount}, id = {self.id}, side = {self.side.name}, ' \
               f'lp = {self.lp_account}, timestamp = {self.timestamp}'


@dataclass
class RangeOrder:
    lower_price: float
    upper_price: float
    base_asset: str
    quote_asset: str
    id: hex
    type: CONSTANTS.RangeOrderType
    timestamp: Optional[datetime.datetime] = None
    amount: Optional[float] = None
    min_amounts: Optional[tuple] = None
    max_amounts: Optional[tuple] = None
    lp_account: Optional[str] = None

    def __str__(self):
        if self.type.value == CONSTANTS.RangeOrderType.LIQUIDITY.value:
            return f'Range Order - {self.base_asset}{self.quote_asset}: ' \
                   f'lower_price = {self.lower_price}, upper_price = {self.upper_price}, ' \
                   f'amount = {self.amount}, id = {self.id}, lp = {self.lp_account}, timestamp = {self.timestamp}'
        else:
            return f'Range Order - {self.base_asset}{self.quote_asset}: ' \
                   f'lower_price = {self.lower_price}, upper_price = {self.upper_price}, ' \
                   f'id = {self.id}, timestamp = {self.timestamp}, ' \
                   f'min_amounts = {self.min_amounts}, max_amounts = {self.max_amounts}, lp = {self.lp_account}'


@dataclass
class BinanceKline:
    start_time: datetime.datetime
    end_time: datetime.datetime
    ticker: str
    interval: str
    open: float = None
    close: float = None
    high: float = None
    low: float = None
    volume: float = None

    def __str__(self):
        return f'Kline Candle - start_time: {self.start_time}, end_time: {self.end_time}, ticker: {self.ticker}, ' \
               f'interval: {self.interval}, open: {self.open}, close: {self.close}, ' \
               f'high: {self.high}, low: {self.low},' \
               f'volume: {self.volume}'
               
               

@dataclass
class WsLevel:
    price: str
    size: str
    order_count: int

    def __str__(self):
        return f"WsLevel(price={self.price}, size={self.size}, order_count={self.order_count})"

@dataclass
class WsTrade:
    coin: str
    side: str
    price: str
    size: str
    transaction_hash: str
    time: int
    trade_id: int

    def __str__(self):
        return (f"WsTrade(coin={self.coin}, side={self.side}, price={self.price}, "
                f"size={self.size}, transaction_hash={self.transaction_hash}, "
                f"time={self.time}, trade_id={self.trade_id})")

@dataclass
class WsBook:
    coin: str
    levels: Tuple[List[WsLevel], List[WsLevel]]
    time: int

    def __str__(self):
        bid_levels = ', '.join(str(level) for level in self.levels[0])
        ask_levels = ', '.join(str(level) for level in self.levels[1])
        return (f"WsBook(coin={self.coin}, bid_levels=[{bid_levels}], "
                f"ask_levels=[{ask_levels}], time={self.time})")

@dataclass
class Notification:
    notification: str

    def __str__(self):
        return f"Notification(notification={self.notification})"

@dataclass
class AllMids:
    mids: Dict[str, str]

    def __str__(self):
        return f"AllMids(mids={self.mids})"


@dataclass
class Candle:
    open_time: int
    close_time: int
    coin: str
    interval: str
    open_price: float
    close_price: float
    high_price: float
    low_price: float
    volume: float
    trade_count: int

    def __str__(self):
        return (f"Candle(open_time={self.open_time}, close_time={self.close_time}, "
                f"coin={self.coin}, interval={self.interval}, open_price={self.open_price}, "
                f"close_price={self.close_price}, high_price={self.high_price}, "
                f"low_price={self.low_price}, volume={self.volume}, trade_count={self.trade_count})")

@dataclass
class FillLiquidation:
    liquidated_user: Optional[str]
    mark_price: float
    method: str

    def __str__(self):
        return (f"FillLiquidation(liquidated_user={self.liquidated_user}, "
                f"mark_price={self.mark_price}, method={self.method})")

@dataclass
class WsFill:
    coin: str
    price: str
    size: str
    side: str
    time: int
    start_position: str
    direction: str
    closed_pnl: str
    transaction_hash: str
    order_id: int
    crossed: bool
    fee: str
    trade_id: int
    liquidation: Optional[FillLiquidation]
    fee_token: str
    builder_fee: Optional[str]

    def __str__(self):
        return (f"WsFill(coin={self.coin}, price={self.price}, size={self.size}, side={self.side}, "
                f"time={self.time}, start_position={self.start_position}, direction={self.direction}, "
                f"closed_pnl={self.closed_pnl}, transaction_hash={self.transaction_hash}, "
                f"order_id={self.order_id}, crossed={self.crossed}, fee={self.fee}, "
                f"trade_id={self.trade_id}, liquidation={self.liquidation}, "
                f"fee_token={self.fee_token}, builder_fee={self.builder_fee})")

@dataclass
class WsUserFunding:
    time: int
    coin: str
    usdc: str
    size_index: str
    funding_rate: str

    def __str__(self):
        return (f"WsUserFunding(time={self.time}, coin={self.coin}, usdc={self.usdc}, "
                f"size_index={self.size_index}, funding_rate={self.funding_rate})")

@dataclass
class WsLiquidation:
    liquidation_id: int
    liquidator: str
    liquidated_user: str
    liquidated_notional_position: str
    liquidated_account_value: str

    def __str__(self):
        return (f"WsLiquidation(liquidation_id={self.liquidation_id}, liquidator={self.liquidator}, "
                f"liquidated_user={self.liquidated_user}, "
                f"liquidated_notional_position={self.liquidated_notional_position}, "
                f"liquidated_account_value={self.liquidated_account_value})")

@dataclass
class WsNonUserCancel:
    coin: str
    order_id: int

    def __str__(self):
        return f"WsNonUserCancel(coin={self.coin}, order_id={self.order_id})"

@dataclass
class WsBasicOrder:
    coin: str
    side: str
    limit_price: str
    size: str
    order_id: int
    timestamp: int
    original_size: str
    client_order_id: Optional[str]

    def __str__(self):
        return (f"WsBasicOrder(coin={self.coin}, side={self.side}, limit_price={self.limit_price}, "
                f"size={self.size}, order_id={self.order_id}, timestamp={self.timestamp}, "
                f"original_size={self.original_size}, client_order_id={self.client_order_id})")

@dataclass
class WsOrder:
    order: WsBasicOrder
    status: str
    status_timestamp: int

    def __str__(self):
        return (f"WsOrder(order={self.order}, status={self.status}, "
                f"status_timestamp={self.status_timestamp})")


@dataclass
class PositionLeverage:
    type: Literal["cross", "isolated"]
    value: int
    rawUsd: Optional[str] = None  # Only present if type is "isolated"


@dataclass
class WsPosition:
    coin: str
    entryPx: Optional[Union[float, str]]
    leverage: PositionLeverage
    liquidationPx: Optional[Union[float, str]]
    marginUsed: str
    positionValue: str
    returnOnEquity: str
    szi: str
    unrealizedPnl: str
    type: Literal["oneWay"]

    def __str__(self):
        return (
            f"Position(coin={self.coin}, entryPx={self.entryPx}, leverage={self.leverage}, "
            f"liquidationPx={self.liquidationPx}, marginUsed={self.marginUsed}, "
            f"positionValue={self.positionValue}, returnOnEquity={self.returnOnEquity}, "
            f"szi={self.szi}, unrealizedPnl={self.unrealizedPnl}, type={self.type})"
        )


@dataclass
class SharedAssetCtx:
    day_notional_volume: float
    previous_day_price: float
    mark_price: float
    mid_price: Optional[float]

    def __str__(self):
        return (f"SharedAssetCtx(day_notional_volume={self.day_notional_volume}, "
                f"previous_day_price={self.previous_day_price}, mark_price={self.mark_price}, "
                f"mid_price={self.mid_price})")

@dataclass
class PerpsAssetCtx(SharedAssetCtx):
    funding: float
    open_interest: float
    oracle_price: float

    def __str__(self):
        return (f"PerpsAssetCtx({super().__str__()}, funding={self.funding}, "
                f"open_interest={self.open_interest}, oracle_price={self.oracle_price})")

@dataclass
class SpotAssetCtx(SharedAssetCtx):
    circulating_supply: float

    def __str__(self):
        return f"SpotAssetCtx({super().__str__()}, circulating_supply={self.circulating_supply})"

@dataclass
class WsActiveAssetCtx:
    coin: str
    context: PerpsAssetCtx

    def __str__(self):
        return f"WsActiveAssetCtx(coin={self.coin}, context={self.context})"

@dataclass
class WsActiveSpotAssetCtx:
    coin: str
    context: SpotAssetCtx

    def __str__(self):
        return f"WsActiveSpotAssetCtx(coin={self.coin}, context={self.context})"

@dataclass
class WsActiveAssetData:
    user: str
    coin: str
    leverage: str
    max_trade_sizes: Tuple[float, float]
    available_to_trade: Tuple[float, float]

    def __str__(self):
        return (f"WsActiveAssetData(user={self.user}, coin={self.coin}, leverage={self.leverage}, "
                f"max_trade_sizes={self.max_trade_sizes}, available_to_trade={self.available_to_trade})")



