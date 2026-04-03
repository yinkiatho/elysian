# Connectors Documentation

## Overview

The connectors module provides exchange integrations for real-time market data feeds and order execution. It implements a multiplexed WebSocket architecture for efficient streaming, supports two exchanges (Binance and Aster) across both spot and perpetual/futures markets, and emits typed events to the `EventBus` for consumption by strategy hooks.

The module is organised into three layers:

1. **Base layer** (`base.py`) — abstract classes defining contracts for feeds, client managers, and exchange connectors
2. **Data layer** (`*DataConnectors.py`) — WebSocket feed implementations, multiplexed client managers, and user data stream managers
3. **Exchange layer** (`*Exchange.py`) — authenticated REST/WS clients for order placement, balance management, and trade recording

---

## Connector Architecture Overview

### Class Hierarchy

```
base.py
├── AbstractDataFeed  (ABC)
│   ├── AbstractOrderBookFeed  (marker subclass)
│   └── [used by all concrete OB feed classes]
│
├── AbstractKlineFeed  (ABC)
│   └── [used by all concrete kline feed classes]
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


Data Feeds (all inherit AbstractDataFeed unless noted):
  BinanceKlineFeed
  BinanceOrderBookFeed
  BinanceFuturesKlineFeed
  BinanceFuturesOrderBookFeed
  AsterKlineFeed
  AsterOrderBookFeed
  AsterPerpKlineFeed            (inherits AbstractDataFeed)
  AsterPerpOrderBookFeed        (inherits AbstractDataFeed)

User Data Managers (standalone, not part of the ABC tree):
  BinanceUserDataClientManager
  BinanceFuturesUserDataClientManager
  AsterUserDataClientManager
  AsterPerpUserDataClientManager
```

### Dependency Injection Pattern

Exchange connectors receive their data managers at construction time via optional constructor arguments. When an argument is `None`, the connector creates a default instance. This allows the same manager instances to be shared across multiple connectors (e.g. one `BinanceKlineClientManager` shared between a spot exchange and any monitoring component).

```python
exchange = BinanceSpotExchange(
    api_key="...",
    api_secret="...",
    symbols=["ETHUSDT"],
    kline_manager=shared_kline_manager,   # injected
    ob_manager=shared_ob_manager,          # injected
    user_data_manager=udm,                 # injected
    event_bus=event_bus,                   # injected
)
```

The `event_bus` argument causes the exchange to immediately call `user_data_manager.set_event_bus(event_bus)`, wiring the user data stream for async event dispatch.

---

## Base Classes (`base.py`)

### `AbstractDataFeed`

Base class for all order-book feeds. Concrete subclasses implement `create_new()` only; all shared logic lives here.

**Constructor:**
```python
AbstractDataFeed(save_data: bool = False, file_dir: Optional[str] = None)
```

**State attributes:**
- `_name: Optional[str]` — symbol identifier, set by `create_new()`
- `_interval: Optional[str]` — update interval string (e.g. `"100ms"`, `"1s"`)
- `_data: Optional[OrderBook]` — latest OrderBook snapshot
- `_vol: Optional[float]` — rolling volatility in fractional terms (multiply by 1e4 for bps)
- `_historical: deque` — rolling deque of last 60 mid-prices (maxlen=60)
- `_historical_ob: deque` — rolling deque of last 60 OrderBook snapshots (maxlen=60)
- `fetched_initial_snapshot: bool` — True once the REST snapshot has been fetched

**Properties:**
- `name -> Optional[str]`
- `interval -> Optional[str]`
- `data -> Optional[OrderBook]` — current OrderBook snapshot
- `volatility -> Optional[float]` — rolling realised vol
- `latest_price -> float` — last mid-price from `_historical[-1]`
- `latest_bid_price -> float` — best bid from `_historical_ob[-1]`
- `latest_ask_price -> float` — best ask from `_historical_ob[-1]`

**Key methods:**

```python
def executed_buy_price(self, amount: float, amount_in_base: bool = True) -> float
```
Walk the ask side of the book and return the VWAP price for buying `amount`. If `amount_in_base=True`, `amount` is in base asset; if `False`, `amount` is in quote asset (cost). Returns the worst ask price if the book is exhausted.

```python
def executed_sell_price(self, amount: float, amount_in_base: bool = True) -> float
```
Walk the bid side and return the VWAP price for selling `amount`.

```python
@staticmethod
def apply_orderbook_delta(
    orders: List[List[float]],
    deltas: list,
    descending: bool = True,
) -> List[List[float]]
```
Apply a list of `[price, qty]` deltas to one side of an order book. A delta with `qty == 0` removes the price level. Handles both 2-element (Binance/Aster) and 4-element (OKX) delta formats. Returns the side re-sorted.

```python
async def update_current_stats(self)
```
Background coroutine that runs every second: appends the current mid-price to `_historical` and computes 60-second realised volatility using `statistics.stdev`. Overridden to a no-op in kline feeds (they update `_historical` inline per closed candle).

