# Strategy Documentation

## Overview

The strategy module provides a hook-based, event-driven framework for implementing spot trading strategies. The core class is `SpotStrategy`, which subscribes to an `EventBus` and receives typed events (kline, orderbook, order, balance) through async hook functions. Strategies override only the hooks they need — all others are no-op by default.

## Key Classes

### SpotStrategy (base_strategy.py)

**Event-driven base class for spot trading strategies. Subclass and override `on_*` hooks.**

#### Constructor

```python
SpotStrategy(
    exchanges: Dict[Venue, SpotExchangeConnector],
    event_bus: EventBus,
    feeds: Optional[Dict[str, AbstractDataFeed]] = None,
    max_heavy_workers: int = 4,
    strategy_id: int = 0,
    strategy_name: str = "",
    optimizer: Optional[PortfolioOptimizer] = None,
    execution_engine: Optional[ExecutionEngine] = None,
)
```

- `exchanges`: Map of venue → exchange connector for order execution
- `event_bus`: Shared EventBus instance (injected by StrategyRunner)
- `feeds`: Optional map of symbol → data feed for price lookups
- `max_heavy_workers`: Number of ProcessPoolExecutor workers for `run_heavy()`
- `strategy_id`: Integer identifier used for ShadowBook routing and `_order_strategy_map` keying
- `strategy_name`: Human-readable label for logging
- `optimizer`: `PortfolioOptimizer` injected by StrategyRunner during pipeline setup
- `execution_engine`: `ExecutionEngine` injected by StrategyRunner during pipeline setup

**Note:** When using `StrategyRunner.run(strategies=[...], strategy_ids=[...])`, the runner re-wires `strategy._event_bus`, `strategy._exchanges`, `strategy._optimizer`, and `strategy._execution_engine` before calling `start()`. Initial constructor values are placeholders.

#### Lifecycle Methods

- `start()`: Registers all `_dispatch_*` callbacks with EventBus, then calls `on_start()`. Called by the runner.
- `stop()`: Unsubscribes from EventBus, shuts down ProcessPoolExecutor, calls `on_stop()`. Called by the runner on shutdown.
- `run_forever()`: Optional long-running coroutine. If overridden, runs concurrently with feed tasks via `asyncio.gather()`. Default implementation returns immediately.

#### Hook Functions (Override These)

All hooks are `async def` with default no-op implementations. Override only what you need.

| Hook | Event Type | Frequency | Description |
|------|-----------|-----------|-------------|
| `on_start()` | — | Once | Called after EventBus subscription. Initialize strategy state here. |
| `on_stop()` | — | Once | Called on shutdown. Cleanup, log totals, persist state. |
| `on_kline(event: KlineEvent)` | KLINE | ~1/sec per symbol | Fired on each closed kline candle. Access OHLCV via `event.kline`. |
| `on_orderbook_update(event: OrderBookUpdateEvent)` | ORDERBOOK_UPDATE | ~10/sec per symbol | Fired on each depth update. Access bids/asks via `event.orderbook`. Keep fast or use `run_heavy()`. |
| `on_order_update(event: OrderUpdateEvent)` | ORDER_UPDATE | On order state change | Fired on fills, cancels, rejects. Access order via `event.order`. |
| `on_balance_update(event: BalanceUpdateEvent)` | BALANCE_UPDATE | On balance change | Fired on balance delta. Access `event.asset`, `event.delta`. |

#### Internal Dispatch (Error Isolation)

Each hook is wrapped in a `_dispatch_*` method that catches exceptions, logs them, and prevents errors from propagating to the EventBus or workers:

```python
async def _dispatch_kline(self, event: KlineEvent):
    try:
        await self.on_kline(event)
    except Exception as e:
        logger.error(f"SpotStrategy.on_kline error: {e}", exc_info=True)
```

The EventBus subscribes to `_dispatch_kline`, not `on_kline` directly. This ensures a strategy bug never crashes the feed worker.

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `strategy_id` | `int` | Owning strategy identifier; used for ShadowBook routing and `_order_strategy_map` |
| `strategy_name` | `str` | Human-readable label used in log output |
| `shadow_book` | `ShadowBook` | Per-strategy virtual position ledger; read-only view of attributed positions, cash, weights, and PnL |
| `rebalance_fsm` | `RebalanceFSM` | The FSM instance driving the compute → validate → execute → cooldown cycle |
| `strategy_config` | `Optional[StrategyConfig]` | Per-strategy config loaded from `strategies/strategy_NNN.yaml`; holds `risk_overrides`, `execution_overrides`, `portfolio_overrides` |

#### Exchange & Feed Access

- `get_exchange(venue=Venue.BINANCE) -> SpotExchangeConnector`: Returns the exchange connector for order execution.
- `get_feed(symbol: str) -> Optional[AbstractDataFeed]`: Returns the data feed for a symbol, or None.

#### Price Helpers

