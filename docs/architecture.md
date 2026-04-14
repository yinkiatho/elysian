# Elysian Trading System — Architecture Documentation

## Overview

Elysian is an event-driven, multi-venue quantitative trading system built on a single `asyncio` event loop per process. It implements a **6-stage pipeline** from raw market data to executed orders, with risk management and portfolio tracking throughout.

In production it runs as a set of Docker containers, with market data published over Redis pub/sub and per-strategy containers consuming it independently.

```
Stage 1          Stage 2              Stage 3          Stage 4          Stage 5          Stage 6
Market Data  →  Redis EventBus    →  Strategy/Alpha  →  Risk Mgmt  →  Execution  →  Exchange
(WebSocket)     (pub/sub transport)  (Weights)          (Optimizer)    (Engine)       (REST/WS)
```

---

## 1. Project Structure

```
elysian_core/
├── config/
│   ├── trading_config.yaml       # System-level config: venues, risk defaults, execution, portfolio
│   ├── strategies/               # Per-strategy config overrides
│   │   └── strategy_NNN.yaml     # risk, execution, portfolio overrides + strategy-specific params
│   └── config.json               # Trading pairs per venue
│
├── connectors/                   # Stages 1 & 6: Exchange I/O
│   ├── base.py                   # ABCs: SpotExchangeConnector, FuturesExchangeConnector,
│   │                             #       AbstractDataFeed, KlineClientManager,
│   │                             #       OrderBookClientManager, AbstractClientManager
│   ├── binance/
│   │   ├── BinanceExchange.py          # BinanceSpotExchange (REST + user data WS)
│   │   ├── BinanceDataConnectors.py    # BinanceKlineFeed, BinanceOrderBookFeed,
│   │   │                               # BinanceKlineClientManager, BinanceOrderBookClientManager,
│   │   │                               # BinanceUserDataClientManager
│   │   ├── BinanceFuturesExchange.py
│   │   └── BinanceFuturesDataConnectors.py
│   └── aster/
│       ├── AsterExchange.py
│       ├── AsterDataConnectors.py
│       ├── AsterPerpDataConnectors.py
│
├── core/                         # Shared domain model
│   ├── enums.py                  # Side, Venue, OrderType, OrderStatus, RebalanceState, …
│   ├── events.py                 # EventType enum + frozen event dataclasses
│   ├── event_bus.py              # In-process async pub/sub (used for private/account events)
│   ├── market_data.py            # Kline (OHLCV), OrderBook (SortedDict-backed)
│   ├── order.py                  # Order, ActiveOrder, ActiveLimitOrder
│   ├── order_fsm.py              # Order lifecycle FSM (warn-mode)
│   ├── position.py               # Single-asset Position tracker
│   ├── portfolio.py              # Portfolio: aggregate NAV across ShadowBooks + Fill record
│   ├── shadow_book.py            # ShadowBook: per-strategy authoritative position ledger
│   ├── fsm.py                    # BaseFSM + PeriodicTask
│   ├── rebalance_fsm.py          # RebalanceFSM: compute→validate→execute→cooldown
│   ├── signals.py                # Pipeline contracts: TargetWeights, ValidatedWeights,
│   │                             #                     OrderIntent, RebalanceResult
│   └── constants.py              # STABLECOINS, FILL_HISTORY_MAXLEN, QTY_EPSILON
│
├── risk/                         # Stage 4: Risk management
│   ├── risk_config.py            # RiskConfig dataclass (mutable for runtime tuning)
│   └── optimizer.py              # PortfolioOptimizer: constraint projection pipeline
│
├── execution/                    # Stage 5: Order generation & submission
│   └── engine.py                 # ExecutionEngine: weights → deltas → orders
│
├── strategy/                     # Stage 3: Alpha generation
│   ├── base_strategy.py          # SpotStrategy base class
│   ├── example_weight_strategy_v2_event_driven.py  # EventDrivenStrategy reference impl
│   └── example_strategy_test_print.py              # PrintEventsStrategy (debug logging)
│
├── transport/                    # Redis pub/sub transport layer
│   ├── redis_event_bus.py        # RedisEventBusPublisher + RedisEventBusSubscriber
│   └── event_serializer.py       # JSON codec for KlineEvent / OrderBookUpdateEvent
│
├── services/                     # Standalone services
│   └── market_data_service.py    # MarketDataService: owns all WebSocket feeds
│
├── db/                           # Persistence
│   ├── database.py               # Peewee ORM + ReconnectPooledPostgresqlDatabase
│   └── models.py                 # CexTrade, DexTrade, PortfolioSnapshot, AccountSnapshots
│
├── utils/
│   ├── logger.py                 # Custom logger (coloured terminal + file + Discord)
│   ├── utils.py                  # config_to_args(), load_config(), DataFrameIterator
│   └── async_helpers.py          # cancel_tasks() helper
│
├── run_strategy.py               # StrategyRunner: Redis-transport entry point (one per container)
└── run_market_data.py            # MarketDataService entry point
```

