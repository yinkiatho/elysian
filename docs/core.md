# Core Module Documentation

## Overview

The `elysian_core/core/` package is the domain kernel of the Elysian trading framework. It contains every exchange-agnostic abstraction that the rest of the system builds on: the event bus, typed event contracts, order and position models, state machines, portfolio accounting, and the signal pipeline that connects alpha generation to execution.

The module is organized into distinct layers:

| Layer | Files | Responsibility |
|-------|-------|----------------|
| Event infrastructure | `event_bus.py`, `events.py` | In-process pub/sub wiring |
| Market data | `market_data.py` | Order book and OHLCV structures |
| Order model | `order.py`, `order_fsm.py` | Order lifecycle and FSM validation |
| FSM base | `fsm.py` | Generic async state machine |
| Accounting | `position.py`, `shadow_book.py`, `portfolio.py` | Per-strategy and aggregate ledgers |
| Rebalance pipeline | `rebalance_fsm.py`, `signals.py` | Compute → validate → execute cycle |
| Shared types | `enums.py` | All enumerations |

---

## EventBus

**File:** `elysian_core/core/event_bus.py`

The `EventBus` is a lightweight, in-process async publish/subscribe bus. It is the central nervous system of the framework: exchange connectors publish typed events after mutating feed state, and strategy hooks, the portfolio, and the shadow book all receive them through registered async callbacks.

### Design Properties

- **Zero serialization overhead.** All dispatch is via direct async function calls within a single process. No queues, no serialization, no threads.
- **Sequential dispatch with backpressure.** `publish()` awaits each subscriber callback in registration order. If a handler is slow, the emitting worker waits. This prevents unbounded queue growth.
- **Fault isolation.** Exceptions in one callback are caught, logged, and do not prevent remaining callbacks from running.
- **Zero-cost subscription.** Uses `defaultdict(list)` internally, so subscribing to an event type that no one has published is free.

### Class: `EventBus`

```python
from elysian_core.core.event_bus import EventBus
from elysian_core.core.events import EventType

bus = EventBus()
```

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `subscribe` | `(event_type: EventType, callback: Callable)` | Register an async callback for a specific event type. Multiple callbacks per type are supported. |
| `unsubscribe` | `(event_type: EventType, callback: Callable)` | Remove a previously registered callback. No-op if the callback is not found. |
| `publish` | `async (event)` | Dispatch the event to every subscriber registered for `event.event_type`. Awaited sequentially. |

#### Internal State

| Attribute | Type | Description |
|-----------|------|-------------|
| `_subscribers` | `Dict[EventType, List[Callable]]` | Subscription registry, backed by `defaultdict(list)` |

#### Example

```python
bus = EventBus()

async def on_kline(event):
    print(f"Close price: {event.kline.close}")

bus.subscribe(EventType.KLINE, on_kline)

# From a connector worker:
await bus.publish(KlineEvent(symbol="ETHUSDT", venue=Venue.BINANCE, kline=k, timestamp=ts))
```

### Subscription Pattern

Components subscribe during their `start()` method and unsubscribe during `stop()`. The pattern is consistent across `ShadowBook`, `Portfolio` (legacy path), and strategy hooks:

```python
def start(self, event_bus):
    self._event_bus = event_bus
    event_bus.subscribe(EventType.KLINE, self._on_kline)
    event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)

def stop(self):
    self._event_bus.unsubscribe(EventType.KLINE, self._on_kline)
    self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._on_order_update)
```

---

## Event Types

**File:** `elysian_core/core/events.py`

All event classes are frozen dataclasses (`@dataclass(frozen=True)`). The `event_type` field is set automatically via `field(default=..., init=False)` and cannot be overridden at construction time. This prevents strategies from accidentally mutating shared event data.

### `EventType` Enum

| Value | Associated Class | Emitted By |
|-------|-----------------|------------|
| `KLINE` | `KlineEvent` | Kline client manager workers |
| `ORDERBOOK_UPDATE` | `OrderBookUpdateEvent` | OrderBook client manager workers |
| `ORDER_UPDATE` | `OrderUpdateEvent` | User data stream client manager |
| `BALANCE_UPDATE` | `BalanceUpdateEvent` | User data stream client manager |
| `REBALANCE_COMPLETE` | `RebalanceCompleteEvent` | `RebalanceFSM._on_enter_executing` |
| `LIFECYCLE` | `LifecycleEvent` | Strategy and runner state changes |
| `REBALANCE_CYCLE` | `RebalanceCycleEvent` | `RebalanceFSM` on every state transition |

### `KlineEvent`

Emitted when a kline candle closes.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair (e.g. `"ETHUSDT"`) |
| `venue` | `Venue` | Exchange where this candle originated |
| `kline` | `Kline` | OHLCV candle data |
| `timestamp` | `int` | Epoch milliseconds of emission |
| `event_type` | `EventType` | Auto-set to `EventType.KLINE` |

### `OrderBookUpdateEvent`

Emitted on each order book depth update.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange |
| `orderbook` | `OrderBook` | Reference to the live mutable `OrderBook` object |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.ORDERBOOK_UPDATE` |

Note: `orderbook` is a reference to the live object. Copy immediately if you need a snapshot that won't be mutated by subsequent updates.

### `OrderUpdateEvent`

Emitted when an order status changes (new fill, cancel, reject, etc.).

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange |
| `order` | `Order` | Parsed order with current status, cumulative fill state, and commission |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.ORDER_UPDATE` |

### `BalanceUpdateEvent`

Emitted when the account balance changes (fill, deposit, withdrawal).

| Field | Type | Description |
|-------|------|-------------|
| `asset` | `str` | Asset symbol (e.g. `"USDT"`, `"ETH"`) |
| `venue` | `Venue` | Exchange |
| `delta` | `float` | Change in balance (positive = increase, negative = decrease) |
| `new_balance` | `float` | New balance after the change (may not be populated on Binance) |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.BALANCE_UPDATE` |

### `RebalanceCompleteEvent`

Published by `RebalanceFSM._on_enter_executing` after a rebalance cycle finishes. Consumed by strategy `on_rebalance_complete` hooks.

| Field | Type | Description |
|-------|------|-------------|
| `result` | `RebalanceResult` | Full result summary from the execution engine |
| `validated_weights` | `ValidatedWeights` | Risk-adjusted weights that were executed |
| `timestamp` | `int` | Epoch milliseconds |
| `strategy_id` | `int` | Owning strategy identifier (default `0`) |
| `event_type` | `EventType` | Auto-set to `EventType.REBALANCE_COMPLETE` |

### `LifecycleEvent`

Published when a strategy or runner changes its lifecycle state.

| Field | Type | Description |
|-------|------|-------------|
| `component` | `str` | Human-readable component name (e.g. `"EqualWeightStrategy"`) |
| `old_state` | `Any` | Previous `StrategyState` or `RunnerState` enum value |
| `new_state` | `Any` | New state |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.LIFECYCLE` |