- `get_current_price(pair: str, side: Optional[Side] = None) -> Optional[float]`

  Resolution order:
  1. Direct feed lookup (bid/ask/mid depending on side)
  2. Inverted pair (e.g., USDTETH → 1/ETHUSDT)
  3. Stablecoin proxy (e.g., ETHUSDC → ETHUSDT if USDC feed missing)
  4. Synthetic via USDT legs (e.g., ETHBTC → ETHUSDT / BTCUSDT)

- `get_current_vol(symbol: str) -> Optional[float]`: Returns annualised rolling volatility in basis points, or None.

#### Heavy Compute Offloading

```python
result = await self.run_heavy(fn, *args)
```

Runs `fn(*args)` in a `ProcessPoolExecutor` (separate OS process). Returns the function's result.

**Constraints:**
- `fn` must be a **top-level picklable function** (not a lambda, method, or closure)
- Arguments must be picklable
- Results must be picklable

### TestPrintStrategy (test_strategy.py)

**Verification strategy that prints a one-liner for every event received. No trading logic.**

Used to verify EventBus wiring end-to-end. Tracks event counts and logs totals on stop.

```python
class TestPrintStrategy(SpotStrategy):
    async def on_start(self):
        # Initialize counters
        self._kline_count = 0
        self._ob_count = 0
        self._order_count = 0
        self._balance_count = 0

    async def on_stop(self):
        # Log totals

    async def on_kline(self, event: KlineEvent):
        # Log OHLC for every kline

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        # Log every 50th OB update (throttled to avoid flooding)

    async def on_order_update(self, event: OrderUpdateEvent):
        # Log order details

    async def on_balance_update(self, event: BalanceUpdateEvent):
        # Log balance delta
```

### Processor (processor.py) — Legacy

Event processing and signal generation component from the older pull-based architecture. Still functional but not used by the new `SpotStrategy` event-driven flow.

### StrategyEngine (strategy_engine.py) — Legacy

Abstract base class with `on_event()` from the older architecture. Superseded by `SpotStrategy` for new strategies but preserved for backward compatibility.

## Strategy Implementation Guide

### Minimal Strategy

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent

class MinimalStrategy(SpotStrategy):
    async def on_kline(self, event: KlineEvent):
        print(f"{event.symbol} close={event.kline.close}")
```

That's it. All other hooks are no-op. The runner handles wiring, startup, and shutdown.

### Full Strategy Example

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import (
    KlineEvent, OrderBookUpdateEvent, OrderUpdateEvent, BalanceUpdateEvent
)
from elysian_core.core.enums import Venue, Side
import asyncio

# Top-level function for run_heavy() — must be picklable
def compute_signal(prices: list) -> float:
    """CPU-intensive signal calculation."""
    import numpy as np
    returns = np.diff(prices) / prices[:-1]
    return float(np.mean(returns) / np.std(returns))  # Sharpe-like metric


class MyArbStrategy(SpotStrategy):
    async def on_start(self):
        self.prices = {"ETHUSDT": [], "BTCUSDT": []}
        self.position = {}
        logger.info("ArbStrategy started")

    async def on_kline(self, event: KlineEvent):
        if event.symbol in self.prices:
            self.prices[event.symbol].append(event.kline.close)

            # Keep last 100 prices
            if len(self.prices[event.symbol]) > 100:
                self.prices[event.symbol] = self.prices[event.symbol][-100:]

            # Offload heavy computation
            if len(self.prices[event.symbol]) >= 60:
                signal = await self.run_heavy(
                    compute_signal, self.prices[event.symbol]
                )
                if signal > 2.0:
                    exchange = self.get_exchange(Venue.BINANCE)
                    await exchange.place_limit_order(
                        symbol=event.symbol, side=Side.BUY,
                        quantity=0.01, price=event.kline.close * 0.999
                    )

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        # ~10/sec per symbol — keep fast
        if event.orderbook.spread_bps > 15.0:
            logger.warning(f"Wide spread on {event.symbol}: {event.orderbook.spread_bps:.1f} bps")

    async def on_order_update(self, event: OrderUpdateEvent):
        o = event.order
        if o.status.value == "FILLED":
            logger.info(f"FILLED {o.side.value} {o.quantity} {event.symbol} @ {o.avg_fill_price}")

    async def on_balance_update(self, event: BalanceUpdateEvent):
        logger.info(f"Balance change: {event.asset} delta={event.delta:+.8f}")

    async def on_stop(self):
        logger.info(f"ArbStrategy stopped. Tracked {len(self.prices)} symbols.")

    async def run_forever(self):
        """Periodic health check — runs alongside feed tasks."""
        while True:
            await asyncio.sleep(300)
            logger.info(f"Health: {sum(len(v) for v in self.prices.values())} prices tracked")
```

### RebalanceFSM Pattern

The `RebalanceFSM` (accessible via `self.rebalance_fsm`) drives the full compute → validate → execute → cooldown cycle. Strategies do not call `submit_weights()` directly; instead they implement `compute_weights()` and trigger the FSM.

**`compute_weights()`** — override this to return the target weight dict for one rebalance cycle. Return an empty dict or `None` to skip the cycle.

```python
async def compute_weights(self, **ctx) -> dict:
    # Return target weights or {} to skip
    return {"ETHUSDT": 0.5, "BTCUSDT": 0.4}
```