```python
async def periodically_log_data(self, interval_secs: int = 60)
```
Background coroutine that saves `_full_df` to a CSV at `file_dir/{name}_ob.csv` on the given interval. Only runs when `save_data=True` and `file_dir` is set.

**Abstract methods:**
```python
@abstractmethod
def create_new(self, asset: str, interval: str = "1s")
```
Set `_name` and `_interval` to configure the feed for a specific symbol.

---

### `AbstractKlineFeed`

Base class for kline/candle feeds. Parallel to `AbstractDataFeed` but operates on `Kline` objects rather than `OrderBook`.

**State attributes:**
- `_kline: Optional[Any]` — latest closed kline
- `_historical: deque` — rolling deque of last 60 close prices (maxlen=60)
- `_vol: Optional[float]` — rolling realised volatility

**Properties:**
- `name`, `interval`, `volatility` — same semantics as `AbstractDataFeed`
- `latest_close -> Optional[float]` — `_historical[-1]` if non-empty, else `None`

**Abstract methods:**
```python
@abstractmethod
def create_new(self, asset: str, interval: str = "1m")

@abstractmethod
async def __call__(self)
```

---

### `AbstractClientManager`

Generic interface for a multiplexing WebSocket client manager. Concrete subclasses manage a shared connection and distribute messages to registered feed instances.

**Shared state:**
- `_running: bool`
- `_active_feeds: Dict[str, Any]` — symbol → feed mapping
- `_queue: asyncio.Queue` — bounded message queue
- `_reader_task`, `_worker_tasks` — managed async tasks
- `_event_bus` — Optional EventBus, injected via `set_event_bus()`

**Methods:**
```python
def set_event_bus(self, event_bus)
```
Inject an `EventBus` so workers can publish typed events. When `None` (default), the manager runs in standalone mode and emits no events.

**Abstract methods:**
```python
@abstractmethod
async def start(self)

@abstractmethod
async def stop(self)

@abstractmethod
async def run_multiplex_feeds(self)
```

`KlineClientManager` additionally requires:
```python
@abstractmethod
def register_feed(self, feed: Any)

@abstractmethod
def unregister_feed(self, symbol: str)

@abstractmethod
def get_feed(self, symbol: str) -> Optional[Any]
```

---

### `SpotExchangeConnector`

Abstract base for spot exchange connectors. Holds all shared state and provides concrete utilities that all spot exchanges use.

**Constructor:**
```python
SpotExchangeConnector(
    args: argparse.Namespace,
    api_key: str,
    api_secret: str,
    symbols: List[str],
    file_path: Optional[str] = None,
    kline_manager: Optional[KlineClientManager] = None,
    ob_manager: Optional[OrderBookClientManager] = None,
)
```

**Shared state:**
- `_balances: Dict[str, float]` — asset → free balance
- `_open_orders: Dict[str, OrderedDict[str, Order]]` — symbol → ordered mapping of order_id → Order
- `_token_infos: Dict[str, dict]` — symbol → metadata (step_size, min_notional, base_asset, quote_asset)

**Concrete methods available to all spot exchanges:**

```python
def kline_feed(self, symbol: str)
def ob_feed(self, symbol: str)
def last_price(self, symbol: str) -> Optional[float]
```
`last_price` resolves the best available mid-price: OB mid-price first, falls back to last kline close.

```python
@staticmethod
def _average_fill_price(resp: dict) -> Tuple[float, float]
```
Compute VWAP fill price and total commission from a market order REST response. Falls back to `cummulativeQuoteQty / executedQty` when the `fills` list is absent (Aster spot).

```python
def order_health_check(self, symbol: str, side: Side, quantity: float, use_quote_order_qty: bool = False) -> bool
```
Validate balance sufficiency and minimum notional before placing an order. Logs an error and returns `False` if checks fail.

```python
def get_balance(self, asset: str) -> float
def base_asset_to_symbol(self, asset: str) -> Optional[str]
async def get_all_open_orders(self)
async def cancel_all_orders(self, symbol: str)
async def monitor_balances(self, interval_secs: int = 5, name: str = "")
async def monitor_open_orders(self, poll_interval: int = 300)
async def print_snapshot(self, poll_interval: int = 120, balance_filter: float = 1e-5)
```

**Abstract methods (must be implemented by subclasses):**
```python
async def initialize(self)
async def _fetch_symbol_info(self, symbol: str) -> dict
async def refresh_balances(self) -> Dict[str, float]
async def get_deposit_address(self, coin: str, network: Optional[str] = None) -> Optional[str]
async def deposit_asset(self, coin: str, amount: float, network: Optional[str] = None) -> bool
async def withdraw_asset(self, coin: str, amount: float, address: str, network: Optional[str] = None) -> bool
async def place_limit_order(self, symbol: str, side: Side, price: float, quantity: float, strategy_id: int)
async def place_market_order(self, symbol: str, side: Side, quantity: float, use_quote_order_qty: bool = False)
async def cancel_order(self, symbol: str, order_id: int)
async def get_open_orders(self, symbol: str)
```

---

### `FuturesExchangeConnector`

