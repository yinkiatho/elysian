# Core Module Documentation

## Overview

The `elysian_core/core/` package is the domain kernel of the Elysian trading framework. It contains every exchange-agnostic abstraction that the rest of the system builds on: the event bus, typed event contracts, order and position models, state machines, portfolio accounting, and the signal pipeline that connects alpha generation to execution.

| Layer | Files | Responsibility |
|-------|-------|----------------|
| Event infrastructure | `event_bus.py`, `events.py` | In-process pub/sub wiring (private bus) |
| Market data | `market_data.py` | Order book and OHLCV structures |
| Order model | `order.py`, `order_fsm.py` | Order lifecycle and FSM validation |
| FSM base | `fsm.py` | Generic async state machine |
| Accounting | `position.py`, `shadow_book.py`, `portfolio.py` | Per-strategy and aggregate ledgers |
| Rebalance pipeline | `rebalance_fsm.py`, `signals.py` | Compute → validate → execute cycle |
| Shared types | `enums.py`, `constants.py` | All enumerations and constants |

---

## EventBus

**File:** `elysian_core/core/event_bus.py`

The `EventBus` is a lightweight, in-process async publish/subscribe bus. In Elysian's two-bus architecture, the in-process `EventBus` is used exclusively for the **private per-strategy bus** carrying account events (`ORDER_UPDATE`, `BALANCE_UPDATE`, `REBALANCE_COMPLETE`, `REBALANCE_CYCLE`). Market data events travel over Redis pub/sub via `RedisEventBusSubscriber` in production.

### Design Properties

- **Zero serialization overhead.** All dispatch is via direct async function calls within a single process.
- **Sequential dispatch with backpressure.** `publish()` awaits each subscriber callback in registration order. If a handler is slow, the emitting worker waits.
- **Fault isolation.** Exceptions in one callback are caught, logged, and do not prevent remaining callbacks from running.
- **`defaultdict(list)` internals.** Subscribing to an event type that no one has published is free.

### Class: `EventBus`

```python
bus = EventBus()
```

| Method | Signature | Description |
|--------|-----------|-------------|
| `subscribe` | `(event_type: EventType, callback: Callable)` | Register an async callback. Multiple callbacks per type are supported. |
| `unsubscribe` | `(event_type: EventType, callback: Callable)` | Remove a callback. No-op if not found. |
| `publish` | `async (event)` | Dispatch to every subscriber for `event.event_type`. Awaited sequentially. |

### Subscription Pattern

Components subscribe during `start()` and unsubscribe during `stop()`:

```python
def start(self, event_bus):
    self._event_bus = event_bus
    event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)
    event_bus.subscribe(EventType.BALANCE_UPDATE, self._on_balance_update)

def stop(self):
    self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._on_order_update)
    self._event_bus.unsubscribe(EventType.BALANCE_UPDATE, self._on_balance_update)
    self._event_bus = None
```

---

## Event Types

**File:** `elysian_core/core/events.py`

All event classes are frozen dataclasses (`@dataclass(frozen=True)`). The `event_type` field is set automatically via `field(default=..., init=False)` and cannot be overridden.

### `EventType` Enum

| Value | Class | Transport |
|-------|-------|-----------|
| `KLINE` | `KlineEvent` | Redis (shared bus) |
| `ORDERBOOK_UPDATE` | `OrderBookUpdateEvent` | Redis (shared bus) |
| `ORDER_UPDATE` | `OrderUpdateEvent` | In-process private bus |
| `BALANCE_UPDATE` | `BalanceUpdateEvent` | In-process private bus |
| `REBALANCE_COMPLETE` | `RebalanceCompleteEvent` | In-process private bus |
| `LIFECYCLE` | `LifecycleEvent` | In-process (not currently published) |
| `REBALANCE_CYCLE` | `RebalanceCycleEvent` | In-process private bus |

### `KlineEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair (e.g. `"ETHUSDT"`) |
| `venue` | `Venue` | Exchange where this candle originated |
| `kline` | `Kline` | OHLCV candle data |
| `timestamp` | `int` | Epoch milliseconds of emission |
| `event_type` | `EventType` | Auto-set to `EventType.KLINE` |

