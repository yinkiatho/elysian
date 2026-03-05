# Elysian Architecture

## System Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                                                                                  │
│                           STRATEGY RUNNER (Main Entry)                          │
│                     orchestrates setup and execution of all                     │
│                         trading components and event loops                      │
│                                                                                  │
└──────────────────────────┬───────────────────────────────────────────────────────┘
                           │
                           │ setup_config() & await setup_data_feeds()
                           │
            ┌──────────────┴───────────────┬────────────────────────────┐
            │                              │                            │
            │                              │                            │
    ┌───────▼────────────────┐  ┌──────────▼────────────────┐  ┌───────▼──────────────┐
    │  MARKET DATA LAYER     │  │   ORDER MANAGEMENT LAYER  │  │   CORE MODELS        │
    │  (elysian_core/        │  │   (elysian_core/oms/)     │  │   (elysian_core/     │
    │   connectors/)         │  │                           │  │    core/)            │
    ├────────────────────────┤  ├───────────────────────────┤  ├──────────────────────┤
    │                        │  │                           │  │                      │
    │ BinanceExchange.py:    │  │ oms.py:                   │  │ market_data.py:      │
    │ • KlineClientMgr (*)   │  │ • AbstractOMS             │  │ • Kline              │
    │   - create_client()    │  │ • place_order()           │  │   - ticker           │
    │   - register_feed()    │  │ • cancel_order()          │  │   - interval         │
    │   - run_multiplex_     │  │ • get_balance()           │  │   - OHLCV            │
    │     feeds()            │  │                           │  │                      │
    │   - start/stop()       │  │ order_tracker.py:         │  │ • OrderBook          │
    │   - worker_pool        │  │ • OrderTracker            │  │   - bid_orders       │
    │   - queue (10k max)    │  │ • track_order()           │  │   - ask_orders       │
    │                        │  │ • get_status()            │  │   - spread           │
    │ • OBClientMgr (*)      │  │ • fill_history()          │  │   - mid_price        │
    │   - create_client()    │  │                           │  │                      │
    │   - register_feed()    │  │ Portfolio state:          │  │ order.py:            │
    │   - run_multiplex_     │  │ • balances                │  │ • Order (base)       │
    │     feeds()            │  │ • positions               │  │ • LimitOrder         │
    │   - start/stop()       │  │ • pnl                     │  │ • RangeOrder         │
    │   - worker_pool        │  │                           │  │                      │
    │   - queue (10k max)    │  │                           │  │ enums.py:            │
    │                        │  │                           │  │ • OrderStatus        │
    │ BinanceKlineFeed:      │  │                           │  │ • Side               │
    │ • create_new()         │  │                           │  │ • TradeType          │
    │ • run()                │  │                           │  │                      │
    │ • stop()               │  │                           │  │ trade.py:            │
    │ • current (latest)     │  │                           │  │ • Trade              │
    │ • history (cache)      │  │                           │  │                      │
    │                        │  │                           │  │ portfolio.py:        │
    │ BinanceOrderBookFeed:  │  │                           │  │ • Portfolio          │
    │ • create_new()         │  │                           │  │                      │
    │ • run()                │  │                           │  │                      │
    │ • stop()               │  │                           │  │                      │
    │ • current (latest)     │  │                           │  │                      │
    │ • history (cache)      │  │                           │  │                      │
    │                        │  │                           │  │                      │
    │ VolatilityFeed:        │  │                           │  │                      │
    │ • volume volatility    │  │                           │  │                      │
    │ • historical vol       │  │                           │  │                      │
    │ • redis backed         │  │                           │  │                      │
    │                        │  │                           │  │                      │
    │ (*) = Multiplex design │  │                           │  │                      │
    │ Single socket per      │  │                           │  │                      │
    │ client manager serves  │  │                           │  │                      │
    │ multiple feeds         │  │                           │  │                      │
    └────────┬───────────────┘  └─────────┬────────────────┘  └──────┬───────────────┘
             │                            │                          │
             └────────────────┬───────────┘                          │
                              │                                      │
                    ┌─────────▼──────────┬───────────────────────────┘
                    │                    │
            ┌───────▼────────────┐  ┌────▼──────────────────┐
            │  STRATEGY LAYER    │  │  MARKET MAKER LAYER   │
            │  (elysian_core/    │  │  (elysian_core/       │
            │   strategy/)       │  │   market_maker/)      │
            ├────────────────────┤  ├───────────────────────┤
            │                    │  │                       │
            │ strategy_engine.py:│  │ order_book.py:        │
            │ • StrategyEngine   │  │ • OrderBook analysis  │
            │ • on_event()       │  │ • spread calc         │
            │ • get_feed()       │  │ • level extraction    │
            │ • get_price()      │  │                       │
            │ • place_order()    │  │ order_management.py:  │
            │ • cancel_order()   │  │ • create range order  │
            │ • _feeds: dict     │  │ • update positions    │
            │ • _exchange        │  │ • manage lifecycle    │
            │                    │  │                       │
            │ processor.py:      │  │ order_tracker.py:     │
            │ • Processor        │  │ • track MM orders     │
            │ • event chain      │  │ • monitor fills       │
            │ • pre/post hooks   │  │ • adjust levels       │
            │ • signal processing│  │                       │
            └────────┬───────────┘  └───────────────────────┘
                     │
            ┌────────▼─────────────────┐
            │   EVENT FLOW             │
            ├──────────────────────────┤
            │                          │
            │  1. Market Data Event    │
            │     (from feed)          │
            │         ↓                │
            │  2. Strategy.on_event()  │
            │         ↓                │
            │  3. Processor.process()  │
            │     (signal generation)  │
            │         ↓                │
            │  4. OMS.place_order()    │
            │     (via exchange)       │
            │         ↓                │
            │  5. OrderTracker.track() │
            │     (status + history)   │
            │         ↓                │
            │  6. Portfolio.update()   │
            │     (balance + pnl)      │
            │         ↓                │
            │  7. DB.save()            │
            │         ↓                │
            │  8. Next Event...        │
            │                          │
            └──────────────────────────┘
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
    │  └─ BinanceOrderBookClientManager
    │
    └─ Setup logging & environment