### `RebalanceCycleEvent`

Published by `RebalanceFSM` on every state transition. Enables external monitoring and audit trails of the rebalance pipeline.

| Field | Type | Description |
|-------|------|-------------|
| `old_state` | `Any` | Previous `RebalanceState` value |
| `new_state` | `Any` | New state after transition |
| `trigger` | `str` | The trigger string that caused the transition (e.g. `"signal"`, `"accepted"`) |
| `timestamp` | `int` | Epoch milliseconds |
| `metadata` | `dict` | Optional context payload (default empty dict) |
| `event_type` | `EventType` | Auto-set to `EventType.REBALANCE_CYCLE` |

---

## Market Data

**File:** `elysian_core/core/market_data.py`

### `OrderBook`

Normalized order book snapshot. Bids and asks are stored in `SortedDict` containers from the `sortedcontainers` library, enabling O(log n) insertion and O(1) best bid/ask access.

| Field | Type | Description |
|-------|------|-------------|
| `last_update_id` | `int` | Exchange-assigned sequence number for the last applied update |
| `last_timestamp` | `int` | Timestamp of the last update (epoch ms) |
| `ticker` | `str` | Trading pair symbol |
| `interval` | `Optional[str]` | Update interval if applicable, else `None` |
| `bid_orders` | `SortedDict` | Price → quantity map for bids (ascending key order; best bid is last) |
| `ask_orders` | `SortedDict` | Price → quantity map for asks (ascending key order; best ask is first) |

**Class method:**

| Method | Description |
|--------|-------------|
| `from_lists(last_update_id, last_timestamp, ticker, interval, bid_levels, ask_levels)` | Construct from lists of `[price, qty]` pairs |

**Mutation methods:**

| Method | Description |
|--------|-------------|
| `apply_update(side, price, qty)` | Apply a single price level update. If `qty == 0`, removes the level. `side` is `"bid"` or `"ask"`. |

**Computed properties:**

| Property | Description |
|----------|-------------|
| `best_bid_price` | Highest bid price (last key in ascending SortedDict); `0.0` if empty |
| `best_bid_amount` | Quantity at the best bid |
| `best_ask_price` | Lowest ask price (first key); `0.0` if empty |
| `best_ask_amount` | Quantity at the best ask |
| `mid_price` | `(best_bid_price + best_ask_price) / 2` |
| `spread` | `best_ask_price - best_bid_price` |
| `spread_bps` | Spread in basis points: `spread / mid_price * 10_000` |

### Exchange-Specific Subclasses

**`BinanceOrderBook`** and **`AsterOrderBook`** both inherit from `OrderBook` and add async batch update methods:

| Method | Description |
|--------|-------------|
| `async apply_update_lists(side, price_levels)` | Apply a list of `[price, qty]` pairs for one side |
| `async apply_both_updates(last_timestamp, new_update_id, bid_levels, ask_levels)` | Apply bid and ask updates concurrently using `asyncio.gather` |

### `Kline`

Exchange-agnostic OHLCV candlestick.

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `str` | Trading pair symbol |
| `interval` | `str` | Time interval (e.g. `"1m"`, `"1h"`, `"4h"`) |
| `start_time` | `datetime.datetime` | Candle open time |
| `end_time` | `datetime.datetime` | Candle close time |
| `open` | `Optional[float]` | Opening price |
| `high` | `Optional[float]` | Highest price during the interval |
| `low` | `Optional[float]` | Lowest price during the interval |
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
| `symbol` | `str` | required | Trading pair (e.g. `"ETHUSDT"`) |
| `side` | `Side` | required | `Side.BUY` or `Side.SELL` |
| `order_type` | `OrderType` | required | `LIMIT`, `MARKET`, `STOP_MARKET`, etc. |
| `quantity` | `float` | required | Total order quantity in base asset |
| `price` | `Optional[float]` | `None` | Limit price; `None` for market orders |
| `use_quote_order_qty` | `bool` | `False` | If `True`, `quantity` is denominated in quote asset |
| `avg_fill_price` | `float` | `0.0` | Running volume-weighted average fill price |
| `filled_qty` | `float` | `0.0` | Cumulative filled quantity |
| `status` | `OrderStatus` | `PENDING` | Current lifecycle state |
| `timestamp` | `Optional[datetime]` | `None` | Order creation time |
| `venue` | `Optional[Venue]` | `None` | Exchange where the order was placed |
| `commission` | `float` | `0.0` | Latest per-fill commission amount |
| `commission_asset` | `Optional[str]` | `None` | Asset in which commission is charged |
| `last_updated_timestamp` | `Optional[int]` | `None` | Epoch ms of last status update |
| `strategy_id` | `Optional[int]` | `None` | Owning strategy; set by `ExecutionEngine` at placement time |

**Computed properties:**

| Property | Type | Description |
|----------|------|-------------|
| `remaining_qty` | `float` | `max(0.0, quantity - filled_qty)` |
| `is_filled` | `bool` | `True` if `status == FILLED` |
| `is_active` | `bool` | `True` if `status.is_active()` returns `True` |
| `is_terminal` | `bool` | `True` if the order is in a final, non-actionable state |

**Methods:**

| Method | Description |
|--------|-------------|
| `transition_to(new_status)` | Validate and apply an `OrderStatus` transition using `validate_order_transition()`. Operates in warn-mode: invalid transitions are logged but always applied because the exchange is authoritative. |
| `update_fill(filled_qty, fill_price)` | Idempotent fill update. Computes running weighted-average fill price, updates `filled_qty`, and transitions status to `PARTIALLY_FILLED` or `FILLED`. No-op if `filled_qty` has not increased. |

### `LimitOrder`

Liquidity-provider limit order for AMM-style exchanges (e.g. Chainflip). Distinct from the CEX `Order` class.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `int` | Order identifier |
| `base_asset` | `str` | Base asset symbol |
| `quote_asset` | `str` | Quote asset symbol |
| `side` | `Side` | BUY or SELL |
| `price` | `float` | Limit price |
| `amount` | `float` | Order amount |
| `lp_account` | `Optional[str]` | Liquidity provider account |
| `timestamp` | `Optional[datetime]` | Creation time |
| `volatility` | `float` | Volatility parameter for AMM pricing |

### `RangeOrder`

LP range order (concentrated liquidity) for AMM protocols.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `int` | Order identifier |
| `base_asset` | `str` | Base asset symbol |
| `quote_asset` | `str` | Quote asset symbol |
| `lower_price` | `float` | Lower bound of the price range |
| `upper_price` | `float` | Upper bound of the price range |
| `order_type` | `RangeOrderType` | `LIQUIDITY` or `ASSET` |
| `amount` | `Optional[float]` | Total amount to provide |
| `min_amounts` | `Optional[Tuple[float, float]]` | Minimum (base, quote) amounts |
| `max_amounts` | `Optional[Tuple[float, float]]` | Maximum (base, quote) amounts |
| `lp_account` | `Optional[str]` | Liquidity provider account |
| `timestamp` | `Optional[datetime]` | Creation time |

