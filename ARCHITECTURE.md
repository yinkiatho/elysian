# Elysian Trading System — Architecture Documentation

## Overview

Elysian is an event-driven, multi-venue quantitative trading system built on a single `asyncio` event loop. It implements a **6-stage pipeline** from raw market data to executed orders, with risk management and portfolio tracking throughout.

```
Stage 1          Stage 2           Stage 3          Stage 4          Stage 5          Stage 6
Market Data  →  Feature Compute  →  Strategy/Alpha  →  Risk Mgmt  →  Execution  →  Exchange
(WebSocket)     (Feeds/Vol)         (Weights)          (Optimizer)    (Engine)       (REST/WS)
```

---

## 1. Project Structure

```
elysian_core/
├── config/
│   ├── config.yaml          # Strategy, DB, networking config
│   └── config.json          # Trading pairs per venue
│
├── connectors/              # Stage 1 & 6: Exchange I/O
│   ├── base.py              # ABC: SpotExchangeConnector, FuturesExchangeConnector,
│   │                        #       AbstractDataFeed, AbstractKlineFeed,
│   │                        #       AbstractClientManager
│   ├── BinanceExchange.py         # BinanceSpotExchange (REST + user data WS)
│   ├── BinanceDataConnectors.py   # BinanceKlineFeed, BinanceOrderBookFeed,
│   │                              # BinanceKlineClientManager, BinanceOrderBookClientManager
│   ├── BinanceFuturesExchange.py
│   ├── BinanceFuturesDataConnectors.py
│   ├── AsterExchange.py
│   ├── AsterDataConnectors.py
│   ├── AsterPerpExchange.py
│   └── AsterPerpDataConnectors.py
│
├── core/                    # Shared domain model
│   ├── enums.py             # Side, Venue, OrderType, OrderStatus, ...
│   ├── events.py            # EventType enum + frozen event dataclasses
│   ├── event_bus.py         # In-process async pub/sub
│   ├── market_data.py       # Kline (OHLCV), OrderBook
│   ├── order.py             # Order, LimitOrder
│   ├── position.py          # Single-asset Position tracker
│   ├── portfolio.py         # Portfolio: positions, cash, weights, risk, snapshots
│   └── signals.py           # Pipeline contracts: TargetWeights, ValidatedWeights,
│                            #                     OrderIntent, RebalanceResult
│
├── risk/                    # Stage 4: Risk management
│   ├── risk_config.py       # RiskConfig dataclass (mutable for runtime tuning)
│   └── optimizer.py         # PortfolioOptimizer: constraint projection
│
├── execution/               # Stage 5: Order generation & submission
│   └── engine.py            # ExecutionEngine: weights → deltas → orders
│
├── strategy/                # Stage 3: Alpha generation
│   ├── base_strategy.py     # SpotStrategy base class (event hooks + submit_weights)
│   ├── example_weight_strategy.py  # EqualWeightStrategy reference impl
│   └── test_strategy.py     # TestPrintStrategy (debug logging)
│
├── db/                      # Persistence
│   ├── database.py          # Peewee ORM + ReconnectPooledPostgresqlDatabase
│   └── models.py            # CexTrade, DexTrade, PortfolioSnapshot, AccountSnapshots
│
├── market_maker/            # Market-making logic (independent module)
├── oms/                     # Order management system
├── utils/
│   ├── logger.py            # Custom logger setup
│   └── utils.py             # config_to_args(), load_config()
│
└── run_strategy.py          # StrategyRunner: main entry point, wires everything
```

---

## 2. The 6-Stage Pipeline

### Stage 1 — Market Data Ingestion

**Files:** `connectors/BinanceDataConnectors.py`, `connectors/base.py`

Raw market data arrives over WebSocket connections using a **multiplex architecture** — a single TCP connection carries data for all tracked symbols. Each venue has two client managers:

| Manager | Feed Type | Update Rate | Event Published |
|---------|-----------|-------------|-----------------|
| `BinanceKlineClientManager` | Kline (OHLCV) | 1s candles | `KlineEvent` |
| `BinanceOrderBookClientManager` | Depth (L2) | 100ms | `OrderBookUpdateEvent` |

**Multiplex Design:**
```
Single WebSocket → asyncio.Queue (10k max) → 4 async workers
                                               ├─ Parse message
                                               ├─ Mutate feed state
                                               └─ EventBus.publish(TypedEvent)
```