### `OrderBookUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange |
| `orderbook` | `OrderBook` | Live mutable `OrderBook` object (SortedDict-backed) |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.ORDERBOOK_UPDATE` |

`orderbook` is a live reference. Copy immediately if you need a stable snapshot.

### `OrderUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `base_asset` | `str` | Base asset (e.g. `"ETH"`) |
| `quote_asset` | `str` | Quote asset (e.g. `"USDT"`) |
| `venue` | `Venue` | Exchange |
| `order` | `Order` | Parsed order with current status and cumulative fill state |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.ORDER_UPDATE` |

### `BalanceUpdateEvent`

| Field | Type | Description |
|-------|------|-------------|
| `asset` | `str` | Asset symbol (e.g. `"USDT"`, `"ETH"`) |
| `venue` | `Venue` | Exchange |
| `delta` | `float` | Change in balance (positive = increase) |
| `new_balance` | `float` | New balance after change (may be `0.0` for Binance `balanceUpdate` events) |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.BALANCE_UPDATE` |

### `RebalanceCompleteEvent`

| Field | Type | Description |
|-------|------|-------------|
| `result` | `RebalanceResult` | Full result summary from the execution engine |
| `validated_weights` | `ValidatedWeights` | Risk-adjusted weights that were executed |
| `timestamp` | `int` | Epoch milliseconds |
| `strategy_id` | `int` | Owning strategy identifier |
| `event_type` | `EventType` | Auto-set to `EventType.REBALANCE_COMPLETE` |

### `RebalanceCycleEvent`

Published on every FSM state transition. Enables external monitoring and audit trails.

| Field | Type | Description |
|-------|------|-------------|
| `old_state` | `RebalanceState` | Previous FSM state |
| `new_state` | `RebalanceState` | New state after transition |
| `trigger` | `str` | Trigger name (e.g. `"signal"`, `"accepted"`) |
| `timestamp` | `int` | Epoch milliseconds |
| `metadata` | `dict` | Optional context payload |
| `event_type` | `EventType` | Auto-set to `EventType.REBALANCE_CYCLE` |

---

## Market Data

**File:** `elysian_core/core/market_data.py`

### `OrderBook`

Normalized order book snapshot. Bids and asks are stored in `SortedDict` containers from `sortedcontainers`, providing O(log n) insertion and O(1) best bid/ask access.

| Field | Type | Description |
|-------|------|-------------|
| `last_update_id` | `int` | Exchange sequence number for the last applied update |
| `last_timestamp` | `int` | Timestamp of the last update (epoch ms) |
| `ticker` | `str` | Trading pair symbol |
| `interval` | `Optional[str]` | Update interval if applicable |
| `bid_orders` | `SortedDict` | Price → quantity map (ascending key; best bid is last = `peekitem(-1)`) |
| `ask_orders` | `SortedDict` | Price → quantity map (ascending key; best ask is first = `peekitem(0)`) |

**Class method:**
```python
OrderBook.from_lists(last_update_id, last_timestamp, ticker, interval, bid_levels, ask_levels)
```

**Mutation method:**
```python
def apply_update(self, side: str, price: float, qty: float) -> None
    # side = "bid" or "ask"; qty == 0 removes the level
```

**Computed properties:**
| Property | Description |
|----------|-------------|
| `best_bid_price` | Highest bid price; `0.0` if empty |
| `best_bid_amount` | Quantity at the best bid |
| `best_ask_price` | Lowest ask price; `0.0` if empty |
| `best_ask_amount` | Quantity at the best ask |
| `mid_price` | `(best_bid + best_ask) / 2` |
| `spread` | `best_ask - best_bid` |
| `spread_bps` | Spread in basis points: `spread / mid_price × 10,000` |

### Exchange-Specific Subclasses

`BinanceOrderBook` and `AsterOrderBook` both inherit from `OrderBook` and add async batch update methods used by their respective worker coroutines:

```python
async def apply_update_lists(self, side: str, price_levels: list) -> None
async def apply_both_updates(self, last_timestamp, new_update_id, bid_levels, ask_levels) -> None
    # Uses asyncio.gather to apply bid and ask updates concurrently
```

### `Kline`

Exchange-agnostic OHLCV candlestick.

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `str` | Trading pair symbol |
| `interval` | `str` | Time interval (e.g. `"1s"`, `"1m"`) |
| `start_time` | `datetime.datetime` | Candle open time |
| `end_time` | `datetime.datetime` | Candle close time |
| `open` | `Optional[float]` | Opening price |
| `high` | `Optional[float]` | Highest price |
| `low` | `Optional[float]` | Lowest price |
| `close` | `Optional[float]` | Closing price |
| `volume` | `Optional[float]` | Base asset trading volume |