---

## OrderFSM

**File:** `elysian_core/core/order_fsm.py`

The `OrderFSM` module validates `OrderStatus` transitions and provides a per-order lifecycle wrapper. It operates in **warn-mode**: the exchange is the authoritative source of truth, so invalid transitions are logged but always applied.

### Order Lifecycle States

```
                     PENDING
                    /   |   \  \
                   /    |    \  \
               OPEN  REJECTED FILLED  CANCELLED
              / | \ \
             /  |  \ \
  PART_FILLED  FILLED  CANCELLED  EXPIRED  TRIGGERED
     |  \                                  / | \ \ \
     |   FILLED                           /  |  \  \
     CANCELLED                        OPEN PART FILLED CANCELLED EXPIRED
     EXPIRED
```

Full ASCII lifecycle diagram:

```
Normal order:
  PENDING --> OPEN --> PARTIALLY_FILLED --> FILLED
                   |-> PARTIALLY_FILLED --> CANCELLED
                   |-> PARTIALLY_FILLED --> EXPIRED
                   |-> FILLED
                   |-> CANCELLED
                   |-> EXPIRED
                   |-> TRIGGERED --> OPEN --> ...
                                 |-> PARTIALLY_FILLED --> ...
                                 |-> FILLED
                                 |-> CANCELLED
                                 |-> EXPIRED

Instant fill (market order):
  PENDING --> FILLED

Conditional (SL/TP):
  PENDING --> OPEN --> TRIGGERED --> PARTIALLY_FILLED --> FILLED
                                |-> FILLED
                                |-> CANCELLED
```

### Transition Table

| From | Trigger (target) | Notes |
|------|-----------------|-------|
| `PENDING` | `OPEN` | Exchange acknowledged the resting order |
| `PENDING` | `REJECTED` | Exchange rejected outright |
| `PENDING` | `CANCELLED` | Cancelled before exchange ack (client-side) |
| `PENDING` | `FILLED` | Market orders can fill instantly |
| `OPEN` | `PARTIALLY_FILLED` | First partial fill arrived |
| `OPEN` | `FILLED` | Fully filled in one shot |
| `OPEN` | `CANCELLED` | User or system cancelled |
| `OPEN` | `EXPIRED` | GTT/IOC time-in-force lapsed |
| `OPEN` | `TRIGGERED` | Conditional order: trigger price hit |
| `PARTIALLY_FILLED` | `PARTIALLY_FILLED` | Additional partial fills |
| `PARTIALLY_FILLED` | `FILLED` | Final fill completes the order |
| `PARTIALLY_FILLED` | `CANCELLED` | Remaining qty cancelled |
| `PARTIALLY_FILLED` | `EXPIRED` | GTT/IOC remainder expired |
| `TRIGGERED` | `OPEN` | Triggered order placed as resting limit |
| `TRIGGERED` | `PARTIALLY_FILLED` | Triggered order began filling |
| `TRIGGERED` | `FILLED` | Triggered order filled in full |
| `TRIGGERED` | `CANCELLED` | Triggered then cancelled |
| `TRIGGERED` | `EXPIRED` | Triggered but expired (IOC child) |
| `FILLED` | — | Terminal — no outbound transitions |
| `CANCELLED` | — | Terminal — no outbound transitions |
| `REJECTED` | — | Terminal — no outbound transitions |
| `EXPIRED` | — | Terminal — no outbound transitions |

**Terminal states:** `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`

**Conditional order types** (go through `TRIGGERED`): `STOP_MARKET`, `TAKE_PROFIT_MARKET`

### Module-Level Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `validate_order_transition` | `(current, target, order_id="") -> bool` | Returns `True` if the transition is valid; logs a warning and returns `False` otherwise. Always applies the transition regardless. |
| `is_terminal` | `(status: OrderStatus) -> bool` | Returns `True` if the status is a terminal state. |

### `OrderFSM` Class

Per-order lifecycle wrapper providing a clean object-oriented API.

```python
fsm = OrderFSM(OrderStatus.PENDING, order_type=OrderType.STOP_MARKET)
fsm.transition(OrderStatus.OPEN, order_id="sl-001")
fsm.transition(OrderStatus.TRIGGERED, order_id="sl-001")
assert fsm.is_conditional   # True — STOP_MARKET is a conditional type
assert fsm.can_fill         # True — TRIGGERED state accepts fills
assert not fsm.is_terminal  # False — not in a final state
```

**Constructor:** `OrderFSM(initial_status=OrderStatus.PENDING, order_type=None)`

**Methods:**

| Method | Returns | Description |
|--------|---------|-------------|
| `transition(new_status, order_id="")` | `bool` | Validate and apply a transition. Returns `True` if valid. |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `is_terminal` | `bool` | `True` if the current status is in the terminal set |
| `is_active` | `bool` | `True` if `current_status.is_active()` |
| `can_fill` | `bool` | `True` if the order is in `OPEN`, `PARTIALLY_FILLED`, or `TRIGGERED` |
| `is_conditional` | `bool` | `True` if `order_type` is `STOP_MARKET` or `TAKE_PROFIT_MARKET` |

---

## AsyncFSM Base

**File:** `elysian_core/core/fsm.py`

`BaseFSM` is the generic async finite-state machine base class used by both `RebalanceFSM` and any other state-driven component in the system.

### Design

- **Dict-based transition table.** Subclasses declare `_TRANSITIONS` as a class-level dict mapping `(State, trigger_name) -> (NextState, callback_method_name_or_None)`.
- **Wildcard transitions.** Using `"*"` as the state key matches any current state. Explicit state matches take priority over wildcards.
- **Re-entrancy guard.** If a callback triggers another transition while one is in progress, the new trigger is queued and drained after the current transition completes. This prevents recursion deadlocks.
- **EventBus integration.** Optionally accepts an `EventBus` instance for publishing state-change notifications.
- **Terminal states.** A set of terminal states can be provided; triggers fired in terminal states raise `InvalidTransition`.

### `BaseFSM`

**Constructor:**

```python
BaseFSM(
    initial_state: Enum,
    event_bus: Optional[EventBus] = None,
    terminal_states: Optional[Set[Enum]] = None,
    name: Optional[str] = None,
)
```

**Public API:**

| Method/Property | Description |
|----------------|-------------|
| `state` | Read-only property returning the current state enum value |
| `is_terminal` | `True` if the current state is in `_terminal_states` |
| `async trigger(event, **ctx)` | Fire a named trigger. Returns the new state. Raises `InvalidTransition` if no matching transition exists. Context kwargs are passed to the callback method and enriched with `_fsm_old_state`. |

**Internal flow of `trigger()`:**