The `BinanceSpotExchange` also runs a **user data stream** that emits:
- `OrderUpdateEvent` — order fills, cancels, partial fills
- `BalanceUpdateEvent` — account balance changes (delta-based)

### Stage 2 — Feature Computation

**Files:** `connectors/base.py` (AbstractDataFeed, AbstractKlineFeed)

Each feed maintains rolling state:
- **`_historical`** — 60-sample ring buffer of mid prices (for volatility)
- **`_historical_ob`** — 60-sample ring buffer of OrderBook snapshots
- **`_vol`** — 60-second realised volatility (annualised)
- **Slippage pricing** — `executed_buy_price()` / `executed_sell_price()` compute VWAP through the book

The `Portfolio` also computes features on every kline:
- Mark-to-market NAV
- Current weight vector
- Peak equity / max drawdown tracking

### Stage 3 — Strategy / Alpha Generation

**Files:** `strategy/base_strategy.py`, `strategy/example_weight_strategy.py`

`SpotStrategy` is the base class. Subclass it and override hooks:

```python
class MyStrategy(SpotStrategy):
    async def on_kline(self, event: KlineEvent):
        # Analyze data, generate signal
        weights = {"ETHUSDT": 0.4, "BTCUSDT": 0.5}
        await self.submit_weights(weights)
```

**Available hooks:**

| Hook | Event | Typical Use |
|------|-------|-------------|
| `on_start()` | — | Initialization logic |
| `on_stop()` | — | Cleanup |
| `on_kline(KlineEvent)` | `KLINE` | Price-based signals, rebalance triggers |
| `on_orderbook_update(OrderBookUpdateEvent)` | `ORDERBOOK_UPDATE` | Spread/depth analysis |
| `on_order_update(OrderUpdateEvent)` | `ORDER_UPDATE` | Fill tracking, execution feedback |
| `on_balance_update(BalanceUpdateEvent)` | `BALANCE_UPDATE` | Cash sync |
| `on_rebalance_complete(RebalanceCompleteEvent)` | `REBALANCE_COMPLETE` | Post-execution observability |
| `run_forever()` | — | Periodic tasks (timer-based rebalancing) |

**Output mechanisms:**

1. **`submit_weights(weights)`** — Primary. Sends target portfolio weights through the risk/execution pipeline. Returns `RebalanceResult`.
2. **Direct exchange access** — Escape hatch. `self.get_exchange(Venue.BINANCE).place_market_order(...)` for time-sensitive single-symbol trades.

### Stage 4 — Risk Management (PortfolioOptimizer)

**Files:** `risk/optimizer.py`, `risk/risk_config.py`

The optimizer is a **stateless constraint projector** — it clips, scales, and rejects weights that violate the risk envelope. It is NOT a Markowitz-style optimizer.

**Constraint pipeline (executed in order):**

```
TargetWeights
    │
    ├─ 1. Rate-limit check     → reject if < min_rebalance_interval_ms since last
    ├─ 2. Symbol filter         → remove blocked, keep only allowed
    ├─ 3. Per-asset clip        → clamp to [min_weight, max_weight] per asset
    ├─ 4. Exposure scaling      → if gross > max_total_exposure, scale proportionally
    ├─ 5. Cash floor            → if sum(long_weights) > 1 - min_cash_weight, scale down
    └─ 6. Turnover cap          → limit sum(abs(delta_w)) per cycle
    │
    ▼
ValidatedWeights (with audit trail: original, clipped amounts, rejection reason)
```

