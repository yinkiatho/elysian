# Strategy Documentation

## 1. Strategy System Overview

The Elysian strategy module provides an event-driven, hook-based framework for implementing trading strategies. The central abstraction is `SpotStrategy` — an async base class that subscribes to a shared `EventBus` (backed by Redis in production), receives typed market and account events through overridable hook methods, and drives portfolio rebalancing through the `RebalanceFSM` pipeline.

The design principle is **minimal surface area**: subclasses override only the hooks they need, implement `compute_weights()` with their alpha logic, and let the framework handle event routing, risk validation, execution, and cooldown.

### Pipeline Position

Strategies live at Stage 3 of the Elysian pipeline:

```
Stage 1: MarketDataService (WebSocket feeds → Redis pub/sub)
Stage 2: RedisEventBusSubscriber (market data dispatch)
Stage 3: Strategy (event hooks, compute_weights)     ← this layer
Stage 4: PortfolioOptimizer (ValidatedWeights)
Stage 5: ExecutionEngine (OrderIntent, RebalanceResult)
Stage 6: Exchange connectors (REST order submission)
```

### Asset Type Scope

One `SpotStrategy` instance corresponds to one exchange and one asset type. A strategy does not share a `ShadowBook` with another strategy — each strategy maintains its own virtual position ledger.

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
    symbols: Set[str] = None,
)
```

**Note:** `StrategyRunner.setup_strategy()` re-injects `_shared_event_bus`, `_private_event_bus`, `_exchanges`, `_optimizer`, `_execution_engine`, `cfg`, `strategy_config`, and `_shadow_book` before calling `start()`. Values passed at construction are placeholders.

### Key Properties

| Property | Type | Description |
|----------|------|-------------|
| `state` | `StrategyState` | Current lifecycle state (read-only) |
| `shadow_book` | `Optional[ShadowBook]` | Per-strategy virtual position ledger. Use this — not `portfolio` — to read positions, cash, weights, PnL |
| `rebalance_fsm` | `Optional[RebalanceFSM]` | The FSM instance; `None` if no optimizer/engine configured |
| `strategy_config` | `Optional[StrategyConfig]` | Per-strategy config from the strategy YAML (`params`, `risk_overrides`, `symbols`, etc.) |
| `portfolio` | `Optional[Portfolio]` | Aggregate portfolio reference (backward compat only). Prefer `shadow_book` |

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

`request_rebalance()` returns `True` if a cycle was started, `False` if the FSM was busy. It is safe to call at any frequency — skipped requests produce no side effects. Any keyword arguments passed to `request_rebalance(**ctx)` flow through the entire FSM pipeline and are available in `compute_weights(**ctx)`.

---

## 3. Strategy Lifecycle

Lifecycle states are defined in `core/enums.py` as `StrategyState`:

```
CREATED → STARTING → READY → RUNNING → STOPPING → STOPPED
                                                  → FAILED
```

### What `start()` Does

1. Validates that the strategy is in `CREATED` state
2. Loads `_symbols` from `strategy_config.symbols` or `cfg.symbols.symbols_for()`
3. Subscribes `_dispatch_kline` to `shared_event_bus` for `KLINE`
4. Subscribes `_dispatch_ob` to `shared_event_bus` for `ORDERBOOK_UPDATE`
5. Subscribes `_dispatch_order`, `_dispatch_balance`, `_dispatch_rebalance` to `private_event_bus`
6. Constructs `RebalanceFSM` if both `_optimizer` and `_execution_engine` are set
7. Calls `on_start()`
8. Transitions to `READY`

### What `stop()` Does

1. Calls `on_stop()` for strategy-specific cleanup
2. Sets `_stop_event` — unblocks `run_forever()`
3. Unsubscribes all dispatch callbacks from both event buses
4. Shuts down the `ProcessPoolExecutor` (non-blocking)
5. Calls `shadow_book.stop()`
6. Transitions to `STOPPED`

### Event Isolation

Each hook is wrapped in a `_dispatch_*` method that catches and logs exceptions without propagating them:

```python
async def _dispatch_kline(self, event: KlineEvent):
    if event.venue not in self.venues or event.symbol not in self.strategy_config.symbols:
        return
    self._mark_running()
    try:
        await self.on_kline(event)
    except Exception as e:
        self.logger.error(f"SpotStrategy.on_kline error: {e}", exc_info=True)
