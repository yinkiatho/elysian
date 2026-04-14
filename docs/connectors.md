# Connectors Documentation

## Overview

The connectors module provides exchange integrations for real-time market data feeds and order execution. It implements a multiplexed WebSocket architecture for efficient streaming, supports two exchanges (Binance and Aster) across both spot and perpetual/futures markets, and emits typed events to the EventBus for consumption by strategy hooks.

The module is organized into three layers:

1. **Base layer** (`base.py`) — abstract classes defining contracts for feeds, client managers, and exchange connectors
2. **Data layer** (`binance/BinanceDataConnectors.py`, `aster/AsterDataConnectors.py`, etc.) — WebSocket feed implementations, multiplexed client managers, and user data stream managers
3. **Exchange layer** (`binance/BinanceExchange.py`, etc.) — authenticated REST/WS clients for order placement, balance management, and trade recording

In Docker deployment, the data layer runs in `elysian_market_data` and publishes to Redis. The exchange layer runs in each `elysian_strategy_N` container.

---

## Connector Architecture Overview

### Class Hierarchy

```
base.py
├── AbstractDataFeed  (ABC)
│   └── [used by all concrete feed classes]
│
├── AbstractClientManager  (ABC)
│   ├── OrderBookClientManager  (ABC)
│   │   ├── BinanceOrderBookClientManager
│   │   ├── BinanceFuturesOrderBookClientManager
│   │   ├── AsterOrderBookClientManager
│   │   └── AsterPerpOrderBookClientManager
│   │
│   └── KlineClientManager  (ABC)
│       ├── BinanceKlineClientManager
│       ├── BinanceFuturesKlineClientManager
│       ├── AsterKlineClientManager
│       └── AsterPerpKlineClientManager
│
├── SpotExchangeConnector  (ABC)
│   ├── BinanceSpotExchange
│   └── AsterSpotExchange
│
└── FuturesExchangeConnector  (ABC)
    └── BinanceFuturesExchange


Concrete Data Feeds:
  BinanceKlineFeed, BinanceOrderBookFeed
  BinanceFuturesKlineFeed, BinanceFuturesOrderBookFeed
  AsterKlineFeed, AsterOrderBookFeed
  AsterPerpKlineFeed, AsterPerpOrderBookFeed

User Data Managers (standalone):
  BinanceUserDataClientManager      ← new WS API (ws-api.binance.com)
  BinanceFuturesUserDataClientManager  ← listenKey approach
  AsterUserDataClientManager           ← listenKey approach
  AsterPerpUserDataClientManager       ← listenKey approach
```

### EventBus Injection

In `MarketDataService`, client managers receive a `RedisEventBusPublisher` via `set_event_bus()`:

```python
pub = RedisEventBusPublisher(redis_url=redis_url, channel_prefix="elysian:md:binance:spot")
binance_kline_manager.set_event_bus(pub)
binance_ob_manager.set_event_bus(pub)
```

In strategy containers, exchange connectors receive the `RedisEventBusSubscriber` (shared bus) via `exchange.start(shared_event_bus)` for mark-price updates.

---

## Base Classes (`base.py`)

### `AbstractDataFeed`

Base class for all exchange data feeds.

**Constructor:**
```python
AbstractDataFeed(save_data: bool = False, file_dir: Optional[str] = None)
```

**State attributes:**
- `_name`, `_interval` — symbol and interval, set by `create_new()`
- `_data: Optional[OrderBook]` — latest OrderBook snapshot
- `_vol: Optional[float]` — rolling volatility (fractional; multiply by 1e4 for bps)
- `_historical: deque` — rolling deque of last 60 mid-prices (maxlen=60)
- `_historical_ob: deque` — rolling deque of last 60 OrderBook snapshots (maxlen=60)
- `fetched_initial_snapshot: bool` — True once the REST snapshot has been fetched

**Properties:**
- `name`, `interval`, `data`, `volatility`
- `latest_price` — `_historical[-1]`
- `latest_bid_price` / `latest_ask_price` — from `_historical_ob[-1]`

