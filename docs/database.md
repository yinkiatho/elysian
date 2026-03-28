# Database Documentation

## Overview
The database module provides data persistence using PostgreSQL with Peewee ORM. It handles storage of trade records, order history, and portfolio state with connection pooling and error handling.

## Key Classes and Functions

### ReconnectPooledPostgresqlDatabase (database.py)
**Custom database class with automatic reconnection and connection pooling.**

**Configuration:**
- `max_connections`: Maximum pooled connections (default: 20)
- `stale_timeout`: Recycle idle connections (default: 300s)
- `autoconnect`: Automatic connection management

### BaseModel (database.py)
**Base Peewee model class with database configuration.**

**Meta:**
- `database`: DATABASE instance

### EnumField (models.py)
**Custom Peewee field for storing enums as strings.**

**Methods:**
- `db_value(value)`: Convert enum to string for storage
- `python_value(value)`: Convert string back to enum on retrieval

**Parameters:**
- `enum_class`: The enum class to handle

### CexTrade (models.py)
**Model for centralized exchange trade records.**

**Fields:**
- `id`: Auto-incrementing primary key
- `datetime`: Trade timestamp (UTC+8)
- `venue`: Exchange venue (enum)
- `symbol`: Trading pair (e.g., "ETHUSDT")
- `asset_type`: SPOT or PERPETUAL (enum)
- `side`: BUY or SELL (enum)
- `amount`: Requested base amount (signed)
- `executed_qty`: Actually executed quantity
- `price`: Average fill price
- `commission_asset`: Fee currency
- `total_commission`: Total fee amount
- `total_commission_quote`: Fee in quote currency
- `order_id`: Exchange order ID
- `status`: Order status (enum)
- `order_side`: Order side from exchange response
- `order_type`: Order type from exchange response

### DexTrade (models.py)
**Model for decentralized exchange swap events.**

**Fields:**
- `tx_digest`: Transaction digest (primary key)
- `datetime`: Event timestamp (UTC+8)
- `venue`: DEX venue (enum)
- `a_to_b`: Swap direction boolean
- `sender`: Transaction sender address
- `pool_address`: Pool contract address
- `token_a`: Token A symbol
- `token_b`: Token B symbol
- `token_a_address`: Token A contract address
- `token_b_address`: Token B contract address
- `amount_in`: Input amount (raw)
- `amount_out`: Output amount (raw)
- `amount_a`: Token A amount (decimal)
- `amount_b`: Token B amount (decimal)
- `before_sqrt_price`: Price before swap
- `after_sqrt_price`: Price after swap
- `sqrt_price`: Square root price
- `type`: Swap protocol type (enum)

## Database Operations

### Table Management
```python
from elysian_core.db.models import create_tables

# Create all tables
create_tables(safe=True)  # safe=True skips existing tables
```

### CRUD Operations
```python
# Create trade record
trade = CexTrade.create(
    venue=Venue.BINANCE,
    symbol="BTCUSDT",
    side=Side.BUY,
    amount=0.001,
    executed_qty=0.001,
    price=50000.0,
    order_id="12345",
    status=OrderStatus.FILLED
)

# Query trades
recent_trades = CexTrade.select().where(
    CexTrade.datetime >= datetime.datetime.now() - datetime.timedelta(days=1)
).order_by(CexTrade.datetime.desc())

# Update trade
trade.status = OrderStatus.CANCELLED
trade.save()

# Delete trade
trade.delete_instance()
```

### Bulk Operations
```python
# Bulk insert
trade_data = [
    {"symbol": "ETHUSDT", "side": Side.BUY, "amount": 0.1},
    {"symbol": "BTCUSDT", "side": Side.SELL, "amount": 0.001},
]

CexTrade.insert_many(trade_data).execute()
```

## Configuration

### Environment Variables
- `POSTGRES_DATABASE`: Database name
- `POSTGRES_USER`: Database username
- `POSTGRES_PASSWORD`: Database password
- `POSTGRES_HOST`: Database host
- `POSTGRES_PORT`: Database port (default: 5432)

### Connection Pooling
- **Max Connections**: 20 concurrent connections
- **Stale Timeout**: 300 seconds for connection recycling
- **Auto-connect**: Automatic connection management

## Schema Design

### Indexing Strategy
- Primary keys on ID fields and tx_digest
- Composite indexes on (venue, symbol, datetime) for trade queries
- Indexes on datetime fields for time-based filtering

### Data Types
- **Numeric**: FloatField for prices/amounts, IntegerField for IDs
- **Text**: TextField for addresses and large strings
- **DateTime**: DateTimeField with UTC+8 timezone
- **Enums**: Custom EnumField for type safety
- **JSON**: JSONField for flexible metadata

## Performance Optimizations

### Connection Pooling
- Reuses connections to reduce overhead
- Automatic reconnection on failures
- Configurable pool size based on load

### Query Optimization
- Selective field loading with `.select()`
- Efficient indexing for common queries
- Bulk operations for high-throughput scenarios

### Error Handling
- Automatic retry on connection failures
- Transaction rollback on errors
- Comprehensive error logging

## Migration Support

### Schema Evolution
```python
# Add new field
from peewee import migrate

migrator = migrate.migrate()

# Add column
migrate.add_column('cex_trades', 'new_field', CharField(null=True))

# Run migration
migrator.run()
```

## Monitoring and Maintenance

### Health Checks
```python
# Test connection
try:
    DATABASE.connect()
    print("Database connection successful")
except Exception as e:
    print(f"Database connection failed: {e}")
```

### Query Performance
```python
# Enable query logging
import logging
logging.getLogger('peewee').setLevel(logging.DEBUG)

# Analyze slow queries
from peewee import query_log
query_log.enable()
```

## Backup and Recovery

### Backup Strategy
```bash
# Full database backup
pg_dump -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE > backup.sql

# Restore from backup
psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DATABASE < backup.sql
```

### Point-in-Time Recovery
- WAL archiving enabled
- Regular base backups
- Continuous archiving for PITR

## Security Considerations

- **Credential Management**: Environment variables for sensitive data
- **Connection Encryption**: SSL/TLS for production deployments
- **Access Control**: Database-level user permissions
- **Audit Logging**: Query logging for security monitoring

## Usage Examples

### Trade Recording
```python
from elysian_core.db.models import CexTrade, DATABASE
from elysian_core.core.enums import Venue, Side, OrderStatus

# Record a Binance spot trade
with DATABASE.atomic():
    trade = CexTrade.create(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        asset_type=AssetType.SPOT,
        side=Side.BUY,
        amount=0.001,
        executed_qty=0.001,
        price=50000.0,
        commission_asset="USDT",
        total_commission=0.1,
        order_id="123456789",
        status=OrderStatus.FILLED
    )
```

### Portfolio Analysis
```python
# Get daily trading volume
daily_volume = CexTrade.select(
    fn.SUM(CexTrade.executed_qty).alias('volume')
).where(
    CexTrade.datetime >= datetime.date.today()
).scalar()

# Get P&L by symbol
pnl_by_symbol = CexTrade.select(
    CexTrade.symbol,
    fn.SUM(
        Case(None, (
            (CexTrade.side == Side.BUY, -CexTrade.executed_qty * CexTrade.price),
            (CexTrade.side == Side.SELL, CexTrade.executed_qty * CexTrade.price),
        ), 0)
    ).alias('pnl')
).group_by(CexTrade.symbol)
```</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\db\documentation.md