```

A bug in `on_kline` will never crash the Redis listener or event dispatcher.

For `ORDER_UPDATE` and `BALANCE_UPDATE`, the dispatch methods use `asyncio.gather` to run both the strategy hook and the `ShadowBook` update concurrently:

```python
async def _dispatch_order(self, event: OrderUpdateEvent):
    try:
        await asyncio.gather(
            self.on_order_update(event),
            self._shadow_book._on_order_update(event)
        )
    except Exception as e:
        self.logger.error(...)
```

---

## 4. Hook Functions (Override These)

All hooks are `async def` with default no-op implementations.

| Hook | Bus | Trigger | Typical Frequency |
|------|-----|---------|-------------------|
| `on_start()` | — | Once at startup | Once |
| `on_stop()` | — | Once at shutdown | Once |
| `run_forever()` | — | Long-running coroutine | Continuous |
| `on_kline(event)` | shared | Closed kline candle | ~1/sec per symbol (Binance 1s interval) |
| `on_orderbook_update(event)` | shared | Depth snapshot | ~10/sec per symbol |
| `on_order_update(event)` | private | Order state change | On fill / cancel / reject |
| `on_balance_update(event)` | private | Balance delta | On any balance change |
| `on_rebalance_complete(event)` | private | FSM cycle finished | After each rebalance |
| `compute_weights(**ctx)` | — | Called by FSM | Once per rebalance cycle |

### `on_start()`

Called after EventBus subscriptions are registered and the FSM is initialized. `self.cfg`, `self.strategy_config`, `self._shadow_book`, `self._optimizer`, and `self._execution_engine` are all guaranteed to be set. Use this for all strategy-state initialization.

```python
async def on_start(self):
    self._price_series = {sym: deque(maxlen=1000) for sym in self._symbols}
    self._last_rebalance_ts = 0.0
    self.logger.info(f"[MyStrategy] Started — symbols={self._symbols}")
```

### `on_stop()`

Called on shutdown. Use for cleanup or a final rebalance-to-cash.

```python
async def on_stop(self):
    await self.request_rebalance(convert_all_base=True)
```

### `run_forever()`

An optional long-running coroutine started by the runner alongside the Redis listener via `asyncio.gather`. The default implementation awaits `self._stop_event`.

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

Fired when a kline candle closes. `event.symbol` filtering is applied by `_dispatch_kline` — only events for symbols in `self.strategy_config.symbols` are delivered.

```python
async def on_kline(self, event: KlineEvent):
    self._price_series[event.symbol].append(event.kline.close)
    now = time.monotonic()
    if now - self._last_rebalance_ts > self._rebalance_interval:
        self._last_rebalance_ts = now
        await self.request_rebalance(trigger="kline")
```

### `on_orderbook_update(event: OrderBookUpdateEvent)`

Fires ~10/sec per symbol. Keep this fast (under 10ms) or offload with `run_heavy()`. `event.orderbook` is a **live reference** — copy immediately if you need a stable snapshot.

```python
async def on_orderbook_update(self, event: OrderBookUpdateEvent):
    spread_bps = event.orderbook.spread_bps
    # Do NOT store event.orderbook directly — it mutates on next update
    # Use copy.deepcopy(event.orderbook) if you need a snapshot
```

### `on_order_update(event: OrderUpdateEvent)`

Runs concurrently with `ShadowBook._on_order_update()`. The shadow book handles fill attribution automatically; this hook is for strategy-specific logic like logging or triggering follow-on orders.

```python
async def on_order_update(self, event: OrderUpdateEvent):
    if event.order.is_filled:
        self.logger.info(f"Order {event.order.id} filled @ {event.order.avg_fill_price}")
```

### `on_balance_update(event: BalanceUpdateEvent)`

Runs concurrently with `ShadowBook._on_balance_update()`. The shadow book handles cash reconciliation automatically.

### `on_rebalance_complete(event: RebalanceCompleteEvent)`

Fires after the execution engine completes a rebalance cycle. Inspect what was submitted vs what the optimizer adjusted.

```python
async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
    r = event.result
    vw = event.validated_weights
    if vw.clipped:
        self.logger.info(f"Optimizer clipped: {vw.clipped}")
    if r.failed > 0:
        self.logger.warning(f"Rebalance had {r.failed} failed orders: {r.errors}")
    # Post-rebalance shadow book state:
    self.logger.info(self._shadow_book.log_snapshot())