**Slippage pricing methods:**
```python
def executed_buy_price(self, amount: float, amount_in_base: bool = True) -> float
def executed_sell_price(self, amount: float, amount_in_base: bool = True) -> float
```
Walk the book and return the VWAP price for a given order size. `amount_in_base=True` means `amount` is in base asset; `False` means quote asset.

**Order book delta application:**
```python
@staticmethod
def apply_orderbook_delta(orders, deltas, descending=True) -> List[List[float]]
```
Apply `[price, qty]` deltas to an order book side. `qty == 0` removes the level.

**Background stats loop:**
```python
async def update_current_stats(self)
```
Runs every second: appends mid-price to `_historical` and computes 60-second realised volatility using `statistics.stdev`. Overridden to a no-op in kline feeds (they update `_historical` inline).

**Abstract interface:**
```python
@abstractmethod
def create_new(self, asset: str, interval: str = "1s")
```

---

### `AbstractClientManager`

Generic interface for a multiplexing WebSocket client manager.

**Shared state:**
- `_running: bool`
- `_active_feeds: Dict[str, Any]` — symbol → feed
- `_queue: asyncio.Queue` — bounded message queue
- `_reader_task`, `_worker_tasks` — managed asyncio Tasks
- `_event_bus` — Optional EventBus/RedisEventBusPublisher, injected via `set_event_bus()`

**Abstract methods:**
```python
async def start(self)
async def stop(self)
async def run_multiplex_feeds(self)
```

`KlineClientManager` additionally requires:
```python
def register_feed(self, feed: Any)
def unregister_feed(self, symbol: str)
def get_feed(self, symbol: str) -> Optional[Any]
```

---

### `SpotExchangeConnector`

Abstract base for spot exchange connectors.

**Constructor:**
```python
SpotExchangeConnector(
    args: argparse.Namespace,
    api_key: str,
    api_secret: str,
    symbols: List[str],
    file_path: Optional[str] = None,
    venue: Venue = None,
    strategy_config: Optional[StrategyConfig] = None,
)
```

**Shared state:**
- `_balances: Dict[str, float]` — asset → free balance
- `_open_orders: Dict[str, collections.OrderedDict[str, Order]]` — symbol → ordered map
- `_token_infos: Dict[str, dict]` — symbol → `{step_size, min_notional, base_asset, quote_asset, …}`
- `kline_feeds: Dict[str, Kline]` — updated via `_on_kline` subscription
- `ob_feeds: Dict[str, OrderBook]` — updated via `_on_orderbook_update` subscription
- `_past_orders: pylru.lrucache(1000)` — recently closed orders for dedup

**EventBus integration:**
```python
def start(self, event_bus)   # subscribe to KLINE and ORDERBOOK_UPDATE
def stop()                   # unsubscribe
```

**Concrete utility methods:**
```python
def last_price(self, symbol) -> Optional[float]          # OB mid → kline close → None
def order_health_check(self, symbol, side, quantity, ...) -> bool  # balance + min-notional
def get_balance(self, asset) -> float
def base_asset_to_symbol(self, asset) -> Optional[str]   # "ETH" → "ETHUSDT"
```

**Abstract methods (must implement):**
```python
async def initialize(self)
async def _fetch_symbol_info(self, symbol) -> dict
async def refresh_balances(self) -> Dict[str, float]
async def place_limit_order(self, symbol, side, quantity, price, strategy_id)
async def place_market_order(self, symbol, side, quantity, ...)
async def cancel_order(self, symbol, order_id)
async def get_open_orders(self, symbol)
async def get_deposit_address(self, coin, network=None)
async def deposit_asset(self, coin, amount, network=None)
async def withdraw_asset(self, coin, amount, address, network=None)
```

**Static utility:**
```python
@staticmethod
def _average_fill_price(resp: dict) -> Tuple[float, float]
```
Computes VWAP fill price and total commission from a market order REST response. Falls back to `cummulativeQuoteQty / executedQty` when the `fills` list is absent (e.g. Aster API).

---

### `FuturesExchangeConnector`