---

## Order Model

**File:** `elysian_core/core/order.py`

### `Order`

Generic exchange order covering CEX limit and market orders.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | required | Exchange-assigned order identifier |
| `symbol` | `str` | required | Trading pair |
| `side` | `Side` | required | `Side.BUY` or `Side.SELL` |
| `order_type` | `OrderType` | required | `LIMIT`, `MARKET`, etc. |
| `quantity` | `float` | required | Total order quantity in base asset |
| `price` | `Optional[float]` | `None` | Limit price; `None` for market orders |
| `avg_fill_price` | `float` | `0.0` | Running volume-weighted average fill price |
| `last_fill_price` | `float` | `0.0` | Last executed price (Binance field `L`); used for incremental fill delta |
| `filled_qty` | `float` | `0.0` | Cumulative filled quantity |
| `status` | `OrderStatus` | `PENDING` | Current lifecycle state |
| `venue` | `Optional[Venue]` | `None` | Exchange |
| `commission` | `float` | `0.0` | Cumulative commission (accumulated per-fill from Binance field `n`) |
| `commission_asset` | `Optional[str]` | `None` | Asset commission is charged in |
| `last_updated_timestamp` | `Optional[int]` | `None` | Epoch ms of last status update |
| `strategy_id` | `Optional[int]` | `None` | Owning strategy |

**Key methods:**

| Method | Description |
|--------|-------------|
| `transition_to(new_status)` | Validate and apply an `OrderStatus` transition. Warn-mode: invalid transitions are logged but always applied. |
| `update_fill(filled_qty, fill_price)` | Update cumulative fill state. Idempotent — no-op if `filled_qty` has not increased. |
| `is_filled`, `is_active`, `is_terminal` | Status properties |

### `ActiveOrder`

Extends `Order` with `_prev_filled: float` for incremental fill-delta tracking. Used by `ShadowBook` for live order tracking.

```python
delta = active.sync_from_event(event_order)  # returns incremental fill delta
```

### `ActiveLimitOrder`

Extends `ActiveOrder` with balance reservation state for LIMIT orders:

| Field | Description |
|-------|-------------|
| `reserved_cash` | Cash locked for BUY (= `quantity × price`) |
| `reserved_qty` | Base-asset quantity locked for SELL |

Key methods:
- `initialize_reservation()` — set initial reservation on placement
- `release_partial(delta_filled)` — proportionally release on each fill
- `release_all()` — drain all remaining on terminal

---

## OrderFSM

**File:** `elysian_core/core/order_fsm.py`

Validates `OrderStatus` transitions and provides a per-order lifecycle wrapper. Operates in **warn-mode**: the exchange is authoritative, so invalid transitions are logged but always applied.

### Order Lifecycle States

```
                     PENDING
                    /   |   \  \
               OPEN  REJECTED FILLED  CANCELLED
              / | \ \
  PART_FILLED  FILLED  CANCELLED  EXPIRED  TRIGGERED
     |                                  / | \ \
     FILLED/CANCELLED/EXPIRED      OPEN PART FILLED CANCELLED EXPIRED
```

### Transition Table

