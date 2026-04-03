# Database Module Documentation

## Overview

The `elysian_core/db/` module provides persistent storage for the Elysian trading system using PostgreSQL via the Peewee ORM with connection pooling and automatic reconnection.

What gets persisted:
- CEX trade fills (one row per exchange order fill)
- DEX swap events (one row per on-chain swap transaction)
- Account balance snapshots (periodic captures of full account state)
- Portfolio snapshots (NAV, positions, weights, risk metrics at a point in time)

---

## Database Connection (`elysian_core/db/database.py`)

### `ReconnectPooledPostgresqlDatabase`

```python
class ReconnectPooledPostgresqlDatabase(ReconnectMixin, PooledPostgresqlDatabase):
    pass
```

Combines Peewee's `ReconnectMixin` (automatic reconnection on stale connections) with `PooledPostgresqlDatabase` (connection pooling). No additional logic is added — the combination of the two mixins provides the full behavior.

### `DATABASE`

The module-level singleton connection instance:

```python
DATABASE = ReconnectPooledPostgresqlDatabase(
    os.getenv("POSTGRES_DATABASE"),
    user=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=int(os.getenv("POSTGRES_PORT", 5432)),
    max_connections=20,
    stale_timeout=300,
    autoconnect=True,
)
```

**Connection pool settings:**

| Parameter | Value | Description |
|-----------|-------|-------------|
| `max_connections` | `20` | Maximum number of pooled connections |
| `stale_timeout` | `300` (seconds) | Idle connections older than this are recycled |
| `autoconnect` | `True` | Peewee manages connection lifecycle automatically |

**Required environment variables:**

| Env var | Description |
|---------|-------------|
| `POSTGRES_DATABASE` | Database name |
| `POSTGRES_USER` | Database username |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_HOST` | Database host |
| `POSTGRES_PORT` | Database port (default: `5432`) |

The module calls `load_dotenv()` at import time so a `.env` file in the working directory is automatically picked up.

### `BaseModel`

```python
class BaseModel(Model):
    class Meta:
        database = DATABASE
```

All ORM models inherit from `BaseModel` so they share the same pooled connection.

---

## Models (`elysian_core/db/models.py`)

All models import enums from `elysian_core.core.enums`. The module-level constant `_UTC8` defines the UTC+8 timezone used as the default for all `DateTimeField` columns.

### `EnumField`

```python
class EnumField(CharField):
    def __init__(self, enum_class, *args, **kwargs)
    def db_value(self, value) -> str
    def python_value(self, value) -> Enum