**RiskConfig parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_weight_per_asset` | 0.25 | No single asset > 25% |
| `min_weight_per_asset` | 0.0 | Long-only floor |
| `max_total_exposure` | 1.0 | Sum of abs(weights); 1.0 = unlevered |
| `min_cash_weight` | 0.05 | Always keep >= 5% in cash |
| `max_turnover_per_rebalance` | 0.5 | Max sum(abs(delta_w)) per cycle |
| `max_leverage` | 1.0 | For futures expansion |
| `max_short_weight` | 0.0 | 0 = no shorts |
| `min_order_notional` | 10.0 | Skip orders below $10 |
| `max_order_notional` | 100,000 | Cap single-order size |
| `min_weight_delta` | 0.005 | Skip rebalance legs < 0.5% weight change |
| `min_rebalance_interval_ms` | 60,000 | 1 minute between rebalances |
| `allowed_symbols` | None | None = all allowed |
| `blocked_symbols` | `frozenset()` | Explicit blacklist |

RiskConfig is **mutable** so limits can be tightened at runtime (e.g., vol spike → reduce exposure).

### Stage 5 — Execution Engine

**Files:** `execution/engine.py`

Converts validated weights into exchange orders:

```
ValidatedWeights
    │
    ├─ 1. Snapshot mark prices from feeds
    ├─ 2. Compute total portfolio value (NAV)
    ├─ 3. For each symbol:
    │      target_qty = weight * total_value / price
    │      delta = target_qty - current_qty
    │      weight_delta check (skip if < min_weight_delta)
    │      Round to exchange step_size
    ├─ 4. Partition: SELLS first (free capital), then BUYS
    └─ 5. Submit each order to exchange connector
    │
    ▼
RebalanceResult (intents, submitted, failed, errors)
```

**Key behaviors:**
- **Sells before buys** — frees quote currency for subsequent buys
- **Weight-delta filtering** — uses `min_weight_delta` from RiskConfig, not absolute quantity
- **Per-order error isolation** — one failure doesn't block others
- **Step-size rounding** — uses exchange's `_token_infos[symbol]["step_size"]`

### Stage 6 — Exchange Submission

**Files:** `connectors/BinanceExchange.py`

The execution engine delegates to `SpotExchangeConnector` methods:
- `place_market_order(symbol, side, quantity)`
- `place_limit_order(symbol, side, price, quantity)`

Fill confirmations flow back as `OrderUpdateEvent` via the user data stream.

---

## 3. Data Contracts (Frozen Dataclasses)

All inter-stage data is carried in **frozen (immutable) dataclasses** defined in `core/signals.py` and `core/events.py`.

### Signal Pipeline Contracts

```
┌─────────────────────┐
│    TargetWeights     │  Stage 3 → 4
├─────────────────────┤
│ weights: {str: float}│  symbol → target weight
│ timestamp: int       │  epoch ms
│ strategy_id: str     │
│ venue: Venue?        │
│ metadata: dict       │
└─────────────────────┘
         │
         ▼
┌─────────────────────┐
│  ValidatedWeights    │  Stage 4 → 5
├─────────────────────┤
│ original: Target...  │  audit trail
│ weights: {str: float}│  risk-adjusted
│ clipped: {str: float}│  per-symbol clip amounts
│ rejected: bool       │
│ rejection_reason: str│
│ timestamp: int       │
└─────────────────────┘
         │
         ▼
┌─────────────────────┐
│    OrderIntent       │  Stage 5 internal
├─────────────────────┤
│ symbol: str          │
│ venue: Venue         │
│ side: Side           │
│ quantity: float      │  base asset qty
│ order_type: OrderType│
│ price: float?        │  None for market
└─────────────────────┘
         │
         ▼
┌─────────────────────┐
│  RebalanceResult     │  Stage 5 → 3
├─────────────────────┤
│ intents: tuple       │  all generated intents
│ submitted: int       │
│ failed: int          │
│ timestamp: int       │
│ errors: tuple[str]   │
└─────────────────────┘
```

### Event Contracts

| Event | Fields | Published By |
|-------|--------|--------------|
| `KlineEvent` | symbol, venue, kline, timestamp | KlineClientManager worker |
| `OrderBookUpdateEvent` | symbol, venue, orderbook, timestamp | OBClientManager worker |
| `OrderUpdateEvent` | symbol, venue, order, timestamp | UserDataClientManager |
| `BalanceUpdateEvent` | asset, venue, delta, new_balance, timestamp | UserDataClientManager |
| `RebalanceCompleteEvent` | result, validated_weights, timestamp | SpotStrategy.submit_weights() |

---

## 4. Portfolio System

**File:** `core/portfolio.py`

The `Portfolio` is the centralized source of truth for position, cash, and risk state. It operates in two modes:

### Event-Driven Mode (production)

```python
portfolio.sync_from_exchange(exchange, venue, feeds)  # startup: full state sync
portfolio.start(event_bus, feeds)                      # subscribe to KLINE + BALANCE_UPDATE
```

After `start()`, the portfolio **automatically**:
- Updates mark prices on every `KlineEvent`
- Recomputes weight vector and NAV
- Tracks peak equity and max drawdown
- Syncs cash from stablecoin `BalanceUpdateEvent` (delta-based)

### Standalone Mode (testing/backtesting)

```python
portfolio.mark_to_market({"ETHUSDT": 3200.0, "BTCUSDT": 64000.0})
portfolio.update_position("ETHUSDT", qty_delta=1.5, price=3200.0)
```

### Key Properties

| Property/Method | Description |
|----------------|-------------|
| `nav` | Cached NAV from last kline (zero-cost read) |
| `cash` / `cash_dict` | Total cash / per-stablecoin breakdown |
| `weights` | Cached weight vector |
| `positions` / `active_positions` | All / non-flat positions |
| `total_realized_pnl` | Cumulative realized P&L |
| `peak_equity` / `max_drawdown` | Risk metrics |
| `gross_exposure()` / `net_exposure()` | Exposure metrics |

### Position Tracking

`Position` (in `core/position.py`) tracks per-asset state:

```python
@dataclass
class Position:
    symbol: str
    venue: Venue
    quantity: float           # >0 long, <0 short
    avg_entry_price: float    # VWAP entry
    realized_pnl: float       # cumulative from closes
    total_commission: float    # cumulative fees
