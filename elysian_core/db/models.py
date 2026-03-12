import datetime

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    IntegerField,
    TextField,
    OperationalError
)
from playhouse.postgres_ext import JSONField

from elysian_core.core.enums import OrderStatus, Side, SwapType, TradeType, Venue, OrderType, AssetType
from elysian_core.db.database import BaseModel
from elysian_core.db.database import DATABASE
from elysian_core.utils.logger import setup_custom_logger


logger = setup_custom_logger('root')

_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


# ── Custom field ──────────────────────────────────────────────────────────────

class EnumField(CharField):
    """Stores an Enum as its .value string in the DB; returns the Enum on read."""

    def __init__(self, enum_class, *args, **kwargs):
        self.enum_class = enum_class
        super().__init__(*args, **kwargs)

    def db_value(self, value):
        if isinstance(value, self.enum_class):
            return value.value
        return value  # already a raw string (e.g. passed from legacy code)

    def python_value(self, value):
        if value is not None:
            return self.enum_class(value)
        return value


# ── Binance CEX trades ────────────────────────────────────────────────────────

class CexTrade(BaseModel):
    """One CEX fill recorded by BinanceExchange._record_trade."""
    id = AutoField(primary_key=True)
    datetime = DateTimeField(default=lambda: datetime.datetime.now(_UTC8))
    strategy_id = IntegerField(null=False) # optional strategy_id for easier querying
    strategy_name = TextField(null=False)
    venue = EnumField(Venue, default=Venue.BINANCE)
    symbol = TextField(null=False)                 # e.g. "ETHUSDT"
    asset_type = EnumField(AssetType, null=False) # AssetType.SPOT or AssetType.PERPETUAL
    side = EnumField(Side, null=False)             # Side.BUY | Side.SELL
    base_amount = FloatField(null=False)                # signed base amount requested
    quote_amount = FloatField(null=False)               # signed quote amount (base_amount * price)
    price = FloatField(null=False)                 # average fill price
    commission_asset = TextField(null=True)
    total_commission = FloatField(null=False)
    total_commission_quote = FloatField(null=False)
    order_id = TextField(null=False)
    status = EnumField(OrderStatus, null=False)    # OrderStatus.FILLED etc.
    order_side = EnumField(Side, null=False)       # Side from Binance response
    order_type = EnumField(OrderType, null=False) # OrderType from Binance response

    class Meta:
        table_name = "cex_trades"



# ── DEX swap events ───────────────────────────────────────────────────────────
class DexTrade(BaseModel):
    tx_digest = TextField(primary_key=True)
    datetime = DateTimeField(default=lambda: datetime.datetime.now(_UTC8))
    venue = EnumField(Venue, default=Venue.ASTER)
    a_to_b = BooleanField(null=False)
    sender = TextField(null=False)
    pool_address = TextField(null=False)
    token_a = TextField(null=False)
    token_b = TextField(null=False)
    token_a_address = TextField(null=False)
    token_b_address = TextField(null=False)
    amount_in = IntegerField(null=False)
    amount_out = IntegerField(null=False)
    amount_a = FloatField(null=False)
    amount_b = FloatField(null=False)
    before_sqrt_price = IntegerField(null=False)
    after_sqrt_price = IntegerField(null=False)
    sqrt_price = IntegerField(null=False)
    type = EnumField(SwapType, null=False)         # SwapType.CETUS etc.

    class Meta:
        table_name = "dex_trades"
        
        
        
class AccountSnapshots(BaseModel):
    datetime = DateTimeField(default=lambda: datetime.datetime.now(_UTC8), primary_key=True)
    venue = EnumField(Venue, default=Venue.BINANCE)
    balances = JSONField(null=False)  # { "USDT": 1000.0, "ETH": 2.5, ... }
    total_usd_value = FloatField(null=False)  # total account value in USD at the time of snapshot
    total_open_orders = IntegerField(null=False)  # total number of open orders at the time of snapshot
    
    class Meta:
        table_name = "account_snapshots"
    


# ── Schema management ─────────────────────────────────────────────────────────

ALL_TABLES = [
    CexTrade,
    DexTrade,
]


def create_tables(safe: bool = True):
    """Create all tables. Pass safe=True to skip existing tables."""
    with DATABASE:
        DATABASE.create_tables(ALL_TABLES, safe=safe)