1. If a transition is already in progress, queue the trigger and return immediately.
2. Resolve `(current_state, trigger_name)` in `_TRANSITIONS`; fall back to `("*", trigger_name)`.
3. Update `_state` to the next state.
4. Inject `_fsm_old_state` into `ctx`.
5. Look up the callback by name on `self` and await it (or call synchronously if not a coroutine).
6. Drain any transitions queued during step 5.

### `InvalidTransition` Exception

Raised when `trigger()` is called with a trigger name that has no matching entry in `_TRANSITIONS` for the current state.

```python
class InvalidTransition(Exception):
    state: Enum    # the state when the trigger was fired
    trigger: str   # the trigger name
```

### Extending `BaseFSM`

```python
from elysian_core.core.fsm import BaseFSM
from enum import Enum, auto

class TrafficColor(Enum):
    RED = auto()
    GREEN = auto()
    YELLOW = auto()

class TrafficLight(BaseFSM):
    _TRANSITIONS = {
        (TrafficColor.RED,    "timer"): (TrafficColor.GREEN,  "_on_green"),
        (TrafficColor.GREEN,  "timer"): (TrafficColor.YELLOW, "_on_yellow"),
        (TrafficColor.YELLOW, "timer"): (TrafficColor.RED,    None),
    }

    def __init__(self):
        super().__init__(initial_state=TrafficColor.RED)

    async def _on_green(self, **ctx):
        print("Go")
```

### `PeriodicTask`

A helper class that wraps a repeating async task, replacing bare `while True` loops.

```python
task = PeriodicTask(callback=portfolio.save_snapshot, interval_s=60, name="snapshot")
task.start()   # non-blocking; runs in background
task.stop()    # cancels the asyncio.Task
```

| Method | Description |
|--------|-------------|
| `start()` | Create a background `asyncio.Task`; no-op if already running |
| `stop()` | Cancel the background task |

**Property:** `running: bool` — `True` if the background task is active and not done.

---

## Position

**File:** `elysian_core/core/position.py`

`Position` is a mutable dataclass tracking a single open position in one asset. It is the leaf node of the accounting hierarchy.

### `Position`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | `str` | required | Trading pair (e.g. `"ETHUSDT"`) |
| `venue` | `Venue` | `Venue.BINANCE` | Exchange this position lives on |
| `quantity` | `float` | `0.0` | Signed quantity: `> 0` is long, `< 0` is short |
| `avg_entry_price` | `float` | `0.0` | Volume-weighted average entry price |
| `realized_pnl` | `float` | `0.0` | Cumulative realized P&L from partial/full closes |
| `total_commission` | `float` | `0.0` | Total fees paid across all fills for this position |

**Methods:**

| Method | Returns | Description |
|--------|---------|-------------|
| `unrealized_pnl(mark_price)` | `float` | `(mark_price - avg_entry_price) * quantity` |
| `total_pnl(mark_price)` | `float` | `realized_pnl + unrealized_pnl(mark_price)` (before commissions) |
| `net_pnl(mark_price)` | `float` | `total_pnl(mark_price) - total_commission` |
| `notional(mark_price)` | `float` | `abs(quantity) * mark_price` |
| `is_flat()` | `bool` | `True` if `quantity == 0.0` |
| `is_long()` | `bool` | `True` if `quantity > 0.0` |
| `is_short()` | `bool` | `True` if `quantity < 0.0` |

---

## Portfolio

**File:** `elysian_core/core/portfolio.py`

`Portfolio` is a thin NAV aggregator across all registered per-strategy `ShadowBook` instances. In the current sub-account mode architecture, `Portfolio` performs no fill attribution of its own — each `ShadowBook` is the authoritative ledger for its strategy. `Portfolio` only reads from shadow books to produce aggregate reporting.

### Design

- One `Portfolio` instance per venue.
- Each strategy's `ShadowBook` is registered via `register_shadow_book()` at startup.
- `Portfolio` owns no event subscriptions and no `_fill_tracker`. It is a pure aggregation read layer.

### `Portfolio`

**Constructor:** `Portfolio(venue: Venue = Venue.BINANCE, cfg: Optional[AppConfig] = None)`

| Parameter | Description |
|-----------|-------------|
| `venue` | The exchange venue this portfolio monitors |
| `cfg` | Application config, used for snapshot persistence |

**Registration:**

| Method | Description |
|--------|-------------|
| `register_shadow_book(shadow_book)` | Register a `ShadowBook` instance. Keyed by `shadow_book.strategy_id`. |

**Aggregation reads:**

| Method/Property | Returns | Description |
|----------------|---------|-------------|
| `aggregate_nav()` | `float` | Sum of `nav` across all registered shadow books |
| `aggregate_cash()` | `float` | Sum of `cash` across all registered shadow books |
| `aggregate_positions()` | `Dict[str, Position]` | Merged position dict; where multiple strategies hold the same symbol, quantities and weighted-average entry prices are summed |
| `total_value(mark_prices=None)` | `float` | Alias for `aggregate_nav()` |
| `nav` | `float` | Property alias for `aggregate_nav()` |
| `cash` | `float` | Property alias for `aggregate_cash()` |
| `name` | `str` | `"Portfolio@{venue.value}"` |

**Persistence:**

| Method | Description |
|--------|-------------|
| `snapshot()` | Returns a dict of the current aggregate state (venue, nav, cash, strategy IDs, positions, per-strategy NAVs) |
| `save_snapshot()` | Persists aggregate state to the database via `PortfolioSnapshot.create()`. Catches and logs exceptions. |

### `Fill` (defined in portfolio.py)

Immutable audit record of a single fill. Used by `ShadowBook` for its fill history ring buffer.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | `str` | required | Trading pair |
| `venue` | `Venue` | required | Exchange |
| `side` | `Side` | required | BUY or SELL |
| `quantity` | `float` | required | Always positive |
| `price` | `float` | required | Fill price |
| `commission` | `float` | `0.0` | Commission paid on this fill |
| `commission_asset` | `str` | `""` | Asset the commission was charged in |
| `timestamp` | `int` | `0` | Epoch ms |
| `order_id` | `str` | `""` | Source order identifier |

---

## ShadowBook

**File:** `elysian_core/core/shadow_book.py`

`ShadowBook` is the per-strategy virtual position and cash ledger. Each strategy owns exactly one `ShadowBook`. It is the primary interface through which a strategy reads "what it owns" — positions, cash, weights, PnL, and outstanding orders — to decide target weights.

### Architecture Role

- **Primary accounting unit.** In sub-account mode, each strategy's `ShadowBook` is the authoritative source for that strategy's positions and cash.
- **Isolated from other strategies.** Orders on the shared exchange flow only into the `ShadowBook` of the strategy that submitted them, routed by sub-account membership.
- **Strategies read their own `ShadowBook`.** `compute_weights()` should read `self._shadow_book.positions`, `self._shadow_book.cash`, etc., not the aggregate `Portfolio`.

