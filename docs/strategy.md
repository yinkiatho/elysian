# Strategy Documentation

## 1. Strategy System Overview

The Elysian strategy module provides an event-driven, hook-based framework for implementing trading strategies. The central abstraction is `SpotStrategy` — an async base class that subscribes to a shared `EventBus`, receives typed market and account events through overridable hook methods, and drives portfolio rebalancing through the `RebalanceFSM` pipeline.

The design principle is **minimal surface area**: subclasses override only the hooks they need, implement `compute_weights()` with their alpha logic, and let the framework handle event routing, risk validation, execution, and cooldown.

### Pipeline Position

Strategies live at Stage 3 of the Elysian pipeline:

```
Stage 1: Data Feeds (exchange connectors, WebSocket workers)
Stage 2: EventBus (typed event dispatch to all subscribers)
Stage 3: Strategy (event hooks, compute_weights)         <-- this layer
Stage 4: Risk / PortfolioOptimizer (ValidatedWeights)
Stage 5: Execution Engine (OrderIntent, RebalanceResult)
```

The strategy receives raw market data via event hooks, computes target weights in `compute_weights()`, and passes them into the FSM. Everything downstream — risk validation, order generation, cooldown — is handled automatically.

### Asset Type Scope

One `SpotStrategy` instance corresponds to one exchange and one asset type. The current `AssetType` enum values are `SPOT` and `PERPETUAL`. A strategy does not share a `ShadowBook` with another strategy — each strategy maintains its own virtual position ledger.

---

## 2. SpotStrategy Base Class

**File:** `elysian_core/strategy/base_strategy.py`

### Constructor

```python
SpotStrategy(
    exchanges: Dict[Venue, SpotExchangeConnector] = {},
    shared_event_bus: EventBus = None,
    private_event_bus: EventBus = None,
    feeds: Optional[Dict[str, AbstractDataFeed]] = None,
    max_heavy_workers: int = 4,
    optimizer: Optional[PortfolioOptimizer] = None,
    execution_engine: Optional[ExecutionEngine] = None,
    cfg: Optional[AppConfig] = None,
    asset_type: AssetType = AssetType.SPOT,
    venue: Venue = Venue.BINANCE,
    venues: Optional[Set[Venue]] = None,
    strategy_id: int = 0,
    strategy_name: str = 'unknown_strategy',
)
```

| Parameter | Description |
|-----------|-------------|
| `exchanges` | Map of `Venue` → `SpotExchangeConnector` for order submission. |
| `shared_event_bus` | Public bus for market data events (`KLINE`, `ORDERBOOK_UPDATE`). Injected by `StrategyRunner`. |
| `private_event_bus` | Private bus for account events (`ORDER_UPDATE`, `BALANCE_UPDATE`, `REBALANCE_COMPLETE`). Injected by `StrategyRunner`. |
| `feeds` | Optional map of symbol → `AbstractDataFeed`. Used for price lookups via `get_current_price()` and `get_current_vol()`. |
| `max_heavy_workers` | Number of `ProcessPoolExecutor` workers for `run_heavy()`. |
| `optimizer` | `PortfolioOptimizer` (Stage 4). Set by `StrategyRunner`. |
| `execution_engine` | `ExecutionEngine` (Stage 5). Set by `StrategyRunner`. |
| `cfg` | Full `AppConfig`. Guaranteed set before `on_start()` is called. |
| `asset_type` | `AssetType.SPOT` or `AssetType.PERPETUAL`. |
| `venue` | Primary venue for this strategy. |
| `venues` | Full set of venues this strategy receives events from. If `None`, defaults to `{venue}`. Stored as `frozenset`. |
| `strategy_id` | Unique integer identifier. Used for `ShadowBook` routing and logging. |
| `strategy_name` | Human-readable label used in log output. |

**Note:** `StrategyRunner.setup_strategy()` re-wires `_shared_event_bus`, `_private_event_bus`, `_exchanges`, `_optimizer`, `_execution_engine`, `cfg`, `strategy_config`, and `_shadow_book` before calling `start()`. Values passed at construction are placeholders.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `state` | `StrategyState` | Current lifecycle state (read-only). |
| `shadow_book` | `Optional[ShadowBook]` | Per-strategy virtual position ledger. Read this for your own positions, cash, weights, and PnL. Do not read the aggregate `Portfolio`. |
| `rebalance_fsm` | `Optional[RebalanceFSM]` | The FSM instance. `None` if no optimizer/engine configured. |
| `is_sub_account_mode` | `bool` | `True` if this strategy uses a dedicated sub-account. |
| `strategy_config` | `Optional[StrategyConfig]` | Per-strategy config loaded from the strategy YAML. Holds `params`, `risk_overrides`, `execution_overrides`, `portfolio_overrides`, `symbols`. |
| `portfolio` | `Optional[Portfolio]` | Aggregate portfolio reference (backward compat). Prefer `shadow_book` for strategy-level reads. |

### Helper Methods

```python
get_exchange(venue: Venue = Venue.BINANCE) -> SpotExchangeConnector
get_feed(symbol: str) -> Optional[AbstractDataFeed]
get_current_vol(symbol: str) -> Optional[float]   # annualised rolling vol in bps
await run_heavy(fn, *args)                          # offload to ProcessPoolExecutor
```

### Rebalance Control

```python
await strategy.request_rebalance(**ctx) -> bool     # trigger FSM cycle
await strategy.suspend_rebalancing() -> None        # pause FSM from any state
await strategy.resume_rebalancing() -> None         # return FSM to IDLE
```

`request_rebalance()` returns `True` if a cycle was started, `False` if the FSM was busy (mid-cycle, cooldown, suspended) or not configured. It is safe to call at any frequency — skipped requests produce no side effects. Any keyword arguments passed to `request_rebalance(**ctx)` flow through the entire FSM pipeline and are available to `compute_weights(**ctx)`.

---

## 3. Strategy Lifecycle

Lifecycle states are defined in `elysian_core/core/enums.py` as `StrategyState`:

```
CREATED → STARTING → READY → RUNNING → STOPPING → STOPPED
                                                 → FAILED
```

### State Transitions

| State | Entered When |
|-------|-------------|
| `CREATED` | Strategy is instantiated. |
| `STARTING` | `start()` is called. EventBus subscriptions are registered. |
| `READY` | `on_start()` has returned without error. FSM is initialized (if optimizer + engine are set). |
| `RUNNING` | First event is dispatched to the strategy. |
| `STOPPING` | `stop()` is called. `_stop_event` is set (unblocks `run_forever()`). |
| `STOPPED` | `on_stop()` has returned. Executor is shut down. `shadow_book.stop()` is called. |
| `FAILED` | Reserved for future use — not currently set by the framework. |