```

Fill processing in `Portfolio.update_position()`:
- **Same direction** (adding to position): updates average entry price via VWAP
- **Opposite direction** (reducing/closing): computes realized P&L, handles position flips
- Always updates mark price and refreshes derived state
- Records fill in audit trail (ring buffer, 10k max)

### Startup Sync: `sync_from_exchange()`

Called once per exchange at startup after `exchange.run()`:

1. **Cash** — stablecoin balances (USDT, USDC, BUSD) → `_cash_dict` / `_cash`
2. **Positions** — non-stablecoin balances → `Position` objects (entry price seeded from feed)
3. **Mark prices** — latest prices from all available feeds
4. **Open orders** — snapshot of exchange's `_open_orders`
5. **Derived state** — NAV, weights, drawdown via `_refresh_derived()`

For multi-venue: call once per exchange — state accumulates.

### Portfolio Snapshots

```python
portfolio.save_snapshot(venue=Venue.BINANCE)
```

Persists to the `PortfolioSnapshot` PostgreSQL table (Peewee ORM):

| Column | Type | Description |
|--------|------|-------------|
| `nav` | float | Total portfolio value |
| `cash` | float | Stablecoin balance |
| `unrealized_pnl` | float | Mark-to-market |
| `realized_pnl` | float | Cumulative |
| `total_commission` | float | Cumulative fees |
| `peak_equity` | float | Highest NAV |
| `max_drawdown` | float | As fraction (0.05 = 5%) |
| `current_drawdown` | float | Current drawdown |
| `gross_exposure` | float | Sum of abs(weights) |
| `net_exposure` | float | Sum of weights |
| `positions` | JSON | `{"ETHUSDT": {"qty": 1.5, "avg_entry": 3200, ...}}` |
| `weights` | JSON | `{"ETHUSDT": 0.25, ...}` |
| `mark_prices` | JSON | `{"ETHUSDT": 3250.0, ...}` |
| `num_fills` | int | Fill count |

---

## 5. EventBus

**File:** `core/event_bus.py`

In-process async pub/sub with zero serialization overhead:

```python
class EventBus:
    subscribe(event_type: EventType, callback: Callable)
    unsubscribe(event_type: EventType, callback: Callable)
    async publish(event)  # awaited — natural backpressure
```

**Design decisions:**
- **Awaited dispatch** — `publish()` is `async` and awaits each subscriber sequentially. This provides natural backpressure: if a strategy hook is slow, the emitting worker waits.
- **Error isolation** — exceptions in one callback are logged but don't prevent others from running.
- **No serialization** — events are direct Python object references (copy if you need a snapshot).

**Subscriber registration:**

| Component | Subscribes To | Handler |
|-----------|--------------|---------|
| SpotStrategy | KLINE, ORDERBOOK_UPDATE, ORDER_UPDATE, BALANCE_UPDATE, REBALANCE_COMPLETE | `_dispatch_*` wrappers |
| Portfolio | KLINE | `_on_kline` (mark prices, weights, drawdown) |
| Portfolio | BALANCE_UPDATE | `_on_balance` (cash sync via delta) |

---

## 6. StrategyRunner (Main Entry Point)

**File:** `run_strategy.py`

Orchestrates all components in a single `asyncio` event loop.

### Startup Sequence

```
StrategyRunner.__init__()
    ├─ Load configs (YAML → args, JSON → config_json)
    ├─ Initialize 8 client managers (Binance/Aster × Spot/Futures × Kline/OB)
    └─ Setup logging & environment (.env)