Parallel abstract base for perpetual/futures connectors. Extends the spot base with futures-specific state: positions, leverage, and margin type.

Additional constructor parameter:
```python
default_leverage: int = 1
```

Additional state:
- `_positions: Dict[str, dict]` — symbol → `{amount, entry_price, unrealized_pnl, leverage}`
- `_leverages: Dict[str, int]` — symbol → current leverage
- `_margin_types: Dict[str, str]` — symbol → `"ISOLATED"` or `"CROSSED"`

Additional abstract methods:
```python
async def refresh_positions(self)
async def set_leverage(self, symbol: str, leverage: int)
async def set_margin_type(self, symbol: str, margin_type: str)
async def place_stop_order(self, symbol: str, side: Side, quantity: float, stop_price: float, reduce_only: bool = True)
async def close_position(self, symbol: str)
```

Concrete accessors:
```python
def get_position(self, symbol: str) -> Optional[dict]
def get_leverage(self, symbol: str) -> int
```

---

## Binance Spot Connector

### `BinanceSpotExchange` (`BinanceExchange.py`)

Authenticated Binance spot client that manages balances, open orders, and order placement for a list of symbols.

**Constructor:**
```python
BinanceSpotExchange(
    args: argparse.Namespace = argparse.Namespace(),
    api_key: str = "",
    api_secret: str = "",
    symbols: Optional[List[str]] = [],
    file_path: Optional[str] = None,
    kline_manager: Optional[BinanceKlineClientManager] = None,
    ob_manager: Optional[BinanceOrderBookClientManager] = None,
    user_data_manager: Optional[BinanceUserDataClientManager] = None,
    event_bus = None,
)
```

When `event_bus` is provided, it is immediately injected into `user_data_manager` via `set_event_bus()`.

**Key methods:**

```python
async def initialize(self)
```
Creates the `AsyncClient`, fetches symbol info for all tracked symbols, registers `_handle_user_data` with the user data manager, and starts the user data stream.

```python
async def _fetch_symbol_info(self, symbol: str)
```
Calls `get_symbol_info` and stores `step_size`, `min_notional`, `base_asset`, `quote_asset`, and precision values in `_token_infos[symbol]`.

```python
async def refresh_balances(self)
```
Fetches the full account and writes free balances to `_balances`.

```python
async def place_market_order(
    self,
    symbol: str,
    side: Side,
    quantity: float,
    strategy_id: int,
    strategy_name: str,
    use_quote_order_qty: bool = False,
) -> Optional[dict]
```
Runs `order_health_check`, rounds quantity to `step_size` via `round_step_size`, then calls `order_market_buy` or `order_market_sell`. On success, parses the VWAP fill price and fires `_record_trade` as a background task (does not block). Returns the raw REST response. The `strategy_id` is passed to Binance as the `strategyId` tag.

```python
async def place_limit_order(
    self,
    symbol: str,
    side: Side,
    quantity: float,
    price: float,
    strategy_id: int,
)
```
Places a GTC limit order. Creates an `Order` with `OrderStatus.PENDING` and registers it in `_open_orders[symbol]` immediately.

```python
async def cancel_order(self, symbol: str, order_id: int)
async def get_open_orders(self, symbol: str)
async def get_all_open_orders(self)
```
`get_all_open_orders` reconciles the full open-orders registry: it preserves local order state if the local `last_updated_timestamp` is more recent than the exchange's `updateTime`.

```python
async def withdraw_asset(self, coin: str, amount: float, address: str, network: str = "SUI") -> bool
async def get_deposit_address(self, coin: str, network: Optional[str] = None) -> Optional[str]
```

```python
async def run(self)
```
Calls `initialize()`, then launches background tasks: `monitor_balances` (10s poll), `monitor_open_orders` (300s poll), and `print_snapshot`.

```python
async def cleanup(self)
```
Stops kline/OB managers and closes the `AsyncClient`.

**User data handling:**

`_handle_user_data(msg: dict)` is registered as a sync callback with `BinanceUserDataClientManager`. It handles three event types:
- `"balanceUpdate"` — applies delta to `_balances`
- `"outboundAccountPosition"` — overwrites free + locked balances for all listed assets
- `"executionReport"` — delegates to `_handle_execution_report`

`_handle_execution_report(msg: dict)` maintains `_open_orders`:
- `"NEW"` exec_type and unknown order ID → creates a new `Order` and registers it
- All other cases → updates `filled_qty`, `avg_fill_price`, `commission`, calls `order.transition_to(status)`; removes terminal orders from the registry

---

## Binance Spot Data Connector

### `BinanceKlineClientManager` (`BinanceDataConnectors.py`)

Multiplexed kline WebSocket manager that drives one 1s-kline stream per registered symbol over a single Binance combined stream connection.

```python
async def start(self)  # creates AsyncClient with DNS pre-resolution + up to 5 retries
async def stop(self)   # cancels reader/worker tasks, closes socket and client
def register_feed(self, feed: BinanceKlineFeed)
def unregister_feed(self, symbol: str)
def get_feed(self, symbol: str) -> Optional[BinanceKlineFeed]
async def run_multiplex_feeds(self)
```