### What `start()` Does

1. Validates that the strategy is in `CREATED` state.
2. Subscribes `_dispatch_kline` to `shared_event_bus` for `KLINE`.
3. Subscribes `_dispatch_ob` to `shared_event_bus` for `ORDERBOOK_UPDATE`.
4. Subscribes `_dispatch_order`, `_dispatch_balance`, `_dispatch_rebalance` to `private_event_bus`.
5. Constructs `RebalanceFSM` if both `_optimizer` and `_execution_engine` are set.
6. Calls `on_start()`.
7. Transitions to `READY`.

### What `stop()` Does

1. Sets `_stop_event` — unblocks any `await self._stop_event.wait()` in `run_forever()`.
2. Unsubscribes all dispatch callbacks from both event buses.
3. Calls `on_stop()`.
4. Shuts down the `ProcessPoolExecutor` (non-blocking).
5. Calls `shadow_book.stop()`.
6. Transitions to `STOPPED`.

### Event Isolation

Each hook is wrapped in a `_dispatch_*` method that catches and logs exceptions without propagating them:

```python
async def _dispatch_kline(self, event: KlineEvent):
    if event.venue not in self.venues:
        return
    if self._shadow_book is not None and not self.is_sub_account_mode:
        if event.kline.close and event.kline.close > 0:
            self._shadow_book.update_mark_prices({event.symbol: event.kline.close})
    self._mark_running()
    try:
        await self.on_kline(event)
    except Exception as e:
        logger.error(f"SpotStrategy.on_kline error: {e}", exc_info=True)
```

A bug in `on_kline` will never crash the feed worker. The EventBus subscribes to `_dispatch_kline`, not `on_kline` directly.

For `ORDER_UPDATE` and `BALANCE_UPDATE`, the dispatch methods use `asyncio.gather` to run both the strategy hook and the `ShadowBook` update concurrently:

```python
async def _dispatch_order(self, event: OrderUpdateEvent):
    try:
        await asyncio.gather(
            self.on_order_update(event),
            self._shadow_book._on_order_update(event)
        )
    except Exception as e:
        logger.error(f"SpotStrategy.on_order_update error: {e}", exc_info=True)
```

---

## 4. Hook Functions (Override These)

All hooks are `async def` with default no-op implementations. Override only what you need.

| Hook | Event Bus | Trigger | Typical Frequency |
|------|-----------|---------|-------------------|
| `on_start()` | — | Once at startup | Once |
| `on_stop()` | — | Once at shutdown | Once |
| `run_forever()` | — | Long-running coroutine | Continuous |
| `on_kline(event)` | shared | Closed kline candle | ~1/min per symbol (1m interval) |
| `on_orderbook_update(event)` | shared | Depth snapshot | ~10/sec per symbol |
| `on_order_update(event)` | private | Order state change | On fill / cancel / reject |
| `on_balance_update(event)` | private | Balance delta | On any balance change |
| `on_rebalance_complete(event)` | private | FSM cycle finished | After each rebalance |
| `compute_weights(**ctx)` | — | Called by FSM | Once per rebalance cycle |

### `on_start()`

Called after EventBus subscriptions are registered and the FSM is initialized. `self.cfg` and `self.strategy_config` are guaranteed to be set. Use this for all strategy-state initialization.

```python
async def on_start(self):
    if not self._symbols and self.cfg:
        self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))
    self._price_series = {sym: deque(maxlen=1000) for sym in self._symbols}
```

### `on_stop()`

Called on shutdown. Use for cleanup, logging final metrics, or triggering a closing rebalance.

```python
async def on_stop(self):
    await self.request_rebalance(convert_all_base=True)
```

### `run_forever()`

An optional long-running coroutine started by the runner alongside feed tasks via `asyncio.gather`. The default implementation simply awaits `self._stop_event`, which is set when `stop()` is called.

Two patterns:

```python
# Timer-driven — periodic rebalance loop
async def run_forever(self):
    while not self._stop_event.is_set():
        await asyncio.sleep(60)
        await self.request_rebalance()

# Event-only — purely reactive, no timer
async def run_forever(self):
    await self._stop_event.wait()
```

### `on_kline(event: KlineEvent)`

Fired when a kline candle closes. Access market data via `event.kline`.

```python
async def on_kline(self, event: KlineEvent):
    if event.symbol not in self._symbols:
        return
    self._price_series[event.symbol].append(event.kline.close)
    if self._ready_to_rebalance():
        await self.request_rebalance(trigger="kline")
```

Venue filtering is done by `_dispatch_kline` before calling `on_kline` — only events from `self.venues` pass through.

### `on_orderbook_update(event: OrderBookUpdateEvent)`

Fired on each orderbook depth update (~10/sec per symbol). Keep this fast (under 10ms) or offload with `run_heavy()`. Access the live book via `event.orderbook`.

```python
async def on_orderbook_update(self, event: OrderBookUpdateEvent):
    # event.orderbook is a live reference — copy if you need a snapshot
    spread = event.orderbook.spread_bps
```

### `on_order_update(event: OrderUpdateEvent)`

Fired when an order changes state. Runs concurrently with `ShadowBook._on_order_update()`.

```python
async def on_order_update(self, event: OrderUpdateEvent):
    order = event.order
    if order.status.value == "FILLED":
        logger.info(f"Filled {order.order.id}: {event.symbol}")
```

### `on_balance_update(event: BalanceUpdateEvent)`

Fired on any balance change for assets at this venue. Runs concurrently with `ShadowBook._on_balance_update()`.

```python
async def on_balance_update(self, event: BalanceUpdateEvent):
    logger.info(f"{event.asset} delta={event.delta:+.8f} new={event.new_balance}")
```

### `on_rebalance_complete(event: RebalanceCompleteEvent)`

Fired after the execution engine completes a rebalance cycle. Use this to inspect what was submitted versus what the optimizer adjusted.

```python
async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
    r = event.result
    vw = event.validated_weights
    if vw.clipped:
        logger.info(f"Optimizer clipped: {vw.clipped}")
    if r.failed > 0:
        logger.warning(f"Rebalance had {r.failed} failed orders: {r.errors}")
```

---

## 5. Signal Generation and the RebalanceFSM

### FSM States

The `RebalanceFSM` drives the full rebalance cycle. States are defined in `RebalanceState`:

```
IDLE → COMPUTING → VALIDATING → EXECUTING → COOLDOWN → IDLE
         ↑                                               ↓
         └────────── suspend / resume ──────────── SUSPENDED
```

| State | What Happens |
|-------|-------------|
| `IDLE` | Waiting for a `request_rebalance()` call. |
| `COMPUTING` | Calls `strategy.compute_weights(**ctx)`. Enriches `ctx["target_weights"]`. |
| `VALIDATING` | Calls `optimizer.validate(target_weights, **ctx)`. Enriches `ctx["validated_weights"]`. |
| `EXECUTING` | Calls `execution_engine.execute(validated_weights, **ctx)`. Enriches `ctx["rebalance_result"]`. |
| `COOLDOWN` | Waits `cooldown_s` seconds before returning to IDLE. |
| `SUSPENDED` | Paused — will not process `request_rebalance()` until `resume_rebalancing()` is called. |
| `ERROR` | Transient — FSM encountered an exception; resets to IDLE. |

### Context Flow

The `**ctx` dict is passed through and enriched at each stage:

- After `COMPUTING`: `ctx["target_weights"]` — a `TargetWeights` dataclass
- After `VALIDATING`: `ctx["validated_weights"]` — a `ValidatedWeights` dataclass
- After `EXECUTING`: `ctx["rebalance_result"]` — a `RebalanceResult` dataclass

Any kwargs passed to `request_rebalance(**ctx)` are also available in `compute_weights(**ctx)`, enabling pattern-matched behaviour:

```python
# In on_stop():
await self.request_rebalance(convert_all_base=True)

# In compute_weights():
def compute_weights(self, **ctx) -> dict:
    if ctx.get('convert_all_base', False):
        return {'USDT': 1.0}
    # ... normal alpha logic
```

---

## 6. Weight Construction

### `compute_weights(**ctx) -> Optional[Dict[str, float]]`

This is the primary method strategies must implement. It is called by the FSM during the `COMPUTING` state.

**Contract:**
- Return a `dict` mapping symbol → target weight fraction. Example: `{"ETHUSDT": 0.4, "BTCUSDT": 0.5}`.
- Cash weight is implicit: `cash_weight = 1.0 - sum(weights.values())`.
- Return `{}` or `None` to skip the current cycle. The FSM will not advance.
- Can be synchronous (`def`) or asynchronous (`async def`) — the FSM handles both.
- Must be pure — no side effects, no exchange calls, no state mutations.

```python
# Sync (preferred for stateless computation)
def compute_weights(self, **ctx) -> dict:
    signals = self._compute_signals()
    return self._signals_to_weights(signals)

# Async (only if you genuinely need to await something)
async def compute_weights(self, **ctx) -> dict:
    price = await self.get_current_price("ETHUSDT")
    ...
    return weights
```

### Normalization

Weights must sum to at most 1.0 for long-only spot strategies. A standard normalization pattern:

```python
def _signals_to_weights(self, signals: Dict[str, float]) -> Dict[str, float]:
    positive = {s: v for s, v in signals.items() if v > 0}
    if not positive:
        return {}
    total = sum(positive.values())
    if total == 0:
        return {}
    max_allocation = 0.90  # 10% cash buffer
    return {s: (v / total) * max_allocation for s, v in positive.items()}
```

### Signal Types from `signals.py`

**File:** `elysian_core/core/signals.py`

#### `TargetWeights`

The output of Stage 3 — what your strategy produces. Constructed by the FSM from the return value of `compute_weights()`.

```python
@dataclass(frozen=True)
class TargetWeights:
    weights: Dict[str, float]      # symbol → target weight
    timestamp: int                  # epoch ms
    strategy_id: int = 0
    venue: Optional[Venue] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    price_overrides: Optional[Dict[str, float]] = None  # per-symbol limit price override
```

#### `ValidatedWeights`

Stage 4 output — the risk-adjusted weights your strategy will actually trade. Returned by `PortfolioOptimizer.validate()`.

```python
@dataclass(frozen=True)
class ValidatedWeights:
    original: TargetWeights        # what the strategy requested
    weights: Dict[str, float]      # adjusted weights after risk constraints
    clipped: Dict[str, float]      # per-symbol: original_w - adjusted_w
    rejected: bool = False
    rejection_reason: str = ""
    timestamp: int = 0
```

#### `RebalanceResult`

Stage 5 output — execution summary. Carried in `RebalanceCompleteEvent.result`.

```python
@dataclass(frozen=True)
class RebalanceResult:
    intents: Tuple[OrderIntent, ...]
    submitted: int
    failed: int
    timestamp: int
    errors: Tuple[str, ...] = ()
    submitted_orders: Dict[str, OrderIntent] = field(default_factory=dict)
```

---

## 7. Event Types

All events are `@dataclass(frozen=True)` — immutable after creation.

**File:** `elysian_core/core/events.py`

### `KlineEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair (e.g. `"ETHUSDT"`) |
| `venue` | `Venue` | Exchange enum value |
| `kline` | `Kline` | OHLCV candle |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.KLINE` |

The `Kline` dataclass (from `elysian_core/core/market_data.py`) fields:

| Field | Type |
|-------|------|
| `ticker` | `str` |
| `interval` | `str` |
| `start_time` | `datetime` |
| `end_time` | `datetime` |
| `open` | `Optional[float]` |
| `high` | `Optional[float]` |
| `low` | `Optional[float]` |
| `close` | `Optional[float]` |
| `volume` | `Optional[float]` |

### `OrderBookUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange enum |
| `orderbook` | `OrderBook` | Live bid/ask snapshot |
| `timestamp` | `int` | Epoch milliseconds |

The `OrderBook` dataclass (from `elysian_core/core/market_data.py`) key properties:

| Property | Description |
|----------|-------------|
| `best_bid_price` | Top-of-book bid price |
| `best_ask_price` | Top-of-book ask price |
| `mid_price` | `(best_bid + best_ask) / 2` |
| `spread` | `best_ask - best_bid` |
| `spread_bps` | Spread in basis points |
| `best_bid_amount` | Quantity at best bid |
| `best_ask_amount` | Quantity at best ask |

**Important:** `event.orderbook` is a live reference to the object held by the feed. If you need a stable snapshot that won't be mutated by the next update, copy it immediately:

```python
async def on_orderbook_update(self, event: OrderBookUpdateEvent):
    import copy
    snapshot = copy.deepcopy(event.orderbook)
    # use snapshot safely
```

