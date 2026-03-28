# NautilusTrader — System Architecture Diagram

> **Repository**: [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)  
> **Version analysed**: `develop` branch (March 2026)  
> **Engine paradigm**: Deterministic, event-driven, single-threaded kernel · Rust-native core · Python control plane

---

## 1. 30,000-foot View — System Paradigm

NautilusTrader is structured around a single **`NautilusKernel`** per OS process (a "trader instance" or "node").  
The kernel is the innermost ring; everything else — venues, strategies, actors — is plugged in around it through the **MessageBus** and the **ports-and-adapters** pattern.

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│  OS PROCESS  (one TradingNode / BacktestNode per process)                                │
│                                                                                          │
│  ┌────────────────────────────────────────────────────────────────────────────────────┐  │
│  │  NautilusKernel  (single-threaded event loop — LMAX disruptor-inspired)            │  │
│  │                                                                                    │  │
│  │   MessageBus ◄──────────────────────────────────────────────────────────────────  │  │
│  │       │  Pub/Sub · Req/Rep · Command/Event                                         │  │
│  │       ▼                                                                            │  │
│  │   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │  │
│  │   │  Cache   │  │DataEngine│  │ExecEngine│  │RiskEngine│  │   Portfolio      │   │  │
│  │   └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘   │  │
│  │                                                                                    │  │
│  │   ┌──────────────────────────────────────────────────────────────────────────┐    │  │
│  │   │  Actor / Strategy Registry  (N strategies, M actors — all on same bus)   │    │  │
│  │   └──────────────────────────────────────────────────────────────────────────┘    │  │
│  └────────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                          │
│  ┌──────────────────────────────────┐   ┌──────────────────────────────────┐            │
│  │  DataClient adapters             │   │  ExecutionClient adapters        │            │
│  │  (one per venue / data feed)     │   │  (one per venue / account)       │            │
│  └──────────────────────────────────┘   └──────────────────────────────────┘            │
│          │ async WebSocket / REST                │ async WebSocket / REST               │
└──────────┼──────────────────────────────────────┼──────────────────────────────────────┘
           ▼                                       ▼
   Exchange A  Exchange B  Exchange C …     (same venues, separate clients)
```

**Key design decisions:**

| Decision | Rationale |
|---|---|
| One node per process | Avoids shared global state (Tokio runtime, loggers, FORCE_STOP flag) |
| Single-threaded kernel | Deterministic event ordering; backtest ↔ live parity |
| Ports & adapters | Any REST/WebSocket venue integrated without changing core |
| Shared MessageBus | All strategies & actors subscribe; no direct coupling between them |
| Unified Portfolio | Single portfolio object aggregates across all venues & strategies |

---

## 2. Class Hierarchy

### 2a. Component & Actor Trait Hierarchy (Rust core → Python surface)

```mermaid
classDiagram
    direction TB

    class Component {
        <<trait>>
        +register(kernel)
        +start()
        +stop()
        +reset()
        +dispose()
        +state: ComponentState
    }

    class Actor {
        <<trait>>
        +handle(message)
        +actor_id: ActorId
    }

    class NautilusComponent {
        <<Python/Cython base>>
        +clock: Clock
        +cache: Cache
        +msgbus: MessageBus
        +log: Logger
        +subscribe(topic)
        +publish(topic, msg)
    }

    class Actor_PY {
        <<nautilus_trader.common.actor>>
        +on_start()
        +on_stop()
        +on_data(data)
        +on_event(event)
        +on_signal(signal)
        +request_data()
        +subscribe_quote_ticks()
        +subscribe_bars()
    }

    class Strategy {
        <<nautilus_trader.trading.strategy>>
        +on_order_filled(event)
        +on_position_opened(event)
        +submit_order(order)
        +cancel_order(order)
        +modify_order(order)
        +close_position(position)
    }

    class DataEngine {
        +process(data)
        +subscribe(client_id, data_type)
        +request(data_type, callback)
    }

    class ExecutionEngine {
        +execute(command)
        +process(event)
        +reconcile_state()
    }

    class RiskEngine {
        +check_submit_order(command) bool
        +check_modify_order(command) bool
        +check_submit_order_list(command) bool
    }

    class Portfolio {
        +net_position(instrument_id)
        +net_exposure(venue)
        +unrealized_pnl(venue)
        +realized_pnl(venue)
        +analyzer: PortfolioAnalyzer
    }

    Component <|-- NautilusComponent : implements
    Actor <|-- NautilusComponent : implements
    NautilusComponent <|-- Actor_PY : extends
    Actor_PY <|-- Strategy : extends
    NautilusComponent <|-- DataEngine
    NautilusComponent <|-- ExecutionEngine
    NautilusComponent <|-- RiskEngine
    NautilusComponent <|-- Portfolio
