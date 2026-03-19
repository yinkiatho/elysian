# Elysian Architecture

## System Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                                                                                │
│                           STRATEGY RUNNER (Main Entry)                          │
│               Single asyncio event loop orchestrating all components            │
│           Creates EventBus, wires exchanges/feeds/strategy, runs forever        │
│                                                                                │
└──────────┬─────────────────────────────────┬───────────────────────────────────┘
           │                                 │
           │  _setup_config()                │  EventBus created here
           │  _setup_exchanges()             │  Injected into all managers
           │  _setup_*_data_feeds()          │  and exchange connectors
           │                                 │
     ┌─────┴──────────┐              ┌──────┴────────────────────────────────┐
     │                 │              │                                       │
     │                 │              │         EVENT BUS                     │
     │                 │              │     (elysian_core/core/event_bus.py)  │
     │                 │              │                                       │
     │                 │              │  In-process async pub/sub:            │
     │                 │              │  • subscribe(EventType, callback)     │
     │                 │              │  • unsubscribe(EventType, callback)   │
     │                 │              │  • publish(event) — awaited for       │
     │                 │              │    natural backpressure               │
     │                 │              │                                       │
     │                 │              │  Zero serialization overhead —        │
     │                 │              │  events are direct object refs        │
     │                 │              │                                       │
     │                 │              └──────┬────────────────────────────────┘
     │                 │                     │
     │                 │        ┌────────────┴──────────────┐
     │                 │        │  publishes                │  subscribes
     │                 │        │                           │
