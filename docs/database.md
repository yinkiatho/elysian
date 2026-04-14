# Database Module Documentation

## Overview

The `elysian_core/db/` module provides persistent storage for the Elysian trading system using PostgreSQL via the Peewee ORM with connection pooling and automatic reconnection.

What gets persisted:
- CEX trade fills (one row per exchange order fill, recorded asynchronously after the order reaches a terminal state)
- DEX swap events (one row per on-chain swap transaction)
- Account balance snapshots (periodic captures of full account state)
- Portfolio snapshots (NAV, positions, weights, risk metrics per strategy)

---

## Database Connection (`elysian_core/db/database.py`)

### `ReconnectPooledPostgresqlDatabase`

```python
class ReconnectPooledPostgresqlDatabase(ReconnectMixin, PooledPostgresqlDatabase):
    pass
```

Combines Peewee's `ReconnectMixin` (automatic reconnection on stale connections) with `PooledPostgresqlDatabase` (connection pooling).

### `DATABASE`

Module-level singleton connection instance:

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
| `max_connections` | `20` | Maximum pooled connections |
| `stale_timeout` | `300` (seconds) | Idle connections older than this are recycled |
| `autoconnect` | `True` | Peewee manages connection lifecycle |

**Required environment variables:**

| Env var | Description |
|---------|-------------|
| `POSTGRES_DATABASE` | Database name |
| `POSTGRES_USER` | Database username |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_HOST` | Database host (use `"postgres"` inside Docker) |
| `POSTGRES_PORT` | Database port (default: `5432`) |

The module calls `load_dotenv()` at import time so a `.env` file in the working directory is automatically picked up.

---

## Models (`elysian_core/db/models.py`)

All models inherit from `BaseModel` (which sets `Meta.database = DATABASE`). The module-level constant `_UTC8` defines the UTC+8 timezone used for all `DateTimeField` defaults.

### `EnumField`

```python
class EnumField(CharField):
    def __init__(self, enum_class, *args, **kwargs)
    def db_value(self, value) -> str      # Enum → .value string
    def python_value(self, value) -> Enum # string → Enum instance