| From | Valid Targets |
|------|--------------|
| `PENDING` | `OPEN`, `REJECTED`, `CANCELLED`, `FILLED` |
| `OPEN` | `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `TRIGGERED` |
| `PARTIALLY_FILLED` | `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED` |
| `TRIGGERED` | `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED` |
| `FILLED` | — (terminal) |
| `CANCELLED` | — (terminal) |
| `REJECTED` | — (terminal) |
| `EXPIRED` | — (terminal) |

### Module-Level Functions

```python
validate_order_transition(current, target, order_id="") -> bool
is_terminal(status: OrderStatus) -> bool
```

### `OrderFSM` Class

```python
fsm = OrderFSM(OrderStatus.PENDING, order_type=OrderType.STOP_MARKET)
fsm.transition(OrderStatus.TRIGGERED)
assert fsm.is_conditional   # True — STOP_MARKET is conditional
assert fsm.can_fill         # True — TRIGGERED accepts fills
```

| Property | Description |
|----------|-------------|
| `is_terminal` | True if current status is terminal |
| `is_active` | True if `status.is_active()` |
| `can_fill` | True if OPEN, PARTIALLY_FILLED, or TRIGGERED |
| `is_conditional` | True if order_type is STOP_MARKET or TAKE_PROFIT_MARKET |

---

## AsyncFSM Base

**File:** `elysian_core/core/fsm.py`

`BaseFSM` is the generic async finite-state machine base class used by `RebalanceFSM`.

### Design

- **Dict-based transition table.** Subclasses declare `_TRANSITIONS` as a class-level dict: `(State, trigger_name) -> (NextState, callback_method_name_or_None)`.
- **Wildcard transitions.** `"*"` as the state key matches any current state. Explicit matches take priority.
- **Re-entrancy guard.** If a callback triggers another transition while one is in progress, the new trigger is queued and drained after the current transition completes.
- **Context threading.** `_fsm_old_state` is injected into `**ctx` so callbacks can access the pre-transition state.

### `BaseFSM`

```python
BaseFSM(
    initial_state: Enum,
    event_bus: Optional[EventBus] = None,
    terminal_states: Optional[Set[Enum]] = None,
    name: Optional[str] = None,
)
```

| Method / Property | Description |
|-------------------|-------------|
| `state` | Current state enum value (read-only) |
| `is_terminal` | `True` if current state is in `_terminal_states` |
| `async trigger(event, **ctx)` | Fire a named trigger; returns the new state. Raises `InvalidTransition` if no matching entry exists. |

### `InvalidTransition` Exception

```python
class InvalidTransition(Exception):
    state: Enum    # state when trigger was fired
    trigger: str   # trigger name
```

### `PeriodicTask`

Wraps a repeating async task, replacing bare `while True` loops.

```python
task = PeriodicTask(callback=portfolio.save_snapshot, interval_s=60, name="snapshot")
task.start()   # non-blocking background task
task.stop()    # cancels the asyncio.Task
```

| Property | Description |
|----------|-------------|
| `running` | `True` if the background task is active and not done |

---

## Position

**File:** `elysian_core/core/position.py`

`Position` is a mutable dataclass tracking a single open position in one asset.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | `str` | required | Trading pair |
| `venue` | `Venue` | `Venue.BINANCE` | Exchange |
| `quantity` | `float` | `0.0` | Signed: `> 0` is long, `< 0` is short |
| `avg_entry_price` | `float` | `0.0` | Volume-weighted average entry price |
| `realized_pnl` | `float` | `0.0` | Cumulative realized P&L from partial/full closes |
| `commission_by_asset` | `Dict[str, float]` | `{}` | Per-asset commission amounts |
| `locked_quantity` | `float` | `0.0` | Reserved for pending LIMIT SELL orders |

**Computed properties:**

| Property / Method | Description |
|-------------------|-------------|
| `free_quantity` | `max(0, quantity - locked_quantity)` |
| `unrealized_pnl(mark_price)` | `(mark_price - avg_entry_price) × quantity` |
| `total_pnl(mark_price)` | `realized_pnl + unrealized_pnl(mark_price)` |
| `net_pnl(mark_price, asset_prices)` | `total_pnl - commissions_in_usdt` |
| `notional(mark_price)` | `abs(quantity) × mark_price` |
| `is_flat()`, `is_long()`, `is_short()` | Position direction checks |

---

## Portfolio

**File:** `elysian_core/core/portfolio.py`

`Portfolio` is a thin, read-only NAV aggregator across registered per-strategy `ShadowBook` instances. It owns no event subscriptions and performs no fill attribution.

```python
portfolio = Portfolio(venue=Venue.BINANCE, cfg=cfg)
portfolio.register_shadow_book(shadow_book)   # once per strategy
portfolio.aggregate_nav()                      # sum of all shadow book NAVs
portfolio.save_snapshot()                      # persist to portfolio_snapshots table
```

**Aggregation reads:**

| Method / Property | Returns | Description |
|-------------------|---------|-------------|
| `aggregate_nav()` | `float` | Sum of `nav` across all shadow books |
| `aggregate_cash()` | `float` | Sum of `cash` across all shadow books |
| `aggregate_positions()` | `Dict[str, Position]` | Merged; same-symbol quantities summed, avg entry VWAP'd |
| `nav` | `float` | Property alias for `aggregate_nav()` |
| `cash` | `float` | Property alias for `aggregate_cash()` |
| `positions` | `Dict[str, Position]` | Property alias for `aggregate_positions()` |
| `unrealized_pnl` | `float` | Sum across all shadow books |
| `realized_pnl` | `float` | Sum across all shadow books |
| `gross_exposure()` | `float` | Sum across all shadow books |
| `net_exposure()` | `float` | Sum across all shadow books |

### `Fill` Record

```python
@dataclass(frozen=True)
class Fill:
    symbol: str
    venue: Venue
    side: Side
    quantity: float      # always positive
    price: float
    commission: float = 0.0
    commission_asset: str = ""
    timestamp: int = 0   # epoch ms
    order_id: str = ""