┌────▼─────────────────▼──┐  ┌─▼───────────────────────┐  │
│  MARKET DATA LAYER      │  │  ORDER MANAGEMENT LAYER  │  │
│  (elysian_core/         │  │  (elysian_core/oms/)     │  │
│   connectors/)          │  │                          │  │
├─────────────────────────┤  ├──────────────────────────┤  │
│                         │  │                          │  │
│ BinanceDataConnectors:  │  │ oms.py:                  │  │
│ • KlineClientMgr (*)    │  │ • AbstractOMS            │  │
│   - register_feed()     │  │ • place_order()          │  │
│   - run_multiplex_      │  │ • cancel_order()         │  │
│     feeds()             │  │ • get_balance()          │  │
│   - set_event_bus()     │  │                          │  │
│   - worker → publish    │  │ order_tracker.py:        │  │
│     KlineEvent          │  │ • OrderTracker           │  │
│   - queue (10k max)     │  │ • track_order()          │  │
│                         │  │ • get_status()           │  │
│ • OBClientMgr (*)       │  │ • fill_history()         │  │
│   - register_feed()     │  │                          │  │
│   - run_multiplex_      │  │                          │  │
│     feeds()             │  │                          │  │
│   - set_event_bus()     │  │                          │  │
│   - worker → publish    │  │                          │  │
│     OrderBookUpdate     │  │                          │  │
│     Event               │  │                          │  │
│   - queue (10k max)     │  │                          │  │
│                         │  │                          │  │
│ BinanceExchange:        │  │                          │  │
│ • BinanceSpotExchange   │  │                          │  │
│   - REST API orders     │  │                          │  │
│   - user_data_manager   │  │                          │  │
│     → publishes         │  │                          │  │
│     OrderUpdateEvent    │  │                          │  │
│     BalanceUpdateEvent  │  │                          │  │
│                         │  │                          │  │
│ (*) = Multiplex design  │  │                          │  │
│ Single socket per       │  │                          │  │
│ client manager serves   │  │                          │  │
│ multiple feeds          │  │                          │  │
└────────┬────────────────┘  └─────────┬────────────────┘  │
         │                             │                    │
         └──────────┬──────────────────┘                    │
                    │                                       │
          ┌─────────▼──────────┬────────────────────────────┘
          │                    │
  ┌───────▼────────────┐  ┌───▼───────────────────┐
  │  STRATEGY LAYER    │  │  MARKET MAKER LAYER   │
  │  (elysian_core/    │  │  (elysian_core/       │
  │   strategy/)       │  │   market_maker/)      │
  ├────────────────────┤  ├───────────────────────┤
  │                    │  │                       │
  │ base_strategy.py:  │  │ order_book.py:        │
  │ • SpotStrategy     │  │ • OrderBook analysis  │
  │   base class       │  │ • spread calc         │
  │                    │  │ • level extraction    │
  │ Hook functions:    │  │                       │
  │ • on_start()       │  │ order_management.py:  │
  │ • on_stop()        │  │ • create range order  │
  │ • on_kline()       │  │ • update positions    │
  │ • on_orderbook_    │  │ • manage lifecycle    │
  │   update()         │  │                       │
  │ • on_order_        │  │ order_tracker.py:     │
  │   update()         │  │ • track MM orders     │
  │ • on_balance_      │  │ • monitor fills       │
  │   update()         │  │ • adjust levels       │
  │ • run_forever()    │  │                       │
  │                    │  │                       │
  │ Utilities:         │  │                       │
  │ • get_exchange()   │  │                       │
  │ • get_current_     │  │                       │
  │   price()          │  │                       │
  │ • get_current_     │  │                       │
  │   vol()            │  │                       │
  │ • run_heavy()      │  │                       │
  │   (ProcessPool)    │  │                       │
  │                    │  │                       │
  │ test_strategy.py:  │  │                       │
  │ • TestPrint        │  │                       │
  │   Strategy         │  │                       │
  └────────┬───────────┘  └───────────────────────┘
           │
  ┌────────▼──────────────────┐
  │   EVENT FLOW              │
  ├───────────────────────────┤
  │                           │
  │  1. WebSocket message     │
  │     arrives in worker     │
  │         ↓                 │
  │  2. Worker mutates feed   │
  │     (feed._kline = ...)   │
  │         ↓                 │
  │  3. Worker publishes      │
  │     typed event to        │
  │     EventBus              │
  │         ↓                 │
  │  4. EventBus dispatches   │
  │     to SpotStrategy       │
  │     hook (awaited)        │
  │         ↓                 │
  │  5. Strategy.on_kline()   │
  │     or on_orderbook_      │
  │     update() fires        │
  │         ↓                 │
  │  6. Strategy logic:       │
  │     get_current_price(),  │
  │     run_heavy(), or       │
  │     exchange.place_order()│
  │         ↓                 │
  │  7. Order fills → user    │
  │     data stream emits     │
  │     OrderUpdateEvent      │
  │         ↓                 │
  │  8. on_order_update()     │
  │     fires in strategy     │
  │                           │
  └───────────────────────────┘
           │
  ┌────────▼──────────────────┐
  │  CORE MODELS              │
  │  (elysian_core/core/)     │
  ├───────────────────────────┤
  │                           │
  │ events.py:                │
  │ • EventType enum          │
  │ • KlineEvent              │
  │ • OrderBookUpdateEvent    │
  │ • OrderUpdateEvent        │
  │ • BalanceUpdateEvent      │
  │ (all frozen dataclasses)  │
  │                           │
  │ event_bus.py:             │
  │ • EventBus                │
  │   subscribe/publish       │
  │                           │
  │ market_data.py:           │
  │ • Kline (OHLCV)          │
  │ • OrderBook (bids/asks)  │
  │                           │
  │ order.py:                 │
  │ • Order, LimitOrder       │
  │ • RangeOrder              │
  │                           │
  │ enums.py:                 │
  │ • Side, Venue, OrderStatus│
  │ • OrderType, TradeType    │
  │                           │
  │ portfolio.py:             │
  │ • Portfolio, Position     │
  │                           │
  └───────────────────────────┘
           │
  ┌────────▼──────────────────┐
  │  PERSISTENCE LAYER        │
  │  (elysian_core/db/)       │
  ├───────────────────────────┤
  │                           │
  │ database.py:              │
  │ • PostgreSQL connection   │
  │ • connection pooling      │
  │ • error handling          │
  │                           │
  │ models.py:                │
  │ • Peewee ORM models       │
  │ • BinanceTrade            │
  │ • OrderHistory            │
  │ • PortfolioState          │
  │                           │
  │ Storage Backends:         │
  │ • PostgreSQL              │
  │   (trade history,         │
  │    order records,         │
  │    portfolio state)       │
  │                           │
  │ • Redis                   │
  │   (volatility cache,      │
  │    live order tracking,   │
  │    balance cache)         │
  │                           │
  │ • File System             │
  │   (logs, config,          │
  │    backtesting data)      │
  │                           │
  └───────────────────────────┘