### `OrderUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange enum |
| `order` | `Order` | Order with status, fills, fees |
| `timestamp` | `int` | Epoch milliseconds |

### `BalanceUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `asset` | `str` | Asset symbol (e.g. `"USDT"`) |
| `venue` | `Venue` | Exchange enum |
| `delta` | `float` | Balance change amount |
| `new_balance` | `float` | Balance after change |
| `timestamp` | `int` | Epoch milliseconds |

### `RebalanceCompleteEvent`

| Field | Type | Description |
|-------|------|-------------|
| `result` | `RebalanceResult` | Execution summary (submitted, failed, errors) |
| `validated_weights` | `ValidatedWeights` | What the optimizer approved (vs what you requested) |
| `timestamp` | `int` | Epoch milliseconds |
| `strategy_id` | `int` | Owning strategy |

### `RebalanceCycleEvent`

Published on every FSM state transition. Useful for observability tooling.

| Field | Type | Description |
|-------|------|-------------|
| `old_state` | `RebalanceState` | Previous FSM state |
| `new_state` | `RebalanceState` | New FSM state |
| `trigger` | `str` | Trigger name that caused the transition |
| `timestamp` | `int` | Epoch milliseconds |
| `metadata` | `dict` | Optional additional context |

---

## 8. Risk Config Integration

**File:** `elysian_core/risk/risk_config.py`

The `RiskConfig` dataclass specifies all constraint parameters consumed by the `PortfolioOptimizer`. It is mutable — limits can be tightened at runtime (e.g. during drawdown breakers).

### RiskConfig Fields

| Field | Default | Description |
|-------|---------|-------------|
| `max_weight_per_asset` | `0.25` | Maximum weight for any single asset. |
| `min_weight_per_asset` | `0.0` | Minimum weight per asset (0 = long-only allowed). |
| `max_total_exposure` | `1.0` | Maximum sum of absolute weights. 1.0 = unlevered. |
| `min_cash_weight` | `0.05` | Minimum fraction to hold in cash/stablecoins. |
| `max_turnover_per_rebalance` | `0.5` | Maximum `sum(|delta_w|)` per rebalance cycle. |
| `max_leverage` | `1.0` | Maximum gross leverage (1.0 = spot only). |
| `max_short_weight` | `0.0` | Maximum short weight per asset (0 = no shorts). |
| `min_order_notional` | `10.0` | Skip orders below this USD notional. |
| `max_order_notional` | `100_000.0` | Cap single order at this USD notional. |
| `min_weight_delta` | `0.005` | Skip rebalance legs with weight change < 0.5%. |
| `min_rebalance_interval_ms` | `60_000` | Minimum milliseconds between rebalance cycles. |

### Optimizer Constraint Pipeline

When `compute_weights()` returns a weight dict, the `PortfolioOptimizer.validate()` applies constraints in this order:

1. **Symbol filter** — remove disallowed symbols (whitelist/blacklist, currently passthrough).
2. **Per-asset clip** — clip each weight to `[min_weight_per_asset, max_weight_per_asset]`.
3. **Total exposure scaling** — if `sum(|w|) > max_total_exposure`, scale all weights down proportionally.
4. **Turnover cap** — limit `sum(|new_w - old_w|)` to `max_turnover_per_rebalance`. If exceeded, each weight is moved only a fraction of the way from the previous to the target.

The cash floor (`min_cash_weight`) is defined but currently commented out in the execution path. Turnover cap is active.

### Config Priority

Risk constraints are merged with this priority (highest wins):

```
strategy risk_overrides  >  venue_configs["{asset_type}_{venue}"]  >  global risk
```

Use `cfg.effective_risk_for(strategy_id=N)` to retrieve the resolved `RiskConfig` for a given strategy — this returns the strategy's `risk_config` directly when a `strategy_id` is provided.

### Runtime Override Example

```python
async def on_drawdown_breach(self):
    # Tighten risk limits at runtime
    self._optimizer.config.max_weight_per_asset = 0.10
    self._optimizer.config.max_total_exposure = 0.50
    await self.suspend_rebalancing()
```

---

## 9. Example: EventDrivenStrategy

**File:** `elysian_core/strategy/example_weight_strategy_v2_event_driven.py`
**Config:** `elysian_core/config/strategies/strategy_000_event_driven.yaml`

### Alpha Thesis

Momentum long-only strategy. Signal = deviation of the latest per-minute return from its rolling 30-bar mean return. A positive deviation (current return above average) is interpreted as positive momentum. Assets with positive signals receive proportional weight.

### Data Structures

Initialized in `on_start()`:

```python
self._price_series       = {sym: deque(maxlen=60*240) for sym in symbols}  # raw closes
self._returns_series     = {sym: deque(maxlen=60*240) for sym in symbols}  # per-bar returns
self._rolling_returns_series = {sym: deque(maxlen=60*240) for sym in symbols}  # 30-bar rolling mean
self._symbol_availability_status = {sym: False for sym in symbols}         # ready flag
self._last_marked_time   = None                                             # last rebalance monotonic ts
```

All deques are bounded at 240 hours of minute data — memory safe regardless of runtime.

### Kline Handler

`on_kline()` does three things on each candle close for each tracked symbol:

1. Appends `event.kline.close` to `_price_series[symbol]`.
2. Computes the per-bar return `(close - prev_close) / prev_close` and appends to `_returns_series[symbol]`.
3. Once 30 returns are available, computes a rolling mean of the last 30 returns and appends to `_rolling_returns_series[symbol]`. Sets `_symbol_availability_status[symbol] = True`.

Rebalance trigger fires when all symbols are available AND the elapsed time since the last trigger exceeds `_rebalance_interval` (60 seconds):

```python
if all(self._symbol_availability_status.values()) and \
   (self._last_marked_time is None or
    time.monotonic() - self._last_marked_time > self._rebalance_interval):
    self._last_marked_time = time.monotonic()
    await self.request_rebalance()
```

### `run_forever()`

This strategy is purely event-driven. `run_forever()` sets the FSM cooldown and then waits on `_stop_event`:

```python
async def run_forever(self):
    self._rebalance_fsm._cooldown_s = self._rebalance_interval
    await self._stop_event.wait()
```

### `compute_weights()`