---

## 2. Deployment Architecture

Elysian is designed for containerised deployment. The architecture separates market data ingestion from strategy execution:

```
┌──────────────────────────────────────────────────────────────────┐
│  elysian_market_data container                                    │
│                                                                   │
│  MarketDataService                                                │
│  ├── BinanceKlineClientManager   → WebSocket → Binance           │
│  ├── BinanceOrderBookClientManager                                │
│  ├── AsterKlineClientManager     → WebSocket → Aster             │
│  └── AsterOrderBookClientManager                                  │
│                    │                                              │
│                    ▼ RedisEventBusPublisher                       │
│            elysian:md:{venue}:{asset_type}:kline:{SYMBOL}        │
│            elysian:md:{venue}:{asset_type}:orderbook:{SYMBOL}    │
└──────────────────────────────────────────────────────────────────┘
                      │
              Redis pub/sub
                      │
┌─────────────────────▼────────────────────────────────────────────┐
│  elysian_strategy_0 container                                     │
│                                                                   │
│  RedisEventBusSubscriber                                          │
│  ├── shared_event_bus (market data from Redis)                    │
│  ├── private_event_bus (in-process: account events)               │
│  ├── BinanceSpotExchange (sub-account REST + user data WS)        │
│  ├── ShadowBook (strategy-0 position ledger)                      │
│  ├── PortfolioOptimizer (risk constraint projection)              │
│  ├── ExecutionEngine (weight → order)                             │
│  └── Strategy (EventDrivenStrategy)                               │
└──────────────────────────────────────────────────────────────────┘
```

This separation means:
- **One set of WebSocket connections** serves all strategies — no duplicate subscriptions
- **Crash isolation** — a strategy container crashing does not affect the market data feed or other strategies
- **Independent restarts** — `docker compose restart elysian_strategy_1`
- **Independent scaling** — strategies can run on separate hosts

---

## 3. The 6-Stage Pipeline

### Stage 1 — Market Data Ingestion

**Files:** `connectors/binance/BinanceDataConnectors.py`, `connectors/aster/AsterDataConnectors.py`, `connectors/base.py`

Raw market data arrives over WebSocket using a **multiplex architecture** — one TCP connection per venue carries data for all tracked symbols. Each venue has two client managers:

| Manager | Feed Type | Update Rate | Event Published |
|---------|-----------|-------------|-----------------|
| `BinanceKlineClientManager` | Kline (OHLCV) | 1s candles | `KlineEvent` |
| `BinanceOrderBookClientManager` | Depth (L2) | 100ms | `OrderBookUpdateEvent` |
| `AsterKlineClientManager` | Kline | 1s | `KlineEvent` |
| `AsterOrderBookClientManager` | Depth | 100ms | `OrderBookUpdateEvent` |

**Worker pattern:**
```
Single WebSocket → asyncio.Queue (bounded) → N async workers
                                               ├─ Parse message
                                               ├─ Mutate feed state (sync)
                                               └─ await event_bus.publish(event) (async)
```

State mutation always precedes event publishing. This means strategy hooks see consistent feed state when they receive an event.

`BinanceSpotExchange` also runs a **user data stream** via the new Binance WebSocket API (`wss://ws-api.binance.com`) that emits:
- `OrderUpdateEvent` — order fills, cancels, partial fills
- `BalanceUpdateEvent` — account balance changes

### Stage 2 — EventBus (Transport)

In production, the EventBus between MarketDataService and strategy containers is Redis pub/sub (`RedisEventBusPublisher` / `RedisEventBusSubscriber`). Within a strategy container, account events travel over an in-process `EventBus` on a private bus.

**Channel naming convention:**
```
elysian:md:{venue_lower}:{asset_type_lower}:kline:{SYMBOL}
elysian:md:{venue_lower}:{asset_type_lower}:orderbook:{SYMBOL}
```

Events are serialised to JSON via `event_serializer.py`.

### Stage 3 — Strategy / Alpha Generation

**Files:** `strategy/base_strategy.py`

`SpotStrategy` is the base class. Subclass it and override hooks:

```python
class MyStrategy(SpotStrategy):
    async def on_kline(self, event: KlineEvent):
        # Analyse data, trigger rebalance
        await self.request_rebalance(trigger="kline")

    def compute_weights(self, **ctx) -> dict:
        return {"ETHUSDT": 0.4, "BTCUSDT": 0.5}
```

**Available hooks:**