```

---

## 5. Signal Generation and the RebalanceFSM

### FSM States

```
IDLE → COMPUTING → VALIDATING → EXECUTING → COOLDOWN → IDLE
         ↑                                               ↓
         └────────── suspend / resume ──────────── SUSPENDED
                              ERROR ─── retry ────→ IDLE
```

### Context Flow

The `**ctx` dict is passed through and enriched at each stage:

- After `COMPUTING`: `ctx["target_weights"]` — a `dict[str, float]`
- After `VALIDATING`: `ctx["validated_weights"]` — a `ValidatedWeights` dataclass
- After `EXECUTING`: `ctx["rebalance_result"]` — a `RebalanceResult` dataclass

Any kwargs passed to `request_rebalance(**ctx)` are available in `compute_weights(**ctx)`:

```python
# Trigger:
await self.request_rebalance(convert_all_base=True)

# In compute_weights:
def compute_weights(self, **ctx) -> dict:
    if ctx.get('convert_all_base', False):
        return {}   # return empty dict for 100% cash
    # ... normal alpha logic
```

---

## 6. Weight Construction

### `compute_weights(**ctx) -> Optional[Dict[str, float]]`

**Contract:**
- Return a `dict` mapping symbol → target weight fraction. Example: `{"ETHUSDT": 0.4, "BTCUSDT": 0.5}`
- Cash weight is implicit: `cash_weight = 1.0 - sum(weights.values())`
- Return `{}` or `None` to skip the current cycle
- Can be synchronous (`def`) or asynchronous (`async def`) — the FSM handles both
- Must be **pure** — no side effects, no exchange calls, no state mutations
- **Never include stablecoin keys** (`USDT`, `USDC`, `BUSD`) — return `{}` for all-cash

```python
# Sync (preferred for stateless computation)
def compute_weights(self, **ctx) -> dict:
    signals = self._compute_signals()
    return self._signals_to_weights(signals)

# To go all-cash:
def compute_weights(self, **ctx) -> dict:
    if ctx.get('convert_all_base', False):
        return {}    # NOT {"USDT": 1.0} — that tries to trade USDTUSDT
    ...
```

### Normalization Example

```python
def _signals_to_weights(self, signals: Dict[str, float]) -> Dict[str, float]:
    positive = {s: v for s, v in signals.items() if v > 0}
    if not positive:
        return {}
    total = sum(positive.values())
    if total == 0:
        return {}
    max_allocation = 0.90   # 10% cash buffer
    return {s: (v / total) * max_allocation for s, v in positive.items()}
```

### Signal Pipeline Dataclasses

#### `TargetWeights`

The output of Stage 3.

```python
@dataclass(frozen=True)
class TargetWeights:
    weights: Dict[str, float]
    timestamp: int
    strategy_id: int = 0
    venue: Optional[Venue] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    price_overrides: Optional[Dict[str, float]] = None  # per-symbol limit price override
    liquidate: bool = False  # bypass all risk constraints (kill-switch)
```

#### `ValidatedWeights`

Stage 4 output.

```python
@dataclass(frozen=True)
class ValidatedWeights:
    original: TargetWeights
    weights: Dict[str, float]     # adjusted weights after risk constraints
    clipped: Dict[str, float]     # per-symbol: original_w - adjusted_w
    rejected: bool = False
    rejection_reason: str = ""
    timestamp: int = 0
    venue: Optional[Venue] = None
```

#### `RebalanceResult`

Stage 5 output, carried in `RebalanceCompleteEvent.result`.

```python
@dataclass(frozen=True)
class RebalanceResult:
    intents: Tuple[OrderIntent, ...]
    submitted: int
    failed: int
    timestamp: int
    errors: Tuple[str, ...] = ()
    submitted_orders: Dict[str, OrderIntent] = field(default_factory=dict)
    # submitted_orders: order_id → OrderIntent for successfully submitted limit orders
```

---

## 7. Event Types

All events are `@dataclass(frozen=True)` — immutable after creation.

### `KlineEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair (e.g. `"ETHUSDT"`) |
| `venue` | `Venue` | Exchange enum value |
| `kline` | `Kline` | OHLCV candle |
| `timestamp` | `int` | Epoch milliseconds |

The `Kline` dataclass: `ticker`, `interval`, `start_time`, `end_time`, `open`, `high`, `low`, `close`, `volume`.