`run_multiplex_feeds()` creates one `_reader_coroutine` task and up to `min(8, len(feeds))` `_worker_coroutine` tasks. The reader uses `BinanceSocketManager.multiplex_socket` with streams `["{symbol}@kline_1s", ...]` and enqueues into a bounded `asyncio.Queue(maxsize=100000)`. Workers dequeue and:

1. Parse the kline via `feed._process_kline_event(data)`
2. Store the closed kline: `feed._kline = kline`
3. Append `kline.close` to `feed._historical`
4. When `_historical` reaches 60 entries, compute realised vol: `stdev(returns) * sqrt(20)`
5. If `_event_bus` is set, publish `KlineEvent(symbol, Venue.BINANCE, kline, timestamp_ms)`

Reconnection: the reader loop catches all exceptions and reconnects with exponential backoff starting at 1s, capping at 60s.

---

### `BinanceOrderBookClientManager` (`BinanceDataConnectors.py`)

Multiplexed order-book WebSocket manager identical in structure to `BinanceKlineClientManager` but subscribing to `"{symbol}@depth@100ms"` streams.

Workers:
1. Call `await feed._process_depth_event(data)` and assign result to `feed._data`
2. If `feed.save_data`, schedule `feed._append_to_df(feed._data)` as a background task
3. If `_event_bus` is set, publish `OrderBookUpdateEvent(symbol, Venue.BINANCE, orderbook, timestamp_ms)`

Queue maxsize is 10,000.

---

### `BinanceKlineFeed` (`BinanceDataConnectors.py`)

Concrete kline feed for a single Binance spot symbol.

```python
def create_new(self, asset: str, interval: str = "1s")

def _process_kline_event(self, raw: dict) -> Optional[Kline]
```
Returns `None` for in-progress (not yet closed) candles. Only closed candles (`k.x == True`) produce a `Kline`.

```python
@property
def latest_kline(self) -> Optional[Kline]
@property
def latest_close(self) -> Optional[float]
```

The `Kline` dataclass has fields: `ticker`, `interval`, `start_time`, `end_time`, `open`, `high`, `low`, `close`, `volume`.

---

### `BinanceOrderBookFeed` (`BinanceDataConnectors.py`)

Concrete order-book feed implementing the Binance diff-depth synchronisation protocol.

```python
def create_new(self, asset: str, interval: str = "100ms")

async def get_initial_snapshot(self)
```
Fetches a REST snapshot from `https://api.binance.com/api/v3/depth` and constructs a `BinanceOrderBook`. After the snapshot, replays any events buffered during the fetch.

```python
async def _process_depth_event(self, event: dict) -> OrderBook
```
Implements the Binance sequence-number validation:
- While snapshot is not ready: buffer the event
- If `event['u'] < last_update_id`: discard as stale
- If `event['U'] > last_update_id + 1`: gap detected, re-fetch snapshot asynchronously, raise `ValueError`
- Otherwise: call `_apply_depth_update`

```python
async def _apply_depth_update(self, event: dict)
```
Calls `self._data.apply_both_updates(ts, event['u'], bid_levels=event['bids'], ask_levels=event['asks'])`.

---

### `BinanceUserDataClientManager` (`BinanceDataConnectors.py`)

Manages the Binance spot user data WebSocket stream using the new WebSocket API (`wss://ws-api.binance.com:443/ws-api/v3`). The deprecated listen-key approach was retired by Binance on 2026-02-20. This manager uses `userDataStream.subscribe.signature` with HMAC-SHA256 keys, which does not require `session.logon`.

**Constructor:**
```python
BinanceUserDataClientManager(api_key: str, api_secret: str)
```

**Lifecycle:**
```python
async def start(self)   # launches _reader_task
async def stop(self)    # cancels task, closes WebSocket
```

**Subscription:**
```python
def register(self, callback: Callable[[dict], None])
def unregister(self, callback: Callable[[dict], None])
def set_event_bus(self, event_bus)
```

**Dispatch order in `_reader_loop()`:**
1. Sync callbacks fire first (`exchange._handle_user_data`) — mutates exchange state
2. Async EventBus dispatch fires second — strategy hooks see consistent state

EventBus events emitted:
- `"executionReport"` → `OrderUpdateEvent(symbol, Venue.BINANCE, order, timestamp_ms)`
- `"balanceUpdate"` → `BalanceUpdateEvent(asset, Venue.BINANCE, delta, new_balance=0.0, timestamp_ms)`

```python
@staticmethod
def _parse_execution_to_order(msg: dict) -> Optional[Order]
```
Maps raw executionReport fields to an `Order` dataclass. Returns `None` if the status is not in `_BINANCE_STATUS`. Key field mappings: `i→id`, `s→symbol`, `S→side`, `o→order_type`, `X→status`, `z→filled_qty`, `Z/z→avg_fill_price`, `n→commission`, `N→commission_asset`.

---

## Binance Futures Connector