```

### 2b. Node / Kernel Hierarchy

```mermaid
classDiagram
    direction TB

    class NautilusKernelConfig {
        trader_id: str
        cache: CacheConfig
        message_bus: MessageBusConfig
        data_engine: DataEngineConfig
        risk_engine: RiskEngineConfig
        exec_engine: ExecEngineConfig
        portfolio: PortfolioConfig
    }

    class NautilusKernel {
        +msgbus: MessageBus
        +cache: Cache
        +data_engine: DataEngine
        +exec_engine: ExecutionEngine
        +risk_engine: RiskEngine
        +portfolio: Portfolio
        +clock: Clock
        +initialize()
        +start()
        +stop()
        +dispose()
    }

    class TradingNodeConfig {
        trader_id: str
        data_clients: dict[str, DataClientConfig]
        exec_clients: dict[str, ExecClientConfig]
        timeout_connection: float
        timeout_reconciliation: float
    }

    class TradingNode {
        +kernel: NautilusKernel
        +add_strategy(strategy)
        +add_actor(actor)
        +add_data_client(client)
        +add_exec_client(client)
        +run()
        +stop()
    }

    class BacktestNodeConfig {
        venues: list[BacktestVenueConfig]
        data: list[BacktestDataConfig]
        engine: BacktestEngineConfig
    }

    class BacktestNode {
        +kernel: NautilusKernel
        +add_venue(config)
        +run()
        +get_result() BacktestResult
    }

    class BacktestEngine {
        +add_instrument(instrument)
        +add_data(data)
        +run(start, end)
        +trader: Trader
    }

    NautilusKernelConfig <|-- TradingNodeConfig
    NautilusKernelConfig <|-- BacktestNodeConfig
    NautilusKernel --* TradingNode : owns
    NautilusKernel --* BacktestNode : owns
    BacktestNode --* BacktestEngine : orchestrates
```

### 2c. Adapter Hierarchy (Ports & Adapters)

```mermaid
classDiagram
    direction TB

    class DataClient {
        <<abstract>>
        +client_id: ClientId
        +subscribe(data_type, instrument_id)
        +unsubscribe(data_type, instrument_id)
        +request(data_type, params, callback)
    }

    class LiveDataClient {
        <<abstract>>
        +connect()
        +disconnect()
    }

    class ExecutionClient {
        <<abstract>>
        +venue: Venue
        +account_id: AccountId
        +submit_order(command)
        +cancel_order(command)
        +modify_order(command)
        +generate_order_status_report()
    }

    class LiveExecutionClient {
        <<abstract>>
        +connect()
        +disconnect()
        +reconcile_state()
    }

    class BinanceDataClient
    class BinanceExecClient
    class InteractiveBrokersDataClient
    class InteractiveBrokersExecClient
    class BybitDataClient
    class BybitExecClient
    class SimulatedDataClient
    class SimulatedExecClient

    DataClient <|-- LiveDataClient
    DataClient <|-- SimulatedDataClient
    ExecutionClient <|-- LiveExecutionClient
    ExecutionClient <|-- SimulatedExecClient
    LiveDataClient <|-- BinanceDataClient
    LiveDataClient <|-- InteractiveBrokersDataClient
    LiveDataClient <|-- BybitDataClient
    LiveExecutionClient <|-- BinanceExecClient
    LiveExecutionClient <|-- InteractiveBrokersExecClient
    LiveExecutionClient <|-- BybitExecClient