```

## Component Interaction Flow

### Startup Sequence

```
StrategyRunner.__init__()
    │
    ├─ Load configs (YAML, JSON)
    ├─ Initialize client managers
    │  ├─ BinanceKlineClientManager
    │  ├─ BinanceOrderBookClientManager
    │  ├─ BinanceFuturesKlineClientManager
    │  ├─ BinanceFuturesOrderBookClientManager
    │  ├─ AsterKlineClientManager
    │  ├─ AsterOrderBookClientManager
    │  ├─ AsterPerpKlineClientManager
    │  └─ AsterPerpOrderBookClientManager
    │
    └─ Setup logging & environment

StrategyRunner.run(strategy=SpotStrategy)
    │
    ├─ _setup_config()
    │  └─ Load trading pairs from config.json (Binance, Aster, Futures)
    │
    ├─ Create shared EventBus
    │  └─ self.event_bus = EventBus()
    │
    ├─ _setup_exchanges()
    │  ├─ BinanceSpotExchange(event_bus=self.event_bus)
    │  │  └─ Injects event_bus into user_data_manager
    │  ├─ BinanceFuturesExchange
    │  └─ AsterPerpExchange
    │
    ├─ Inject event_bus into data client managers
    │  ├─ binance_kline_manager.set_event_bus(self.event_bus)
    │  └─ binance_ob_manager.set_event_bus(self.event_bus)
    │
    ├─ Wire strategy (if provided)
    │  ├─ strategy._event_bus = self.event_bus
    │  ├─ strategy._exchanges = {Venue.BINANCE: exchange}
    │  └─ await strategy.start()
    │     ├─ Subscribes on_kline, on_orderbook_update,
    │     │  on_order_update, on_balance_update to EventBus
    │     └─ Calls strategy.on_start()
    │
    ├─ await binance_exchange.run()
    │  └─ Initializes AsyncClient, starts user data stream
    │
    └─ asyncio.gather(
         _setup_binance_data_feeds(),   ← blocks forever (multiplex feeds)
         strategy.run_forever()          ← optional long-running coroutine
       )