```

Used in `ShadowBook._fills` ring buffer (defined in `portfolio.py` for import convenience).

---

## ShadowBook

**File:** `elysian_core/core/shadow_book.py`

Per-strategy virtual position and cash ledger. This is the **primary accounting unit** for each strategy. See the Architecture documentation for the full ShadowBook design — key points summarized here.

### Construction

```python
ShadowBook(
    strategy_id: int,
    venue: Venue = Venue.BINANCE,
    strategy_name: str = "",
    strategy_config: Optional[StrategyConfig] = None,
)
```

`strategy_config` sets `_tracked_symbols` (limiting which symbols are tracked) and `_asset_type` (SPOT or PERPETUAL).

### Initialization Paths

| Method | When |
|--------|------|
| `sync_from_exchange(exchange, feeds)` | Startup in sub-account mode; reads real balances, open orders |
| `init_from_portfolio_cash(cash, mark_prices)` | Fresh deployment; no prior positions |

### Lifecycle

```python
shadow_book.start(shared_event_bus, private_event_bus=private_bus)
# Subscribes _on_kline to shared bus
# private_bus subscriptions are handled by SpotStrategy._dispatch_*

shadow_book.stop()
# Unsubscribes all
```

### Key Read Properties

| Property / Method | Description |
|-------------------|-------------|
| `nav` | Cached NAV (updates on every kline or fill) |
| `cash` | Total stablecoin balance |
| `free_cash` | `cash - _locked_cash` |
| `free_quantity(symbol)` | `position.quantity - position.locked_quantity` |
| `weights` | Cached weight vector |
| `positions` / `active_positions` | All / non-flat positions (copies) |
| `total_commission(mark_prices)` | Net commissions in USDT (excludes position-deducted amounts) |
| `unrealized_pnl(mark_prices)` | Mark-to-market unrealized P&L |
| `realized_pnl` | Cumulative realized P&L |
| `peak_equity` / `max_drawdown` | High-water mark and max drawdown fraction |
| `active_orders` | Live orders dict (copy) |
| `fills` | Fill history ring buffer (copy, maxlen=10,000) |

### Active Order Tracking

| Method | Description |
|--------|-------------|
| `lock_for_order(order_id, intent)` | Reserve cash (BUY) or qty (SELL) for a LIMIT order |
| `_partial_release_lock(order_id, delta)` | Proportionally release on partial fill |
| `release_all() → (cash, qty)` | Drain remaining reservation on terminal |

### Utility

```python
shadow_book.log_snapshot(num_fills_show=10) -> str
# Returns a detailed multi-line string: NAV, cash, locked cash, positions,
# active orders, fill audit trail — used in log output after each fill/rebalance