```

---

## 3. Multi-Venue · Multi-Strategy · Multi-Portfolio Paradigm

### The Core Insight

NautilusTrader deliberately has **one** portfolio and **one** cache per node. Multi-dimensionality is achieved by **namespacing through IDs**, not by instantiating separate portfolio objects.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SINGLE TRADING NODE                                │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  STRATEGIES (all share the same MessageBus, Cache, Portfolio)        │   │
│  │                                                                      │   │
│  │  StrategyA (id="EMACross-BTCUSDT-001")   ← instrument: BTC/USDT      │   │
│  │    ├─ subscribes to: QuoteTick[BINANCE:BTCUSDT]                      │   │
│  │    └─ submits orders to: BINANCE exec client                         │   │
│  │                                                                      │   │
│  │  StrategyB (id="MarketMaker-ETHUSDT-001") ← instrument: ETH/USDT     │   │
│  │    ├─ subscribes to: OrderBook[BINANCE:ETHUSDT]                      │   │
│  │    └─ submits orders to: BINANCE exec client                         │   │
│  │                                                                      │   │
│  │  StrategyC (id="StatArb-ES-NQ-001")      ← cross-venue arb           │   │
│  │    ├─ subscribes to: Bar[CME:ES] and Bar[CME:NQ]                     │   │
│  │    └─ submits orders to: IB exec client (CME)                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  VENUE ADAPTERS                                                      │   │
│  │                                                                      │   │
│  │  DataClients:   [BINANCE_SPOT] [BINANCE_FUTURES] [IB]               │   │
│  │  ExecClients:   [BINANCE_SPOT] [BINANCE_FUTURES] [IB]               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  PORTFOLIO (unified — aggregates all venues & strategies)            │   │
│  │                                                                      │   │
│  │  Accounts:    { BINANCE_SPOT: Account, BINANCE_FUTURES: Account,    │   │
│  │                 IB: Account }                                        │   │
│  │  Positions:   keyed by (PositionId → strategy_id + instrument_id)   │   │
│  │  Net PnL:     queryable per venue, per instrument, or total          │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Multi-Portfolio Isolation Pattern

NautilusTrader does **not** provide separate Portfolio objects per strategy. Instead, isolation is achieved by:

1. **Strategy ID namespacing** — every order and position is tagged with its `strategy_id`, so per-strategy PnL can be reconstructed from the unified portfolio/cache.
2. **Separate nodes in separate processes** — for hard isolation (e.g., separate risk limits), run two `TradingNode` processes; each gets its own kernel, cache, and portfolio.

```
Process 1 (TradingNode "Alpha-Fund")          Process 2 (TradingNode "Beta-Fund")
├─ Strategy: LongOnlyEquity                   ├─ Strategy: HFT-MarketMaker
├─ Venue: Interactive Brokers                 ├─ Venue: Binance Futures
├─ Portfolio: tracks IB account               ├─ Portfolio: tracks BINANCE_FUTURES
└─ Risk limits: position size $1M             └─ Risk limits: delta-neutral ±$50K
```

---

## 4. Data Flow

```mermaid
sequenceDiagram
    participant V as Venue WebSocket
    participant DC as DataClient (Adapter)
    participant DE as DataEngine
    participant MB as MessageBus
    participant CA as Cache
    participant S1 as Strategy A
    participant S2 as Strategy B

    V->>DC: Raw market data (venue protocol)
    DC->>DC: normalize → NautilusTrader data types
    DC->>DE: DataEngine.process(QuoteTick / Bar / OrderBook …)
    DE->>CA: cache.add(data)
    DE->>MB: publish(topic="data.quotes.BINANCE:BTCUSDT", data)
    MB->>S1: on_quote_tick(tick)  [subscribed]
    MB->>S2: on_quote_tick(tick)  [subscribed]
    S1->>S1: strategy logic → generate order
    S1->>MB: publish SubmitOrder command
