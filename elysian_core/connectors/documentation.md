# Connectors Documentation

## Overview
The connectors module provides exchange integrations for real-time market data feeds and trading operations. It implements a multiplex architecture for efficient WebSocket connections and supports multiple cryptocurrency exchanges.

## Key Classes and Functions

### AbstractDataFeed (base.py)
**Base class for all exchange data feeds providing common functionality.**

**Methods:**
- `__init__(save_data, file_dir)`: Initialize feed with data saving options
- `create_new(asset, interval)`: Configure feed for specific asset and interval (abstract)
- `__call__()`: Open connection and run feed until cancelled (abstract)
- `executed_buy_price(amount, amount_in_base)`: Calculate VWAP buy price for slippage simulation
- `executed_sell_price(amount, amount_in_base)`: Calculate VWAP sell price for slippage simulation
- `apply_orderbook_delta(orders, deltas, descending)`: Apply price level deltas to order book side
- `update_current_stats()`: Calculate 60-second rolling volatility from mid prices
- `periodically_log_data(interval_secs)`: Save order book snapshots to CSV periodically
- `_append_to_df(ob)`: Add order book snapshot to internal DataFrame

**Properties:**
- `name`: Feed name/asset identifier
- `interval`: Update interval
- `data`: Current OrderBook snapshot
- `volatility`: Rolling volatility in basis points
- `latest_price`: Most recent mid price
- `latest_bid_price`: Best bid price
- `latest_ask_price`: Best ask price

### BinanceKlineClientManager (BinanceExchange.py)
**Shared AsyncClient for multiplex kline WebSocket streams.**

**Methods:**
- `__init__()`: Initialize with queue and feed registry
- `_create_client(retries)`: Create Binance AsyncClient with retry logic
- `start()`: Initialize client and socket manager
- `stop()`: Close connections and cancel tasks
- `register_feed(feed)`: Add feed to multiplex registry
- `run_multiplex_feeds()`: Process multiplex stream and dispatch to registered feeds

### BinanceOrderBookClientManager (BinanceExchange.py)
**Shared AsyncClient for multiplex order book WebSocket streams.**

**Methods:**
- `__init__()`: Initialize with queue and feed registry
- `_create_client(retries)`: Create Binance AsyncClient with retry logic
- `start()`: Initialize client and socket manager
- `stop()`: Close connections and cancel tasks
- `register_feed(feed)`: Add feed to multiplex registry
- `run_multiplex_feeds()`: Process multiplex stream and dispatch to registered feeds

### BinanceKlineFeed (BinanceExchange.py)
**Kline (candlestick) data feed implementation inheriting from AbstractDataFeed.**

**Methods:**
- `create_new(asset, interval)`: Configure for specific symbol and interval
- `__call__()`: Establish WebSocket connection and process kline updates
- `get_initial_snapshot()`: Fetch historical kline data for initialization

### BinanceOrderBookFeed (BinanceExchange.py)
**Order book data feed implementation inheriting from AbstractDataFeed.**

**Methods:**
- `create_new(asset, interval)`: Configure for specific symbol and interval
- `__call__()`: Establish WebSocket connection and process order book updates
- `get_initial_snapshot()`: Fetch current order book snapshot
- `_process_buffered_events()`: Apply buffered updates to order book
- `_sync_with_snapshot(snapshot)`: Synchronize order book with full snapshot

### BinanceSpotExchange (BinanceExchange.py)
**Spot trading exchange client for order execution.**

**Methods:**
- `__init__(api_key, api_secret, testnet)`: Initialize with API credentials
- `get_account_info()`: Fetch account balances and permissions
- `place_order(symbol, side, order_type, quantity, price)`: Place spot order
- `cancel_order(symbol, order_id)`: Cancel existing order
- `get_order_status(symbol, order_id)`: Check order status
- `get_open_orders(symbol)`: Get all open orders for symbol
- `get_trade_history(symbol, limit)`: Fetch recent trade history

### BinanceFuturesExchange (BinanceFuturesExchange.py)
**Futures trading exchange client for order execution.**

**Methods:**
- `__init__(api_key, api_secret, testnet)`: Initialize with API credentials
- `get_account_info()`: Fetch futures account balances and positions
- `place_order(symbol, side, order_type, quantity, price, leverage)`: Place futures order
- `cancel_order(symbol, order_id)`: Cancel existing futures order
- `get_order_status(symbol, order_id)`: Check futures order status
- `get_open_orders(symbol)`: Get all open futures orders
- `get_positions()`: Get current futures positions

### AsterKlineFeed (AsterExchange.py)
**Aster exchange kline data feed.**

**Methods:**
- `create_new(asset, interval)`: Configure for Aster symbol and interval
- `__call__()`: Establish Aster WebSocket connection for kline data
- `get_initial_snapshot()`: Fetch initial kline data

### AsterOrderBookFeed (AsterExchange.py)
**Aster exchange order book data feed.**

**Methods:**
- `create_new(asset, interval)`: Configure for Aster symbol and interval
- `__call__()`: Establish Aster WebSocket connection for order book data
- `get_initial_snapshot()`: Fetch initial order book snapshot

### AsterKlineClientManager (AsterExchange.py)
**Shared client manager for Aster kline feeds.**

### AsterOrderBookClientManager (AsterExchange.py)
**Shared client manager for Aster order book feeds.**

### VolatilityBarbClientLocal (VolatiltyFeed.py)
**Redis-backed volatility data client.**

**Methods:**
- `new(redis)`: Initialize with Redis connection
- `get_volatility(symbol)`: Fetch volatility data for symbol
- `subscribe_volatility(symbol)`: Subscribe to volatility updates

### CacheKeys (VolatiltyFeed.py)
**Enum for Redis cache keys.**

### RedisConfig (VolatiltyFeed.py)
**Redis connection configuration.**

**Methods:**
- `local()`: Create local Redis connection
- `remote(host, port)`: Create remote Redis connection

## Data Structures

### Kline Data Format
```python
{
    "symbol": "BTCUSDT",
    "interval": "1m",
    "open_time": 1640995200000,
    "open": "43100.00",
    "high": "43200.00",
    "low": "43000.00",
    "close": "43150.00",
    "volume": "123.456"
}
```

### Order Book Update Format
```python
{
    "stream": "btcusdt@depth",
    "data": {
        "e": "depthUpdate",
        "E": 1640995200000,
        "s": "BTCUSDT",
        "U": 1000000,
        "u": 1000010,
        "b": [["43000.00", "1.234"]],
        "a": [["43001.00", "2.345"]]
    }
}
```

## Configuration Parameters

### Feed Configuration
- `save_data`: bool - Whether to persist data to files
- `file_dir`: str - Directory for data persistence
- `interval`: str - Update interval ("1s", "100ms", "1m", etc.)

### Exchange Configuration
- `api_key`: str - Exchange API key
- `api_secret`: str - Exchange API secret
- `testnet`: bool - Use testnet instead of live trading

## Error Handling

- Automatic reconnection on WebSocket disconnection
- Exponential backoff for API rate limits
- Graceful degradation when feeds become unavailable
- Comprehensive logging for debugging

## Performance Optimizations

- **Multiplex Architecture**: Single WebSocket connection serves multiple feeds
- **Async Processing**: Non-blocking I/O operations
- **Queue-Based Processing**: Bounded queues prevent memory overflow
- **LRU Caching**: Efficient data structure reuse
- **Background Stats**: Rolling volatility calculation without blocking feeds</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\connectors\documentation.md