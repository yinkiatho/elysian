# Connectors Documentation

## Overview
The connectors module provides exchange integrations for real-time market data feeds and trading operations. It implements a multiplex architecture for efficient WebSocket connections, supports multiple cryptocurrency exchanges (Binance, Aster), and emits typed events to the EventBus for consumption by `SpotStrategy` hooks.

## EventBus Integration

All client managers inherit `set_event_bus(event_bus)` from `AbstractClientManager` (base.py). When an EventBus is injected, workers publish typed events after mutating feed state:

- **Kline workers** → `KlineEvent` after `feed._kline = kline` and volatility calculation
- **OrderBook workers** → `OrderBookUpdateEvent` after `feed._data = orderbook`
- **User data reader** → `OrderUpdateEvent` (from `executionReport`) and `BalanceUpdateEvent` (from `balanceUpdate`)

Events are published via `await self._event_bus.publish(event)` — awaited for natural backpressure. If no EventBus is set (`_event_bus is None`), no events are emitted and the connectors work in standalone mode.

### Event Emission Points

```
BinanceKlineClientManager._worker_coroutine():
    feed = self._active_feeds.get(symbol)
    kline = feed._process_kline_event(data)
    feed._kline = kline                          # mutate feed state
    feed._historical.append(kline.close)          # rolling history
    # ... volatility calculation ...
    if self._event_bus is not None:               # emit event
        await self._event_bus.publish(KlineEvent(
            symbol=symbol, venue=Venue.BINANCE,
            kline=kline, timestamp=int(time.time() * 1000)
        ))

BinanceOrderBookClientManager._worker_coroutine():
    feed = self._active_feeds.get(symbol)
    feed._data = await feed._process_depth_event(data)  # mutate feed state
    if self._event_bus is not None and feed._data is not None:
        await self._event_bus.publish(OrderBookUpdateEvent(
            symbol=symbol, venue=Venue.BINANCE,
            orderbook=feed._data, timestamp=int(time.time() * 1000)
        ))

BinanceUserDataClientManager._reader_loop():
    # FIRST: sync callbacks (exchange._handle_user_data)
    for cb in self._subscribers:
        cb(event)                                 # mutate exchange state
    # THEN: async event bus dispatch
    if self._event_bus is not None:
        if event_type == "executionReport":
            order = self._parse_execution_to_order(event)
            await self._event_bus.publish(OrderUpdateEvent(...))
        elif event_type == "balanceUpdate":
            await self._event_bus.publish(BalanceUpdateEvent(...))
```

**Key design**: Sync callbacks (exchange state mutation) run FIRST, then async EventBus publish. This ensures strategy hooks see consistent exchange state.

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

### AbstractClientManager (base.py)
**Base class for multiplex WebSocket client managers.**

**Attributes:**
- `_event_bus`: Optional EventBus reference (default None)

**Methods:**
- `set_event_bus(event_bus)`: Inject an EventBus for event emission. Called by StrategyRunner during setup.

All concrete client managers (Binance kline/OB, Aster kline/OB, futures variants) inherit this field and method.

### BinanceKlineClientManager (BinanceDataConnectors.py)
**Shared AsyncClient for multiplex kline WebSocket streams.**

**Methods:**
- `__init__()`: Initialize with queue and feed registry
- `_create_client(retries)`: Create Binance AsyncClient with retry logic
- `start()`: Initialize client and socket manager
- `stop()`: Close connections and cancel tasks
- `register_feed(feed)`: Add feed to multiplex registry
- `run_multiplex_feeds()`: Start network reader + worker pool. Workers parse kline messages, mutate feed state, and publish `KlineEvent` to EventBus
- `set_event_bus(event_bus)`: Inject EventBus (inherited from AbstractClientManager)

**Architecture:**
- Single multiplex WebSocket connection for all symbol kline streams
- `asyncio.Queue` (maxsize=10,000) buffers incoming messages
- 4 async worker tasks dequeue, parse, mutate feed, and publish events

### BinanceOrderBookClientManager (BinanceDataConnectors.py)
**Shared AsyncClient for multiplex order book WebSocket streams.**

**Methods:**
- Same interface as BinanceKlineClientManager
- Workers publish `OrderBookUpdateEvent` after processing depth updates
- `set_event_bus(event_bus)`: Inject EventBus (inherited)

### BinanceKlineFeed (BinanceDataConnectors.py)
**Kline (candlestick) data feed implementation inheriting from AbstractDataFeed.**

**Methods:**
- `create_new(asset, interval)`: Configure for specific symbol and interval
- `__call__()`: Establish WebSocket connection and process kline updates
- `get_initial_snapshot()`: Fetch historical kline data for initialization
- `_process_kline_event(data)`: Parse raw WebSocket message into `Kline` object

### BinanceOrderBookFeed (BinanceDataConnectors.py)
**Order book data feed implementation inheriting from AbstractDataFeed.**

**Methods:**
- `create_new(asset, interval)`: Configure for specific symbol and interval
- `__call__()`: Establish WebSocket connection and process order book updates
- `get_initial_snapshot()`: Fetch current order book snapshot via REST API
- `_process_depth_event(data)`: Apply depth update to order book
- `_process_buffered_events()`: Apply buffered updates to order book after snapshot sync
- `_sync_with_snapshot(snapshot)`: Synchronize order book with full snapshot