### `BinanceFuturesExchange` (`BinanceFuturesExchange.py`)

Authenticated Binance USDS-M futures client. Derives from `FuturesExchangeConnector`.

**Constructor:**
```python
BinanceFuturesExchange(
    args: argparse.Namespace = argparse.Namespace(),
    api_key: str = "",
    api_secret: str = "",
    symbols: Optional[List[str]] = [],
    file_path: Optional[str] = None,
    kline_manager: Optional[BinanceFuturesKlineClientManager] = None,
    ob_manager: Optional[BinanceFuturesOrderBookClientManager] = None,
    user_data_manager: Optional[BinanceFuturesUserDataClientManager] = None,
    default_leverage: int = 1,
)
```

**Initialisation (`initialize()`):**
Creates `AsyncClient`, fetches symbol info (step_size, min_notional, tick_size), calls `set_leverage` for each symbol at `default_leverage`, then starts the futures user data stream.

**Symbol info** additionally stores `tick_size` (minimum price increment) for futures symbols.

**Leverage and margin:**
```python
async def set_leverage(self, symbol: str, leverage: int)
async def set_margin_type(self, symbol: str, margin_type: str)
```
`set_margin_type` silently handles `BinanceAPIException` code `-4046` (already set).

**Order placement:**
```python
async def place_market_order(
    self, symbol: str, side: Side, quantity: float, reduce_only: bool = False
)
async def place_limit_order(
    self, symbol: str, side: Side, price: float, quantity: float,
    reduce_only: bool = False, time_in_force: str = "GTC"
)
async def place_stop_order(
    self, symbol: str, side: Side, quantity: float, stop_price: float,
    reduce_only: bool = True
)
async def place_take_profit_order(
    self, symbol: str, side: Side, quantity: float, stop_price: float,
    reduce_only: bool = True
)
```
All order methods round quantity to `step_size`, run `order_health_check`, then call `futures_create_order`. Market order fills are parsed immediately; all order types register an `Order` in `_open_orders`.

**Futures-specific order health check:**
Validates minimum notional and approximate margin: `required_margin = notional / leverage`. Checks wallet balance against required margin.

**Position management:**
```python
async def close_position(self, symbol: str)
```
Reads `_positions[symbol]["amount"]`, determines close side, and places a `MARKET` order with `reduceOnly=True`.

```python
async def refresh_positions(self)
async def refresh_balances(self)
```
Both call `futures_account()` which returns both balances and positions in one request.

**User data event types handled:**
- `"ACCOUNT_UPDATE"` — updates `_balances` (from `B` list) and `_positions` (from `P` list)
- `"ORDER_TRADE_UPDATE"` — updates `_open_orders` using cumulative fill fields `z` (qty) and `ap` (avg price)
- `"MARGIN_CALL"` — logged as warning
- `"ACCOUNT_CONFIG_UPDATE"` — updates `_leverages[symbol]`

**Supported `OrderType` values for futures:**
`LIMIT`, `MARKET`, `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET` (mapped via `_BINANCE_ORDER_TYPE` dict)

---

## Binance Futures Data Connector

### `BinanceFuturesKlineClientManager` (`BinanceFuturesDataConnectors.py`)

Multiplexed futures kline manager. Identical structure to `BinanceKlineClientManager` with these differences:
- DNS pre-resolves to `fapi.binance.com` (futures domain)
- Streams use `"{symbol}@kline_1m"` (1-minute candles instead of 1-second)
- Volatility annualisation factor: `sqrt(12)` (for 1m candles, 12 candles per hour, vs `sqrt(20)` for 1s)
- No EventBus publishing — futures kline workers do not publish `KlineEvent` in current implementation

---

### `BinanceFuturesOrderBookClientManager` (`BinanceFuturesDataConnectors.py`)

Multiplexed futures order-book manager using `manager.futures_multiplex_socket(streams)` (futures-specific socket factory). Streams: `"{symbol}@depth@100ms"`. No EventBus publishing in current implementation.

---

### `BinanceFuturesKlineFeed` / `BinanceFuturesOrderBookFeed` (`BinanceFuturesDataConnectors.py`)

Concrete feed classes for futures data. Identical in interface to their spot equivalents. `BinanceFuturesOrderBookFeed._process_depth_event` uses `event['pu']` (previous update ID) for sequence validation, matching the Binance futures API spec (spot uses `event['U']`/`event['u']` with different logic).

---

### `BinanceFuturesUserDataClientManager` (`BinanceFuturesDataConnectors.py`)

Manages the Binance USDS-M futures user data stream using the listenKey approach (as opposed to the new WS API used for spot):

1. `POST /fapi/v1/listenKey` — obtain a listenKey
2. Connect to `wss://fstream.binance.com/ws/<listenKey>`
3. `PUT /fapi/v1/listenKey` every 30 minutes via a background keepalive task
4. `DELETE /fapi/v1/listenKey` on `stop()`

Reconnection on disconnect obtains a fresh listenKey. Cancels the keepalive task if the connection drops.