**`request_rebalance()`** — call this to trigger a cycle from any event hook or `run_forever()`. Only fires if the FSM is currently IDLE; returns `False` silently if a cycle is already in progress.

```python
async def on_kline(self, event: KlineEvent):
    if self._should_rebalance(event):
        await self.request_rebalance()

async def run_forever(self):
    while True:
        await asyncio.sleep(60)
        await self.request_rebalance()
```

The FSM states are: `IDLE → COMPUTING → VALIDATING → EXECUTING → COOLDOWN → IDLE`. A wildcard `suspend` trigger pauses from any state; `resume` returns to IDLE.

### Running a Strategy

```python
from elysian_core.run_strategy import StrategyRunner
from elysian_core.core.event_bus import EventBus

async def main():
    runner = StrategyRunner()
    strategy = MyArbStrategy(exchanges={}, event_bus=EventBus(), strategy_id=1)
    await runner.run(strategies=[strategy], strategy_ids=[1])

asyncio.run(main())
```

## Event Types

All events are `@dataclass(frozen=True)` — immutable after creation.

### KlineEvent
| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair (e.g., "ETHUSDT") |
| `venue` | `Venue` | Exchange enum (e.g., `Venue.BINANCE`) |
| `kline` | `Kline` | OHLCV candle data |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.KLINE` |

### OrderBookUpdateEvent
| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange enum |
| `orderbook` | `OrderBook` | Bid/ask snapshot with spread properties |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.ORDERBOOK_UPDATE` |

### OrderUpdateEvent
| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | Trading pair |
| `venue` | `Venue` | Exchange enum |
| `order` | `Order` | Parsed order with status, fills, fees |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.ORDER_UPDATE` |

### BalanceUpdateEvent
| Field | Type | Description |
|-------|------|-------------|
| `asset` | `str` | Asset symbol (e.g., "USDT") |
| `venue` | `Venue` | Exchange enum |
| `delta` | `float` | Balance change amount |
| `new_balance` | `float` | New balance after change |
| `timestamp` | `int` | Epoch milliseconds |
| `event_type` | `EventType` | Auto-set to `EventType.BALANCE_UPDATE` |

## Architecture Notes

### Event Flow: Worker → EventBus → Strategy

```
Worker dequeues WebSocket message
    ↓
Worker mutates feed state (feed._kline = kline)
    ↓
Worker calls: await self._event_bus.publish(KlineEvent(...))
    ↓
EventBus iterates subscribers for KLINE
    ↓
EventBus calls: await strategy._dispatch_kline(event)
    ↓
_dispatch_kline calls: await strategy.on_kline(event)
    ↓
Strategy hook executes (user code)
    ↓
Control returns to worker (backpressure: worker waited)
```

### Backpressure

`EventBus.publish()` is `await`ed by the worker, not fire-and-forget. If `on_orderbook_update()` takes 50ms, the OB worker blocks for 50ms before processing the next message. This provides natural backpressure — slow strategies automatically throttle the data rate.

### Object Sharing

Events carry **references** to live objects (e.g., `event.orderbook` is the same `OrderBook` instance the feed holds). If your strategy needs a consistent snapshot that won't be mutated by the next update, copy the object immediately:

```python
async def on_orderbook_update(self, event: OrderBookUpdateEvent):
    # event.orderbook may be mutated by the next depth update
    snapshot = copy.deepcopy(event.orderbook)
    # ... use snapshot safely
```

### run_forever() vs Hooks

- **Hooks** are reactive — they fire when events arrive
- **run_forever()** is proactive — it runs as a long-lived coroutine alongside feeds via `asyncio.gather()`
- Use `run_forever()` for periodic tasks (rebalancing, health checks, timers) that don't depend on specific events

## Performance Considerations

- **on_orderbook_update**: ~10 calls/sec per symbol. Keep execution under 10ms or use `run_heavy()`.
- **on_kline**: ~1 call/sec per symbol. Can tolerate heavier logic.
- **run_heavy()**: Crosses process boundary — adds ~1-5ms overhead. Use only for genuinely CPU-bound work (numpy, signal processing, ML inference).
- **Event creation**: Frozen dataclasses are lightweight. No significant overhead from event emission.

## Best Practices

### Strategy Design
- Override only the hooks you need
- Keep `on_orderbook_update()` fast (it fires ~10x/sec per symbol)
- Use `on_start()` for initialization, not `__init__()`
- Use `on_stop()` for cleanup and logging final metrics

### CPU-Bound Work
- Use `run_heavy(fn, *args)` for calculations >10ms
- `fn` must be a top-level function (not a method or lambda)
- Pass data by value (it gets pickled across process boundary)

### State Management
- Strategy instance state persists across events (use `self.` attributes)
- Initialize state in `on_start()`, not in `__init__()`
- Events are immutable — don't try to modify them

### Error Handling
- Each hook is wrapped in try/except — exceptions are logged but don't crash the system
- The EventBus also catches exceptions per-callback
- For critical errors, raise in `on_start()` to prevent the strategy from running