### BinanceSpotExchange (BinanceExchange.py)
**Spot trading exchange client for order execution and user data streaming.**

**Constructor:**
```python
BinanceSpotExchange(
    args, api_key, api_secret, symbols,
    kline_manager=None, ob_manager=None,
    user_data_manager=None, event_bus=None,  # EventBus injection
)
```

When `event_bus` is provided, it is automatically injected into the `user_data_manager` via `set_event_bus()`.

**Methods:**
- `run()`: Initialize AsyncClient, start user data stream
- `cleanup()`: Close connections
- `get_account_info()`: Fetch account balances and permissions
- `place_order(symbol, side, order_type, quantity, price)`: Place spot order
- `place_limit_order(symbol, side, quantity, price)`: Place limit order
- `cancel_order(symbol, order_id)`: Cancel existing order
- `get_order_status(symbol, order_id)`: Check order status
- `get_open_orders(symbol)`: Get all open orders for symbol
- `_handle_user_data(event)`: Sync callback for user data events (order fills, balance changes)
- `_handle_execution_report(event)`: Parse execution report into internal order state

### BinanceUserDataClientManager (BinanceDataConnectors.py)
**Manages the user data WebSocket stream for order and balance updates.**

**Methods:**
- `__init__()`: Initialize with subscriber list
- `start()`: Start user data stream
- `stop()`: Stop user data stream
- `register(callback)`: Register sync callback for user data events
- `unregister(callback)`: Remove sync callback
- `set_event_bus(event_bus)`: Inject EventBus for async event dispatch
- `_reader_loop()`: Main read loop — dispatches to sync callbacks first, then publishes typed events to EventBus
- `_parse_execution_to_order(msg)`: Static method that parses raw `executionReport` dict into an `Order` dataclass

**Dispatch order in `_reader_loop()`:**
1. Sync callbacks fire first (`exchange._handle_user_data`) — mutates exchange state
2. Async EventBus dispatch fires second — strategy hooks see consistent state

### Aster Connectors

**AsterKlineClientManager / AsterOrderBookClientManager** (AsterDataConnectors.py)
- Same multiplex architecture as Binance equivalents
- Inherit `set_event_bus()` from AbstractClientManager

**AsterKlineFeed / AsterOrderBookFeed** (AsterDataConnectors.py)
- Aster exchange kline and order book feeds

**AsterSpotExchange** (AsterExchange.py)
- Aster spot trading client

### Futures Connectors

**BinanceFuturesKlineClientManager / BinanceFuturesOrderBookClientManager** (BinanceFuturesDataConnectors.py)
- Futures variants of the Binance multiplex managers

**BinanceFuturesExchange** (BinanceFuturesExchange.py)
- Futures trading client with leverage support

**AsterPerpKlineClientManager / AsterPerpOrderBookClientManager** (AsterPerpDataConnectors.py)
- Aster perpetual futures data managers

**AsterPerpExchange** (AsterPerpExchange.py)
- Aster perpetual futures trading client

### VolatilityBarbClientLocal (VolatilityFeed.py)
**Redis-backed volatility data client.**

**Methods:**
- `new(redis)`: Initialize with Redis connection
- `get_volatility(symbol)`: Fetch volatility data for symbol
- `subscribe_volatility(symbol)`: Subscribe to volatility updates

## Data Structures

### Kline Data Format (from WebSocket)
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

### Order Book Update Format (from WebSocket)
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

### User Data Event Formats (from WebSocket)
```python
# executionReport — parsed into OrderUpdateEvent
{
    "e": "executionReport",
    "E": 1640995200000,
    "s": "BTCUSDT",
    "S": "BUY",
    "o": "LIMIT",
    "q": "0.001",
    "p": "43000.00",
    "X": "FILLED",
    "i": 12345,
    ...
}

# balanceUpdate — parsed into BalanceUpdateEvent
{
    "e": "balanceUpdate",
    "E": 1640995200000,
    "a": "USDT",
    "d": "-43.00",
    ...
}
```

## Configuration Parameters

### Feed Configuration
- `save_data`: bool — Whether to persist data to files
- `file_dir`: str — Directory for data persistence
- `interval`: str — Update interval ("1s", "100ms", "1m", etc.)

### Exchange Configuration
- `api_key`: str — Exchange API key
- `api_secret`: str — Exchange API secret
- `event_bus`: Optional[EventBus] — EventBus for event emission (injected by StrategyRunner)

## Error Handling

- Automatic reconnection on WebSocket disconnection
- Exponential backoff for API rate limits
- Graceful degradation when feeds become unavailable
- EventBus callback exceptions are caught and logged — one failing callback doesn't affect others
- Comprehensive logging for debugging

## Performance Optimizations

- **Multiplex Architecture**: Single WebSocket connection serves multiple feeds
- **Async Processing**: Non-blocking I/O operations in single event loop
- **Queue-Based Processing**: Bounded asyncio.Queue (maxsize=10k) prevents memory overflow
- **Zero-Overhead Events**: EventBus dispatch is direct async function call — no serialization
- **Backpressure**: Awaited publish means slow strategies throttle workers naturally
- **Background Stats**: Rolling volatility calculation without blocking feeds