StrategyRunner.run(strategy)
    │
    ├─ _setup_config()           → Load trading pairs from config.json
    ├─ EventBus()                → Create shared event bus
    ├─ _setup_exchanges()        → BinanceSpotExchange(event_bus=...)
    ├─ Inject EventBus           → kline_manager.set_event_bus(), ob_manager.set_event_bus()
    ├─ Wire strategy             → strategy._event_bus, ._exchanges, .args, .config_json
    ├─ strategy.start()          → Subscribe hooks to EventBus
    ├─ exchange.run()            → AsyncClient, user data stream
    │
    ├─ _setup_pipeline()         → Stage 3-5 wiring:
    │   ├─ _setup_portfolio()    → sync_from_exchange() + portfolio.start()
    │   ├─ _setup_risk()         → RiskConfig from args.risk.* + PortfolioOptimizer
    │   └─ _setup_execution()    → ExecutionEngine (shares RiskConfig)
    │
    └─ asyncio.gather(
         _setup_binance_data_feeds(),   ← blocks forever (multiplex feeds)
         strategy.run_forever()          ← optional long-running coroutine
       )
```

### Shutdown Sequence

```
finally:
    ├─ exchange.cleanup()
    ├─ portfolio.stop()          → Unsubscribe from EventBus
    ├─ strategy.stop()           → Unsubscribe hooks, shutdown ProcessPool
    └─ _cleanup()                → Stop all client managers
```

---

## 7. Concurrency Model

```
Main Thread — asyncio.run() — SINGLE EVENT LOOP
    │
    ├─ KlineClientManager: 1 reader task + 4 worker tasks
    │  └─ Workers: parse → mutate feed → publish KlineEvent
    │
    ├─ OBClientManager: 1 reader task + 4 worker tasks
    │  └─ Workers: parse → mutate feed → publish OrderBookUpdateEvent
    │
    ├─ UserDataClientManager: 1 reader loop
    │  └─ Sync callbacks first → then publish OrderUpdateEvent / BalanceUpdateEvent
    │
    ├─ Portfolio: subscribed handlers (mark price, cash sync)
    │
    ├─ Strategy hooks: run in same event loop
    │  └─ For CPU-heavy work: strategy.run_heavy(fn) → ProcessPoolExecutor
    │
    └─ strategy.run_forever(): optional long-running coroutine
```

**Key design decisions:**
- Everything runs in ONE event loop (no ThreadPoolExecutor for feeds)
- `EventBus.publish()` is awaited — backpressure propagates from strategy to worker
- `ProcessPoolExecutor` reserved ONLY for CPU-bound strategy calculations
- Sync callbacks (exchange state mutation) run BEFORE async event dispatch

---

## 8. Full Pipeline Call Chain

The Stage 3→5 pipeline uses **direct method calls**, NOT EventBus events:

```
strategy.submit_weights({"ETHUSDT": 0.4, "BTCUSDT": 0.5})
    │
    ├─ TargetWeights(weights=..., timestamp=now)
    │
    ├─ optimizer.validate(target)                    ← Stage 4
    │   ├─ Rate-limit check
    │   ├─ Symbol filter (whitelist/blacklist)
    │   ├─ Per-asset weight clipping [0, 0.25]
    │   ├─ Exposure scaling (gross ≤ 1.0)
    │   ├─ Cash floor enforcement (≥ 5% cash)
    │   └─ Turnover cap (Δw ≤ 0.5)
    │   └─ → ValidatedWeights
    │
    ├─ execution_engine.execute(validated)            ← Stage 5
    │   ├─ Snapshot mark prices
    │   ├─ Compute NAV
    │   ├─ compute_order_intents()
    │   │   ├─ target_qty = weight * NAV / price
    │   │   ├─ delta = target_qty - current_qty
    │   │   ├─ Skip if weight_delta < min_weight_delta
    │   │   └─ Round to step_size
    │   ├─ Sort: sells first, then buys
    │   └─ Submit each order to exchange
    │   └─ → RebalanceResult
    │
    └─ event_bus.publish(RebalanceCompleteEvent)      ← Observability
        └─ strategy.on_rebalance_complete()