```

Stores Python `Enum` values as their `.value` string in the database and converts them back on read.

---

### `CexTrade`

**Table:** `cex_trades`

One row per CEX order fill. Written by `ShadowBook._async_record_trade()` as a background `asyncio.create_task` after an order reaches a terminal state (`FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED` with at least one fill).

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `id` | `AutoField` | No | Auto-incrementing primary key |
| `datetime` | `DateTimeField` | No | Fill timestamp (UTC+8); defaults to `now()` |
| `strategy_id` | `IntegerField` | No | ID of the strategy that generated this order |
| `strategy_name` | `TextField` | No | Name of the strategy |
| `venue` | `EnumField(Venue)` | No | Exchange venue |
| `symbol` | `TextField` | No | Trading pair, e.g. `"ETHUSDT"` |
| `asset_type` | `EnumField(AssetType)` | No | `AssetType.SPOT` or `AssetType.PERPETUAL` |
| `side` | `EnumField(Side)` | No | `Side.BUY` or `Side.SELL` |
| `base_amount` | `FloatField` | No | Filled base asset quantity (always positive) |
| `quote_amount` | `FloatField` | No | `base_amount × avg_fill_price` |
| `price` | `FloatField` | No | Average fill price |
| `commission_asset` | `TextField` | Yes | Asset commission was charged in |
| `total_commission` | `FloatField` | No | Total commission in `commission_asset` |
| `total_commission_quote` | `FloatField` | No | Commission converted to quote currency |
| `order_id` | `TextField` | No | Exchange-assigned order ID |
| `status` | `EnumField(OrderStatus)` | No | Terminal order status |
| `order_side` | `EnumField(Side)` | No | Side from exchange response |
| `order_type` | `EnumField(OrderType)` | No | Order type |

**Note:** Orders with `filled_qty <= 0` (e.g. cancelled with no fills) are skipped and not recorded.

---

### `DexTrade`

**Table:** `dex_trades`

One row per DEX swap event.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `tx_digest` | `TextField` | No | Transaction digest (primary key) |
| `datetime` | `DateTimeField` | No | Event timestamp (UTC+8) |
| `venue` | `EnumField(Venue)` | No | DEX venue |
| `a_to_b` | `BooleanField` | No | Swap direction |
| `sender` | `TextField` | No | On-chain sender address |
| `pool_address` | `TextField` | No | Liquidity pool contract address |
| `token_a` | `TextField` | No | Symbol of token A |
| `token_b` | `TextField` | No | Symbol of token B |
| `token_a_address` | `TextField` | No | Contract address of token A |
| `token_b_address` | `TextField` | No | Contract address of token B |
| `amount_in` | `IntegerField` | No | Raw input amount (chain integer) |
| `amount_out` | `IntegerField` | No | Raw output amount (chain integer) |
| `amount_a` | `FloatField` | No | Token A amount in decimal units |
| `amount_b` | `FloatField` | No | Token B amount in decimal units |
| `before_sqrt_price` | `IntegerField` | No | Square root price before the swap |
| `after_sqrt_price` | `IntegerField` | No | Square root price after the swap |
| `sqrt_price` | `IntegerField` | No | Execution square root price |
| `type` | `EnumField(SwapType)` | No | DEX protocol type |

---

### `AccountSnapshots`

**Table:** `account_snapshots`

Periodic capture of full exchange account state.

| Field | Type | Description |
|-------|------|-------------|
| `datetime` | `DateTimeField` | Snapshot timestamp (UTC+8); primary key |
| `venue` | `EnumField(Venue)` | Exchange venue |
| `balances` | `JSONField` | `{"USDT": 1000.0, "ETH": 2.5, …}` |
| `total_usd_value` | `FloatField` | Total account value in USD at snapshot time |
| `total_open_orders` | `IntegerField` | Number of open orders at snapshot time |

---

### `PortfolioSnapshot`

**Table:** `portfolio_snapshots`

Point-in-time capture of per-strategy ShadowBook state. Written by `ShadowBook.save_snapshot()` on the periodic snapshot task interval. Also written by the aggregate `Portfolio.save_snapshot()`.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `strategy_id` | `IntegerField` | Yes | Owning strategy; primary key |
| `datetime` | `DateTimeField` | No | Snapshot timestamp (UTC+8); indexed |
| `venue` | `EnumField(Venue)` | No | Exchange venue |
| `nav` | `FloatField` | No | Total portfolio value |
| `cash` | `FloatField` | No | Stablecoin balance |
| `unrealized_pnl` | `FloatField` | No | Mark-to-market unrealized P&L |
| `realized_pnl` | `FloatField` | No | Cumulative realized P&L |
| `total_commission` | `FloatField` | No | Cumulative commissions in USDT |
| `peak_equity` | `FloatField` | No | All-time peak NAV |
| `max_drawdown` | `FloatField` | No | Max drawdown fraction (e.g. `0.05` = 5%) |
| `current_drawdown` | `FloatField` | No | Current drawdown from `peak_equity` |
| `gross_exposure` | `FloatField` | No | Sum of absolute position weights |
| `net_exposure` | `FloatField` | No | Long exposure minus short exposure |
| `positions` | `JSONField` | No | `{"ETHUSDT": {"qty": 1.5, "avg_entry": 3200, "realized_pnl": …}, …}` |
| `weights` | `JSONField` | No | `{"ETHUSDT": 0.25, …}` |
| `mark_prices` | `JSONField` | No | `{"ETHUSDT": 3250.0, …}` |
| `num_fills` | `IntegerField` | No | Number of fills in ring buffer at snapshot time |

---

## Schema Management

### `ALL_TABLES`

```python
ALL_TABLES = [CexTrade, DexTrade, PortfolioSnapshot, AccountSnapshots]
```

### `create_tables`

```python
def create_tables(safe: bool = True)
```

Creates all tables with `IF NOT EXISTS` when `safe=True`. Called automatically by `StrategyRunner.__init__()` at startup.

```python
from elysian_core.db.models import create_tables
create_tables(safe=True)
```

---

## How Trades Are Recorded

Trade recording is non-blocking. When a `ShadowBook` detects that an order has reached a terminal state with at least one fill, it schedules:

```python
asyncio.create_task(self._async_record_trade(active))
```

`_async_record_trade()` writes a `CexTrade` row using the `ActiveOrder`'s accumulated state (`filled_qty`, `avg_fill_price`, `commission`, `commission_asset`). It silently skips orders where `filled_qty <= 0`.

This design ensures that DB writes never block the event loop or delay subsequent fill processing.

---

## Usage Examples

### Initialize Tables

```python
from elysian_core.db.models import create_tables
create_tables(safe=True)
```

### Query Recent Trades

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

### Query Trades by Strategy

```python
from elysian_core.db.models import CexTrade
from elysian_core.core.enums import OrderStatus

filled = (
    CexTrade
    .select()
    .where(
        (CexTrade.strategy_id == 0) &
        (CexTrade.status == OrderStatus.FILLED)
    )
    .order_by(CexTrade.datetime.desc())
)
```

### Insert a Portfolio Snapshot

```python
from elysian_core.db.models import PortfolioSnapshot
from elysian_core.core.enums import Venue

PortfolioSnapshot.create(
    strategy_id=0,
    venue=Venue.BINANCE,
    nav=100000.0,
    cash=10000.0,
    unrealized_pnl=1500.0,
    realized_pnl=500.0,
    total_commission=25.0,
    peak_equity=102000.0,
    max_drawdown=0.02,
    current_drawdown=0.0,
    gross_exposure=0.85,
    net_exposure=0.85,
    positions={"ETHUSDT": {"qty": 20.0, "avg_entry": 3200.0, "realized_pnl": 500.0}},
    weights={"ETHUSDT": 0.64},
    mark_prices={"ETHUSDT": 3275.0},
    num_fills=3,
)
```

### Verify Database Connectivity

```python
from elysian_core.db.database import DATABASE

try:
    DATABASE.connect()
    DATABASE.close()
    print("Database connection successful")
except Exception as e:
    print(f"Database connection failed: {e}")
```

### Clear All Rows (Reset)

```python
from elysian_core.db.database import DATABASE

with DATABASE.atomic():
    DATABASE.execute_sql(
        "TRUNCATE TABLE cex_trades, dex_trades, portfolio_snapshots, account_snapshots "
        "RESTART IDENTITY CASCADE;"
    )
```

Or via the Makefile:
```bash
make db-clear
```