### Internal State

| Attribute | Type | Description |
|-----------|------|-------------|
| `_strategy_id` | `int` | Owning strategy identifier |
| `_venue` | `Venue` | Default venue for positions |
| `_positions` | `Dict[str, Position]` | Active positions, keyed by symbol |
| `_cash` | `float` | Available cash (stablecoin total) |
| `_cash_dict` | `Dict[str, float]` | Per-stablecoin balances (USDT, USDC, BUSD) |
| `_mark_prices` | `Dict[str, float]` | Latest known mark price per symbol |
| `_realized_pnl` | `float` | Cumulative realized PnL across all fills |
| `_total_commission` | `float` | Cumulative commissions paid |
| `_fills` | `Deque[Fill]` | Ring buffer of fill audit records (`maxlen=10_000`) |
| `_fill_tracker` | `Dict[str, float]` | `order_id -> cumulative filled_qty` for incremental delta computation |
| `_completed_order_ids` | `Set[str]` | Guards against duplicate terminal cleanup events |
| `_outstanding_orders` | `Dict[str, Order]` | Live orders for this strategy, keyed by `order_id` |
| `_nav` | `float` | Cached NAV from last `_refresh_derived()` call |
| `_weights` | `Dict[str, float]` | Cached weight vector |
| `_peak_equity` | `float` | Highest NAV observed (high-water mark) |
| `_max_drawdown` | `float` | Maximum drawdown fraction observed |
| `_locked_cash` | `float` | Cash reserved for outstanding LIMIT BUY orders |
| `_locked_quantities` | `Dict[str, float]` | Per-symbol quantities reserved for LIMIT SELL orders |
| `_locked_cash_per_order` | `Dict[str, float]` | Per-order lock amounts (BUY) |
| `_locked_qty_per_order` | `Dict[str, float]` | Per-order lock quantities (SELL) |
| `_pending_order_id_to_intent` | `Dict[str, OrderIntent]` | Maps order_id to its `OrderIntent` for lock management |

### Initialization Paths

| Method | When to Use |
|--------|-------------|
| `init_from_portfolio_cash(portfolio_cash, mark_prices)` | Fresh deployment; no prior positions. Sets `_cash` and initializes derived metrics. |
| `init_from_existing(portfolio_cash, portfolio_positions, mark_prices)` | Restart with pre-existing positions. Distributes both cash and positions. |
| `sync_from_exchange(exchange, feeds)` | Sub-account mode startup. Reads real stablecoin balances, open positions, and outstanding orders directly from the exchange connector. |

### Lifecycle

| Method | Description |
|--------|-------------|
| `start(event_bus, sub_account_mode, private_event_bus)` | Subscribe to `KLINE` on the shared bus. Subscribe to `ORDER_UPDATE` and `BALANCE_UPDATE` on the private bus (when sub-account mode is active). |
| `stop()` | Unsubscribe from all event buses. |

### Event Handlers

**`_on_kline(event)`:** Updates `_mark_prices[symbol] = event.kline.close` and calls `_refresh_derived()`. Only processes events for the matching venue where `close > 0`.

**`_on_order_update(event)`:**
1. Calls `_update_order(order)` to apply FSM-validated status transitions on `_outstanding_orders`.
2. Skips non-fill statuses (`PENDING`, `OPEN`, etc.), handling terminal cleanup on those paths.
3. Computes incremental fill delta: `delta_filled = order.filled_qty - _fill_tracker.get(order_id, 0.0)`.
4. Calls `_partial_release_lock(order_id, delta_filled)` to proportionally release LIMIT order balance reservations.
5. Calls `apply_fill(symbol, qty_delta, price, commission, venue)`.
6. On terminal orders: moves `order_id` to `_completed_order_ids`, cleans up `_fill_tracker`, calls `release_order_lock(order_id)`.

**`_on_balance_update(event)`:** Updates stablecoin cash totals for known stablecoins (`USDT`, `USDC`, `BUSD`); for non-stablecoins, maps the raw asset to its trading symbol and updates the position quantity.

### Fill Attribution: `apply_fill()`

`apply_fill(symbol, qty_delta, price, commission, venue)` is the primary method for recording a fill.

- `qty_delta > 0` means BUY (position increases); `< 0` means SELL.
- **Same direction:** computes new weighted-average entry price.
- **Opposing direction (partial or full close):** computes realized PnL = `(fill_price - avg_entry_price) * closing_qty * direction_sign`. If the position flips, the new entry price is set to `price`.
- Flat positions (`quantity == 0.0` after fill) are removed from `_positions`.
- Appends a `Fill` record to the `_fills` ring buffer.
- Calls `_refresh_derived()`.

### Lock Management

LIMIT orders require balance reservations to prevent over-allocation before fills arrive.

| Method | Description |
|--------|-------------|
| `lock_for_order(order_id, intent)` | Reserve `quantity * price` cash (BUY) or `quantity` base asset (SELL) for the submitted limit order. Idempotent. No-op for MARKET orders. |
| `_partial_release_lock(order_id, delta_filled)` | Release lock proportionally as partial fills arrive. Called on each fill event. |
| `release_order_lock(order_id)` | Release any remaining lock for a terminal order (FILLED, CANCELLED, EXPIRED, REJECTED). |

**Read properties:**

| Property | Description |
|----------|-------------|
| `free_cash` | `max(0, cash - _locked_cash)` — cash available for new orders |
| `free_quantity(symbol)` | `max(0, position.quantity - _locked_quantities[symbol])` — base qty available for new SELL orders |

### Outstanding Orders

`_update_order(order)` maintains `_outstanding_orders` using FSM-validated transitions:
- New orders not yet tracked: stored if not terminal.
- Existing tracked orders: `update_fill()` or `transition_to()` applied to the tracked instance.
- Terminal orders: removed from `_outstanding_orders`.

**Property:** `outstanding_orders: Dict[str, Order]` — returns a copy of the live orders dict.

### Read API

| Method/Property | Returns | Description |
|----------------|---------|-------------|
| `position(symbol)` | `Position` | Returns the position or a flat placeholder if not held |
| `positions` | `Dict[str, Position]` | Copy of all positions |
| `active_positions` | `Dict[str, Position]` | Positions where `not is_flat()` |
| `cash` | `float` | Total cash (stablecoin sum) |
| `nav` | `float` | Cached NAV from last `_refresh_derived()` |
| `weights` | `Dict[str, float]` | Cached weight vector |
| `mark_prices` | `Dict[str, float]` | Latest known prices |
| `total_value(mark_prices)` | `float` | `cash + sum(position_notional)` |
| `current_weights(mark_prices)` | `Dict[str, float]` | Live weight vector computed on demand |
| `unrealized_pnl(mark_prices)` | `float` | Sum of unrealized PnL across all positions |
| `gross_exposure(mark_prices)` | `float` | Sum of `abs(weight)` |
| `net_exposure(mark_prices)` | `float` | Sum of signed weights |
| `long_exposure(mark_prices)` | `float` | Sum of positive weights |
| `short_exposure(mark_prices)` | `float` | Sum of `abs(negative weights)` |
| `cash_weight(mark_prices)` | `float` | `cash / nav` |
| `net_pnl(mark_prices)` | `float` | `realized_pnl + unrealized_pnl - total_commission` |
| `fills` | `List[Fill]` | Copy of the fill ring buffer |
| `realized_pnl` | `float` | Cumulative realized PnL |
| `total_commission` | `float` | Total fees paid |
| `peak_equity` | `float` | High-water mark NAV |
| `max_drawdown` | `float` | Maximum drawdown fraction observed |
| `current_drawdown(mark_prices)` | `float` | `(peak_equity - nav) / peak_equity` |