For each symbol, computes `signal = current_return - rolling_mean_return`. Only positive signals are kept (long-only). Weights are normalized to sum to 1.0 (no explicit cap in this implementation — the optimizer's `max_total_exposure = 1.0` provides the ceiling):

```python
def compute_weights(self, **ctx) -> dict:
    if ctx.get('convert_all_base', False):
        return {'USDT': 1.0}

    signals = {}
    for sym in self._symbols:
        returns = self._returns_series.get(sym)
        rolling = self._rolling_returns_series.get(sym)
        if not returns or not rolling or len(returns) == 0 or len(rolling) == 0:
            continue
        signal = returns[-1] - rolling[-1]
        if signal > 0:
            signals[sym] = signal

    if not signals:
        return {}

    total_signal = sum(signals.values())
    if total_signal == 0:
        return {}

    return {sym: sig / total_signal for sym, sig in signals.items()}
```

### Shutdown Behaviour

`on_stop()` requests a closing rebalance with the `convert_all_base=True` flag, which `compute_weights()` detects to return `{'USDT': 1.0}` — converting all positions to the base stablecoin.

---

## 10. Example: PrintEventsStrategy

**File:** `elysian_core/strategy/example_strategy_test_print.py`
**Config:** `elysian_core/config/strategies/strategy_001_test_print.yaml`

### Purpose

Diagnostic strategy that logs every event received. No trading logic. Useful for:

- Verifying EventBus wiring end-to-end.
- Confirming that feed workers are emitting events.
- Checking order and balance update routing in a real or paper environment.

### What It Does

| Hook | Behaviour |
|------|-----------|
| `on_start()` | Loads symbols from `cfg` if not explicitly set. Logs startup. |
| `on_stop()` | Requests a `convert_all_base=True` rebalance (closes positions). |
| `run_forever()` | Blocks on `_stop_event`. Purely event-driven — no timer. |
| `on_kline()` | Filters by `_symbols`. Kline logging is currently commented out (uncomment to enable). |
| `on_orderbook_update()` | Filters by `_symbols`. Reads `best_bid_price` / `best_ask_price`. OB logging is commented out. |
| `on_order_update()` | Logs order ID, status, filled qty, remaining qty. Always active. |
| `on_balance_update()` | Logs asset, delta, new balance. Always active. |
| `on_rebalance_complete()` | Logs result and validated weights. Always active. |

The strategy does not override `compute_weights()`, so it inherits the base-class no-op that returns `{}` — no rebalances are initiated unless `convert_all_base=True` is passed.

---

## 11. Writing a New Strategy

### Step 1: Create the strategy file

Place your strategy in `elysian_core/strategy/`. Keep the file under 500 lines.

### Step 2: Subclass SpotStrategy

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent, RebalanceCompleteEvent
from collections import deque
from typing import Dict
import time
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("MyStrategy")


class MyStrategy(SpotStrategy):
    """
    Alpha thesis: describe what signal you are capturing,
    what market conditions it works in, and expected risk profile.
    """

    def __init__(self, *args, symbols: set = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._symbols: set = set(symbols) if symbols else set()
        self._rebalance_interval: int = 60       # seconds between rebalances
        self._lookback: int = 30                 # bars for rolling computation

    async def on_start(self):
        # Load symbols from config if not passed at construction
        if not self._symbols and self.cfg:
            self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))

        # Initialize bounded rolling state
        self._price_series: Dict[str, deque] = {
            sym: deque(maxlen=self._lookback * 10) for sym in self._symbols
        }
        self._ready: Dict[str, bool] = {sym: False for sym in self._symbols}
        self._last_rebalance_ts: float = 0.0

        logger.info(f"[MyStrategy] Started — symbols={self._symbols}")

    async def on_stop(self):
        # Convert all positions to base stablecoin on shutdown
        await self.request_rebalance(convert_all_base=True)

    async def run_forever(self):
        # This strategy is event-driven — no timer needed.
        # Set FSM cooldown and block until stop.
        if self._rebalance_fsm is not None:
            self._rebalance_fsm._cooldown_s = self._rebalance_interval
        await self._stop_event.wait()

    async def on_kline(self, event: KlineEvent):
        if event.symbol not in self._symbols:
            return

        # Update rolling state
        self._price_series[event.symbol].append(event.kline.close)
        if len(self._price_series[event.symbol]) >= self._lookback:
            self._ready[event.symbol] = True

        # Trigger rebalance when all symbols are ready and interval has elapsed
        now = time.monotonic()
        if all(self._ready.values()) and (now - self._last_rebalance_ts) > self._rebalance_interval:
            self._last_rebalance_ts = now
            await self.request_rebalance(trigger="kline")

    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        r = event.result
        vw = event.validated_weights
        if vw.clipped:
            logger.info(f"[MyStrategy] Optimizer clipped: {vw.clipped}")
        if r.failed > 0:
            logger.warning(f"[MyStrategy] {r.failed} failed orders: {r.errors}")

    def compute_weights(self, **ctx) -> Dict[str, float]:
        """Pure weight computation. No side effects."""
        if ctx.get('convert_all_base', False):
            return {'USDT': 1.0}

        signals = self._compute_signals()
        return self._signals_to_weights(signals)

    def _compute_signals(self) -> Dict[str, float]:
        """Override with your alpha logic. Must be pure."""
        signals = {}
        for sym in self._symbols:
            series = list(self._price_series[sym])
            if len(series) < self._lookback:
                continue
            # Example: simple return signal
            signal = (series[-1] - series[0]) / series[0] if series[0] != 0 else 0.0
            if signal > 0:
                signals[sym] = signal
        return signals

    def _signals_to_weights(self, signals: Dict[str, float]) -> Dict[str, float]:
        """Normalize positive signals into portfolio weights."""
        if not signals:
            return {}
        total = sum(signals.values())
        if total == 0:
            return {}
        max_allocation = 0.90   # keep 10% in cash
        return {sym: (v / total) * max_allocation for sym, v in signals.items()}
```

### Step 3: Create a strategy YAML config

See Section 12 below for the full YAML schema.

### Step 4: Register in run_strategy.py

Add the strategy YAML path to the `strategy_config_yamls` list passed to `load_app_config()`.

### Step 5: Verify the checklist

- [ ] `compute_weights()` is pure — no side effects, no exchange calls.
- [ ] Weights sum to <= 1.0 (implicit cash for the remainder).
- [ ] Signal generation is separated from weight construction.
- [ ] Strategy subclasses `SpotStrategy` and overrides only needed hooks.
- [ ] `on_rebalance_complete` tracks what actually executed vs what was requested.
- [ ] Rolling state uses bounded collections (`deque(maxlen=N)`).
- [ ] Strategy handles missing data gracefully (returns `{}` to skip cycle).
- [ ] Rebalance trigger has appropriate throttling (`_last_rebalance_ts` check).
- [ ] Config parameters are not hardcoded — read from `strategy_config.params` or constructor args.
- [ ] `on_start()` handles the case where `_symbols` is empty.

---

## 12. Config YAML

Each strategy is configured in an individual YAML file in `elysian_core/config/strategies/`. These are loaded alongside `trading_config.yaml` by `load_app_config()` via the `strategy_config_yamls` parameter.

### Complete Schema

```yaml
# ── Identity ──────────────────────────────────────────────────────────────────
strategy_id: 0                              # unique integer; used for shadow book routing
strategy_name: "my_strategy_binance_spot"  # human-readable label
log_dir: "logs/{strategy_name}.log"        # log file path; {strategy_name} is substituted