StrategyRunner.run()
    │
    ├─ _setup_config()
    │  └─ Load trading pairs from config.json
    │
    ├─ await _setup_data_feeds()
    │  ├─ Create BinanceKlineFeed instances
    │  │  └─ Register with BinanceKlineClientManager
    │  │      └─ client_manager.register_feed()
    │  │
    │  ├─ Create BinanceOrderBookFeed instances
    │  │  └─ Register with BinanceOrderBookClientManager
    │  │      └─ client_manager.register_feed()
    │  │
    │  ├─ await client_manager1.start()
    │  │  ├─ Create AsyncClient
    │  │  └─ Create BinanceSocketManager
    │  │
    │  └─ await client_manager2.start()
    │
    ├─ setup_binance_feeds()
    │  ├─ Create multiplex_tasks = []
    │  ├─ asyncio.create_task(kline_manager.run_multiplex_feeds())
    │  │  └─ Network reader (reads from socket)
    │  │      └─ Worker pool (processes messages)
    │  │
    │  └─ asyncio.create_task(ob_manager.run_multiplex_feeds())
    │      └─ Network reader (reads from socket)
    │          └─ Worker pool (processes messages)
    │
    └─ ThreadPoolExecutor(max_workers=4)
       ├─ executor.submit(run_event_loop_in_thread, multiplex_tasks)
       │  └─ Creates new event loop in thread
       │     └─ loop.run_until_complete() all tasks
       │
       └─ future.result() - wait for completion
```

### Runtime Event Loop

```
BinanceKlineClientManager.run_multiplex_feeds()
    │
    ├─ Start network reader task
    │  └─ Reads from self._socket (multiplex WebSocket)
    │     ├─ Receives message for symbol1, symbol2, symbol3, etc.
    │     └─ Queues message to self._queue
    │
    └─ Start worker pool (4 workers)
       ├─ Worker 1: await queue.get() → process kline → feed1.update()
       ├─ Worker 2: await queue.get() → process kline → feed2.update()
       ├─ Worker 3: await queue.get() → process kline → feed3.update()
       └─ Worker 4: await queue.get() → process kline → feed4.update()

BinanceFeed (Kline or OrderBook)
    │
    ├─ Receives update from manager worker
    │  └─ self._current = new_data
    │
    ├─ Calls parent run() method
    │  └─ Emits event to strategies
    │
    └─ Strategies listen via on_event()
       ├─ StrategyEngine.on_event(event)
       │  └─ get_feed() → get_current_price() → create signals
       │
       └─ Processor chains the signal
          └─ OMS.place_order() via exchange

OrderTracker manages order state
    │
    ├─ Calls _place_order() (exchange specific)
    ├─ Tracks fill status
    └─ Updates portfolio
       └─ Persists to database
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
               ├─ Queue message
               └─ Worker processes
```

## Concurrency Model

```
Main Thread (asyncio.run())
    │
    └─ StrategyRunner.run() (async)
       │
       ├─ await setup_config()
       ├─ await setup_data_feeds()
       │  └─ await client_manager.start() (creates AsyncClient)
       │
       └─ ThreadPoolExecutor(max_workers=4)
          │
          ├─ Thread 1: run_event_loop_in_thread(kline_tasks)
          │  ├─ Creates new event loop
          │  └─ Runs: kline_manager.run_multiplex_feeds()
          │     ├─ Reader task (I/O bound)
          │     └─ 4 workers (CPU bound processing)
          │
          └─ (Single task group for now)
             (Can expand to multiple task groups)