### `_refresh_derived()`

Called after every fill and mark price update. Recomputes:
- `_nav` via `total_value()`
- `_weights`: `(position.quantity * mark_price) / nav` for non-flat positions with known mark prices
- `_peak_equity`: updated if `_nav > _peak_equity`
- `_max_drawdown`: updated if the current drawdown exceeds the prior maximum

Only positions with entries in `_mark_prices` contribute to weights (stale prices are excluded).

### Persistence

| Method | Description |
|--------|-------------|
| `snapshot()` | Returns a dict with strategy_id, cash, nav, PnL, commissions, peak equity, max drawdown, positions, and weights |
| `save_snapshot()` | Persists the full snapshot to the database via `PortfolioSnapshot.create()` |

---

## RebalanceFSM

**File:** `elysian_core/core/rebalance_fsm.py`

`RebalanceFSM` drives the compute → validate → execute → cooldown pipeline. It extends `BaseFSM` and is the coordinator between the strategy, the risk optimizer, and the execution engine.

### Design Principles

- **Pure state machine.** The FSM owns no timer or scheduler. The strategy decides when to trigger a cycle by calling `request()`.
- **Context threading.** Each pipeline stage enriches a shared `ctx` dict and passes it to the next stage via `trigger(**ctx)`.
- **EventBus observability.** A `RebalanceCycleEvent` is published on every state transition.
- **Cooldown via `loop.call_later`.** After execution completes, the FSM enters COOLDOWN and uses `asyncio.get_running_loop().call_later()` to schedule the return to IDLE without blocking the event loop.

### States

```
                      +----------+
              +------>|   IDLE   |<------+
              |       +----------+       |
              |            | signal      |
              |            v             |
              |      +-----------+       |
              |      | COMPUTING |       |
              |      +-----------+       |
              |     / weights_ready      |
              |    /   no_signal --> IDLE|
              |   v                      |
              | +-----------+            |
              | | VALIDATING|            |
              | +-----------+            |
              |  / accepted              |
              | /   rejected ---> COOLDOWN
              |v                        |
          +-----------+   complete --> COOLDOWN --> cooldown_done --|
          | EXECUTING |                  ^                          |
          +-----------+                  |                          |
               |   failed               | rejected                 |
               v                        |---------------------------|
           +-------+   retry --> IDLE
           | ERROR |
           +-------+

           Any state --[suspend]--> SUSPENDED --[resume]--> IDLE
```

| State | Description |
|-------|-------------|
| `IDLE` | Waiting for a rebalance trigger. Only `request()` can initiate a cycle. |
| `COMPUTING` | `compute_weights()` is executing on the strategy. |
| `VALIDATING` | The optimizer is validating/adjusting the computed weights. |
| `EXECUTING` | The execution engine is submitting orders. |
| `COOLDOWN` | Post-execution wait period before the next cycle is allowed. |
| `SUSPENDED` | All rebalancing paused (wildcard transition from any state). |
| `ERROR` | Execution failed. Auto-retries after cooldown. |

### Transition Table

| From | Trigger | To | Callback |
|------|---------|----|----------|
| `IDLE` | `"signal"` | `COMPUTING` | `_on_enter_computing` |
| `COMPUTING` | `"weights_ready"` | `VALIDATING` | `_on_enter_validating` |
| `COMPUTING` | `"no_signal"` | `IDLE` | None |
| `VALIDATING` | `"accepted"` | `EXECUTING` | `_on_enter_executing` |
| `VALIDATING` | `"rejected"` | `COOLDOWN` | `_on_enter_cooldown` |
| `EXECUTING` | `"complete"` | `COOLDOWN` | `_on_enter_cooldown` |
| `EXECUTING` | `"failed"` | `ERROR` | `_on_enter_error` |
| `COOLDOWN` | `"cooldown_done"` | `IDLE` | None |
| `ERROR` | `"retry"` | `IDLE` | None |
| `*` (any) | `"suspend"` | `SUSPENDED` | `_on_enter_suspended` |
| `SUSPENDED` | `"resume"` | `IDLE` | `_on_resume` |

### Constructor

```python
RebalanceFSM(
    strategy,              # must implement compute_weights(**ctx)
    optimizer,             # PortfolioOptimizer instance
    execution_engine,      # ExecutionEngine instance
    event_bus=None,        # for RebalanceCycleEvent publication
    cooldown_s=0.0,        # seconds between rebalance cycles
    name="RebalanceFSM",
)
```

### Public API

| Method | Returns | Description |
|--------|---------|-------------|
| `async request(**ctx)` | `bool` | Request a rebalance cycle. Returns `True` if the cycle was triggered (FSM was IDLE). Returns `False` if the FSM is busy; the request is silently dropped. |
| `last_result` | `Optional[RebalanceResult]` | The most recent `RebalanceResult`, or `None` if no cycle has completed. |

### Pipeline Context Flow

Each callback stage reads from `ctx` and enriches it before passing downstream:

| Stage | Reads | Adds |
|-------|-------|------|
| `_on_enter_computing` | — | `ctx["target_weights"]`, `ctx["strategy_id"]` |
| `_on_enter_validating` | `ctx["target_weights"]` | `ctx["validated_weights"]` |
| `_on_enter_executing` | `ctx["validated_weights"]` | `ctx["rebalance_result"]` |

After `_on_enter_executing`, a `RebalanceCompleteEvent` is published to the EventBus before triggering `"complete"`.

### Lock Integration

After `execution_engine.execute()` returns, `_on_enter_executing` iterates over `result.submitted_orders` and calls `shadow_book.lock_for_order(order_id, intent)` for each submitted limit order. This reserves balance in the shadow book to prevent over-allocation before fills arrive.

---

## Signals Pipeline

**File:** `elysian_core/core/signals.py`

These frozen dataclasses define the typed data contracts flowing between the three pipeline stages (Strategy → Risk → Execution).

```
Strategy.compute_weights() --> TargetWeights
        |
        v
PortfolioOptimizer.validate() --> ValidatedWeights
        |
        v
ExecutionEngine.execute() --> RebalanceResult
                         (uses OrderIntent internally)
```