shadow_book.save_snapshot()
# Persists to portfolio_snapshots DB table
```

### Non-Stablecoin Commission Handling

When an order is filled and the commission asset is NOT a stablecoin (e.g. BNB on Binance), the ShadowBook:
1. Records the commission in `_total_commission_dict[commission_asset]`
2. Deducts `commission` quantity from the corresponding position (e.g. reduces BNB position)
3. Tracks the deducted amount in `_position_deducted_comm` so `total_commission()` does not double-count (the cost is already reflected in the reduced position NAV)

---

## RebalanceFSM

**File:** `elysian_core/core/rebalance_fsm.py`

`RebalanceFSM` drives the compute → validate → execute → cooldown pipeline. See the Architecture documentation for the full state diagram and transition table.

### Constructor

```python
RebalanceFSM(
    strategy,              # must implement compute_weights(**ctx) → dict | None
    optimizer,             # PortfolioOptimizer instance
    execution_engine,      # ExecutionEngine instance
    event_bus=None,        # for RebalanceCycleEvent publication on private bus
    cooldown_s=0.0,        # seconds between rebalance cycles
    name="RebalanceFSM",
)
```

### Public API

| Method | Returns | Description |
|--------|---------|-------------|
| `async request(**ctx)` | `bool` | Request a cycle. Returns `True` if started, `False` if FSM busy. |
| `last_result` | `Optional[RebalanceResult]` | Most recent `RebalanceResult`, or `None`. |

### Pipeline Context Flow

| Stage | Callback | Adds to ctx |
|-------|----------|-------------|
| COMPUTING | `_on_enter_computing` | `target_weights` (raw dict), `strategy_id` |
| VALIDATING | `_on_enter_validating` | `validated_weights` (ValidatedWeights) |
| EXECUTING | `_on_enter_executing` | `rebalance_result` (RebalanceResult) |

After `_on_enter_executing` completes, `lock_for_order()` is called on the ShadowBook for each submitted LIMIT order, then `RebalanceCompleteEvent` is published to the private bus.

### Cooldown

Cooldown uses `asyncio.get_running_loop().call_later(cooldown_s, callback)` to schedule the return to IDLE without blocking the event loop. If `cooldown_s <= 0`, the FSM transitions to IDLE immediately.

---

## Signals Pipeline

**File:** `elysian_core/core/signals.py`

These frozen dataclasses define the typed data contracts flowing between stages.

### `TargetWeights`

```python
@dataclass(frozen=True)
class TargetWeights:
    weights: Dict[str, float]       # symbol → target weight [0, 1] for long-only spot
    timestamp: int                   # epoch ms when signal was generated
    strategy_id: int = 0
    venue: Optional[Venue] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    price_overrides: Optional[Dict[str, float]] = None  # per-symbol limit price overrides
    liquidate: bool = False          # bypass all risk constraints if True
```

### `ValidatedWeights`

```python
@dataclass(frozen=True)
class ValidatedWeights:
    original: TargetWeights          # full audit trail
    weights: Dict[str, float]        # risk-adjusted
    clipped: Dict[str, float]        # per-symbol: original_w - adjusted_w
    rejected: bool = False
    rejection_reason: str = ""
    timestamp: int = 0
    venue: Optional[Venue] = None
```

### `OrderIntent`

```python
@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    venue: Venue
    side: Side
    quantity: float              # base asset quantity (already rounded to step_size)
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None  # None for market orders
    strategy_id: int = 0
```

### `RebalanceResult`

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
    # Used by RebalanceFSM to call shadow_book.lock_for_order()
```

---

## Enums

**File:** `elysian_core/core/enums.py`

### `Side`

| Value | String |
|-------|--------|
| `BUY` | `"Buy"` |
| `SELL` | `"Sell"` |

**Method:** `opposite() -> Side`

### `AssetType`

| Value | String |
|-------|--------|
| `SPOT` | `"Spot"` |
| `PERPETUAL` | `"Perpetuals"` |

### `OrderType`

| Value | String |
|-------|--------|
| `LIMIT` | `"Limit"` |
| `MARKET` | `"Market"` |
| `STOP_MARKET` | `"StopMarket"` |
| `TAKE_PROFIT_MARKET` | `"TakeProfitMarket"` |
| `TRAILING_STOP_MARKET` | `"TrailingStopMarket"` |
| `RANGE` | `"Range"` |
| `OTHERS` | `"OTHERS"` |

### `OrderStatus`

| Value | Terminal | Description |
|-------|----------|-------------|
| `PENDING` | No | Submitted; awaiting exchange ack |
| `OPEN` | No | Resting on the order book |
| `PARTIALLY_FILLED` | No | Partial fill; still open |
| `FILLED` | Yes | Fully executed |
| `CANCELLED` | Yes | Cancelled |
| `REJECTED` | Yes | Exchange rejected; never rested |
| `EXPIRED` | Yes | GTT/IOC lapsed |
| `TRIGGERED` | No | Conditional order trigger hit; now executing |

**Method:** `is_active() -> bool` — True for PENDING, OPEN, PARTIALLY_FILLED, TRIGGERED.

**Binance mapping dicts:**
```python
_BINANCE_SIDE: Dict[str, Side]         # "BUY" → Side.BUY, etc.
_BINANCE_STATUS: Dict[str, OrderStatus] # "NEW" → OPEN, "CANCELED" → CANCELLED, etc.
```

### `Venue`