```

### Runtime Event Loop (Single asyncio.run())

```
asyncio.run() — ONE event loop for everything
    │
    ├─ BinanceKlineClientManager.run_multiplex_feeds()
    │  ├─ Network reader task
    │  │  └─ Reads from multiplex WebSocket
    │  │     └─ Queues messages to self._queue
    │  │
    │  └─ Worker pool (4 workers)
    │     └─ Worker dequeues message
    │        ├─ Parses kline data
    │        ├─ Mutates feed: feed._kline = kline
    │        ├─ Calculates rolling volatility
    │        └─ if self._event_bus is not None:
    │           await self._event_bus.publish(KlineEvent(...))
    │              └─ EventBus dispatches to strategy._dispatch_kline()
    │                 └─ try: await strategy.on_kline(event)
    │
    ├─ BinanceOrderBookClientManager.run_multiplex_feeds()
    │  ├─ Network reader task
    │  │  └─ Reads from multiplex WebSocket
    │  │     └─ Queues messages to self._queue
    │  │
    │  └─ Worker pool (4 workers)
    │     └─ Worker dequeues message
    │        ├─ Processes depth update
    │        ├─ Mutates feed: feed._data = orderbook
    │        └─ if self._event_bus is not None:
    │           await self._event_bus.publish(OrderBookUpdateEvent(...))
    │              └─ EventBus dispatches to strategy._dispatch_ob()
    │                 └─ try: await strategy.on_orderbook_update(event)
    │
    ├─ BinanceUserDataClientManager._reader_loop()
    │  └─ On WebSocket message:
    │     ├─ FIRST: Sync callbacks (exchange._handle_user_data)
    │     │  └─ Mutates exchange state (orders, balances)
    │     │
    │     └─ THEN: Async event bus dispatch
    │        ├─ executionReport → OrderUpdateEvent
    │        │  └─ EventBus → strategy.on_order_update()
    │        └─ balanceUpdate → BalanceUpdateEvent
    │           └─ EventBus → strategy.on_balance_update()
    │
    └─ strategy.run_forever()  (optional)
       └─ Long-running coroutine for periodic tasks
          (e.g., rebalancing, timer-based signals)
```

### Multiplex Socket Design

```
Single AsyncClient
    │
    └─ BinanceSocketManager
        │
        └─ multiplex_socket([stream1, stream2, stream3, stream4])
            │
            ├─ WebSocket connection (single TCP)
            │  ├─ klineETHUSDT@1s
            │  ├─ klineBTCUSDT@1s
            │  ├─ klineSOLUSDT@1s
            │  └─ klineBNBUSDT@1s
            │
            └─ On message receipt:
               ├─ Decode message
               ├─ Extract symbol & data
               ├─ Queue message (asyncio.Queue, maxsize=10k)
               └─ Worker dequeues → processes → publishes event
```

## Concurrency Model

```
Main Thread — asyncio.run() — SINGLE EVENT LOOP
    │
    └─ StrategyRunner.run(strategy) (async)
       │
       ├─ await setup_config()
       ├─ Create EventBus (shared by all components)
       ├─ await setup_exchanges()
       ├─ Inject event_bus into managers
       ├─ Wire and start strategy
       │
       └─ asyncio.gather(
            _setup_binance_data_feeds(),   ← runs kline + OB multiplex feeds
            strategy.run_forever()          ← optional strategy coroutine
          )
          │
          ├─ Kline manager: reader task + 4 worker tasks
          │  └─ Workers publish KlineEvent to EventBus
          │
          ├─ OB manager: reader task + 4 worker tasks
          │  └─ Workers publish OrderBookUpdateEvent to EventBus
          │
          ├─ User data manager: reader loop
          │  └─ Publishes OrderUpdateEvent, BalanceUpdateEvent
          │
          └─ Strategy hooks run IN the same event loop
             └─ For CPU-heavy work: strategy.run_heavy(fn, *args)
                └─ Offloads to ProcessPoolExecutor (separate OS process)
                   └─ fn must be a top-level picklable function

KEY DESIGN DECISIONS:
  • Everything runs in ONE asyncio event loop (no ThreadPoolExecutor for feeds)
  • EventBus.publish() is awaited — natural backpressure from strategy to worker
  • ProcessPoolExecutor reserved ONLY for CPU-bound strategy calculations
  • Events carry references to live objects — copy if you need a snapshot
  • Sync callbacks (exchange state mutation) run BEFORE async event dispatch
```

## Event System Architecture

### Event Types (elysian_core/core/events.py)

```
EventType enum:
  ├─ KLINE              → KlineEvent
  ├─ ORDERBOOK_UPDATE   → OrderBookUpdateEvent
  ├─ ORDER_UPDATE       → OrderUpdateEvent
  └─ BALANCE_UPDATE     → BalanceUpdateEvent

All events are @dataclass(frozen=True) — immutable after creation.
event_type field is set automatically via field(default=..., init=False).

