# Core Documentation

## Overview
The core module contains fundamental data structures, enums, business logic for trading operations, and the event system that drives the strategy framework. It provides exchange-agnostic representations of market data, orders, portfolios, and typed events consumed by `SpotStrategy` hooks.

## Event System

### EventType (events.py)
**Enum identifying the kind of event flowing through the EventBus.**

| Value | Associated Event Class | Emitted By |
|-------|----------------------|------------|
| `KLINE` | `KlineEvent` | Kline client manager workers |
| `ORDERBOOK_UPDATE` | `OrderBookUpdateEvent` | OrderBook client manager workers |
| `ORDER_UPDATE` | `OrderUpdateEvent` | User data client manager |
| `BALANCE_UPDATE` | `BalanceUpdateEvent` | User data client manager |

### KlineEvent (events.py)
**Frozen dataclass emitted when a kline candle closes.**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair (e.g., "ETHUSDT") |
| `venue` | `Venue` | Exchange enum |
| `kline` | `Kline` | OHLCV candle data |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `KLINE` (not in `__init__`) |

### OrderBookUpdateEvent (events.py)
**Frozen dataclass emitted on each orderbook depth update.**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange enum |
| `orderbook` | `OrderBook` | Bid/ask snapshot (reference to live object) |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `ORDERBOOK_UPDATE` |

### OrderUpdateEvent (events.py)
**Frozen dataclass emitted when an order status changes (fill, cancel, reject).**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange enum |
| `order` | `Order` | Parsed order with status, fills, fees |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `ORDER_UPDATE` |

### BalanceUpdateEvent (events.py)
**Frozen dataclass emitted when account balance changes.**

| Field | Type | Description |
|-------|------|-------------|
| `asset` | `str` | Asset symbol (e.g., "USDT") |
| `venue` | `Venue` | Exchange enum |
| `delta` | `float` | Balance change amount |
| `new_balance` | `float` | New balance after change |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `BALANCE_UPDATE` |

**Design notes:**
- All event dataclasses use `@dataclass(frozen=True)` to prevent strategies from accidentally mutating shared data
- `event_type` uses `field(default=..., init=False)` so it is set automatically and cannot be overridden
- Events carry **references** to live objects (e.g., `orderbook`). Copy immediately if you need a snapshot

### EventBus (event_bus.py)
**Lightweight in-process async event bus with zero serialization overhead.**

Workers publish typed events after mutating feed state; the bus dispatches to all registered async callbacks keyed by `EventType`.

**Methods:**

| Method | Description |
|--------|-------------|
| `subscribe(event_type, callback)` | Register an async callback for a specific event type |
| `unsubscribe(event_type, callback)` | Remove a previously registered callback (no-op if not found) |
| `publish(event)` | Dispatch event to all subscribers. Awaited sequentially for backpressure |

**Key behaviors:**
- `publish()` is `await`ed (not fire-and-forget) — if a strategy hook is slow, the emitting worker waits, providing natural backpressure
- Exceptions in one callback are logged but do not prevent remaining callbacks from running
- Uses `defaultdict(list)` internally — subscribing to an unused event type is zero-cost

```python
from elysian_core.core.event_bus import EventBus
from elysian_core.core.events import EventType

bus = EventBus()

async def my_handler(event):
    print(f"Got {event.symbol}")

bus.subscribe(EventType.KLINE, my_handler)

# Later, from a worker:
await bus.publish(KlineEvent(symbol="ETHUSDT", venue=Venue.BINANCE, kline=k, timestamp=ts))
```

## Market Data Classes

### OrderBook (market_data.py)
**Normalized order book snapshot with bid/ask management.**

**Methods:**
- `__init__(last_update_id, last_timestamp, ticker, interval, bid_orders, ask_orders)`: Initialize order book
- `from_lists(last_update_id, last_timestamp, ticker, interval, bid_levels, ask_levels)`: Create from bid/ask lists
- `apply_update(side, price, qty)`: Apply single price level update
- `apply_both_updates(last_timestamp, new_update_id, bid_levels, ask_levels)`: Apply concurrent bid/ask updates (async)

**Properties:**
- `best_bid_price`: Highest bid price
- `best_bid_amount`: Quantity at best bid
- `best_ask_price`: Lowest ask price
- `best_ask_amount`: Quantity at best ask
- `mid_price`: Midpoint price ((best_bid + best_ask) / 2)
- `spread`: Bid-ask spread
- `spread_bps`: Spread in basis points

### Kline (market_data.py)
**OHLCV candlestick data structure.**