### `OrderBookUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange |
| `orderbook` | `OrderBook` | Live bid/ask snapshot (SortedDict-backed) |
| `timestamp` | `int` | Epoch milliseconds |

`event.orderbook` is a live reference. Copy immediately if you need a stable snapshot.

Key `OrderBook` properties: `best_bid_price`, `best_ask_price`, `mid_price`, `spread`, `spread_bps`, `best_bid_amount`, `best_ask_amount`.

### `OrderUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `base_asset` | `str` | Base asset (e.g. `"ETH"`) |
| `quote_asset` | `str` | Quote asset (e.g. `"USDT"`) |
| `venue` | `Venue` | Exchange |
| `order` | `Order` | Order with status, fills, fees |
| `timestamp` | `int` | Epoch milliseconds |

`Order` key fields: `id`, `symbol`, `side`, `order_type`, `quantity`, `price`, `status`, `avg_fill_price`, `last_fill_price`, `filled_qty`, `commission`, `commission_asset`, `strategy_id`.

### `BalanceUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `asset` | `str` | Asset symbol (e.g. `"USDT"`) |
| `venue` | `Venue` | Exchange |
| `delta` | `float` | Balance change (positive = increase) |
| `new_balance` | `float` | Balance after change (may be 0.0 for Binance `balanceUpdate`) |
| `timestamp` | `int` | Epoch milliseconds |

### `RebalanceCompleteEvent`

| Field | Type | Description |
|-------|------|-------------|
| `result` | `RebalanceResult` | Execution summary |
| `validated_weights` | `ValidatedWeights` | What the optimizer approved |
| `timestamp` | `int` | Epoch milliseconds |
| `strategy_id` | `int` | Owning strategy |

---

## 8. Risk Config Integration

### RiskConfig Fields

| Field | Default | Description |
|-------|---------|-------------|
| `max_weight_per_asset` | `0.25` | Maximum weight for any single asset |
| `min_weight_per_asset` | `0.0` | Minimum weight per asset (0 = long-only) |
| `max_total_exposure` | `1.0` | Maximum sum of absolute weights |
| `min_cash_weight` | `0.05` | Minimum fraction to hold in cash |
| `max_turnover_per_rebalance` | `0.5` | Maximum sum(|Δw|) per rebalance cycle |
| `max_leverage` | `1.0` | Maximum gross leverage |
| `max_short_weight` | `0.0` | Maximum short weight per asset |
| `min_order_notional` | `10.0` | Skip orders below this USD notional |
| `max_order_notional` | `100_000.0` | Cap single order size |
| `min_weight_delta` | `0.005` | Skip rebalance legs with |Δw| < 0.5% |

### Optimizer Constraint Pipeline

When `compute_weights()` returns weights, `PortfolioOptimizer.validate()` applies constraints in order:

1. **Input validation** — drop NaN / Inf / non-numeric
2. **Symbol filter** — whitelist/blacklist
3. **Per-asset clip** — `[min_weight_per_asset, max_weight_per_asset]`
4. **Total-exposure scaling** — scale down if `sum(|w|) > max_total_exposure`
5. **Cash-floor enforcement** — scale down long exposure if it crowds out `min_cash_weight`
6. **Turnover cap** — partial rebalance (scale each Δw) if total turnover exceeds limit
7. **Min-delta prune** — drop legs with `|Δw| < min_weight_delta`

A `liquidate=True` flag (set when `compute_weights()` returns weights with the `convert_all_base` context key being True) bypasses all constraints for immediate liquidation.

### Config Priority

```
strategy risk: section  >  venue_configs["{asset_type}_{venue}"]  >  global risk
```

Use `cfg.effective_risk_for(strategy_id=N)` to retrieve the resolved `RiskConfig`.

### Runtime Override

```python
async def on_drawdown_breach(self):
    self._optimizer.config.max_weight_per_asset = 0.10
    self._optimizer.config.max_total_exposure = 0.50
    await self.suspend_rebalancing()
```

---

## 9. Example: EventDrivenStrategy

**File:** `elysian_core/strategy/example_weight_strategy_v2_event_driven.py`

### Alpha Thesis

Price momentum — compares current price against a 30-bar simple moving average. If price is below the SMA (downward momentum relative to average), the asset receives a full equal weight allocation. If above (mean-reverting / overbought), it receives zero weight.

### Data Structures (initialized in `on_start()`)