KlineEvent:
  ├─ symbol: str        (e.g., "ETHUSDT")
  ├─ venue: Venue       (e.g., Venue.BINANCE)
  ├─ kline: Kline       (OHLCV data)
  └─ timestamp: int     (epoch ms)

OrderBookUpdateEvent:
  ├─ symbol: str
  ├─ venue: Venue
  ├─ orderbook: OrderBook  (bid/ask snapshot)
  └─ timestamp: int

OrderUpdateEvent:
  ├─ symbol: str
  ├─ venue: Venue
  ├─ order: Order       (parsed from execution report)
  └─ timestamp: int

BalanceUpdateEvent:
  ├─ asset: str         (e.g., "USDT")
  ├─ venue: Venue
  ├─ delta: float       (balance change)
  ├─ new_balance: float
  └─ timestamp: int
```

### EventBus (elysian_core/core/event_bus.py)

```
EventBus
  │
  ├─ _subscribers: Dict[EventType, List[Callable]]
  │
  ├─ subscribe(event_type, callback)
  │  └─ Appends callback to subscriber list for event_type
  │
  ├─ unsubscribe(event_type, callback)
  │  └─ Removes callback (no-op if not found)
  │
  └─ publish(event)  [async]
     └─ For each subscriber of event.event_type:
        ├─ await callback(event)        ← sequential, backpressure
        └─ On exception: log and continue to next callback
```

### Event Emission Points

```
BinanceKlineClientManager._worker_coroutine():
  └─ After feed._kline = kline and volatility calculation:
     await self._event_bus.publish(KlineEvent(
         symbol=symbol, venue=Venue.BINANCE,
         kline=kline, timestamp=int(time.time() * 1000)
     ))

BinanceOrderBookClientManager._worker_coroutine():
  └─ After feed._data = orderbook:
     await self._event_bus.publish(OrderBookUpdateEvent(
         symbol=symbol, venue=Venue.BINANCE,
         orderbook=feed._data, timestamp=int(time.time() * 1000)
     ))

BinanceUserDataClientManager._reader_loop():
  └─ After sync callbacks complete:
     ├─ executionReport → _parse_execution_to_order() → OrderUpdateEvent
     └─ balanceUpdate → BalanceUpdateEvent
```

## SpotStrategy Architecture

### Class Hierarchy

```
SpotStrategy (base class — NOT abstract, all hooks are no-op)
    │
    ├─ __init__(exchanges, event_bus, feeds=None, max_heavy_workers=4)
    │
    ├─ Lifecycle:
    │  ├─ start()        → subscribe all hooks to EventBus, call on_start()
    │  ├─ stop()         → unsubscribe, shutdown executor, call on_stop()
    │  └─ run_forever()  → optional long-running coroutine (gathered with feeds)
    │
    ├─ Hook functions (override what you need):
    │  ├─ on_start()              → initialization logic
    │  ├─ on_stop()               → cleanup logic
    │  ├─ on_kline(KlineEvent)    → react to closed candles
    │  ├─ on_orderbook_update(OrderBookUpdateEvent) → react to depth updates
    │  ├─ on_order_update(OrderUpdateEvent)         → react to order fills
    │  └─ on_balance_update(BalanceUpdateEvent)     → react to balance changes
    │
    ├─ Internal dispatch (error isolation):
    │  ├─ _dispatch_kline()    → try/except wraps on_kline()
    │  ├─ _dispatch_ob()       → try/except wraps on_orderbook_update()
    │  ├─ _dispatch_order()    → try/except wraps on_order_update()
    │  └─ _dispatch_balance()  → try/except wraps on_balance_update()
    │
    ├─ Exchange access:
    │  └─ get_exchange(venue)  → returns SpotExchangeConnector
    │
    ├─ Feed access:
    │  └─ get_feed(symbol)     → returns AbstractDataFeed or None
    │
    ├─ Price helpers:
    │  ├─ get_current_price(pair, side) → direct/inverted/synthetic lookup
    │  └─ get_current_vol(symbol)       → annualised rolling vol in bps
    │
    └─ Heavy compute:
       └─ run_heavy(fn, *args) → ProcessPoolExecutor offload
          └─ fn must be top-level picklable (not lambda/method)