# ── Class ─────────────────────────────────────────────────────────────────────
class: "elysian_core.strategy.my_strategy.MyStrategy"  # fully qualified Python path

# ── Venue + asset type ────────────────────────────────────────────────────────
asset_type: "Spot"         # "Spot" or "Perpetual"
venue: "Binance"           # primary venue: "Binance", "Aster"
venues:                    # all venues this strategy receives events from
  - "Binance"

# ── Resources ─────────────────────────────────────────────────────────────────
max_heavy_workers: 4       # ProcessPoolExecutor workers for run_heavy()

# ── Symbols ───────────────────────────────────────────────────────────────────
symbols: []                # explicit list; empty = loaded from config.json for this venue+asset_type
# symbols: ['BTCUSDT', 'ETHUSDT']  # explicit override

# ── Risk Management (PortfolioOptimizer / ShadowBook level) ───────────────────
# Only list fields you want to change; omitted fields inherit from venue/global.
risk:
  max_weight_per_asset: 0.25        # no single asset > 25%
  min_weight_per_asset: 0.0         # 0 = long-only ok
  max_total_exposure: 1.0           # 1.0 = no leverage
  min_cash_weight: 0.05             # keep >= 5% cash
  max_turnover_per_rebalance: 0.5   # max sum(|delta_w|) per cycle
  max_leverage: 1.0
  max_short_weight: 0.0             # 0 = no shorts
  min_order_notional: 10.0          # skip orders below $10
  max_order_notional: 100000.0      # cap single order at $100k
  min_weight_delta: 0.005           # skip legs < 0.5% change
  # min_rebalance_interval_ms: 60000

# ── Overrides ─────────────────────────────────────────────────────────────────
execution_overrides: {}    # override trading_config.yaml execution section
portfolio_overrides: {}    # override trading_config.yaml portfolio section

# ── Strategy-specific parameters ──────────────────────────────────────────────
# Accessible via self.strategy_config.params["key"] or self.cfg.strategies[N].params
params:
  rebalance_interval_s: 60          # example custom parameter
```

### Loading Config in a Strategy

```python
async def on_start(self):
    # Read strategy-specific params
    if self.strategy_config:
        self._rebalance_interval = self.strategy_config.params.get("rebalance_interval_s", 60)

    # Read symbols
    if not self._symbols and self.cfg:
        self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))
```

### Config Merge Priority

Risk constraints are resolved as follows, with higher-priority sources overwriting lower:

```
Global risk (trading_config.yaml)
  ↓ overridden by
venue_configs["{asset_type}_{venue}"] risk section
  ↓ overridden by
strategy risk: section (strategy_NNN.yaml)
```

The resolved `RiskConfig` for a strategy is accessible via:

```python
cfg.effective_risk_for(strategy_id=0)   # returns the strategy's risk_config directly
```

### Sub-account Mode

If a strategy should use a dedicated exchange sub-account, set credentials via environment variables. The runner reads them using the pattern `{VENUE}_API_KEY_{strategy_id}` and `{VENUE}_API_SECRET_{strategy_id}`:

```bash
# .env
BINANCE_API_KEY_0=your_key
BINANCE_API_SECRET_0=your_secret
```

The `StrategyConfig.sub_account_api_key` field is populated automatically from these env vars.

---

## 13. Strategy Testing

### Unit Testing compute_weights()

Because `compute_weights()` must be pure, it can be tested without any event infrastructure:

```python
import pytest
from elysian_core.strategy.my_strategy import MyStrategy


def make_strategy() -> MyStrategy:
    """Build a minimal strategy instance for testing."""
    s = MyStrategy(strategy_id=0, strategy_name="test")
    s._symbols = {"ETHUSDT", "BTCUSDT"}
    return s


def test_compute_weights_all_positive_signals():
    s = make_strategy()
    # Populate enough data for signals
    for i in range(35):
        s._price_series["ETHUSDT"].append(1000.0 + i)   # rising
        s._price_series["BTCUSDT"].append(50000.0 + i)  # rising
        s._ready["ETHUSDT"] = True
        s._ready["BTCUSDT"] = True

    weights = s.compute_weights()

    assert sum(weights.values()) <= 1.0
    assert all(w >= 0 for w in weights.values())


def test_compute_weights_returns_empty_on_insufficient_data():
    s = make_strategy()
    weights = s.compute_weights()
    assert weights == {}


def test_compute_weights_convert_all_base():
    s = make_strategy()
    weights = s.compute_weights(convert_all_base=True)
    assert weights == {'USDT': 1.0}
```

### Unit Testing Signal Generation

Test `_compute_signals()` and `_signals_to_weights()` independently:

```python
def test_signals_to_weights_normalizes_to_max_allocation():
    s = make_strategy()
    signals = {"ETHUSDT": 0.03, "BTCUSDT": 0.01}
    weights = s._signals_to_weights(signals)
    assert abs(sum(weights.values()) - 0.90) < 1e-9

def test_signals_to_weights_filters_negative():
    s = make_strategy()
    signals = {"ETHUSDT": -0.01, "BTCUSDT": 0.02}
    weights = s._signals_to_weights(signals)
    assert "ETHUSDT" not in weights
```

### Integration Testing with EventBus

For hook wiring tests, construct the EventBus and feed events directly:

```python
import asyncio
from elysian_core.core.event_bus import EventBus
from elysian_core.core.events import KlineEvent
from elysian_core.core.market_data import Kline
from elysian_core.core.enums import Venue
import datetime