```

A `CharField` subclass that stores Python `Enum` values as their `.value` string in the database and converts them back to the enum on read.

- `db_value(value)` — if `value` is an instance of `enum_class`, returns `value.value`; otherwise passes through as-is (for legacy raw strings).
- `python_value(value)` — calls `enum_class(value)` to reconstruct the enum; returns `None` if value is `None`.

---

### `CexTrade`

**Table:** `cex_trades`

Records one CEX order fill, written by the exchange connector after a fill is confirmed.

| Field | Peewee Type | Python Type | Nullable | Description |
|-------|-------------|-------------|----------|-------------|
| `id` | `AutoField` | `int` | No | Auto-incrementing primary key |
| `datetime` | `DateTimeField` | `datetime` | No | Fill timestamp (UTC+8); defaults to `now()` |
| `strategy_id` | `IntegerField` | `int` | No | ID of the strategy that generated this order |
| `strategy_name` | `TextField` | `str` | No | Name of the strategy |
| `venue` | `EnumField(Venue)` | `Venue` | No | Exchange venue; default `Venue.BINANCE` |
| `symbol` | `TextField` | `str` | No | Trading pair, e.g. `"ETHUSDT"` |
| `asset_type` | `EnumField(AssetType)` | `AssetType` | No | `AssetType.SPOT` or `AssetType.PERPETUAL` |
| `side` | `EnumField(Side)` | `Side` | No | `Side.BUY` or `Side.SELL` |
| `base_amount` | `FloatField` | `float` | No | Signed base asset quantity requested |
| `quote_amount` | `FloatField` | `float` | No | Signed quote amount (`base_amount * price`) |
| `price` | `FloatField` | `float` | No | Average fill price |
| `commission_asset` | `TextField` | `str` | Yes | Asset in which commission was charged |
| `total_commission` | `FloatField` | `float` | No | Total commission amount in `commission_asset` |
| `total_commission_quote` | `FloatField` | `float` | No | Commission converted to quote currency |
| `order_id` | `TextField` | `str` | No | Exchange-assigned order ID |
| `status` | `EnumField(OrderStatus)` | `OrderStatus` | No | Final order status (e.g. `OrderStatus.FILLED`) |
| `order_side` | `EnumField(Side)` | `Side` | No | Side as returned in the exchange response |
| `order_type` | `EnumField(OrderType)` | `OrderType` | No | Order type from exchange response |

**Relevant enum values:**

`Venue`: `BINANCE`, `BYBIT`, `OKX`, `ASTER`, `CETUS`, `TURBOS`, `DEEPBOOK`

`AssetType`: `SPOT` (`"Spot"`), `PERPETUAL` (`"Perpetuals"`)

`Side`: `BUY` (`"Buy"`), `SELL` (`"Sell"`)

`OrderStatus`: `PENDING`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `TRIGGERED`

`OrderType`: `LIMIT`, `MARKET`, `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET`, `RANGE`, `OTHERS`

---

### `DexTrade`

**Table:** `dex_trades`

Records one on-chain DEX swap event.

| Field | Peewee Type | Python Type | Nullable | Description |
|-------|-------------|-------------|----------|-------------|
| `tx_digest` | `TextField` | `str` | No | Transaction digest (primary key) |
| `datetime` | `DateTimeField` | `datetime` | No | Event timestamp (UTC+8); defaults to `now()` |
| `venue` | `EnumField(Venue)` | `Venue` | No | DEX venue; default `Venue.ASTER` |
| `a_to_b` | `BooleanField` | `bool` | No | Swap direction: `True` = token A → token B |
| `sender` | `TextField` | `str` | No | On-chain address of the transaction sender |
| `pool_address` | `TextField` | `str` | No | Contract address of the liquidity pool |
| `token_a` | `TextField` | `str` | No | Symbol of token A |
| `token_b` | `TextField` | `str` | No | Symbol of token B |
| `token_a_address` | `TextField` | `str` | No | Contract address of token A |
| `token_b_address` | `TextField` | `str` | No | Contract address of token B |
| `amount_in` | `IntegerField` | `int` | No | Raw input amount (chain integer representation) |
| `amount_out` | `IntegerField` | `int` | No | Raw output amount (chain integer representation) |
| `amount_a` | `FloatField` | `float` | No | Token A amount in decimal units |
| `amount_b` | `FloatField` | `float` | No | Token B amount in decimal units |
| `before_sqrt_price` | `IntegerField` | `int` | No | Square root price before the swap |
| `after_sqrt_price` | `IntegerField` | `int` | No | Square root price after the swap |
| `sqrt_price` | `IntegerField` | `int` | No | Execution square root price |
| `type` | `EnumField(SwapType)` | `SwapType` | No | DEX protocol type |

**`SwapType` values:** `BLUEFIN`, `CETUS`, `FLOW_X`, `TURBOS`

---

### `AccountSnapshots`

**Table:** `account_snapshots`

A periodic snapshot of the full exchange account state.

| Field | Peewee Type | Python Type | Nullable | Description |
|-------|-------------|-------------|----------|-------------|
| `datetime` | `DateTimeField` | `datetime` | No | Snapshot timestamp (UTC+8); primary key |
| `venue` | `EnumField(Venue)` | `Venue` | No | Exchange venue; default `Venue.BINANCE` |
| `balances` | `JSONField` | `dict` | No | Asset balances as `{"USDT": 1000.0, "ETH": 2.5, ...}` |
| `total_usd_value` | `FloatField` | `float` | No | Total account value in USD at snapshot time |
| `total_open_orders` | `IntegerField` | `int` | No | Number of open orders at snapshot time |

---

### `PortfolioSnapshot`

**Table:** `portfolio_snapshots`

A point-in-time capture of the portfolio's full state. Used for equity-curve reconstruction, risk reporting, and post-trade analysis.

| Field | Peewee Type | Python Type | Nullable | Description |
|-------|-------------|-------------|----------|-------------|
| `id` | `AutoField` | `int` | No | Auto-incrementing primary key |
| `datetime` | `DateTimeField` | `datetime` | No | Snapshot timestamp (UTC+8); indexed |
| `strategy_id` | `IntegerField` | `int` | Yes | Strategy that owns this snapshot |
| `venue` | `EnumField(Venue)` | `Venue` | No | Exchange venue; default `Venue.BINANCE` |
| `nav` | `FloatField` | `float` | No | Total portfolio value (net asset value) |
| `cash` | `FloatField` | `float` | No | Stablecoin / undeployed cash balance |
| `unrealized_pnl` | `FloatField` | `float` | No | Mark-to-market unrealized P&L |
| `realized_pnl` | `FloatField` | `float` | No | Cumulative realized P&L |
| `total_commission` | `FloatField` | `float` | No | Cumulative commissions paid |
| `peak_equity` | `FloatField` | `float` | No | All-time peak NAV |
| `max_drawdown` | `FloatField` | `float` | No | Maximum drawdown as a fraction (e.g. `0.05` = 5%) |
| `current_drawdown` | `FloatField` | `float` | No | Current drawdown from `peak_equity` |
| `gross_exposure` | `FloatField` | `float` | No | Sum of absolute position values |
| `net_exposure` | `FloatField` | `float` | No | Long exposure minus short exposure |
| `positions` | `JSONField` | `dict` | No | Per-symbol position detail: `{"ETHUSDT": {"qty": 1.5, "avg_entry": 3200, ...}}` |
| `weights` | `JSONField` | `dict` | No | Per-symbol portfolio weight: `{"ETHUSDT": 0.25, ...}` |
| `mark_prices` | `JSONField` | `dict` | No | Per-symbol mark price: `{"ETHUSDT": 3250.0, ...}` |
| `num_fills` | `IntegerField` | `int` | No | Number of fills included in this snapshot period |

---

## Schema Management

### `ALL_TABLES`

```python
ALL_TABLES = [CexTrade, DexTrade, PortfolioSnapshot, AccountSnapshots]
```

The ordered list of all model classes. Used by `create_tables`.

### `create_tables`

```python
def create_tables(safe: bool = True)
```

Creates all tables in `ALL_TABLES` within a database context manager. When `safe=True` (default), the `IF NOT EXISTS` clause is used so existing tables are not modified.

```python
from elysian_core.db.models import create_tables