```

### Strategy Wiring in StrategyRunner.run()

```
1. runner creates EventBus
2. runner creates exchanges with event_bus injected
3. runner injects event_bus into kline/ob client managers
4. runner sets strategy._event_bus = self.event_bus
5. runner sets strategy._exchanges = {Venue.BINANCE: exchange}
6. runner calls await strategy.start()
   └─ strategy subscribes _dispatch_* to EventBus
7. runner gathers [_setup_data_feeds(), strategy.run_forever()]
   └─ feeds run, events fire, strategy hooks execute
8. On shutdown: runner calls await strategy.stop()
   └─ strategy unsubscribes from EventBus
```

## Data Flow Diagram

```
Exchange (Binance / Aster)
    │
    └─ WebSocket Streams
        │
        ├─ Kline Stream @1s
        │  ├─ {open, high, low, close, volume}
        │  └─ Symbols: ETHUSDT, BTCUSDT, SOLUSDT, etc.
        │
        ├─ OrderBook Stream @100ms
        │  ├─ {bids[], asks[]}
        │  └─ Symbols: ETHUSDT, BTCUSDT, SOLUSDT, etc.
        │
        └─ User Data Stream
           ├─ executionReport (order fills/cancels)
           └─ balanceUpdate (balance changes)

ClientManagers (one per asset class)
    │
    ├─ Multiplex Socket (ONE TCP connection per manager)
    │  └─ Receives all symbols on single WebSocket
    │
    ├─ Queue (asyncio.Queue, FIFO, maxsize=10,000)
    │  └─ Buffers incoming messages
    │
    └─ Worker Pool (4 async workers)
       └─ Dequeue → Parse → Mutate Feed → Publish Event
          │
          └─ EventBus.publish(TypedEvent)
             │
             └─ SpotStrategy hook fires
                ├─ on_kline(KlineEvent)
                ├─ on_orderbook_update(OrderBookUpdateEvent)
                ├─ on_order_update(OrderUpdateEvent)
                └─ on_balance_update(BalanceUpdateEvent)

SpotStrategy
    │
    ├─ Hook receives typed, immutable event
    ├─ Reads feed data via get_feed(), get_current_price()
    ├─ Generates signals
    ├─ Optionally offloads to run_heavy() for CPU work
    └─ Places orders via get_exchange().place_order()
       └─ Order fill comes back as OrderUpdateEvent

Database
    │
    ├─ PostgreSQL
    │  ├─ Trade history
    │  ├─ Order records
    │  └─ Portfolio snapshots
    │
    └─ Redis
       ├─ Live order cache
       ├─ Balance cache
       └─ Volatility data
```

## Configuration Architecture

```
config.yaml
├─ Pools (liquidity pools)
├─ Version name
├─ Database config
│  ├─ PostgreSQL (host, port, name)
│  └─ Redis (host, port)
└─ Networking
   ├─ RPC URL
   ├─ Package Address
   └─ Feed URL

config.json
├─ Spot Trading Pairs
│  ├─ Binance Pairs (ETHUSDT, BTCUSDT)
│  ├─ Binance Symbols (ETH/USDT, BTC/USDT)
│  ├─ Aster Pairs
│  └─ Aster Symbols
├─ Futures Trading Pairs
│  ├─ Binance Futures Pairs
│  ├─ Binance Futures Symbols
│  ├─ Aster Futures Pairs
│  └─ Aster Futures Symbols
├─ Spot Total Tokens
└─ Futures Total Tokens