async def test_on_kline_updates_price_series():
    shared_bus = EventBus()
    private_bus = EventBus()
    strategy = MyStrategy(
        shared_event_bus=shared_bus,
        private_event_bus=private_bus,
        venue=Venue.BINANCE,
        strategy_id=0,
    )
    strategy._symbols = {"ETHUSDT"}
    strategy._shadow_book = MockShadowBook()  # minimal mock

    await strategy.start()

    kline = Kline(
        ticker="ETHUSDT", interval="1m",
        start_time=datetime.datetime.now(),
        end_time=datetime.datetime.now(),
        close=2000.0
    )
    event = KlineEvent(symbol="ETHUSDT", venue=Venue.BINANCE,
                       kline=kline, timestamp=0)
    await shared_bus.publish(event)

    assert list(strategy._price_series["ETHUSDT"])[-1] == 2000.0
```

### Performance Considerations

- `on_orderbook_update`: fires ~10 times/sec per symbol. Keep execution under 10ms. Offload heavier work with `run_heavy()`.
- `on_kline`: fires ~1 time/min per symbol (1m interval). Can tolerate heavier logic.
- `compute_weights()`: runs once per rebalance cycle. Can be more expensive but should remain deterministic and side-effect free.
- `run_heavy(fn, *args)`: adds ~1–5ms process boundary overhead. Reserve for genuinely CPU-bound work (numpy, ML inference). `fn` must be a top-level picklable function — not a lambda, method, or closure.

---

## 14. End-to-End Tutorial: Creating and Running a Strategy

This section walks through every step required to go from zero to a live-running strategy, and documents the critical gotchas that are easy to miss.

---

### Step 1: Configure your environment

Create a `.env` file in the project root. Each strategy uses a dedicated sub-account identified by `strategy_id`:

```bash
# .env
# Sub-account for strategy_id=0 on Binance
BINANCE_API_KEY_0=your_api_key_here
BINANCE_API_SECRET_0=your_api_secret_here

# Add more strategies with incrementing IDs
BINANCE_API_KEY_1=your_second_api_key
BINANCE_API_SECRET_1=your_second_api_secret
```

The env var pattern is always `{VENUE}_API_KEY_{strategy_id}` / `{VENUE}_API_SECRET_{strategy_id}`. The venue prefix must match the `venue` field in your strategy YAML (e.g., `BINANCE`, `ASTER`). `StrategyConfig.sub_account_api_key` is populated automatically from these env vars in `load_strategy_yaml()` ([app_config.py:367](elysian_core/config/app_config.py#L367)).

---

### Step 2: Create the strategy YAML config

Create a new file in `elysian_core/config/strategies/`, e.g. `strategy_002_my_strategy.yaml`:

```yaml
strategy_id: 2                            # Must be unique across all strategies
strategy_name: "my_strategy_binance_spot"
class: "elysian_core.strategy.my_strategy.MyStrategy"  # Fully qualified path
asset_type: "Spot"
venue: "Binance"
venues:
  - "Binance"

max_heavy_workers: 4

# Leave empty to load all symbols from config.json for this venue+asset_type
# Or provide an explicit list to restrict the universe
symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

risk:
  max_weight_per_asset: 0.25
  min_weight_per_asset: 0.0
  max_total_exposure: 1.0
  min_cash_weight: 0.05
  max_turnover_per_rebalance: 0.5
  max_leverage: 1.0
  max_short_weight: 0.0
  min_order_notional: 10.0
  max_order_notional: 100000.0
  min_weight_delta: 0.005

execution_overrides: {}
portfolio_overrides: {}

params:
  rebalance_interval_s: 60
```

---

### Step 3: Create the strategy class

Create `elysian_core/strategy/my_strategy.py`. See Section 11 for the full boilerplate template.

**The minimum viable strategy:**

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent
import elysian_core.utils.logger as log
import time

logger = log.setup_custom_logger("MyStrategy")

class MyStrategy(SpotStrategy):

    def __init__(self, *args, symbols: set = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._symbols: set = set(symbols) if symbols else set()
        self._rebalance_interval = 60
        self._last_rebalance_ts: float = 0.0

    async def on_start(self):
        if not self._symbols and self.cfg:
            self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))
        logger.info(f"[MyStrategy] Started — symbols={self._symbols}")

    async def run_forever(self):
        if self._rebalance_fsm is not None:
            self._rebalance_fsm._cooldown_s = self._rebalance_interval
        await self._stop_event.wait()

    async def on_kline(self, event: KlineEvent):
        if event.symbol not in self._symbols:
            return
        now = time.monotonic()
        if now - self._last_rebalance_ts > self._rebalance_interval:
            self._last_rebalance_ts = now
            await self.request_rebalance()

    def compute_weights(self, **ctx) -> dict:
        if ctx.get('convert_all_base', False):
            return {}                     # return {} to go all-cash, not {'USDT': 1.0}
        return {"BTCUSDT": 0.5, "ETHUSDT": 0.4}  # 10% implicit cash
```

---

### Step 4: Register the strategy in run_strategy.py