**Attributes:**
- `ticker`: Trading pair symbol
- `interval`: Time interval (e.g., "1m", "1h")
- `start_time`: Candle start timestamp
- `end_time`: Candle end timestamp
- `open`: Opening price
- `high`: Highest price
- `low`: Lowest price
- `close`: Closing price
- `volume`: Trading volume

### Exchange-Specific OrderBooks

- **BinanceOrderBook** (market_data.py): Binance-specific with async `apply_update_lists()` and `apply_both_updates()`
- **AsterOrderBook** (market_data.py): Aster-specific with same async update interface

## Order Classes

### Order (order.py)
**Generic exchange order representation.**

**Attributes:**
- `id`: Unique order identifier
- `symbol`: Trading pair
- `side`: BUY or SELL
- `order_type`: LIMIT, MARKET, or RANGE
- `quantity`: Order quantity
- `price`: Limit price (None for market orders)
- `filled_qty`: Quantity already filled
- `avg_fill_price`: Average fill price
- `status`: Current order status
- `timestamp`: Order creation time
- `venue`: Exchange venue
- `client_order_id`: Client-specified order ID
- `fee`: Trading fee amount
- `fee_currency`: Fee currency

**Properties:**
- `remaining_qty`: Unfilled quantity
- `is_filled`: Whether order is completely filled
- `is_active`: Whether order is still active

### LimitOrder (order.py)
**Liquidity provider limit order for AMM-style exchanges.**

### RangeOrder (order.py)
**Liquidity provider range order (concentrated liquidity).**

## Portfolio Classes

### Position (portfolio.py)
**Single asset position tracking.**

**Attributes:**
- `symbol`: Asset symbol
- `quantity`: Position size (>0 long, <0 short)
- `avg_entry_price`: Average entry price
- `realized_pnl`: Realized profit/loss

**Methods:**
- `unrealized_pnl(mark_price)`: Calculate unrealized P&L at given price
- `notional(mark_price)`: Position notional value
- `is_flat()`: Check if position is closed

### Portfolio (portfolio.py)
**Multi-asset portfolio management.**

**Methods:**
- `__init__(initial_cash)`: Initialize with starting cash
- `position(symbol)`: Get or create position for symbol

**Properties:**
- `cash`: Available cash balance
- `total_value`: Total portfolio value (cash + positions)

## Enums

### Side (enums.py)
- `BUY`, `SELL`
- `opposite()`: Return opposite side

### AssetType (enums.py)
- `SPOT`, `PERPETUAL`

### OrderType (enums.py)
- `LIMIT`, `MARKET`, `RANGE`

### OrderStatus (enums.py)
- `PENDING`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`
- `is_active()`: Check if order can still be filled

### Venue (enums.py)
- `BINANCE`, `BYBIT`, `OKX`, `CETUS`, `TURBOS`, `DEEPBOOK`, `ASTER`

### Chain (enums.py)
- `SUI`, `TON`, `ETHEREUM`, `BITCOIN`, `SOLANA`

### TradeType (enums.py)
- `REAL`, `SIMULATED`

### SwapType (enums.py)
- `BLUEFIN`, `CETUS`, `FLOW_X`, `TURBOS`

### WalletType (enums.py)
- `WALLET`, `CEX`

## Data Flow

1. **Market Data**: Exchange workers create `Kline`/`OrderBook` objects and mutate feed state
2. **Event Emission**: Workers publish `KlineEvent`/`OrderBookUpdateEvent` to EventBus
3. **Strategy Hooks**: EventBus dispatches to `SpotStrategy.on_kline()` / `on_orderbook_update()`
4. **Order Execution**: Strategy calls `exchange.place_order()` → creates `Order` objects
5. **Order Updates**: User data stream emits `OrderUpdateEvent` → `on_order_update()` fires
6. **Balance Updates**: User data stream emits `BalanceUpdateEvent` → `on_balance_update()` fires
7. **Portfolio Tracking**: `Position`/`Portfolio` objects updated with fills
8. **Persistence**: Trade/order records saved to database

## Key Design Patterns

- **Frozen Dataclasses**: Event types are immutable to prevent shared-data mutation
- **EventBus Pub/Sub**: Decouples data producers (workers) from consumers (strategies)
- **Enums with Methods**: Type-safe constants with utility methods (e.g., `Side.opposite()`)
- **Properties**: Computed attributes for derived values (spreads, midpoints)
- **Inheritance**: Exchange-specific subclasses extend base OrderBook
- **Async Methods**: Non-blocking updates for high-frequency data

## Performance Considerations

- Efficient data structures (SortedDict for order books)
- Minimal object creation in hot paths
- Frozen dataclasses are lightweight (no copy overhead for event creation)
- Lazy computation of derived properties
- EventBus dispatch is zero-overhead (direct async function calls, no serialization)