Parallel abstract base for futures connectors. Adds:
- `_positions: Dict[str, dict]` — symbol → `{amount, entry_price, unrealized_pnl, leverage}`
- `_leverages: Dict[str, int]`
- `_margin_types: Dict[str, str]`
- Abstract: `refresh_positions()`, `set_leverage()`, `set_margin_type()`, `close_position()`, `place_stop_order()`

---

## Binance Spot Connector

### `BinanceSpotExchange`

**File:** `connectors/binance/BinanceExchange.py`

**Constructor:**
```python
BinanceSpotExchange(
    args=argparse.Namespace(),
    api_key="",
    api_secret="",
    symbols=[],
    file_path=None,
    user_data_manager=None,           # BinanceUserDataClientManager (optional)
    private_event_bus=None,           # per-strategy EventBus for account events
    strategy_config=None,             # StrategyConfig for logging prefix
)
```

When `private_event_bus` is provided, it is injected into `user_data_manager` via `set_event_bus()`.

**Initialization (`initialize()`):**
1. Creates `AsyncClient` (python-binance)
2. Fetches symbol info for all tracked symbols → populates `_token_infos`
3. Passes token info to `user_data_manager` for OrderUpdateEvent `base_asset` / `quote_asset` population
4. Registers `_handle_user_data` with user data manager
5. Starts user data stream

**`place_market_order()`:**
- Runs `order_health_check()` (balance + min-notional)
- Rounds quantity to `step_size` via `round_step_size` (python-binance helper)
- Calls `order_market_buy` / `order_market_sell`
- Returns the raw REST response (fill details are from the exchange REST response, not the subsequent user-data stream event)
- Does NOT record the trade here — trade recording happens in `ShadowBook._async_record_trade()`

**User Data Handling:**

`_handle_user_data(event: dict)` receives normalized events from `BinanceUserDataClientManager`:

```
event["type"] == "balance_update"    → update _balances[asset] += delta
event["type"] == "account_position"  → overwrite _balances from full position list
event["type"] == "execution_report"  → call _handle_execution_report(order, exec_type)
```

`_handle_execution_report()` maintains `_open_orders`:
- `exec_type == "NEW"` and unknown order_id → wrap in `Order`, add to registry; raises `ValueError` if order was recently closed (dedup via `_past_orders`)
- Otherwise → call `existing.update_fill()` or `existing.transition_to()` on the tracked order; remove from registry on terminal

**`run()`:**
Calls `initialize()`, then starts background tasks: `monitor_balances` (10s poll), `monitor_open_orders` (5 min poll).

**`cleanup()`:**
- Calls `stop()` (unsubscribes from shared event bus)
- Stops `user_data_manager`
- Closes `AsyncClient`

---

## Binance Spot Data Connectors

**File:** `connectors/binance/BinanceDataConnectors.py`

### `BinanceKlineClientManager`

Multiplexed kline WebSocket manager for Binance spot.

- Connects to Binance combined stream via `BinanceSocketManager.multiplex_socket`
- Streams: `["{symbol}@kline_1s", …]`
- Queue: `asyncio.Queue(maxsize=100000)`
- Workers: `min(8, len(feeds))`

**Worker per message:**
1. Parse kline via `feed._process_kline_event(data)` — returns `None` for open (unclosed) candles
2. `feed._kline = kline`
3. `feed._historical.append(kline.close)`
4. If `_event_bus` set: `await _event_bus.publish(KlineEvent(…))`

Note: volatility computation (`statistics.stdev`) has been removed from the Binance spot kline worker in the current codebase (unlike futures workers which still compute it).

### `BinanceOrderBookClientManager`

Multiplexed order book manager for Binance spot.

- Streams: `["{symbol}@depth@100ms", …]`
- Queue: `asyncio.Queue(maxsize=10000)`

**Worker per message:**
1. `feed._data = await feed._process_depth_event(data)` — applies incremental depth update
2. If `_event_bus` set: `await _event_bus.publish(OrderBookUpdateEvent(…))`

### `BinanceKlineFeed`

Concrete kline feed. `_process_kline_event()` returns `None` for in-progress candles (Binance field `k.x == False`).

### `BinanceOrderBookFeed`

Implements the Binance diff-depth synchronisation protocol:

1. On first message, buffers events while fetching a REST snapshot (`GET /api/v3/depth`)
2. REST snapshot sets `last_update_id` and marks `snapshot_ready = True`
3. Replays buffered events: discards those with `u < last_update_id`; applies those where `U <= last_update_id <= u`
4. Subsequent events validated by `U <= last_update_id + 1`; gaps trigger a snapshot re-fetch

### `BinanceUserDataClientManager`

Manages the Binance spot user data WebSocket stream using the **new WS API** (`wss://ws-api.binance.com:443/ws-api/v3`).

> **Important:** The deprecated listenKey approach (`/api/v3/userDataStream`) was retired by Binance on 2026-02-20. This manager uses `userDataStream.subscribe.signature` which works with HMAC-SHA256 keys without requiring `session.logon` (which requires Ed25519 keys).

**Subscription:**
```python
await ws.send(json.dumps({
    "id": "user-data-sub",
    "method": "userDataStream.subscribe.signature",
    "params": {
        "apiKey": api_key,
        "timestamp": timestamp,
        "signature": hmac_sha256(f"apiKey={api_key}&timestamp={timestamp}"),
    }
}))
```

**Incoming message format:** `{"subscriptionId": 0, "event": {"e": "executionReport", …}}`

**Dispatch sequence per event:**
1. Parse raw WS event → normalized dict via `_parse_user_data_event()`
2. Dispatch to sync callbacks (`exchange._handle_user_data`) — mutates exchange state
3. Dispatch async events to EventBus (private bus):
   - `executionReport` → `OrderUpdateEvent(symbol, base_asset, quote_asset, venue, order, timestamp)`
   - `balanceUpdate` → `BalanceUpdateEvent(asset, venue, delta, new_balance=0.0, timestamp)`

`_parse_execution_report()` maps raw WS fields to `Order`:
```
i → id
s → symbol
S → side
o → order_type
X → status
z → filled_qty (cumulative)
Z/z → avg_fill_price (cumulative quote / cumulative qty)
L → last_fill_price
n → commission (per-execution, not cumulative)
N → commission_asset
j → strategy_id (Binance strategyId tag)
```

---

## Binance Futures Connectors

**Files:** `connectors/binance/BinanceFuturesExchange.py`, `connectors/binance/BinanceFuturesDataConnectors.py`

### `BinanceFuturesExchange`

Authenticated Binance USDS-M futures client. Key differences from spot:

- Symbol info additionally stores `tick_size` (minimum price increment)
- `set_leverage()` and `set_margin_type()` called during `initialize()`
- `order_health_check()` validates margin availability: `required_margin = notional / leverage`
- `place_market_order()` supports `reduce_only=True`
- Additional order types: `place_stop_order()`, `place_take_profit_order()`
- `close_position()` — reads current position amount, places market order in opposite direction with `reduceOnly=True`
- `refresh_positions()` and `refresh_balances()` both call `futures_account()` (returns both in one request)

**User data events handled:**
- `"ACCOUNT_UPDATE"` → updates `_balances` (from `a.B` list) and `_positions` (from `a.P` list)
- `"ORDER_TRADE_UPDATE"` → updates `_open_orders` using cumulative fields `z` (qty) and `ap` (avg price)
- `"MARGIN_CALL"` → logged as warning
- `"ACCOUNT_CONFIG_UPDATE"` → updates `_leverages[symbol]`

### `BinanceFuturesUserDataClientManager`

Uses the listenKey approach for futures (Binance futures user data stream has not retired listenKeys):

1. `POST /fapi/v1/listenKey` — obtain listenKey
2. Connect to `wss://fstream.binance.com/ws/<listenKey>`
3. `PUT /fapi/v1/listenKey` every 30 minutes via keepalive background task
4. `DELETE /fapi/v1/listenKey` on `stop()`

### `BinanceFuturesKlineClientManager`

Same structure as `BinanceKlineClientManager` but:
- DNS pre-resolves to `fapi.binance.com`
- Streams use `"{symbol}@kline_1m"` (1-minute candles)
- Volatility annualisation: `sqrt(12)` for 1m candles