```

---

## 5. Execution Flow

```mermaid
sequenceDiagram
    participant S as Strategy
    participant MB as MessageBus
    participant RE as RiskEngine
    participant EE as ExecutionEngine
    participant EC as ExecClient (Adapter)
    participant V as Venue

    S->>MB: SubmitOrder(instrument=BINANCE:BTCUSDT, side=BUY, qty=0.1)
    MB->>RE: pre-trade risk check
    RE-->>MB: OrderDenied (if risk breach) OR pass-through
    MB->>EE: route command
    EE->>EC: submit_order(command)
    EC->>V: REST/WebSocket order submission
    V-->>EC: OrderAccepted / OrderRejected event
    EC->>EE: generate_order_status_event()
    EE->>MB: publish OrderAccepted
    MB->>S: on_order_accepted(event)
    V-->>EC: OrderFilled event
    EC->>EE: generate_fill_event()
    EE->>MB: publish OrderFilled
    MB->>S: on_order_filled(event)
    EE->>MB: publish PositionOpened / PositionChanged
    MB->>S: on_position_opened(event)
```

---

## 6. Component State Machine

Every component (DataEngine, ExecutionEngine, RiskEngine, Strategy, Actor) follows this FSM:

```mermaid
stateDiagram-v2
    [*] --> PRE_INITIALIZED
    PRE_INITIALIZED --> STARTING : start()
    STARTING --> RUNNING
    RUNNING --> STOPPING : stop()
    STOPPING --> STOPPED
    STOPPED --> STARTING : start() [resume]
    RUNNING --> DEGRADING : degrade()
    DEGRADING --> DEGRADED
    DEGRADED --> STOPPING : stop()
    RUNNING --> FAULTING : fault()
    FAULTING --> FAULTED
    FAULTED --> [*]
    STOPPED --> DISPOSING : dispose()
    DISPOSING --> DISPOSED
    DISPOSED --> [*]
    STOPPED --> RESETTING : reset()
    RESETTING --> READY
    READY --> STARTING : start()
    PRE_INITIALIZED --> READY : register(kernel)
```

---

## 7. Codebase Layer Map

```
nautilus_trader/                     crates/  (Rust)
│                                    │
├── core/          ← constants       ├── core/        ← primitives, UUID, math
├── common/        ← shared infra    ├── common/      ← MessageBus, Clock, Cache
├── model/         ← domain model    ├── model/       ← Price, Quantity, Order, Position
├── accounting/    ← account types   ├── data/        ← DataEngine
├── cache/         ← Cache impl      ├── execution/   ← ExecutionEngine
├── data/          ← DataEngine      ├── risk/        ← RiskEngine
├── execution/     ← ExecEngine      ├── portfolio/   ← Portfolio
├── risk/          ← RiskEngine      ├── trading/     ← Actor, Strategy base
├── portfolio/     ← Portfolio       ├── system/      ← NautilusKernel
├── trading/       ← Strategy base   ├── backtest/    ← SimulatedExchange
├── indicators/    ← built-in        ├── live/        ← LiveNode runtime
├── analysis/      ← statistics      ├── adapters/    ← Binance, IB, Bybit …
├── persistence/   ← Parquet/catalog ├── pyo3/        ← Python bindings (PyO3)
├── serialization/ ← msgpack/JSON    └── network/     ← async WebSocket/REST
├── adapters/      ← venue adapters
├── backtest/      ← BacktestNode
├── live/          ← TradingNode
└── system/        ← NautilusKernel
```

**Dependency flow** (arrows = depends on):

```
Strategy / Actor
      │
      ▼
  NautilusKernel  ──►  MessageBus  ──►  Cache
      │                    │
      ▼                    ▼
  DataEngine          ExecutionEngine  ──►  RiskEngine
      │                    │
      ▼                    ▼
  DataClient          ExecutionClient
  (per venue)         (per venue)
      │                    │
      └──────────┬──────────┘
                 ▼
           Venue / Exchange