| Hook | Bus | Trigger | Typical Frequency |
|------|-----|---------|-------------------|
| `on_start()` | — | Once at startup | Once |
| `on_stop()` | — | Once at shutdown | Once |
| `run_forever()` | — | Long-running coroutine | Continuous |
| `on_kline(KlineEvent)` | shared | Closed kline candle | ~1/sec per symbol |
| `on_orderbook_update(OrderBookUpdateEvent)` | shared | Depth snapshot | ~10/sec per symbol |
| `on_order_update(OrderUpdateEvent)` | private | Order state change | On fill / cancel |
| `on_balance_update(BalanceUpdateEvent)` | private | Balance delta | On balance change |
| `on_rebalance_complete(RebalanceCompleteEvent)` | private | FSM cycle finished | After each rebalance |
| `compute_weights(**ctx)` | — | Called by FSM | Once per cycle |

### Stage 4 — Risk Management (PortfolioOptimizer)

**Files:** `risk/optimizer.py`, `risk/risk_config.py`

The optimizer is a **stateless constraint projector** — it clips, scales, and rejects weights that violate the risk envelope. It is not a Markowitz-style optimizer.

**Constraint pipeline (executed in order):**

```
TargetWeights
    │
    ├─ 1. Input validation      → drop NaN / Inf / non-numeric weights
    ├─ 2. Symbol filter          → apply whitelist / blacklist
    ├─ 3. Per-asset clip         → clamp to [min_weight_per_asset, max_weight_per_asset]
    ├─ 4. Total-exposure scaling → if gross > max_total_exposure, scale proportionally
    ├─ 5. Cash-floor enforcement → if long_exposure > 1 - min_cash_weight, scale down
    ├─ 6. Turnover cap           → limit sum(|target_w - current_w|) per cycle
    └─ 7. Min-delta prune        → drop legs with |Δw| < min_weight_delta
    │
    ▼
ValidatedWeights (with audit trail: original weights, clipped amounts, rejection reason)
```

A special `liquidate=True` flag on `TargetWeights` bypasses all constraints (kill-switch path for `convert_all_base`).

**RiskConfig parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_weight_per_asset` | 0.25 | No single asset > 25% |
| `min_weight_per_asset` | 0.0 | Long-only floor |
| `max_total_exposure` | 1.0 | Sum of abs(weights); 1.0 = unlevered |
| `min_cash_weight` | 0.05 | Always keep >= 5% in cash |
| `max_turnover_per_rebalance` | 0.5 | Max sum(|Δw|) per cycle |
| `max_leverage` | 1.0 | For futures expansion |
| `max_short_weight` | 0.0 | 0 = no shorts |
| `min_order_notional` | 10.0 | Skip orders below $10 |
| `max_order_notional` | 100,000 | Cap single-order size |
| `min_weight_delta` | 0.005 | Skip rebalance legs < 0.5% weight change |

`RiskConfig` is **mutable** so limits can be tightened at runtime.

### Stage 5 — Execution Engine

**Files:** `execution/engine.py`

Converts validated weights into exchange orders:

```
ValidatedWeights
    │
    ├─ 1. Snapshot mark prices from _mark_prices dict (updated via KLINE events)
    ├─ 2. Compute total portfolio value (ShadowBook.total_value())
    ├─ 3. For each symbol:
    │      target_qty = weight * total_value / price
    │      delta = target_qty - current_qty
    │      weight_delta check (skip if < 0.001 threshold)
    │      Round down to exchange step_size
    ├─ 4. Partition: SELLS first (free capital), then BUYS
    └─ 5. Submit each order to exchange connector
    │
    ▼
RebalanceResult (intents, submitted, failed, errors, submitted_orders)
```

After submission, `lock_for_order()` is called on the ShadowBook for each successfully submitted LIMIT order, reserving the required cash or quantity.

**Key behaviours:**
- **Sells before buys** — frees quote currency for subsequent buys
- **Weight-delta filtering** — skips legs with |Δw| < 0.001 to avoid noise-driven churn
- **Per-order error isolation** — one failure doesn't block others
- **Step-size rounding** — uses `exchange._token_infos[symbol]["step_size"]`
- **Stablecoin guard** — emits a warning and skips any stablecoin key that appears in the weight dict

### Stage 6 — Exchange Submission

**Files:** `connectors/binance/BinanceExchange.py`, `connectors/aster/AsterExchange.py`

The execution engine delegates to `SpotExchangeConnector` methods:
- `place_market_order(symbol, side, quantity, strategy_id)`
- `place_limit_order(symbol, side, price, quantity, strategy_id)`

Fill confirmations flow back as `OrderUpdateEvent` via the user data stream into the private EventBus, which triggers `ShadowBook._on_order_update()` for incremental fill attribution.

---

## 4. Data Contracts (Frozen Dataclasses)

All inter-stage data is carried in **frozen (immutable) dataclasses** defined in `core/signals.py` and `core/events.py`.

### Signal Pipeline Contracts

```
Strategy.compute_weights() → dict[str, float]
        │
        │  TargetWeights  (TargetWeights(weights, timestamp, strategy_id, venue, liquidate))
        ▼