| Value | String |
|-------|--------|
| `BINANCE` | `"Binance"` |
| `BYBIT` | `"Bybit"` |
| `OKX` | `"OKX"` |
| `ASTER` | `"Aster"` |
| `CETUS` | `"Cetus Protocol"` |
| `TURBOS` | `"Turbos"` |
| `DEEPBOOK` | `"Deepbook"` |

### State Machine States

#### `RebalanceState`

`IDLE`, `COMPUTING`, `VALIDATING`, `EXECUTING`, `COOLDOWN`, `SUSPENDED`, `ERROR`

#### `StrategyState`

`CREATED`, `STARTING`, `READY`, `RUNNING`, `STOPPING`, `STOPPED`, `FAILED`

#### `RunnerState`

`CREATED`, `CONFIGURING`, `CONNECTING`, `SYNCING`, `RUNNING`, `STOPPING`, `STOPPED`, `FAILED`

---

## Constants

**File:** `elysian_core/core/constants.py`

| Constant | Value | Used By |
|----------|-------|---------|
| `STABLECOINS` | `frozenset({"USDT", "USDC", "BUSD"})` | ShadowBook cash accounting, ExecutionEngine stablecoin guard, commission handling |
| `FILL_HISTORY_MAXLEN` | `10_000` | ShadowBook `_fills` deque |
| `QTY_EPSILON` | `1e-12` | ShadowBook `apply_fill()` near-zero quantity guard |

---

## End-to-End Data Flow

```
MarketDataService (separate container)
    │
    ▼ Redis pub/sub
RedisEventBusSubscriber
    │
    +──→ ShadowBook._on_kline()         [updates mark prices, _refresh_derived()]
    │
    +──→ ExecutionEngine._on_kline()    [updates _mark_prices dict]
    │
    +──→ SpotExchangeConnector._on_kline() [updates kline_feeds dict for last_price()]
    │
    +──→ SpotStrategy._dispatch_kline() → on_kline()
               │
               ▼
         RebalanceFSM.request()
           (only if FSM is IDLE)
               │
         [COMPUTING] strategy.compute_weights()
                      → TargetWeights(weights)
               │
         [VALIDATING] optimizer.validate()
                      → ValidatedWeights
               │
         [EXECUTING] engine.execute()
                      → OrderIntent(s) submitted to exchange REST
                      → shadow_book.lock_for_order() for LIMIT orders
                      → RebalanceResult returned
               │
         private_bus.publish(RebalanceCompleteEvent)
               │
         [COOLDOWN] → back to [IDLE]

BinanceUserDataClientManager (private, in-process)
    │
    +──→ private_bus.publish(OrderUpdateEvent)
    │         │
    │         +──→ SpotStrategy._dispatch_order()
    │         │         ├── on_order_update(event)   [strategy hook]
    │         │         └── shadow_book._on_order_update(event)
    │         │               ├── _register_new_order() / sync_from_event()
    │         │               ├── _partial_release_lock()
    │         │               ├── apply_fill() → update Position, cash, realized_pnl
    │         │               └── _finalize_terminal_order() → asyncio.create_task(record_trade)
    │
    +──→ private_bus.publish(BalanceUpdateEvent)
              │
              +──→ SpotStrategy._dispatch_balance()
                        ├── on_balance_update(event)   [strategy hook]
                        └── shadow_book._on_balance_update(event)
                              └── update _cash_dict or position quantity
```

---

## Key Design Invariants

- **Frozen events.** All event dataclasses are immutable. Strategies cannot accidentally mutate shared event data.
- **Incremental fill accounting.** `ShadowBook` uses `ActiveOrder._prev_filled` to compute incremental deltas on each `ORDER_UPDATE` event, preventing double-counting of partial fills.
- **Sells before buys.** The execution engine always submits SELL orders before BUY orders within a cycle to free capital before consuming it.
- **Step-size rounding.** Quantities are rounded down to exchange step size before submission. Zero-quantity intents are filtered.
- **Sub-account isolation.** Each strategy has a dedicated exchange sub-account. All `ORDER_UPDATE` events arrive on the private bus exclusively for that strategy.
- **Bounded collections.** Fill history: `deque(maxlen=10,000)`. All rolling state in strategies should use `deque(maxlen=N)` or `NumpySeries(maxlen=N)`.
- **No blocking I/O in async handlers.** All event handlers are `async def` and contain no synchronous blocking calls.
- **State mutations before event dispatch.** Exchange connectors always update their internal state (sync) before calling `await event_bus.publish()` (async), so strategy hooks see consistent connector state.
