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

from elysian_core.core.enums import OrderStatus, Side, SwapType, TradeType, Venue, OrderType
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

class BinanceTrade(BaseModel):
    """One CEX fill recorded by BinanceExchange._record_trade."""
    id = AutoField(primary_key=True)
    datetime = DateTimeField(default=lambda: datetime.datetime.now(_UTC8))
    venue = EnumField(Venue, default=Venue.BINANCE)
    symbol = TextField(null=False)                 # e.g. "ETHUSDT"
    side = EnumField(Side, null=False)             # Side.BUY | Side.SELL
    amount = FloatField(null=False)                # signed base amount requested
    executed_qty = FloatField(null=False)
    price = FloatField(null=False)                 # average fill price
    commission_asset = TextField(null=True)
    total_commission = FloatField(null=False)
    total_commission_quote = FloatField(null=False)
    order_id = TextField(null=False)
    status = EnumField(OrderStatus, null=False)    # OrderStatus.FILLED etc.
    order_side = EnumField(Side, null=False)       # Side from Binance response
    order_type = EnumField(OrderType, null=False) # OrderType from Binance response

    class Meta:
        table_name = "binance_trades"



class CexTrade(BaseModel):
    """One CEX fill recorded by CEX Exchanges"""
    id = AutoField(primary_key=True)
    datetime = DateTimeField(default=lambda: datetime.datetime.now(_UTC8))
    venue = EnumField(Venue)
    symbol = TextField(null=False)                 # e.g. "ETHUSDT"
    side = EnumField(Side, null=False)             # Side.BUY | Side.SELL
    amount = FloatField(null=False)                # signed base amount requested
    executed_qty = FloatField(null=False)
    price = FloatField(null=False)                 # average fill price
    commission_asset = TextField(null=True)
    total_commission = FloatField(null=False)
    total_commission_quote = FloatField(null=False)
    order_id = TextField(null=False)
    status = EnumField(OrderStatus, null=False)    # OrderStatus.FILLED etc.
    order_type = EnumField(OrderType, null=False) # OrderType from Binance response
    order_side = EnumField(Side, null=False)       # Side from Binance response

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


# ── Schema management ─────────────────────────────────────────────────────────

ALL_TABLES = [
    BinanceTrade,
    DexTrade,
]


def create_tables(safe: bool = True):
    """Create all tables. Pass safe=True to skip existing tables."""
    with DATABASE:
        DATABASE.create_tables(ALL_TABLES, safe=safe)