### `TargetWeights`

Raw alpha output from a strategy.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `weights` | `Dict[str, float]` | required | Symbol → target weight. Cash weight is implicit: `1.0 - sum(values)`. For long-only spot: `[0, 1]` range, sum ≤ 1.0. |
| `timestamp` | `int` | required | Epoch ms when the signal was generated |
| `strategy_id` | `int` | `0` | Owning strategy identifier |
| `venue` | `Optional[Venue]` | `None` | Target venue; `None` means use default |
| `metadata` | `Dict[str, Any]` | `{}` | Arbitrary strategy-specific context |
| `price_overrides` | `Optional[Dict[str, float]]` | `None` | Per-symbol limit price overrides; if set, these are used instead of mark prices for order pricing |

### `ValidatedWeights`

Risk-adjusted weights output from `PortfolioOptimizer`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `original` | `TargetWeights` | required | The original signal (full audit trail) |
| `weights` | `Dict[str, float]` | required | Risk-adjusted weights after clipping and constraint enforcement |
| `clipped` | `Dict[str, float]` | required | Per-symbol clip amounts: `original_weight - adjusted_weight` |
| `rejected` | `bool` | `False` | `True` if the optimizer rejected the entire signal |
| `rejection_reason` | `str` | `""` | Human-readable reason for rejection |
| `timestamp` | `int` | `0` | Epoch ms when validation completed |

### `OrderIntent`

A single order to be submitted, computed by `ExecutionEngine.compute_order_intents()`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | `str` | required | Trading pair |
| `venue` | `Venue` | required | Target exchange |
| `side` | `Side` | required | BUY or SELL |
| `quantity` | `float` | required | Base asset quantity (already rounded to step size) |
| `order_type` | `OrderType` | `MARKET` | Order type to use |
| `price` | `Optional[float]` | `None` | Limit price; `None` for market orders |
| `strategy_id` | `int` | `0` | Owning strategy — used for shadow book routing |

### `RebalanceResult`

Summary returned by `ExecutionEngine.execute()` after a rebalance cycle.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `intents` | `Tuple[OrderIntent, ...]` | required | All order intents that were computed (including failed) |
| `submitted` | `int` | required | Number of orders successfully submitted |
| `failed` | `int` | required | Number of orders that failed to submit |
| `timestamp` | `int` | required | Epoch ms when the cycle completed |
| `errors` | `Tuple[str, ...]` | `()` | Human-readable error strings for failed orders |
| `submitted_orders` | `Dict[str, OrderIntent]` | `{}` | `order_id -> OrderIntent` for successfully submitted limit orders (used by shadow book lock management) |

---

## Execution Engine

**File:** `elysian_core/execution/engine.py`

The `ExecutionEngine` (Stage 5) converts `ValidatedWeights` into exchange orders. It is instantiated per-venue.

### Pipeline

```
ValidatedWeights
    --> snapshot mark prices from feeds
    --> compute total portfolio value
    --> for each symbol: target_qty = weight * total_value / price
    --> delta = target_qty - current_qty
    --> round to step_size, filter near-zero deltas
    --> sell orders first (free capital), then buys
    --> return RebalanceResult
```

### Constructor

```python
ExecutionEngine(
    exchanges: Dict[Venue, SpotExchangeConnector],
    portfolio: Union[Portfolio, ShadowBook],
    feeds: Dict[str, AbstractDataFeed],
    default_venue: Venue = Venue.BINANCE,
    default_order_type: OrderType = OrderType.MARKET,
    symbol_venue_map: Optional[Dict[str, Venue]] = None,
    cfg: Optional[Any] = None,
    asset_type: AssetType = None,
    venue: Venue = None,
)
```

**Key internal state:**

| Attribute | Description |
|-----------|-------------|
| `_order_strategy_map: Dict[str, int]` | `order_id -> strategy_id`; populated on placement, read by `ShadowBook` for routing in shared-account mode |
| `_execute_lock: asyncio.Lock` | Prevents concurrent `execute()` calls from multiple strategy FSMs |

### `execute(validated, **ctx) -> RebalanceResult`

The primary async entry point. Protected by `_execute_lock`. Snapshots mark prices, computes intents, submits sells first then buys, builds `_order_strategy_map`, calls `portfolio.lock_for_order()` for each submitted order, and returns a `RebalanceResult`.

### `compute_order_intents(validated, mark_prices, order_type) -> List[OrderIntent]`

Pure computation with no side effects:
1. Reads `portfolio.total_value(mark_prices)`.
2. For each symbol in `validated.weights`: computes `target_qty = target_weight * total_value / price`.
3. Computes `delta_qty = target_qty - current_qty`.
4. Rounds `abs(delta_qty)` to the exchange step size via `_round_step()`.
5. Filters out zero-quantity intents.
6. Uses `price_overrides` from `TargetWeights` if provided, otherwise uses the mark price.
7. Sets `price=None` for MARKET orders, or `price=price` for LIMIT orders.

### Helper: `_round_step(value, step) -> float`

Rounds `value` down to the nearest multiple of `step`. Returns `value` unchanged if `step <= 0`.

---

## Enums

**File:** `elysian_core/core/enums.py`

### Order / Execution Enums

#### `Side`

| Value | String | Description |
|-------|--------|-------------|
| `BUY` | `"Buy"` | Buy / long direction |
| `SELL` | `"Sell"` | Sell / short direction |

**Method:** `opposite() -> Side` — returns the opposing side.

#### `AssetType`

| Value | String |
|-------|--------|
| `SPOT` | `"Spot"` |
| `PERPETUAL` | `"Perpetuals"` |

#### `OrderType`

| Value | String | Description |
|-------|--------|-------------|
| `LIMIT` | `"Limit"` | Resting limit order |
| `MARKET` | `"Market"` | Market order; immediate fill at best available price |
| `STOP_MARKET` | `"StopMarket"` | Stop-market order; triggers on price condition |
| `TAKE_PROFIT_MARKET` | `"TakeProfitMarket"` | Take-profit market; triggers on profit condition |
| `TRAILING_STOP_MARKET` | `"TrailingStopMarket"` | Trailing stop market |
| `RANGE` | `"Range"` | AMM / LP range order (concentrated liquidity) |
| `OTHERS` | `"OTHERS"` | Unclassified types |

#### `OrderStatus`

| Value | String | Terminal | Description |
|-------|--------|----------|-------------|
| `PENDING` | `"Pending"` | No | Submitted to exchange; awaiting acknowledgement |
| `OPEN` | `"Open"` | No | Resting on the exchange order book |
| `PARTIALLY_FILLED` | `"PartiallyFilled"` | No | One or more partial fills received; still open |
| `FILLED` | `"Filled"` | Yes | Fully executed |
| `CANCELLED` | `"Cancelled"` | Yes | Cancelled by user, system, or exchange |
| `REJECTED` | `"Rejected"` | Yes | Exchange rejected the order; it never rested |
| `EXPIRED` | `"Expired"` | Yes | GTT/IOC order that lapsed without full fill |
| `TRIGGERED` | `"Triggered"` | No | Conditional order whose trigger condition was met; now executing |