PortfolioOptimizer.validate()
        │
        │  ValidatedWeights  (original, weights, clipped, rejected, rejection_reason, timestamp)
        ▼
ExecutionEngine.execute()
        │
        │  OrderIntent(symbol, venue, side, quantity, order_type, price, strategy_id)  [per order]
        ▼
        RebalanceResult(intents, submitted, failed, timestamp, errors, submitted_orders)
```

### Event Contracts

| Event | Fields | Published By |
|-------|--------|--------------|
| `KlineEvent` | symbol, venue, kline, timestamp | KlineClientManager worker (via Redis in production) |
| `OrderBookUpdateEvent` | symbol, venue, orderbook, timestamp | OBClientManager worker (via Redis in production) |
| `OrderUpdateEvent` | symbol, base_asset, quote_asset, venue, order, timestamp | BinanceUserDataClientManager (private bus) |
| `BalanceUpdateEvent` | asset, venue, delta, new_balance, timestamp | BinanceUserDataClientManager (private bus) |
| `RebalanceCompleteEvent` | result, validated_weights, timestamp, strategy_id | RebalanceFSM._on_enter_executing (private bus) |
| `RebalanceCycleEvent` | old_state, new_state, trigger, timestamp | RebalanceFSM on every state transition |

---

## 5. ShadowBook — Per-Strategy Ledger

**File:** `core/shadow_book.py`

Each strategy owns exactly one `ShadowBook` — the authoritative source for that strategy's positions, cash, weights, PnL, and outstanding orders. Strategies must read `self._shadow_book` (not the aggregate `Portfolio`) when computing weights.

```python
# CORRECT
qty = self._shadow_book.position("ETHUSDT").quantity
cash = self._shadow_book.free_cash           # excludes LIMIT BUY reservations

# WRONG — Portfolio is a read-only aggregate across all strategies
qty = self.portfolio.positions.get("ETHUSDT")
```

### Key ShadowBook Properties

| Property | Description |
|----------|-------------|
| `nav` | Cached NAV (cash + sum of position notionals at mark prices) |
| `cash` | Total stablecoin balance |
| `free_cash` | `cash - _locked_cash` (cash available for new BUY orders) |
| `free_quantity(symbol)` | `position.quantity - locked_quantity` (qty available for new SELL orders) |
| `weights` | Cached weight vector from last mark price update |
| `positions` / `active_positions` | All / non-flat positions |
| `realized_pnl` | Cumulative realized P&L |
| `total_commission()` | Commissions in USDT (non-stablecoin fees converted at mark price) |
| `peak_equity` / `max_drawdown` | High-water mark and max drawdown fraction |
| `active_orders` | Live `ActiveOrder` / `ActiveLimitOrder` objects keyed by `order_id` |

### Initialization Paths

| Method | When to Use |
|--------|-------------|
| `sync_from_exchange(exchange, feeds)` | Sub-account mode startup — reads real balances, open orders |
| `init_from_portfolio_cash(cash, mark_prices)` | Fresh deployment with no prior positions |

### Event Handlers

**`_on_kline(event)`** — updates `_mark_prices[symbol]` and calls `_refresh_derived()`. Also patches `avg_entry_price` for positions that were synced at startup before feed data was available.

**`_on_order_update(event)`** — the primary fill attribution path:
1. Wraps first-seen orders into `ActiveOrder` / `ActiveLimitOrder`
2. Calls `active.sync_from_event(order)` to compute incremental `delta_filled`
3. Calls `_partial_release_lock()` for limit orders
4. Calls `apply_fill()` to update positions, cash, and realized PnL
5. On terminal orders: drains remaining lock, removes from `_active_orders`, schedules DB trade recording

**`_on_balance_update(event)`** — updates stablecoin cash totals; for non-stablecoins, maps the raw asset to its trading symbol and updates the position quantity.

### Balance Reservation (LIMIT Orders)

`lock_for_order(order_id, intent)` reserves balance when a LIMIT order is submitted:
- **BUY**: locks `quantity × price` from `_cash` into `_locked_cash`
- **SELL**: locks `quantity` from `position.quantity` into `position.locked_quantity`

`_partial_release_lock()` proportionally releases the reservation as partial fills arrive. `release_all()` drains the remainder on terminal orders (FILLED / CANCELLED / EXPIRED / REJECTED).

### Fill Attribution: `apply_fill()`

- `qty_delta > 0` = BUY (position increases); `< 0` = SELL
- **Same direction** (adding to position): computes new weighted-average entry price
- **Opposing direction** (reducing / closing): computes realized PnL; handles position flips
- Adjusts `_cash` immediately (credit SELL proceeds / debit BUY cost)
- Commission handling:
  - Stablecoin fees deducted from `_cash`
  - Non-stablecoin fees (e.g. BNB) deducted from the corresponding position quantity, tracked separately to avoid double-counting in `net_pnl()`
- Appends a `Fill` record to the ring buffer (`maxlen=10,000`)

---

## 6. Portfolio — Aggregate NAV Monitor

**File:** `core/portfolio.py`

`Portfolio` is a **thin, read-only aggregator** across all registered `ShadowBook` instances. It owns no event subscriptions and performs no fill attribution. Its sole purpose is to produce aggregate reports and persist portfolio snapshots.

```python
portfolio.register_shadow_book(shadow_book)   # once per strategy
nav = portfolio.aggregate_nav()               # sum of all shadow book NAVs
portfolio.save_snapshot()                     # persist to DB
```

Do not write to `Portfolio` directly. All position tracking belongs in `ShadowBook`.

---

## 7. RebalanceFSM

**File:** `core/rebalance_fsm.py`

`RebalanceFSM` drives the compute → validate → execute → cooldown pipeline. It is the only permitted entry point to the Stage 3–5 pipeline.

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
            |     /  weights_ready     |
            |    /    no_signal → IDLE |
            |   v                      |
            | +-----------+            |
            | | VALIDATING|            |
            | +-----------+            |
            |  / accepted              |
            | /   rejected → COOLDOWN  |
            |v                         |
        +-----------+  complete → COOLDOWN → cooldown_done --|
        | EXECUTING |                  ^                     |
        +-----------+                  |                     |
               |  failed               |---------------------|
               v
           +-------+   retry → IDLE
           | ERROR |
           +-------+

           Any state --[suspend]--> SUSPENDED --[resume]--> IDLE
```