Event dispatch: synchronous callbacks only (no EventBus wiring in current implementation for futures).

---

## Aster Spot Connector

### `AsterSpotExchange` (`AsterExchange.py`)

Authenticated Aster spot exchange client. Derives from `SpotExchangeConnector`. The Aster REST API is Binance-compatible (same field names, same filter types), enabling reuse of `_BINANCE_STATUS` and `_average_fill_price`.

**Base URL:** `https://sapi.asterdex.com`

**Constructor:**
```python
AsterSpotExchange(
    args: argparse.Namespace = argparse.Namespace(),
    api_key: str = "",
    api_secret: str = "",
    symbols: Optional[List[str]] = None,
    file_path: Optional[str] = None,
    kline_manager: Optional[AsterKlineClientManager] = None,
    ob_manager: Optional[AsterOrderBookClientManager] = None,
    user_data_manager: Optional[AsterUserDataClientManager] = None,
)
```

Note: No `event_bus` parameter — Aster spot exchange does not wire EventBus to its user data manager.

**HTTP layer:**
```python
def _sign(self, params: dict) -> dict
def _headers(self) -> dict
async def _request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False) -> Any
```
`_request` lazily creates an `aiohttp.ClientSession`. Signed requests add timestamp and HMAC-SHA256 signature. Non-2xx responses are raised via `resp.raise_for_status()`.

**Initialisation (`initialize()`):**
Pings `/api/v1/ping`, fetches symbol info for all symbols, refreshes balances, then starts user data stream.

**Order methods:**

```python
async def place_limit_order(self, symbol: str, side: Side, price: float, quantity: float, strategy_id: int = 0)
async def place_market_order(self, symbol: str, side: Side, quantity: float, use_quote_order_qty: bool = False)
async def cancel_order(self, symbol: str, order_id: str)
async def get_open_orders(self, symbol: str)
async def get_all_open_orders(self)
```

Quantity rounding uses `_round_step(symbol, qty)` instead of `round_step_size` (Aster does not use the `binance-python` library):
```python
def _round_step(self, symbol: str, qty: float) -> float
```
Rounds down using `floor(qty / step) * step` to the precision implied by step size.

**Key differences from BinanceSpotExchange:**
- Uses `aiohttp` for all REST calls (not `binance-python` AsyncClient)
- `_sign` is inline HMAC rather than delegated to the library
- `get_deposit_address` and `deposit_asset` are stubs returning `None`/`False` (not supported by Aster spot API)
- `withdraw_asset` calls `POST /api/v1/withdraw`
- No `strategy_id` forwarding to the exchange API for market orders
- Execution reports use `Venue.ASTER` in Order objects

---

## Aster Data Connector (`AsterDataConnectors.py`)

### `AsterKlineClientManager`

**WebSocket endpoints:**
- Connection: `wss://sstream.asterdex.com/ws`
- Subscription format: JSON `{"method": "SUBSCRIBE", "params": ["ethusdt@kline_1s", ...], "id": 1}`

Handles two incoming message formats:
- Combined stream envelope: `{"stream": "...", "data": {...}}`
- Direct kline event: `{"e": "kline", "s": "ETHUSDT", ...}`

No EventBus publishing in current implementation.

### `AsterOrderBookClientManager`

Same connection approach as `AsterKlineClientManager`. Subscribes to `"{symbol}@depth@100ms"`. Handles both combined-stream envelope and direct `depthUpdate` event formats.

### `AsterUserDataClientManager`

listen-key lifecycle:
1. `POST /api/v1/listenKey` — via sync `requests` call, run in thread via `asyncio.to_thread`
2. Connect to `wss://sstream.asterdex.com/ws/<listenKey>`
3. `PUT /api/v1/listenKey` every 30 minutes
4. `DELETE /api/v1/listenKey` on stop

On reconnection, fetches a fresh listenKey before reconnecting.

```python
def register(self, callback: Callable[[dict], None])
```
Registered callbacks receive raw event dicts. No EventBus wiring.

### `AsterKlineFeed`

Identical to `BinanceKlineFeed`. `_process_kline_event` only returns closed candles (`k.x == True`).

### `AsterOrderBookFeed`

Implements snapshot + delta sync for Aster spot. Uses `AsterOrderBook.from_lists()`. Sequence validation uses `event['pu']` (previous update ID) for live events but `event['U']`/`event['u']` for buffered replay — correctly matching the Aster/Binance dual-field protocol.

REST snapshot: `GET https://sapi.asterdex.com/api/v1/depth` (sync, offloaded via `asyncio.to_thread`).

---

## Aster Perp Data Connector (`AsterPerpDataConnectors.py`)

### `AsterPerpKlineClientManager`

**WebSocket endpoint:** `wss://fstream.asterdex.com/stream?streams={streams}` (query-string combined stream, not subscription-message approach used by Aster spot).

Streams: `"{symbol}@kline_1m"` (1-minute candles).

Volatility annualisation: `stdev(returns) * sqrt(12)`.

### `AsterPerpOrderBookClientManager`