.env
├─ DATABASE_* (credentials)
├─ REDIS_* (credentials)
├─ BINANCE_API_KEY / API_SECRET
├─ ASTER_API_KEY / API_SECRET
└─ Other exchange credentials
```

## Error Handling & Cleanup

```
StrategyRunner.run(strategy)
    │
    try:
    │  ├─ Setup phase (config, event_bus, exchanges, managers)
    │  ├─ Strategy start (subscribe hooks)
    │  ├─ Exchange run (AsyncClient, user data stream)
    │  └─ asyncio.gather(feeds, strategy.run_forever())
    │
    │  try (inner):
    │  │  └─ Feed and strategy execution
    │  finally:
    │     ├─ await exchange.cleanup()
    │     └─ await strategy.stop()
    │        ├─ Unsubscribe all hooks from EventBus
    │        ├─ Shutdown ProcessPoolExecutor
    │        └─ Call strategy.on_stop()
    │
    except asyncio.CancelledError:
    │  └─ Log warning, graceful exit
    │
    except Exception:
    │  └─ Log error with traceback, re-raise
    │
    finally:
       └─ _cleanup([])
          ├─ Stop all client managers (kline, OB, futures, aster)
          │  ├─ Cancel reader_task
          │  ├─ Cancel worker_tasks[]
          │  ├─ Close WebSocket
          │  └─ Close AsyncClient
          └─ Cancel and gather remaining tasks

SpotStrategy dispatch error isolation:
    │
    └─ Each _dispatch_* wrapper catches exceptions:
       try:
           await self.on_kline(event)
       except Exception as e:
           logger.error(f"on_kline error: {e}", exc_info=True)
       # Exception does NOT propagate to worker or EventBus

EventBus error isolation:
    │
    └─ Each callback exception is caught and logged:
       for cb in subscribers:
           try:
               await cb(event)
           except Exception as e:
               logger.error(...)
       # Other subscribers still fire
```

## Scalability Considerations

### Horizontal Scaling

1. **Multiple Exchanges**: Add new exchange connectors (Bybit, OKX, etc.)
   - Each uses same multiplex architecture and AbstractClientManager base
   - All publish events through shared EventBus
   - Unified Venue enum for exchange identification

2. **Multiple Strategies**: Run multiple SpotStrategy instances
   - Each subscribes to the same EventBus independently
   - EventBus dispatches to all subscribers sequentially
   - Strategies share exchange connectors but have independent state

3. **Worker Scalability**:
   - Currently 4 workers per client manager
   - Increase `max_workers` for higher-throughput symbols
   - Workers are async tasks (not threads), so scaling is lightweight

### Vertical Scaling

1. **CPU-Bound Strategy Logic**:
   - Use `strategy.run_heavy(fn, *args)` to offload to ProcessPoolExecutor
   - Default 4 worker processes (configurable via `max_heavy_workers`)
   - fn must be top-level picklable function

2. **Database**:
   - Add read replicas for analytics
   - Partitioning by date for trade history
   - Materialized views for reporting

3. **Redis**:
   - Cluster mode for cache scaling
   - Sentinel for high availability

## Performance Metrics

### Latency

- **Feed Latency**: <100ms from exchange to feed update
- **Event Dispatch**: <1ms from feed update to strategy hook (in-process, zero serialization)
- **Order Latency**: <500ms from signal to exchange API call
- **Message Processing**: 1-5ms per message (async workers)

### Throughput

- **Kline Feed**: ~1000 messages/second per socket
- **OrderBook Feed**: ~10,000 messages/second per socket (~10/sec per symbol)
- **Queue Capacity**: 10,000 messages max (prevents unbounded memory)
- **EventBus**: No overhead — direct async function calls

### Resource Usage

- **Memory**: ~200MB base + ~50MB per 1000 active symbols
- **CPU**: 1-2 cores for I/O, scales with strategy complexity
- **Network**: 1-2 Mbps per 100 active symbols
- **Processes**: 1 main process + up to max_heavy_workers for run_heavy()