```

---

## 8. Environment Context Swap Table

The same strategy code runs in all three environments. Only the injected implementations change:

| Component | Backtest | Sandbox | Live |
|---|---|---|---|
| `Clock` | `TestClock` (simulated time) | `LiveClock` | `LiveClock` |
| `DataClient` | `BacktestDataClient` (replays Parquet) | Live adapter | Live adapter |
| `ExecutionClient` | `SimulatedExchangeClient` | `SimulatedExchangeClient` | Live adapter |
| `MessageBus` | In-memory | In-memory or Redis | In-memory or Redis |
| `Cache` | In-memory | In-memory or Redis | In-memory or Redis |
| Node type | `BacktestNode` / `BacktestEngine` | `TradingNode` (sandbox mode) | `TradingNode` |

---

## 9. Multi-Venue Configuration Pattern (Live)

```python
config = TradingNodeConfig(
    trader_id="MyTrader-001",

    # ── Data sources ── one DataClient per venue/account-type
    data_clients={
        "BINANCE_SPOT":    BinanceDataClientConfig(account_type=SPOT),
        "BINANCE_FUTURES": BinanceDataClientConfig(account_type=USDT_FUTURES),
        "IB":              InteractiveBrokersDataClientConfig(),
    },

    # ── Execution venues ── one ExecClient per venue/account-type
    exec_clients={
        "BINANCE_SPOT":    BinanceExecClientConfig(account_type=SPOT),
        "BINANCE_FUTURES": BinanceExecClientConfig(account_type=USDT_FUTURES),
        "IB":              InteractiveBrokersExecClientConfig(),
    },
)

node = TradingNode(config=config)

# ── Multiple strategies ── each independently subscribes/trades
node.add_strategy(EMACrossStrategy(config=StrategyConfig(instrument_id="BINANCE_SPOT:BTCUSDT")))
node.add_strategy(MarketMakerStrategy(config=StrategyConfig(instrument_id="BINANCE_FUTURES:ETHUSDT-PERP")))
node.add_strategy(StatArbStrategy(config=StrategyConfig(instruments=["IB:ES", "IB:NQ"])))

# ── Optional: custom actors (e.g. signal publishers, portfolio monitors)
node.add_actor(RiskMonitorActor())
node.add_actor(SignalPublisherActor())

node.run()   # blocks; starts event loop
```

---

## 10. Summary: Architectural Principles

| Principle | Implementation |
|---|---|
| **Event-driven** | All state changes are events on the MessageBus; no polling |
| **Domain-driven design** | Rich domain model: `Order`, `Position`, `Instrument`, `Price`, `Quantity` etc. |
| **Ports & adapters** | `DataClient` / `ExecutionClient` are the ports; venue connectors are adapters |
| **Single-threaded kernel** | Deterministic ordering; network I/O on background async threads |
| **Crash-only** | Panics abort the process; state re-hydrated from Redis on restart |
| **Fail-fast** | Arithmetic overflow, NaN prices, invalid types → immediate panic |
| **Research-to-live parity** | Identical strategy code across backtest, sandbox, live via clock/adapter swaps |
| **Multi-venue** | N DataClients + N ExecClients per node, all routed through one kernel |
| **Multi-strategy** | N strategies registered on the same node, isolated by `strategy_id` namespacing |
| **Multi-portfolio** | Soft isolation: per-strategy PnL via ID tagging; hard isolation: separate processes |