### `BinanceFuturesOrderBookFeed`

Same protocol as `BinanceOrderBookFeed` but uses `event['pu']` (previous update ID) for sequence validation matching the Binance futures API spec.

---

## Aster Spot Connector

**File:** `connectors/aster/AsterExchange.py`

`AsterSpotExchange` uses `aiohttp` for all REST calls (not python-binance). The Aster REST API is Binance-compatible.

**Base URL:** `https://sapi.asterdex.com`

Key differences from BinanceSpotExchange:
- Uses `aiohttp.ClientSession` lazily created in `_request()`
- `_sign()` is inline HMAC-SHA256 (not delegated to a library)
- `_round_step()` uses `floor(qty / step) * step` (not `round_step_size`)
- `get_deposit_address()` and `deposit_asset()` return None/False (not supported by Aster spot API)
- No `strategy_id` forwarding to the API for market orders
- Execution reports use `Venue.ASTER`

`_handle_user_data()` is a sync callback registered with `AsterUserDataClientManager`. It handles `balanceUpdate`, `outboundAccountPosition`, and `executionReport` event types directly (no normalized-dict middleware layer like Binance).

---

## Aster Data Connectors

**Files:** `connectors/aster/AsterDataConnectors.py`, `connectors/aster/AsterPerpDataConnectors.py`

### `AsterKlineClientManager`

- WebSocket: `wss://sstream.asterdex.com/ws` (subscription-message approach)
- Subscribe: `{"method": "SUBSCRIBE", "params": ["ethusdt@kline_1s", …]}`
- Handles both combined-stream envelope (`{"stream": …, "data": …}`) and direct event formats (`{"e": "kline", …}`)

### `AsterUserDataClientManager`

- listenKey lifecycle: `POST /api/v1/listenKey` → connect to `wss://sstream.asterdex.com/ws/<listenKey>` → `PUT` every 30 min → `DELETE` on stop
- Registered callbacks receive raw event dicts; no EventBus wiring (account events stay in-process)

### `AsterOrderBookFeed`

Implements snapshot + delta sync for Aster spot. Uses `AsterOrderBook.from_lists()`. Sequence validation uses `event['pu']` (previous update ID) for live events.

REST snapshot: `GET https://sapi.asterdex.com/api/v1/depth` (sync, via `asyncio.to_thread`).

### `AsterPerpKlineClientManager`

- WebSocket: `wss://fstream.asterdex.com/stream?streams={streams}` (query-string combined stream)
- Streams: `"{symbol}@kline_1m"` (1-minute candles)

### `AsterPerpOrderBookFeed`

REST snapshot from `https://fapi.asterdex.com/fapi/v1/depth` (sync, no `asyncio.to_thread`).

---

## Common Patterns

### Worker Coroutine Pattern (State Mutation Before Event Dispatch)

```python
async def _worker_coroutine(self, worker_id: int):
    while self._running:
        msg = await self._queue.get()
        # 1. Parse and mutate feed state (synchronous)
        kline = feed._process_kline_event(data)
        if kline:
            feed._kline = kline
            feed._historical.append(kline.close)
        # 2. Publish event (async) — strategy hooks see consistent state
        if self._event_bus is not None:
            await self._event_bus.publish(KlineEvent(...))
        self._queue.task_done()
```

The sync-before-async order is an invariant: strategy hooks always see the updated feed state when they handle an event.

### WebSocket Reconnection

All managers implement exponential backoff:
```python
reconnect_delay = 1
while self._running:
    try:
        async with websockets.connect(...) as ws:
            reconnect_delay = 1   # reset on successful connect
            ...
    except Exception:
        pass
    await asyncio.sleep(reconnect_delay)
    reconnect_delay = min(reconnect_delay * 2, 60)
```

Backoff starts at 1s and doubles up to a 60s maximum.

### Order Fill Handling

Orders in `_open_orders[symbol][order_id]` are `Order` objects. The exchange updates them from user-data events using cumulative fields (Binance `z` = cumulative filled qty, `ap` or `Z/z` = cumulative avg price). `Order.update_fill()` computes the running weighted-average fill price idempotently. Terminal orders are removed from `_open_orders` immediately after the status transition.