### Context Flow

Each FSM callback enriches a shared `**ctx` dict:

| Stage | Adds to ctx |
|-------|-------------|
| `_on_enter_computing` | `ctx["target_weights"]` (dict), `ctx["strategy_id"]` |
| `_on_enter_validating` | `ctx["validated_weights"]` (ValidatedWeights) |
| `_on_enter_executing` | `ctx["rebalance_result"]` (RebalanceResult) |

Arbitrary kwargs passed to `request_rebalance(**ctx)` flow through the entire pipeline and are available in `compute_weights(**ctx)`.

### Triggering

```python
# From any on_* hook — safe to call at any frequency
await self.request_rebalance(trigger="kline", my_signal=0.42)

# Timer-driven (in run_forever())
while not self._stop_event.is_set():
    await asyncio.sleep(60)
    await self.request_rebalance()
```

`request_rebalance()` returns `True` if a cycle was started, `False` if the FSM was busy (cooldown, mid-cycle, suspended). There is no queue — dropped requests have no side effects.

---

## 8. EventBus

**File:** `core/event_bus.py`

In-process async pub/sub with zero serialization overhead. Used for the **private per-strategy bus** (account events: `ORDER_UPDATE`, `BALANCE_UPDATE`, `REBALANCE_COMPLETE`, `REBALANCE_CYCLE`).

```python
bus.subscribe(EventType.ORDER_UPDATE, callback)
await bus.publish(OrderUpdateEvent(...))   # awaits each subscriber sequentially
```

**Design decisions:**
- **Awaited dispatch** — `publish()` awaits each subscriber sequentially. This provides natural backpressure: if a strategy hook is slow, the publisher waits.
- **Error isolation** — exceptions in one callback are logged but do not prevent others from running.
- **No serialization** — events are direct Python object references.

### Two-Bus Architecture

| Bus | Carries | Wired By |
|-----|---------|---------|
| `shared_event_bus` (Redis in production) | `KLINE`, `ORDERBOOK_UPDATE` | MarketDataService → Redis → RedisEventBusSubscriber |
| `private_event_bus` (in-process) | `ORDER_UPDATE`, `BALANCE_UPDATE`, `REBALANCE_COMPLETE`, `REBALANCE_CYCLE` | StrategyRunner._create_sub_account_exchange() |

Never subscribe a strategy to account events on the shared bus. Cross-strategy fill contamination would corrupt ShadowBooks.

### Subscription Map