Same endpoint pattern as `AsterPerpKlineClientManager`. Streams: `"{symbol}@depth@100ms"`.

Supports direct `depthUpdate` event format and combined-stream envelope.

### `AsterPerpUserDataClientManager`

Identical lifecycle to `AsterUserDataClientManager` but uses futures endpoints:
- REST: `https://fapi.asterdex.com/fapi/v1/listenKey`
- WebSocket: `wss://fstream.asterdex.com/ws/<listenKey>`

### `AsterPerpKlineFeed`

Same as `AsterKlineFeed` with default interval `"1m"`.

### `AsterPerpOrderBookFeed`

REST snapshot from `https://fapi.asterdex.com/fapi/v1/depth` (sync, no `asyncio.to_thread`). Sequence validation uses `event['pu']` for both live and buffered events with explicit `pu` matching. Constructs `AsterOrderBook.from_lists()`.

---

## Common Patterns

### EventBus Integration

All data client managers that support event publishing hold `_event_bus = None`. It is injected via `set_event_bus(event_bus)`, called during setup.

**Emission sequence for user data (spot Binance):**
```
BinanceUserDataClientManager._reader_loop():
    for cb in self._subscribers:
        cb(event)                           # 1. sync callbacks: mutate exchange state
    if self._event_bus is not None:
        if et == "executionReport":         # 2. async EventBus: strategy hooks see consistent state
            await self._event_bus.publish(OrderUpdateEvent(...))
        elif et == "balanceUpdate":
            await self._event_bus.publish(BalanceUpdateEvent(...))
```

**Emission for market data (spot Binance klines):**
```
BinanceKlineClientManager._worker_coroutine():
    feed._kline = kline
    feed._historical.append(kline.close)
    feed._vol = computed_vol
    if self._event_bus is not None:
        await self._event_bus.publish(KlineEvent(...))
```

`publish` is always awaited, providing natural backpressure — slow strategy hooks throttle the worker.

### WebSocket Reconnection

All managers implement the same reconnection pattern:

```python
reconnect_delay = 1
while self._running:
    try:
        async with websockets.connect(...) as ws:
            reconnect_delay = 1   # reset on successful connect
            ...
    except Exception as e:
        logger.error(...)
    if self._running:
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)
```

Backoff starts at 1s and doubles up to a 60s maximum.

### Order Fill Handling

Order state is maintained as `Order` objects in `_open_orders[symbol][order_id]`. The `Order` class provides:

- `update_fill(filled_qty, fill_price)` — computes running weighted-average fill price, transitions status to `PARTIALLY_FILLED` or `FILLED`
- `transition_to(new_status)` — validates the transition via `order_fsm.validate_order_transition` (warn-mode: always applies, logs invalid transitions)
- `is_terminal` — True for FILLED, CANCELLED, REJECTED
- `is_active` — True for PENDING, OPEN, PARTIALLY_FILLED, TRIGGERED

Terminal orders are removed from `_open_orders` immediately after the status transition.

### Quantity Rounding

Before any order submission:
- Binance uses `binance.helpers.round_step_size(qty, step)` from the binance-python library
- Aster uses `_round_step(symbol, qty)` which applies `floor(qty / step) * step`

Both approaches obtain `step_size` from `_token_infos[symbol]["step_size"]` populated during `initialize()`.

---

## Data Contracts — Event Types

All event classes are defined in `elysian_core/core/events.py` as `@dataclass(frozen=True)`.

### `KlineEvent`

```python
@dataclass(frozen=True)
class KlineEvent:
    symbol: str
    venue: Venue
    kline: Kline
    timestamp: int   # epoch milliseconds
    event_type: EventType = EventType.KLINE  # auto-set
```

Emitted by: `BinanceKlineClientManager` (spot)

`Kline` fields: `ticker`, `interval`, `start_time`, `end_time`, `open`, `high`, `low`, `close`, `volume`

---

### `OrderBookUpdateEvent`

```python
@dataclass(frozen=True)
class OrderBookUpdateEvent:
    symbol: str
    venue: Venue
    orderbook: OrderBook
    timestamp: int
    event_type: EventType = EventType.ORDERBOOK_UPDATE
```

Emitted by: `BinanceOrderBookClientManager` (spot)

---

### `OrderUpdateEvent`

```python
@dataclass(frozen=True)
class OrderUpdateEvent:
    symbol: str
    venue: Venue
    order: Order
    timestamp: int
    event_type: EventType = EventType.ORDER_UPDATE
```

Emitted by: `BinanceUserDataClientManager` on `executionReport`

`Order` fields: `id`, `symbol`, `side`, `order_type`, `quantity`, `price`, `status`, `avg_fill_price`, `filled_qty`, `commission`, `commission_asset`, `venue`, `strategy_id`, `last_updated_timestamp`

---

### `BalanceUpdateEvent`

```python
@dataclass(frozen=True)
class BalanceUpdateEvent:
    asset: str
    venue: Venue
    delta: float
    new_balance: float  # 0.0 for Binance (not sent in balanceUpdate)
    timestamp: int
    event_type: EventType = EventType.BALANCE_UPDATE
```