**Method:** `is_active() -> bool` — returns `True` for `PENDING`, `OPEN`, `PARTIALLY_FILLED`, `TRIGGERED`.

**Binance mapping helpers** (module-level dicts in enums.py):

- `_BINANCE_SIDE: Dict[str, Side]` — maps `"BUY"/"SELL"` to `Side` enum
- `_BINANCE_STATUS: Dict[str, OrderStatus]` — maps Binance status strings (`"NEW"`, `"PARTIALLY_FILLED"`, etc.) to `OrderStatus`

### Venue / Chain Enums

#### `Venue`

| Value | String |
|-------|--------|
| `BINANCE` | `"Binance"` |
| `BYBIT` | `"Bybit"` |
| `OKX` | `"OKX"` |
| `CETUS` | `"Cetus Protocol"` |
| `TURBOS` | `"Turbos"` |
| `DEEPBOOK` | `"Deepbook"` |
| `ASTER` | `"Aster"` |

#### `Chain`

| Value | String |
|-------|--------|
| `SUI` | `"sui"` |
| `TON` | `"ton"` |
| `ETHEREUM` | `"ethereum"` |
| `BITCOIN` | `"bitcoin"` |
| `SOLANA` | `"solana"` |

**Method:** `to_dict() -> dict` — returns `{"chain": self.value}`.

### Trade / Analytics Enums

#### `TradeType`

| Value | String |
|-------|--------|
| `REAL` | `"Real"` |
| `SIMULATED` | `"Simulated"` |

#### `TradeSubtype`

| Value | String |
|-------|--------|
| `SWAP` | `"Swap"` |

#### `CashflowSubType`

| Value | String |
|-------|--------|
| `SWAP_IN` | `"TransferIn"` |
| `SWAP_OUT` | `"TransferOut"` |
| `GAS_FEE` | `"GasFee"` |
| `FEE_REBATE` | `"FeeRebate"` |

### DEX / On-Chain Enums

#### `SwapType`

| Value | String |
|-------|--------|
| `BLUEFIN` | `"Bluefin"` |
| `CETUS` | `"Cetus"` |
| `FLOW_X` | `"FlowX"` |
| `TURBOS` | `"Turbos"` |

#### `WalletType`

| Value | String |
|-------|--------|
| `WALLET` | `"sui_wallet"` |
| `CEX` | `"cex"` |

#### `RangeOrderType`

| Value | Int | Description |
|-------|-----|-------------|
| `LIQUIDITY` | `1` | Range order specified by liquidity amount |
| `ASSET` | `2` | Range order specified by asset amounts |

### State Machine States

#### `RebalanceState`

States for `RebalanceFSM`.

| Value | Description |
|-------|-------------|
| `IDLE` | Waiting for next rebalance trigger |
| `COMPUTING` | `compute_weights()` executing |
| `VALIDATING` | Optimizer running risk validation |
| `EXECUTING` | Orders being submitted to exchange |
| `COOLDOWN` | Post-execution cooldown in progress |
| `SUSPENDED` | All rebalancing paused |
| `ERROR` | Execution error; awaiting retry |

#### `StrategyState`

Lifecycle states for `SpotStrategy`.

| Value | Description |
|-------|-------------|
| `CREATED` | Object constructed, not yet started |
| `STARTING` | Performing startup initialization |
| `READY` | Ready to begin execution |
| `RUNNING` | Actively processing events and rebalancing |
| `STOPPING` | Shutdown initiated |
| `STOPPED` | Fully stopped |
| `FAILED` | Unrecoverable error |

#### `RunnerState`

Lifecycle states for `StrategyRunner`.

| Value | Description |
|-------|-------------|
| `CREATED` | Runner constructed |
| `CONFIGURING` | Loading configuration |
| `CONNECTING` | Establishing exchange connections |
| `SYNCING` | Syncing state from exchange |
| `RUNNING` | Active |
| `STOPPING` | Shutdown initiated |
| `STOPPED` | Fully stopped |
| `FAILED` | Unrecoverable error |

---

## End-to-End Data Flow

```
Exchange WebSocket
      |
      v
Connector Worker (kline/orderbook data)
      |
      v
EventBus.publish(KlineEvent / OrderBookUpdateEvent)
      |
      +---> ShadowBook._on_kline()         [updates mark prices, _refresh_derived()]
      |
      +---> BaseStrategy.on_kline()        [user-defined hook]
                  |
                  v
          RebalanceFSM.request()
            (only if FSM is IDLE)
                  |
                  v
          [COMPUTING] strategy.compute_weights()
                       --> TargetWeights
                  |
                  v
          [VALIDATING] optimizer.validate()
                       --> ValidatedWeights
                  |
                  v
          [EXECUTING] engine.execute()
                       --> OrderIntent(s) submitted
                       --> _order_strategy_map populated
                       --> shadow_book.lock_for_order() called
                       --> RebalanceResult returned
                  |
                  v
          EventBus.publish(RebalanceCompleteEvent)
                  |
                  v
          [COOLDOWN] then back to [IDLE]

Exchange WebSocket (user data stream)
      |
      v
EventBus.publish(OrderUpdateEvent)   [on private per-strategy bus]
      |
      +---> ShadowBook._on_order_update()
                  |  - _update_order(): FSM-validated status transition
                  |  - incremental delta via _fill_tracker
                  |  - _partial_release_lock()
                  |  - apply_fill() -> updates Position, realized_pnl
                  |  - _refresh_derived() -> updates nav, weights, drawdown
                  v
          Portfolio.aggregate_nav() reads from ShadowBook
```

---

## Key Design Invariants

- **Frozen events.** All event dataclasses are immutable. Strategies cannot accidentally mutate shared event data.
- **Incremental fill accounting.** Both `ShadowBook` and `Portfolio` use `_fill_tracker` to prevent double-counting partial fills across multiple `ORDER_UPDATE` events for the same order.
- **Sells before buys.** The execution engine always submits SELL orders before BUY orders within a rebalance cycle to free capital before consuming it.
- **Step-size rounding.** Quantities are rounded to exchange step size before submission. Zero-quantity intents are filtered.
- **Sub-account isolation.** In sub-account mode, each strategy has a dedicated exchange sub-account. Order updates flow through a private `EventBus`, so all `ORDER_UPDATE` events on a strategy's private bus belong to that strategy without needing `_order_strategy_map` routing.
- **Bounded collections.** The `ShadowBook` fill history uses `deque(maxlen=10_000)` to prevent unbounded memory growth.
- **No blocking I/O in async handlers.** All `_on_kline`, `_on_order_update`, and `_on_balance_update` handlers are `async def` and contain no synchronous blocking calls.