```

**Rationale for direct calls (not EventBus):**
- The pipeline is inherently sequential (weights → risk → execute)
- Nesting `EventBus.publish()` calls creates fragile, hard-to-debug chains
- The strategy controls "when" to emit signals
- Only `REBALANCE_COMPLETE` is published for post-execution observability

---

## 9. Exchange Connector Architecture

### Base Classes (`connectors/base.py`)

```
AbstractDataFeed (ABC)
    ├─ AbstractOrderBookFeed    → depth data, slippage pricing
    └─ AbstractKlineFeed (ABC)  → candle data, rolling stats

AbstractClientManager (ABC)
    ├─ KlineClientManager       → multiplex kline socket
    └─ OrderBookClientManager   → multiplex depth socket

SpotExchangeConnector (ABC)     → REST orders, balances, account state
FuturesExchangeConnector (ABC)  → + positions, leverage, margin
```

### Account State (SpotExchangeConnector)

| Field | Type | Description |
|-------|------|-------------|
| `_balances` | `Dict[str, float]` | `{"USDT": 1000.0, "ETH": 2.5}` |
| `_open_orders` | `Dict[str, OrderedDict]` | Per-symbol open orders |
| `_token_infos` | `Dict[str, dict]` | `{"ETHUSDT": {"step_size": 0.001, "min_notional": 10, "base_asset": "ETH"}}` |

### Supported Venues

| Venue | Spot | Futures | Data | Status |
|-------|------|---------|------|--------|
| Binance | `BinanceSpotExchange` | `BinanceFuturesExchange` | Kline + OB + UserData | Production |
| Aster | `AsterSpotExchange` | `AsterPerpExchange` | Kline + OB | Production |
| Bybit | — | — | — | Enum defined |
| OKX | — | — | — | Enum defined |

---

## 10. Persistence Layer

### Database (`db/database.py`)

- **ORM:** Peewee with `ReconnectPooledPostgresqlDatabase`
- **Timezone:** UTC+8 for all timestamps

### Tables (`db/models.py`)

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `cex_trades` | CEX fill records | symbol, side, price, qty, commission, order_id |
| `dex_trades` | DEX swap events | tx_digest, pool, amounts, sqrt_price |
| `portfolio_snapshots` | Point-in-time portfolio state | nav, cash, pnl, exposure, positions (JSON) |
| `account_snapshots` | Raw exchange balance captures | balances (JSON), total_usd_value |

---

## 11. Configuration

### `config.yaml` → `args` (argparse.Namespace)

```yaml
strategy_id: 1
spot:
  venues: [Binance, Aster]
futures:
  venues: [Binance]
database:
  host: localhost
  port: 5432
  name: elysian
risk:                          # Optional — overrides RiskConfig defaults
  max_weight_per_asset: 0.3
  min_cash_weight: 0.10
  min_rebalance_interval_ms: 30000
```

### `config.json` → `config_json` (dict)

```json
{
  "Spot Trading Pairs": {
    "Binance Pairs": ["ETHUSDT", "BTCUSDT", "SOLUSDT"],
    "Binance Symbols": ["ETH", "BTC", "SOL"],
    "Aster Pairs": [...],
    "Aster Symbols": [...]
  },
  "Futures Trading Pairs": { ... },
  "Spot Total Tokens": ["ETH", "BTC", "SOL"],
  "Futures Total Tokens": [...]
}
```

### `.env` — Credentials

```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
ASTER_API_KEY=...
ASTER_API_SECRET=...
DATABASE_HOST=...
DATABASE_PORT=...
DATABASE_NAME=...
DATABASE_USER=...
DATABASE_PASSWORD=...
```

---

## 12. Logging

All components use consistent log prefixes with structured severity:

| Prefix | Component | Example |
|--------|-----------|---------|
| `[Runner]` | StrategyRunner | `[Runner] Pipeline complete: MyStrategy.portfolio -> Optimizer -> ExecutionEngine` |
| `[Strategy]` | SpotStrategy | `[Strategy] submit_weights: 3 symbols, sum=0.9000` |
| `[Optimizer]` | PortfolioOptimizer | `[Optimizer] Clipped 1 symbols: {'ETHUSDT': 0.05}` |
| `[ExecutionEngine]` | ExecutionEngine | `[ExecutionEngine] Intent: Buy ETHUSDT qty=1.500000 @ ~3200.0000` |
| `[Portfolio]` | Portfolio | `[Portfolio] New max drawdown: 0.0000% -> 2.3456%` |

**Severity guidelines:**
- `INFO` — pipeline transitions, state changes, summaries
- `WARNING` — rejected signals, rate limits, new max drawdown
- `ERROR` — order failures, sync failures (with `exc_info=True`)
- `DEBUG` — skipped legs, intermediate calculations

---

## 13. Error Handling & Resilience

### Strategy Hook Isolation

```python
async def _dispatch_kline(self, event):
    try:
        await self.on_kline(event)
    except Exception as e:
        logger.error(f"on_kline error: {e}", exc_info=True)
    # Exception does NOT propagate to worker or EventBus