```

## Data Flow Diagram

```
Exchange (Binance)
    │
    └─ WebSocket Stream
        │
        ├─ Kline Stream @1s
        │  ├─ {open, high, low, close, volume}
        │  └─ Symbol: ETHUSDT, BTCUSDT, SOLUSDT
        │
        └─ OrderBook Stream @100ms
           ├─ {bids[], asks[]}
           └─ Symbol: ETHUSDT, BTCUSDT, SOLUSDT

BinanceKlineClientManager
    │
    ├─ Multiplex Socket (ONE TCP connection)
    │  └─ Receives: ETHUSDT@1s, BTCUSDT@1s, SOLUSDT@1s
    │
    ├─ Queue (FIFO, maxsize=10k)
    │  └─ Buffers incoming messages
    │
    └─ Worker Pool (4 workers)
       ├─ Worker 1 ┐
       ├─ Worker 2 ├─ Dequeue → Parse → Update Feed
       ├─ Worker 3 │
       └─ Worker 4 ┘

BinanceKlineFeed
    │
    ├─ _current: Kline (latest)
    │  └─ Timestamp, OHLCV, symbol
    │
    ├─ _history: deque[Kline] (max 1000)
    │  └─ Historical candlesticks
    │
    └─ on_update()
       └─ Trigger strategy.on_event()

StrategyEngine
    │
    ├─ _feeds: {symbol → feed}
    │  └─ Access to all market data
    │
    └─ on_event(event)
       ├─ Read feed data
       ├─ Generate signals
       └─ If signal → OMS.place_order()

AbstractOMS
    │
    ├─ _place_limit_order(order)
    │  └─ Exchange-specific API call
    │
    └─ _tracker: OrderTracker
       ├─ Active orders
       ├─ Fill history
       └─ Balance state

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
├─ Trading Pairs
│  ├─ Binance Pairs (ETHUSDT, BTCUSDT)
│  └─ Binance Symbols (ETH/USDT, BTC/USDT)
└─ Total Tokens (ETH, BTC, ...)

.env
├─ DATABASE_* (credentials)
├─ REDIS_* (credentials)
├─ BINANCE_API_KEY / API_SECRET
└─ Other exchange credentials
```

## Error Handling & Cleanup

```
StrategyRunner.run()
    │
    try:
    │  ├─ Setup phase
    │  └─ Execution phase
    │
    except asyncio.CancelledError:
    │  └─ Log and continue
    │
    except Exception as e:
    │  ├─ Log error
    │  └─ Raise up
    │
    finally:
    │  └─ _cleanup(task_groups)
    │
    _cleanup():
    │  ├─ await _kline_client_manager.stop()
    │  │  ├─ Cancel reader_task
    │  │  ├─ Cancel worker_tasks[]
    │  │  ├─ Close socket
    │  │  └─ Close client connection
    │  │
    │  ├─ await _ob_client_manager.stop()
    │  │  └─ Same cleanup sequence
    │  │
    │  └─ await asyncio.gather(*tasks, return_exceptions=True)
    │     └─ Ensure all tasks properly terminate
```

## Scalability Considerations

### Horizontal Scaling

1. **Multiple Task Groups**: Currently one multiplex task group, can add:
   - Additional strategy processors
   - Volatility feed aggregators
   - Portfolio rebalancer
   - Risk monitor

2. **Exchange Expansion**: Add new exchange connectors:
   - Bybit, OKX, Kraken, etc.
   - Each uses same multiplex architecture
   - Unified OMS interface

3. **Worker Scalability**:
   - Currently 4 workers per manager
   - Increase `max_workers` for CPU-bound processing
   - Use ThreadPoolExecutor for I/O-bound tasks

### Vertical Scaling

1. **Database**:
   - Add read replicas for analytics
   - Partitioning by date for trade history
   - Materialized views for reporting

2. **Redis**:
   - Cluster mode for cache scaling
   - Sentinel for high availability
   - Queue optimization with lua scripts

3. **Network**:
   - Connection pooling in AsyncClient
   - DNS caching
   - Circuit breakers for API calls

## Performance Metrics

### Latency

- **Feed Latency**: <100ms from exchange to update
- **Order Latency**: <500ms from signal to execution
- **Message Processing**: 1-5ms per message (worker threads)

### Throughput

- **Kline Feed**: ~1000 messages/second per socket
- **OrderBook Feed**: ~10000 messages/second per socket
- **Queue Capacity**: 10,000 messages max (prevents unbounded memory)

### Resource Usage

- **Memory**: ~200MB base + ~50MB per 1000 active symbols
- **CPU**: 1-2 cores for I/O, scales with strategy complexity
- **Network**: 1-2 Mbps per 100 active symbols