```python
self._price_series       = {sym: NumpySeries(maxlen=60*240) for sym in self._symbols}
self._returns_series     = {sym: NumpySeries(maxlen=60*240) for sym in self._symbols}
self._rolling_returns_series = {sym: NumpySeries(maxlen=60*240) for sym in self._symbols}
self._symbol_availability_status = {sym: False for sym in self._symbols}
self._last_marked_time   = None
```

All series are bounded — memory safe regardless of runtime.

### Kline Handler

`on_kline()` per candle close:
1. Appends `event.kline.close` to `_price_series[symbol]`
2. Computes per-bar return and appends to `_returns_series`
3. Once 30 returns available, computes 30-bar rolling mean and stores in `_rolling_returns_series`; sets `_symbol_availability_status[symbol] = True`

Rebalance trigger fires when all symbols are available AND elapsed time since the last trigger exceeds `_rebalance_interval` (30 seconds):

```python
if all(self._symbol_availability_status.values()) and \
   (self._last_marked_time is None or
    time.monotonic() - self._last_marked_time > self._rebalance_interval):
    self._last_marked_time = time.monotonic()
    await self.request_rebalance()
```

### `compute_weights()`

```python
def compute_weights(self, **ctx) -> dict:
    if ctx.get('convert_all_base', False):
        return {sym: 0.0 for sym in self._symbols}

    symbol_max_weight = 1.0 / len(self._symbols)
    weights = {}
    for sym in self._symbols:
        price_series = self._price_series.get(sym)
        current_price = price_series[-1] if price_series else None
        sma = price_series[-30:].mean() if price_series and len(price_series) >= 30 else None

        if current_price is not None and sma is not None and current_price < sma:
            weights[sym] = symbol_max_weight
        else:
            weights[sym] = 0.0

    return weights
```

### Shutdown

`on_stop()` requests a closing rebalance. `compute_weights()` detects `convert_all_base=True` and returns all-zero weights, which the optimizer and executor translate into SELL orders for all current positions.

---

## 10. Example: PrintEventsStrategy

**File:** `elysian_core/strategy/example_strategy_test_print.py`

A diagnostic strategy that logs received events. No trading logic. Useful for verifying EventBus wiring, confirming feed workers are emitting events, and checking order/balance routing.

| Hook | Behaviour |
|------|-----------|
| `on_start()` | Loads symbols from `cfg`; logs startup |
| `on_stop()` | Requests `convert_all_base=True` rebalance |
| `run_forever()` | Blocks on `_stop_event` |
| `on_kline()` | Filters by `_symbols`; logging commented out by default |
| `on_orderbook_update()` | Filters by `_symbols`; logging commented out by default |
| `on_order_update()` | Logs order ID, status, fill quantities |
| `on_balance_update()` | Logs asset, delta, new balance |
| `on_rebalance_complete()` | Logs result and validated weights |

The strategy does not override `compute_weights()` — it inherits the base no-op that returns `{}`, so no rebalances are initiated unless `convert_all_base=True` is passed.

---

## 11. Writing a New Strategy

### Step 1: Create the Strategy File

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
    Describe your alpha thesis here.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rebalance_interval: int = 60
        self._lookback: int = 30

    async def on_start(self):
        # Initialize bounded rolling state
        self._price_series: Dict[str, deque] = {
            sym: deque(maxlen=self._lookback * 10) for sym in self._symbols
        }
        self._ready: Dict[str, bool] = {sym: False for sym in self._symbols}
        self._last_rebalance_ts: float = 0.0

        logger.info(f"[MyStrategy] Started — symbols={self._symbols}")

    async def on_stop(self):
        await self.request_rebalance(convert_all_base=True)

    async def run_forever(self):
        if self._rebalance_fsm is not None:
            self._rebalance_fsm._cooldown_s = self._rebalance_interval
        await self._stop_event.wait()

    async def on_kline(self, event: KlineEvent):
        if event.symbol not in self._symbols:
            return

        self._price_series[event.symbol].append(event.kline.close)
        if len(self._price_series[event.symbol]) >= self._lookback:
            self._ready[event.symbol] = True

        now = time.monotonic()
        if all(self._ready.values()) and (now - self._last_rebalance_ts) > self._rebalance_interval:
            self._last_rebalance_ts = now
            await self.request_rebalance(trigger="kline")

    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        r = event.result
        if r.failed > 0:
            logger.warning(f"[MyStrategy] {r.failed} failed orders: {r.errors}")

    def compute_weights(self, **ctx) -> Dict[str, float]:
        """Pure weight computation. No side effects."""
        if ctx.get('convert_all_base', False):
            return {}

        signals = self._compute_signals()
        return self._signals_to_weights(signals)

    def _compute_signals(self) -> Dict[str, float]:
        signals = {}
        for sym in self._symbols:
            series = list(self._price_series[sym])
            if len(series) < self._lookback:
                continue
            signal = (series[-1] - series[0]) / series[0] if series[0] != 0 else 0.0
            if signal > 0:
                signals[sym] = signal
        return signals

    def _signals_to_weights(self, signals: Dict[str, float]) -> Dict[str, float]:
        if not signals:
            return {}
        total = sum(signals.values())
        if total == 0:
            return {}
        return {sym: (v / total) * 0.90 for sym, v in signals.items()}