```

### EventBus Error Isolation

```python
for cb in subscribers:
    try:
        await cb(event)
    except Exception:
        logger.error(...)
    # Other subscribers still fire
```

### Execution Engine Per-Order Isolation

Each order intent is submitted independently. One failure doesn't block others:
```python
for intent in sells + buys:
    ok = await self._submit_order(intent)
    if ok: submitted += 1
    else:  failed += 1; errors.append(...)
```

### Shutdown

```
try:
    ... (main loop)
except asyncio.CancelledError:
    logger.warning("Cancelled by user")
except Exception:
    logger.error("Fatal error", exc_info=True)
    raise
finally:
    exchange.cleanup()
    portfolio.stop()
    strategy.stop()
    _cleanup([])  # stop all client managers
```

---

## 14. Writing a New Strategy

### Minimal Example

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent

class MyStrategy(SpotStrategy):
    async def on_kline(self, event: KlineEvent):
        price = event.kline.close
        # Your alpha logic here
        weights = {"ETHUSDT": 0.5, "BTCUSDT": 0.4}
        result = await self.submit_weights(weights)
```

### Available to Strategies

| Method | Description |
|--------|-------------|
| `self.submit_weights(weights, metadata)` | Send weights through risk → execution pipeline |
| `self.get_exchange(venue)` | Direct exchange access (escape hatch) |
| `self.get_feed(symbol)` | Access raw feed data |
| `self.get_current_price(pair, side)` | Smart price lookup (direct → inverted → synthetic) |
| `self.get_current_vol(symbol)` | Annualised rolling vol in bps |
| `self.run_heavy(fn, *args)` | Offload CPU work to ProcessPoolExecutor |
| `self.portfolio` | Full portfolio state (cash, positions, weights, risk) |
| `self.args` | Parsed config.yaml |
| `self.config_json` | Parsed config.json |

### Running a Strategy

```python
from elysian_core.run_strategy import StrategyRunner

runner = StrategyRunner()
strategy = MyStrategy(exchanges={}, event_bus=EventBus())
await runner.run(strategy=strategy)
```

The runner handles all wiring: EventBus creation, exchange init, feed setup, pipeline injection.

---

## 15. Performance Characteristics

| Metric | Value |
|--------|-------|
| Feed latency | <100ms exchange → feed update |
| Event dispatch | <1ms feed → strategy hook (in-process, zero serialization) |
| Order latency | <500ms signal → exchange API call |
| Kline throughput | ~1000 msg/s per socket |
| OB throughput | ~10,000 msg/s per socket (~10/s per symbol) |
| Queue capacity | 10,000 messages max (prevents unbounded memory) |
| Memory footprint | ~200MB base + ~50MB per 1000 symbols |
| CPU | 1-2 cores for I/O, scales with strategy complexity |

---

## 16. Future Expansion Points

- **Multi-exchange portfolios** — `sync_from_exchange()` is designed to be called once per venue; `_cash_dict` supports multi-stablecoin aggregation
- **Futures strategies** — `FuturesExchangeConnector` base class exists with leverage/margin/position management; `RiskConfig` has `max_leverage` and `max_short_weight` fields
- **Backtesting** — Portfolio works standalone (no EventBus required); `mark_to_market()` and `update_position()` can be driven by historical data
- **Dynamic risk** — `RiskConfig` is mutable; tighten limits at runtime during vol spikes or drawdown breakers
- **Custom order types** — `ExecutionEngine` reads `default_order_type` and supports both MARKET and LIMIT; extensible to TWAP/VWAP