`BinanceSpotExchange` additionally maintains `_past_orders: pylru.lrucache(1000)` to detect and warn on duplicate `NEW` execution reports for recently closed orders.

---

## Supported Venues

| Venue | Spot Connector | Futures Connector | Spot Data | Futures Data | User Data |
|-------|---------------|-------------------|-----------|--------------|-----------|
| Binance | `BinanceSpotExchange` | `BinanceFuturesExchange` | `BinanceKlineClientManager` + `BinanceOrderBookClientManager` | `BinanceFuturesKlineClientManager` + `BinanceFuturesOrderBookClientManager` | `BinanceUserDataClientManager` (new WS API) / `BinanceFuturesUserDataClientManager` (listenKey) |
| Aster | `AsterSpotExchange` | — | `AsterKlineClientManager` + `AsterOrderBookClientManager` | `AsterPerpKlineClientManager` + `AsterPerpOrderBookClientManager` | `AsterUserDataClientManager` / `AsterPerpUserDataClientManager` |

---

## Adding a New Exchange Connector

Follow these steps to add a new spot exchange.

**Step 1: Create data connector feeds**
```python
class NewExchangeKlineFeed(AbstractDataFeed):
    def create_new(self, asset: str, interval: str = "1s"): ...
    def _process_kline_event(self, raw: dict) -> Optional[Kline]: ...

class NewExchangeOrderBookFeed(AbstractDataFeed):
    def create_new(self, asset: str, interval: str = "100ms"): ...
    async def get_initial_snapshot(self): ...
    async def _process_depth_event(self, event: dict) -> OrderBook: ...
    async def _apply_depth_update(self, event: dict): ...
```

**Step 2: Create multiplex client managers**
```python
class NewExchangeKlineClientManager(KlineClientManager):
    async def start(self): ...
    async def stop(self): ...
    def register_feed(self, feed): ...
    def get_feed(self, symbol): ...
    def unregister_feed(self, symbol): ...
    async def run_multiplex_feeds(self): ...
    # Worker: mutate feed state first (sync), then publish event (async)
```

**Step 3: Create user data stream manager**
```python
class NewExchangeUserDataClientManager:
    async def start(self): ...
    async def stop(self): ...
    def register(self, callback): ...
    def set_event_bus(self, event_bus): ...
    async def _reader_loop(self):
        # Exponential backoff reconnection
        # Dispatch sync callbacks first, then async EventBus
```

**Step 4: Create the exchange connector**
```python
class NewExchangeSpotExchange(SpotExchangeConnector):
    async def initialize(self): ...
    async def _fetch_symbol_info(self, symbol): ...
    async def refresh_balances(self): ...
    async def place_limit_order(self, ...): ...
    async def place_market_order(self, ...): ...
    async def cancel_order(self, ...): ...
    async def get_open_orders(self, symbol): ...
    async def get_deposit_address(self, ...): ...
    async def deposit_asset(self, ...): ...
    async def withdraw_asset(self, ...): ...
    async def run(self): ...
    async def cleanup(self): ...
```

**Step 5: Add `Venue` enum value** in `core/enums.py`.

**Step 6: Register** in `StrategyRunner._exchange_connector_callables` in `run_strategy.py`.

---

## Performance Notes

- **Multiplexing**: One TCP connection per venue/market-type carries all symbols, reducing connection overhead and rate-limit surface
- **Bounded queues**: `asyncio.Queue(maxsize=N)` prevents unbounded memory growth under load; kline managers use 100,000, OB managers use 10,000
- **Worker pool**: `min(8, len(feeds))` concurrent workers process the shared queue in parallel, preventing head-of-line blocking on slow parse paths
- **Awaited publish**: `await event_bus.publish(event)` means slow strategy callbacks naturally throttle workers (backpressure)
- **Background stats**: `update_current_stats()` (for OB feeds) runs as a separate 1s-sleep coroutine, so volatility calculation never blocks the depth processing path
- **State-before-publish**: sync feed state mutation always precedes async event dispatch, ensuring hooks see consistent state