```

### Step 2: Create a Strategy YAML Config

```yaml
# elysian_core/config/strategies/strategy_002_my_strategy.yaml

strategy_id: 2                              # unique integer
strategy_name: "my_strategy_binance_spot"
class: "elysian_core.strategy.my_strategy.MyStrategy"
asset_type: "Spot"
venue: "Binance"
venues:
  - "Binance"
max_heavy_workers: 4
symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
risk:
  max_weight_per_asset: 0.25
  min_cash_weight: 0.05
  max_turnover_per_rebalance: 0.5
  min_order_notional: 10.0
  min_weight_delta: 0.005
params:
  rebalance_interval_s: 60
```

### Step 3: Add API Keys to `.env`

```dotenv
BINANCE_API_KEY_2=your_key
BINANCE_API_SECRET_2=your_secret
```

### Step 4: Add a Docker Service

In `docker-compose.yml`:

```yaml
  elysian_strategy_2:
    <<: *strategy-defaults
    container_name: elysian_strategy_2
    environment:
      STRATEGY_CONFIG_YAML: elysian_core/config/strategies/strategy_002_my_strategy.yaml
      BINANCE_API_KEY_2: ${BINANCE_API_KEY_2}
      BINANCE_API_SECRET_2: ${BINANCE_API_SECRET_2}
    command:
      - python
      - elysian_core/run_strategy.py
      - --strategy-yaml
      - elysian_core/config/strategies/strategy_002_my_strategy.yaml
    profiles:
      - strategy_2
      - all
```

### Step 5: Verify the Checklist

- [ ] `compute_weights()` is pure — no side effects, no exchange calls
- [ ] Weights sum to <= 1.0 (implicit cash for remainder)
- [ ] Returns `{}` (not `{"USDT": 1.0}`) for all-cash
- [ ] `on_start()` handles the case where `_symbols` is empty
- [ ] Rolling state uses bounded collections
- [ ] Strategy handles missing data gracefully (returns `{}` to skip cycle)
- [ ] Rebalance trigger has appropriate throttling
- [ ] Config parameters read from `strategy_config.params` or constructor args (not hardcoded)
- [ ] `strategy_id` is unique and matches env var suffix

---

## 12. Config YAML Schema

```yaml
# ── Identity ──────────────────────────────────────────────────────────────────
strategy_id: 0                              # unique integer; shadow book routing + API key suffix
strategy_name: "my_strategy"               # human-readable label

# ── Class ─────────────────────────────────────────────────────────────────────
class: "elysian_core.strategy.my_strategy.MyStrategy"  # fully qualified Python path

# ── Venue + asset type ────────────────────────────────────────────────────────
asset_type: "Spot"         # "Spot" or "Perpetual"
venue: "Binance"           # primary venue
venues:
  - "Binance"

# ── Resources ─────────────────────────────────────────────────────────────────
max_heavy_workers: 4       # ProcessPoolExecutor workers for run_heavy()

# ── Symbols ───────────────────────────────────────────────────────────────────
symbols: []                # explicit list; empty = loaded from config.json for this venue+asset_type

# ── Risk ──────────────────────────────────────────────────────────────────────
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

# ── Overrides ─────────────────────────────────────────────────────────────────
execution_overrides: {}
portfolio_overrides: {}

# ── Strategy-specific parameters ──────────────────────────────────────────────
# Accessible via self.strategy_config.params["key"]
params:
  rebalance_interval_s: 60