| Component | Subscribes To | Bus |
|-----------|--------------|-----|
| `SpotStrategy._dispatch_kline` | `KLINE` | shared |
| `SpotStrategy._dispatch_ob` | `ORDERBOOK_UPDATE` | shared |
| `SpotStrategy._dispatch_order` | `ORDER_UPDATE` | private |
| `SpotStrategy._dispatch_balance` | `BALANCE_UPDATE` | private |
| `SpotStrategy._dispatch_rebalance` | `REBALANCE_COMPLETE` | private |
| `ShadowBook._on_kline` | `KLINE` | shared |
| `SpotExchangeConnector._on_kline` | `KLINE` | shared |
| `ExecutionEngine._on_kline` | `KLINE` | shared |

---

## 9. StrategyRunner

**File:** `run_strategy.py`

`StrategyRunner` orchestrates all components for one strategy container. It runs in a single `asyncio` event loop.

### Startup Sequence

```
StrategyRunner.__init__()
    ├─ Load cfg (trading_config.yaml + strategy_NNN.yaml + config.json + .env)
    └─ Create DB tables

StrategyRunner.run()
    │
    ├─ _state = CONFIGURING
    ├─ Create RedisEventBusSubscriber (shared_event_bus)
    │
    ├─ _state = CONNECTING
    │
    ├─ _state = SYNCING
    ├─ _setup_pipeline()                 → creates Portfolio per (asset_type, venue)
    ├─ setup_strategy()
    │   ├─ _create_sub_account_exchange()
    │   │   ├─ Create private EventBus
    │   │   ├─ Instantiate exchange connector (BinanceSpotExchange, etc.)
    │   │   ├─ exchange.start(shared_event_bus)   ← subscribe to KLINE / OB for price feeds
    │   │   └─ await exchange.run()               ← initialize + start user data stream
    │   ├─ Create ShadowBook
    │   ├─ shadow_book.sync_from_exchange()       ← seed positions, cash, open orders
    │   ├─ shadow_book.start(shared_bus, private_bus)
    │   ├─ Create PortfolioOptimizer (with strategy risk_config)
    │   ├─ Create ExecutionEngine
    │   ├─ execution_engine.start(shared_bus)     ← subscribe to KLINE for mark prices
    │   ├─ portfolio.register_shadow_book()
    │   ├─ Inject into strategy: shadow_book, optimizer, engine, exchanges, cfg
    │   └─ await strategy.start()                 ← subscribe hooks, init FSM
    │
    ├─ _state = RUNNING
    ├─ await shared_event_bus.start_listener(channels)
    └─ await asyncio.gather(
         redis_listener_task,
         strategy.run_forever()
       )
```

### Shutdown Sequence

```
finally:
    strategy.stop()                → unsubscribe hooks, set _stop_event, stop executor, stop shadow_book
    exchange.cleanup()             → stop user data stream, close HTTP client
    snapshot tasks stop
    redis subscriber close
```

---

## 10. Concurrency Model

```
Strategy Container — asyncio.run() — SINGLE EVENT LOOP
    │
    ├─ RedisEventBusSubscriber._listener_loop   ← asyncio.Task
    │  └─ deserialise → dispatch KlineEvent / OrderBookUpdateEvent to subscribers
    │
    ├─ BinanceUserDataClientManager._reader_loop   ← asyncio.Task
    │  └─ dispatch OrderUpdateEvent / BalanceUpdateEvent on private bus
    │
    ├─ SpotStrategy._dispatch_*()   ← called from subscriber callbacks (same loop)
    │
    ├─ RebalanceFSM._on_enter_*()   ← called from strategy hooks (same loop)
    │
    ├─ ExecutionEngine.execute()    ← async, protected by asyncio.Lock
    │
    └─ strategy.run_forever()   ← asyncio.Task
```

**Key design decisions:**
- Everything runs in ONE asyncio event loop — no `ThreadPoolExecutor` for I/O
- `EventBus.publish()` is awaited sequentially — backpressure propagates naturally
- `ProcessPoolExecutor` reserved ONLY for CPU-bound strategy calculations via `strategy.run_heavy(fn, *args)`
- `ExecutionEngine.execute()` is protected by `asyncio.Lock` to prevent concurrent rebalances

---

## 11. Full Pipeline Call Chain (Stage 3→5)

The Stage 3→5 pipeline uses **direct method calls** via the FSM, not EventBus events:

```
await strategy.request_rebalance(**ctx)
    │
    └─ RebalanceFSM.request(**ctx)
           │
           ├─ [COMPUTING] strategy.compute_weights(**ctx)
           │   └─ returns dict[str, float] or {}
           │
           ├─ [VALIDATING] optimizer.validate(TargetWeights(...), **ctx)
           │   ├─ Input validation (drop NaN/Inf)
           │   ├─ Symbol filter (whitelist/blacklist)
           │   ├─ Per-asset clip → [0, max_weight_per_asset]
           │   ├─ Exposure scaling → sum(|w|) ≤ max_total_exposure
           │   ├─ Cash floor → long_exposure ≤ 1 - min_cash_weight
           │   ├─ Turnover cap → sum(|Δw|) ≤ max_turnover_per_rebalance
           │   └─ Min-delta prune → drop |Δw| < min_weight_delta
           │       └─ → ValidatedWeights
           │
           ├─ [EXECUTING] execution_engine.execute(validated, **ctx)
           │   ├─ Snapshot mark prices
           │   ├─ compute_order_intents()
           │   │   ├─ target_qty = weight × NAV / price
           │   │   ├─ delta = target_qty - current_qty
           │   │   ├─ Filter by weight_delta threshold (0.001)
           │   │   └─ Round to step_size
           │   ├─ Sells first, then buys
           │   ├─ _submit_order() → exchange REST
           │   └─ shadow_book.lock_for_order() for LIMIT orders
           │       └─ → RebalanceResult
           │
           ├─ private_bus.publish(RebalanceCompleteEvent)
           │
           └─ [COOLDOWN] → wait cooldown_s → [IDLE]
```

**Rationale for direct calls (not EventBus):**
- The pipeline is inherently sequential (weights → risk → execute)
- Only `REBALANCE_COMPLETE` is published for post-execution observability
- EventBus is reserved for fan-out notifications, not sequential pipelines

---

## 12. Exchange Connector Architecture

### Base Classes (`connectors/base.py`)

```
AbstractDataFeed (ABC)
    ├─ create_new(asset, interval)      ← configure for a symbol
    ├─ executed_buy_price(amount)       ← VWAP through the ask side
    ├─ executed_sell_price(amount)      ← VWAP through the bid side
    └─ update_current_stats()           ← background vol computation

KlineClientManager (ABC)
    ├─ register_feed(feed)
    ├─ run_multiplex_feeds()            ← reader + worker pool
    └─ set_event_bus(bus)

SpotExchangeConnector (ABC)
    ├─ start(event_bus)                 ← subscribe to KLINE/OB for price feeds
    ├─ stop()
    ├─ initialize()
    ├─ place_market_order(...)
    ├─ place_limit_order(...)
    ├─ get_balance(asset)
    ├─ order_health_check(...)          ← validate balance + min-notional
    └─ last_price(symbol)              ← OB mid → kline close → None
```

### Supported Venues

| Venue | Spot | Futures | User Data WS | Status |
|-------|------|---------|--------------|--------|
| Binance | `BinanceSpotExchange` | `BinanceFuturesExchange` | New WS API (`ws-api.binance.com`) | Production |
| Aster | `AsterSpotExchange` | `AsterPerpDataConnectors` | listenKey approach | Production |

**Binance User Data Note:** The deprecated listenKey approach was retired by Binance on 2026-02-20. `BinanceUserDataClientManager` now uses `userDataStream.subscribe.signature` on `wss://ws-api.binance.com:443/ws-api/v3`, which works with HMAC-SHA256 keys without requiring `session.logon`.

### WebSocket Reconnection Pattern

All managers implement exponential backoff:
```python
reconnect_delay = 1
while self._running:
    try:
        async with websockets.connect(...) as ws:
            reconnect_delay = 1  # reset on success
            ...
    except Exception:
        pass
    await asyncio.sleep(reconnect_delay)
    reconnect_delay = min(reconnect_delay * 2, 60)
```

---

## 13. Persistence Layer

### Database (`db/database.py`)

- **ORM:** Peewee with `ReconnectPooledPostgresqlDatabase`
- **Pool:** max 20 connections, 300s stale timeout
- **Timezone:** UTC+8 for all timestamps

### Tables (`db/models.py`)

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `cex_trades` | CEX fill records | symbol, side, price, qty, commission, order_id, strategy_id |
| `dex_trades` | DEX swap events | tx_digest, pool, amounts, sqrt_price |
| `portfolio_snapshots` | Point-in-time per-strategy state | strategy_id, nav, cash, pnl, exposure, positions (JSON) |
| `account_snapshots` | Raw exchange balance captures | balances (JSON), total_usd_value |

Trade recording happens asynchronously (`asyncio.create_task`) after each order reaches a terminal state, so it never blocks the event loop.

---

## 14. Configuration

### Two-Tier Config System

#### `trading_config.yaml` (system-level)

```yaml
spot:
  venues: [Binance]
futures:
  venues: []
portfolio:
  snapshot_interval_s: 300
execution:
  default_order_type: "MARKET"
  default_venue: "Binance"
venue_configs:
  spot_binance:
    risk: {}
    execution: {}
    portfolio: {}
```

#### `strategies/strategy_NNN.yaml` (per-strategy)