create_tables(safe=True)
```

---

## Entity Relationship Summary

```
CexTrade           — one row per CEX order fill
                     FK (logical): strategy_id → StrategyConfig.strategy_id

DexTrade           — one row per on-chain DEX swap
                     Primary key: tx_digest (unique transaction hash)

AccountSnapshots   — one row per account-balance capture
                     Primary key: datetime (unique per snapshot timestamp)

PortfolioSnapshot  — one row per portfolio state capture
                     FK (logical): strategy_id → StrategyConfig.strategy_id
```

All tables are independent (no foreign key constraints at the DB level). Relationships between records and strategies are maintained via the `strategy_id` integer field.

---

## Usage Examples

### Initialize tables

```python
from elysian_core.db.models import create_tables

create_tables(safe=True)
```

### Record a CEX trade fill

```python
import datetime
from elysian_core.db.models import CexTrade
from elysian_core.db.database import DATABASE
from elysian_core.core.enums import Venue, Side, AssetType, OrderStatus, OrderType

with DATABASE.atomic():
    CexTrade.create(
        strategy_id=1,
        strategy_name="event_driven_momentum",
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        asset_type=AssetType.SPOT,
        side=Side.BUY,
        base_amount=0.001,
        quote_amount=50.0,
        price=50000.0,
        commission_asset="USDT",
        total_commission=0.05,
        total_commission_quote=0.05,
        order_id="123456789",
        status=OrderStatus.FILLED,
        order_side=Side.BUY,
        order_type=OrderType.MARKET,
    )
```

### Query recent trades

```python
import datetime
from elysian_core.db.models import CexTrade

since = datetime.datetime.now() - datetime.timedelta(hours=24)
trades = (
    CexTrade
    .select()
    .where(CexTrade.datetime >= since)
    .order_by(CexTrade.datetime.desc())
)
for t in trades:
    print(t.symbol, t.side, t.base_amount, t.price)
```

### Query trades by strategy

```python
from elysian_core.db.models import CexTrade
from elysian_core.core.enums import OrderStatus

filled = (
    CexTrade
    .select()
    .where(
        (CexTrade.strategy_id == 1) &
        (CexTrade.status == OrderStatus.FILLED)
    )
    .order_by(CexTrade.datetime.desc())
)
```

### Insert a portfolio snapshot

```python
import json
from elysian_core.db.models import PortfolioSnapshot
from elysian_core.core.enums import Venue

PortfolioSnapshot.create(
    strategy_id=1,
    venue=Venue.BINANCE,
    nav=100000.0,
    cash=20000.0,
    unrealized_pnl=1500.0,
    realized_pnl=500.0,
    total_commission=25.0,
    peak_equity=102000.0,
    max_drawdown=0.02,
    current_drawdown=0.0,
    gross_exposure=80000.0,
    net_exposure=80000.0,
    positions={"ETHUSDT": {"qty": 20.0, "avg_entry": 3200.0}},
    weights={"ETHUSDT": 0.64, "CASH": 0.36},
    mark_prices={"ETHUSDT": 3275.0},
    num_fills=3,
)
```

### Record a DEX swap

```python
from elysian_core.db.models import DexTrade
from elysian_core.core.enums import Venue, SwapType

DexTrade.create(
    tx_digest="0xabc123...",
    venue=Venue.ASTER,
    a_to_b=True,
    sender="0xsender...",
    pool_address="0xpool...",
    token_a="SUI",
    token_b="USDT",
    token_a_address="0xtokenA...",
    token_b_address="0xtokenB...",
    amount_in=1000000000,
    amount_out=995000,
    amount_a=1.0,
    amount_b=0.995,
    before_sqrt_price=79228162514264337593543950336,
    after_sqrt_price=79100000000000000000000000000,
    sqrt_price=79200000000000000000000000000,
    type=SwapType.CETUS,
)
```

### Verify database connectivity

```python
from elysian_core.db.database import DATABASE

try:
    DATABASE.connect()
    DATABASE.close()
    print("Database connection successful")
except Exception as e:
    print(f"Database connection failed: {e}")
```