Emitted by: `BinanceUserDataClientManager` on `balanceUpdate`

---

### `RebalanceCompleteEvent`

```python
@dataclass(frozen=True)
class RebalanceCompleteEvent:
    result: RebalanceResult
    validated_weights: ValidatedWeights
    timestamp: int
    strategy_id: int = 0
    event_type: EventType = EventType.REBALANCE_COMPLETE
```

Emitted by the execution engine after a rebalance cycle completes. Not connector-emitted.

---

### `LifecycleEvent` and `RebalanceCycleEvent`

Internal system events for state machine transitions. Not emitted by connectors.

---

## Adding a New Exchange

Follow these steps to add a new spot exchange. Futures exchanges follow the same pattern using `FuturesExchangeConnector`.

**Step 1: Create data connector feeds**

```python
# In elysian_core/connectors/NewExchangeDataConnectors.py

class NewExchangeKlineFeed(AbstractDataFeed):
    def create_new(self, asset: str, interval: str = "1s"):
        self._name = asset
        self._interval = interval

    def _process_kline_event(self, raw: dict) -> Optional[Kline]:
        # parse exchange-specific WebSocket message into Kline
        ...

class NewExchangeOrderBookFeed(AbstractDataFeed):
    def create_new(self, asset: str, interval: str = "100ms"):
        self._name = asset
        self._interval = interval

    async def get_initial_snapshot(self):
        # fetch REST snapshot, initialise self._data, replay event_buffer
        ...

    async def _process_depth_event(self, event: dict) -> OrderBook:
        # validate sequence numbers, call _apply_depth_update or re-sync
        ...

    async def _apply_depth_update(self, event: dict):
        ts = int(time.time() * 1000)
        await self._data.apply_both_updates(ts, event["u"], bid_levels=..., ask_levels=...)
```

**Step 2: Create multiplex client managers**

```python
class NewExchangeKlineClientManager(KlineClientManager):
    async def start(self): ...
    async def stop(self): ...
    def register_feed(self, feed): ...
    def unregister_feed(self, symbol: str): ...
    def get_feed(self, symbol: str): ...
    async def run_multiplex_feeds(self):
        # create _reader_coroutine and _worker_coroutine tasks
        # _worker_coroutine must call set_event_bus-aware EventBus publish
        ...

class NewExchangeOrderBookClientManager(OrderBookClientManager):
    # same pattern
    ...
```

**Step 3: Create user data stream manager**

```python
class NewExchangeUserDataClientManager:
    def __init__(self, api_key: str, api_secret: str): ...
    async def start(self): ...
    async def stop(self): ...
    def register(self, callback): ...
    def set_event_bus(self, event_bus): ...
    async def _reader_loop(self):
        # implement with exponential backoff reconnection
        # dispatch sync callbacks first, then EventBus
        ...
```

**Step 4: Create the exchange connector**

```python
class NewExchangeSpotExchange(SpotExchangeConnector):
    async def initialize(self): ...
    async def _fetch_symbol_info(self, symbol: str) -> dict: ...
    async def refresh_balances(self) -> Dict[str, float]: ...
    async def place_limit_order(self, symbol, side, price, quantity, strategy_id): ...
    async def place_market_order(self, symbol, side, quantity, use_quote_order_qty=False): ...
    async def cancel_order(self, symbol, order_id): ...
    async def get_open_orders(self, symbol): ...
    async def get_deposit_address(self, coin, network=None): ...
    async def deposit_asset(self, coin, amount, network=None): ...
    async def withdraw_asset(self, coin, amount, address, network=None): ...
    async def run(self): ...
    async def cleanup(self): ...
```

**Step 5: Add `Venue` enum value**

In `elysian_core/core/enums.py`, add the new venue:
```python
class Venue(Enum):
    ...
    NEW_EXCHANGE = "NewExchange"
```

**Step 6: Register with StrategyRunner**

In `run_strategy.py`, instantiate the new managers and exchange, call `set_event_bus(event_bus)` on each manager, register feeds, and add `run_multiplex_feeds()` to the task list.

---

## Performance Notes

- **Multiplexing**: A single WebSocket connection handles all symbols via Binance combined streams or Aster subscription messages. This avoids per-symbol connection overhead and reduces rate limit surface.
- **Bounded queues**: All managers use `asyncio.Queue(maxsize=10000)` (kline managers use 100000) to prevent unbounded memory growth under load. If the queue is full, the reader blocks until a worker drains it — this is intentional backpressure.
- **Worker pool**: Up to `min(8, len(feeds))` concurrent workers process the shared queue in parallel. For strategies with many symbols this prevents head-of-line blocking on slow parse paths.
- **Awaited publish**: `await self._event_bus.publish(event)` means slow strategy callbacks naturally throttle workers. This is preferable to a fire-and-forget pattern that would allow unbounded queue growth inside the EventBus.
- **Background stats**: `update_current_stats` runs as a separate coroutine sleeping 1s between iterations, so volatility calculation never blocks the depth processing hot path.