Open [elysian_core/run_strategy.py](elysian_core/run_strategy.py) and add your YAML path to the `strategy_config_yamls` list in the `__main__` block ([run_strategy.py:1044](elysian_core/run_strategy.py#L1044)):

```python
if __name__ == "__main__":
    trading_config_yaml = 'elysian_core/config/trading_config.yaml'
    strategy_config_yamls = [
        'elysian_core/config/strategies/strategy_000_event_driven.yaml',
        'elysian_core/config/strategies/strategy_001_test_print.yaml',
        'elysian_core/config/strategies/strategy_002_my_strategy.yaml',  # <-- add this
    ]
    asyncio.run(run_test_strategy(
        trading_config_yaml=trading_config_yaml,
        strategy_config_yamls=strategy_config_yamls,
    ))
```

---

### Step 5: Run the strategy

From the project root:

```bash
python -m elysian_core.run_strategy
# or
python elysian_core/run_strategy.py
```

Logs are written to `logs/<run_timestamp>/` with one file per strategy name.

---

### Key Entry Points

| Location | What It Does |
|----------|-------------|
| [run_strategy.py:1037](elysian_core/run_strategy.py#L1037) `__main__` block | Top-level entry point. Configures YAML paths and calls `asyncio.run(run_test_strategy(...))`. |
| [run_strategy.py:883](elysian_core/run_strategy.py#L883) `StrategyRunner.run()` | Main async orchestrator. Runs all setup stages sequentially and then launches all strategies concurrently via `asyncio.gather`. |
| [run_strategy.py:727](elysian_core/run_strategy.py#L727) `StrategyRunner.setup_strategy()` | Wires a single strategy with its ShadowBook, PortfolioOptimizer, ExecutionEngine, and private EventBus. Called once per strategy during startup. |
| [base_strategy.py:367](elysian_core/strategy/base_strategy.py#L367) `SpotStrategy.compute_weights()` | **Your alpha logic lives here.** Called by the RebalanceFSM on every rebalance cycle. |
| [base_strategy.py:150](elysian_core/strategy/base_strategy.py#L150) `SpotStrategy.start()` | Registers all EventBus subscriptions and constructs the RebalanceFSM. Called by the runner — do not call manually. |
| [run_strategy.py:704](elysian_core/run_strategy.py#L704) `StrategyRunner._setup_pipeline()` | Initializes Portfolio, PortfolioOptimizer, and ExecutionEngine for each (asset_type, venue) pair. Called before `setup_strategy()`. |

### Runner State Machine

The runner transitions through these states:

```
CREATED → CONFIGURING → CONNECTING → SYNCING → RUNNING → STOPPING → STOPPED
                                                                    → FAILED
```

| State | What Happens |
|-------|-------------|
| `CONFIGURING` | Loads symbols, tokens, and config from YAML/JSON |
| `CONNECTING` | Creates exchange connectors; calls `exchange.run()` on each |
| `SYNCING` | Builds Portfolio, Optimizer, and ExecutionEngine; wires all strategies |
| `RUNNING` | Launches feed WebSockets and all strategy `run_forever()` coroutines via `asyncio.gather` |
| `STOPPING` | Stops snapshot tasks; calls `strategy.stop()` on all strategies |

---

### Critical Gotchas

#### USDT / Stablecoins Are NOT a Tradeable Weight

The weight dict returned by `compute_weights()` must only contain base asset pairs (e.g. `BTCUSDT`, `ETHUSDT`). **Do not include `USDT`, `USDC`, or `BUSD` as weight keys.**

Cash is always implicit:

```
cash_weight = 1.0 - sum(weights.values())
```

If you want to go all-cash, return `{}` or `None` — not `{'USDT': 1.0}`. Returning a stablecoin as a weight key will cause the ExecutionEngine to attempt to place an order for `USDTUSDT`, which does not exist.

```python
# WRONG — do not do this
def compute_weights(self, **ctx) -> dict:
    return {"USDT": 1.0}   # ← this will error at execution time

# CORRECT — return empty to go all-cash
def compute_weights(self, **ctx) -> dict:
    return {}              # ← framework treats this as 100% cash
```

The `_STABLECOINS` constant in [base_strategy.py:54](elysian_core/strategy/base_strategy.py#L54) and [shadow_book.py:36](elysian_core/core/shadow_book.py#L36) (`frozenset({"USDT", "USDC", "BUSD"})`) is used internally for cash accounting, not for weight allocation.

#### `strategy_id` Must Be Unique and Match Env Vars

The `strategy_id` in the YAML must be unique across all loaded strategies. The ShadowBook, logging, and sub-account API key lookup all key off this integer:

```
BINANCE_API_KEY_{strategy_id}    # env var loaded by load_strategy_yaml()
BINANCE_API_SECRET_{strategy_id}
```

Reusing a `strategy_id` will cause one strategy to overwrite another's ShadowBook and use the wrong API keys.

#### Initialization Goes in `on_start()`, Not `__init__()`

`self.cfg`, `self.strategy_config`, `self._shadow_book`, `self._optimizer`, and `self._execution_engine` are all `None` inside `__init__()`. They are injected by `StrategyRunner.setup_strategy()` before `start()` is called. `on_start()` is the first hook where these are guaranteed to be populated:

```python
# WRONG
def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._symbols = self.cfg.symbols.symbols_for("binance", "spot")  # ← cfg is None here

# CORRECT
async def on_start(self):
    self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))  # ← cfg is set here
```

#### Weight Sum Must Be ≤ 1.0

For `AssetType.SPOT`, all weights must be non-negative (no shorts) and must sum to ≤ 1.0. The `PortfolioOptimizer` enforces `max_total_exposure = 1.0` by default — it will scale weights down if the sum exceeds the limit. However, passing weights that already sum to > 1.0 is wasteful because the optimizer will silently clip them.

The conventional pattern is to reserve an explicit cash buffer:

```python
TARGET_ALLOC = 0.90    # 10% cash buffer
final_weights = {sym: raw_w * TARGET_ALLOC for sym, raw_w in normalized.items()}
```

#### One Strategy = One Exchange + One Asset Type

`SpotStrategy` is scoped to a single (venue, asset_type) pair. The `venues` field in the YAML is a frozenset of venues the strategy *receives events from*, but all order execution happens via the single `private_exchange` wired at setup. A strategy cannot trade on Binance Spot and Aster Spot simultaneously from one instance.

#### Kline Feed Interval Is Fixed at Startup

The `BinanceKlineFeed` interval is hardcoded to `"1s"` in `StrategyRunner._setup_binance_data_feeds()` ([run_strategy.py:341](elysian_core/run_strategy.py#L341)). If your strategy logic requires 1-minute candles, aggregate 60 `on_kline` events yourself rather than expecting the feed to emit 1m candles.

#### `request_rebalance()` Is Idempotent

Calling `request_rebalance()` while the FSM is mid-cycle, in cooldown, or suspended is silently ignored — it returns `False` and has no side effects. There is no queue. You cannot "stack up" rebalance requests. A new cycle only starts when the FSM returns to `IDLE`.

#### Sub-account Mode Is Always Active

The current architecture always runs in sub-account mode: `setup_strategy()` always calls `_create_sub_account_exchange()`, which creates a per-strategy exchange connector with its own API keys and private EventBus. There is no "shared account" fallback. Every strategy must have `BINANCE_API_KEY_{strategy_id}` and `BINANCE_API_SECRET_{strategy_id}` set in `.env`.

---

### Verifying Your Setup

Before going live, run the `PrintEventsStrategy` (Section 10) as `strategy_id=1` to confirm:

1. WebSocket feeds are connecting and emitting klines.
2. Order updates and balance updates are routing to the correct private EventBus.
3. The RebalanceFSM is cycling without errors.

Look for these log lines as indicators:

```
[Runner] Starting strategy runner...
[Runner] EventBus created
[Runner] All components and strategies initialized. Starting event loops...
[Strategy] MyStrategy started — subscribed to 5 event types
[Strategy] RebalanceFSM initialized for MyStrategy
```

And after the first rebalance trigger:

```
[RebalanceFSM] IDLE → COMPUTING
[RebalanceFSM] COMPUTING → VALIDATING
[RebalanceFSM] VALIDATING → EXECUTING
[RebalanceFSM] EXECUTING → COOLDOWN
[RebalanceFSM] COOLDOWN → IDLE
```