```yaml
strategy_id: 0
strategy_name: "event_driven_momentum_binance_spot"
class: "elysian_core.strategy.example_weight_strategy_v2_event_driven.EventDrivenStrategy"
asset_type: "Spot"
venue: "Binance"
venues: ["Binance"]
symbols: ["SUIUSDT", "BTCUSDT", "ETHUSDT"]
risk:
  max_weight_per_asset: 0.25
  min_cash_weight: 0.05
params:
  rebalance_interval_s: 60
```

### Config Priority (highest wins)

```
strategy risk: section (strategy_NNN.yaml)
  ↓ overrides
venue_configs["{asset_type}_{venue}"] (trading_config.yaml)
  ↓ overrides
global risk: section (trading_config.yaml)
```

`cfg.effective_risk_for(strategy_id=N)` returns the fully-merged `RiskConfig` for strategy N.

### Sub-account Credentials

API keys are resolved from environment variables using the pattern:
```
{VENUE}_API_KEY_{strategy_id}
{VENUE}_API_SECRET_{strategy_id}
```

e.g., `BINANCE_API_KEY_0` and `BINANCE_API_SECRET_0` for strategy 0.

---

## 15. Adding a New Strategy (Checklist)

1. Subclass `SpotStrategy` in `elysian_core/strategy/`
2. Create `elysian_core/config/strategies/strategy_NNN_<name>.yaml` with a unique `strategy_id`
3. Add env vars `{VENUE}_API_KEY_{strategy_id}` and `{VENUE}_API_SECRET_{strategy_id}` to `.env`
4. Register the YAML path in `run_strategy.py` (or via `STRATEGY_CONFIG_YAML` env var in Docker)
5. Implement `compute_weights(**ctx)` as a pure function — no side effects
6. All initialization that needs `self.cfg` or `self.strategy_config` goes in `on_start()`, not `__init__()`
7. Use bounded collections (`deque(maxlen=N)`, `NumpySeries(maxlen=N)`) for all rolling state
8. Gate rebalance triggers: check elapsed time before calling `request_rebalance()`
9. Read `self._shadow_book` for position/cash state, never `self.portfolio`
10. Never include stablecoin keys (`USDT`, `USDC`, `BUSD`) in the weight dict

---

## 16. Adding a New Exchange Connector (Checklist)

1. Create `NewExchangeKlineFeed`, `NewExchangeOrderBookFeed` subclassing `AbstractDataFeed`
2. Create `NewExchangeKlineClientManager`, `NewExchangeOrderBookClientManager` subclassing the abstract managers
3. Create `NewExchangeUserDataClientManager` with `register()`, `set_event_bus()`, `start()`, `stop()`
4. Worker coroutine must: update feed state first (sync), then `await self._event_bus.publish(...)` (async). Order matters — strategy hooks see consistent state
5. Implement exponential backoff reconnection: start at 1s, double, cap at 60s
6. Create `NewExchangeSpotExchange(SpotExchangeConnector)` with all abstract methods
7. Add `Venue.NEW_EXCHANGE` to `core/enums.py`
8. Register in `StrategyRunner._exchange_connector_callables`

---

## 17. What NOT to Do

| Anti-pattern | Why it breaks Elysian |
|---|---|
| `await event_bus.publish()` inside `compute_weights()` | compute_weights must be pure; FSM is not re-entrant during execution |
| Calling `optimizer.validate()` directly in a strategy | Bypasses FSM state machine; cooldown and error recovery break |
| Subscribing to `ORDER_UPDATE` on the shared bus | All strategies receive all fills; ShadowBook corruption |
| `threading.Thread` or a second event loop | Breaks single-loop invariant |
| Reading `self.portfolio` for per-strategy state | Portfolio is an aggregate; use `self._shadow_book` |
| `asyncio.create_task()` inside `on_kline` | Escapes backpressure; can cause unbounded queue growth |
| `{"USDT": 1.0}` in compute_weights return value | ExecutionEngine will attempt to place `USDTUSDT` order (symbol doesn't exist) |
| Return `{}` with stablecoin key | Same as above — return `{}` (empty dict) for all-cash |
| Mutable objects in frozen event dataclasses | Downstream subscribers mutate shared state; copy if needed |
| Hardcoding `strategy_id` references in API keys | Must match env var suffix exactly; reusing IDs corrupts ShadowBook routing |

---

## 18. Performance Characteristics

| Metric | Value |
|--------|-------|
| Feed latency | < 100ms exchange → feed update |
| Event dispatch (in-process) | < 1ms per subscriber (zero serialization) |
| Redis serialization overhead | ~1ms per event (JSON encode/decode) |
| Order latency | < 500ms signal → exchange REST |
| Kline throughput | ~1,000 msg/s per Binance socket |
| OB throughput | ~10,000 msg/s per Binance socket |
| Queue capacity | 10,000–100,000 messages (bounded per manager) |
| Fill history | 10,000 fills per ShadowBook (ring buffer) |