```

### Config Priority

```
strategy risk:  >  venue_configs["{asset_type}_{venue}"]  >  global risk
```

### Sub-account Credentials

```bash
# .env — pattern: {VENUE}_API_KEY_{strategy_id}
BINANCE_API_KEY_0=...
BINANCE_API_SECRET_0=...
BINANCE_API_KEY_1=...
BINANCE_API_SECRET_1=...
```

---

## 13. Strategy Testing

### Unit Testing `compute_weights()`

```python
def make_strategy() -> MyStrategy:
    s = MyStrategy(strategy_id=0, strategy_name="test")
    s._symbols = {"ETHUSDT", "BTCUSDT"}
    return s


def test_compute_weights_all_positive_signals():
    s = make_strategy()
    for i in range(35):
        s._price_series["ETHUSDT"].append(1000.0 + i)
        s._price_series["BTCUSDT"].append(50000.0 + i)
        s._ready["ETHUSDT"] = True
        s._ready["BTCUSDT"] = True

    weights = s.compute_weights()

    assert sum(weights.values()) <= 1.0
    assert all(w >= 0 for w in weights.values())


def test_compute_weights_returns_empty_on_insufficient_data():
    s = make_strategy()
    assert s.compute_weights() == {}


def test_compute_weights_convert_all_base():
    s = make_strategy()
    assert s.compute_weights(convert_all_base=True) == {}
```

### Integration Testing with EventBus

```python
async def test_on_kline_updates_price_series():
    shared_bus = EventBus()
    private_bus = EventBus()
    strategy = MyStrategy(
        shared_event_bus=shared_bus,
        private_event_bus=private_bus,
        venue=Venue.BINANCE,
        strategy_id=0,
    )
    # Inject required dependencies before start()
    strategy.strategy_config = MockStrategyConfig(symbols=["ETHUSDT"])
    strategy._shadow_book = MockShadowBook()
    strategy._optimizer = None
    strategy._execution_engine = None

    await strategy.start()

    kline = Kline(ticker="ETHUSDT", interval="1s",
                  start_time=datetime.now(), end_time=datetime.now(), close=2000.0)
    event = KlineEvent(symbol="ETHUSDT", venue=Venue.BINANCE, kline=kline, timestamp=0)
    await shared_bus.publish(event)

    assert list(strategy._price_series["ETHUSDT"])[-1] == 2000.0
```

---

## 14. Critical Gotchas

### `USDT` Is NOT a Tradeable Weight

Return `{}` (empty dict) for all-cash — never `{"USDT": 1.0}`. The execution engine will attempt to place an `USDTUSDT` order, which does not exist.

### Initialization Goes in `on_start()`, Not `__init__()`

`self.cfg`, `self.strategy_config`, `self._shadow_book`, `self._optimizer`, and `self._execution_engine` are all `None` inside `__init__()`. They are injected by `StrategyRunner.setup_strategy()` before `start()` is called.

### Weight Sum Must Be ≤ 1.0

For `AssetType.SPOT`, all weights must be non-negative and must sum to ≤ 1.0. The optimizer enforces `max_total_exposure = 1.0` by default, but passing weights that already exceed 1.0 is wasteful.

### `strategy_id` Must Be Unique and Match Env Vars

The `strategy_id` in the YAML must be unique across all loaded strategies. The shadow book, logging, and sub-account API key lookup all key off this integer. Reusing a `strategy_id` will cause one strategy to overwrite another's ShadowBook and use the wrong API keys.

### `request_rebalance()` Is Idempotent

Calling `request_rebalance()` while the FSM is mid-cycle, in cooldown, or suspended is silently ignored. There is no queue. A new cycle only starts when the FSM returns to `IDLE`.

### Sub-account Mode Is Always Active

Every strategy must have `{VENUE}_API_KEY_{strategy_id}` and `{VENUE}_API_SECRET_{strategy_id}` set in `.env`. There is no shared-account fallback in the current architecture.

### Kline Feed Interval Is Fixed at 1 Second

`BinanceKlineClientManager` subscribes to `{symbol}@kline_1s`. If your strategy logic requires per-minute or per-hour candles, aggregate the 1-second events yourself in `on_kline()` rather than expecting a different feed interval.

### `event.orderbook` Is a Live Reference

The `OrderBook` object in `OrderBookUpdateEvent` is the live object held by the feed and will be mutated by subsequent updates. If you need a stable snapshot, call `copy.deepcopy(event.orderbook)` immediately.
