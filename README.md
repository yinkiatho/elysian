# Elysian - Cryptocurrency Trading System

A high-performance, event-driven cryptocurrency trading system built with Python and asyncio, featuring real-time market data aggregation from multiple exchanges, an in-process EventBus for zero-overhead event dispatch, and an extensible strategy framework with hook-based event handling.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Core Components](#core-components)
- [Development](#development)
- [Testing](#testing)

## Overview

Elysian is a production-ready trading system designed to:
- **Stream market data** efficiently from multiple cryptocurrency exchanges (Binance, Aster)
- **React to events in real-time** via typed hook functions (`on_kline`, `on_orderbook_update`, `on_order_update`, `on_balance_update`)
- **Process orders** through a unified Order Management System (OMS)
- **Execute strategies** using an event-driven `SpotStrategy` base class with automatic EventBus wiring
- **Offload heavy compute** to separate CPU cores via `ProcessPoolExecutor` without blocking the event loop
- **Manage state** with database persistence (PostgreSQL) and caching (Redis)

### Key Characteristics

- **Single Event Loop**: All feeds, exchanges, and strategy hooks run in one `asyncio.run()` event loop — no thread/process boundaries for the hot path
- **EventBus Architecture**: In-process async pub/sub with zero serialization overhead and natural backpressure via awaited publish
- **Hook-Based Strategies**: Subclass `SpotStrategy`, override only the hooks you need — all others are no-op by default
- **Immutable Events**: All event dataclasses are `frozen=True` to prevent accidental mutation of shared data
- **Multiplex WebSockets**: Single TCP connection per asset class serves multiple symbol feeds
- **Extensible**: Abstract base classes for adding new exchanges, strategies, and data feeds

## Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│                  Strategy Runner (Main Entry)                   │
│          Single asyncio event loop, shared EventBus            │
└───────┬────────────────────┬───────────────────────────────────┘
        │                    │
   ┌────┴────────────┐  ┌───┴──────────────────┐
   │                  │  │    EventBus           │
   │                  │  │  subscribe/publish    │
   │                  │  │  (async, backpressure)│
   │                  │  └───┬──────────────┬───┘
   │                  │      │ publishes     │ subscribes
   │                  │      │              │
┌──▼──────────────────▼──┐   │   ┌──────────▼──────────────┐
│   Market Data Layer     │───┘   │   Strategy Layer         │
├─────────────────────────┤       ├──────────────────────────┤
│ • Binance Kline Feed    │       │ • SpotStrategy (base)    │
│ • Binance OrderBook Feed│       │   - on_kline()           │
│ • Aster Kline/OB Feeds  │       │   - on_orderbook_update()│
│ • User Data Stream      │       │   - on_order_update()    │
│ • Multiplex Managers    │       │   - on_balance_update()  │
│   (event_bus injected)  │       │   - run_heavy()          │
└────────┬────────────────┘       │   - run_forever()        │
         │                        └──────────┬───────────────┘
    ┌────┴──────────────┐                    │
    │  Order Layer      │                    │
    ├───────────────────┤          ┌─────────▼───────────┐
    │ • OMS             │          │  Persistence Layer   │
    │ • OrderTracker    │          ├─────────────────────┤
    │ • Portfolio       │          │ • PostgreSQL         │
    │ • Market Maker    │          │ • Redis              │
    └───────────────────┘          └─────────────────────┘
```

## Features

### Event-Driven Strategy Framework

- **SpotStrategy Base Class**: Subclass and override `on_kline()`, `on_orderbook_update()`, `on_order_update()`, `on_balance_update()` — no-op by default
- **Typed Events**: `KlineEvent`, `OrderBookUpdateEvent`, `OrderUpdateEvent`, `BalanceUpdateEvent` — all frozen dataclasses with `symbol`, `venue`, `timestamp` fields
- **EventBus**: Lightweight in-process async pub/sub. Workers publish after mutating feed state; strategy hooks consume. Backpressure is natural via awaited publish
- **CPU Offloading**: `run_heavy(fn, *args)` sends CPU-bound work to a `ProcessPoolExecutor` without blocking the event loop
- **Lifecycle Hooks**: `on_start()`, `on_stop()`, `run_forever()` for initialization, cleanup, and long-running background tasks

### Market Data Ingestion

- **Multiplex WebSocket Streams**: Single TCP connection per client manager serves all symbol feeds
- **Real-time Kline Data**: 1-second candlestick updates with rolling volatility calculation
- **Order Book Snapshots**: 100ms depth data with full snapshot sync and buffered event replay
- **User Data Stream**: Real-time order execution reports and balance updates
- **Multi-Exchange**: Binance (spot + futures), Aster (spot + perps)

### Order Management

- **Unified OMS Interface**: Abstraction across multiple exchanges
- **Order Tracking**: Real-time order status and fill monitoring
- **Portfolio Management**: Asset balance tracking and position sizing
- **Market Maker Support**: Range orders and algorithmic order types

## Project Structure

```
elysian/
├── ARCHITECTURE.md                    # Detailed architecture documentation
├── README.md                          # This file
├── requirements.txt                   # Python dependencies
├── LICENSE                            # License
│
└── elysian_core/                      # Main package
    │
    ├── run_strategy.py                # Main entry point (StrategyRunner)
    │
    ├── config/                        # Configuration
    │   ├── config.yaml                # Strategy config
    │   ├── config.json                # Trading pairs
    │   └── __init__.py
    │
    ├── connectors/                    # Exchange connectors
    │   ├── base.py                    # AbstractDataFeed, AbstractClientManager
    │   ├── BinanceDataConnectors.py   # Kline/OB feeds + client managers
    │   ├── BinanceExchange.py         # BinanceSpotExchange + UserDataManager
    │   ├── BinanceFuturesDataConnectors.py
    │   ├── BinanceFuturesExchange.py
    │   ├── AsterDataConnectors.py     # Aster spot feeds
    │   ├── AsterExchange.py           # Aster spot exchange
    │   ├── AsterPerpDataConnectors.py # Aster perp feeds
    │   ├── AsterPerpExchange.py       # Aster perp exchange
    │   ├── VolatilityFeed.py          # Redis-backed volatility data
    │   └── documentation.md
    │
    ├── core/                          # Core models & event system
    │   ├── events.py                  # EventType enum + frozen event dataclasses
    │   ├── event_bus.py               # In-process async EventBus
    │   ├── enums.py                   # Side, Venue, OrderStatus, OrderType, etc.
    │   ├── market_data.py             # Kline, OrderBook
    │   ├── order.py                   # Order, LimitOrder, RangeOrder
    │   ├── portfolio.py               # Portfolio, Position
    │   ├── trade.py                   # Trade records
    │   └── documentation.md
    │
    ├── db/                            # Database layer
    │   ├── database.py                # PostgreSQL setup
    │   ├── models.py                  # ORM models
    │   └── documentation.md
    │
    ├── market_maker/                  # Market making utilities
    │   ├── order_book.py              # LP order book analysis
    │   ├── order_management.py        # Order lifecycle
    │   ├── order_tracker.py           # MM order tracking
    │   └── documentation.md
    │
    ├── oms/                           # Order Management System
    │   ├── oms.py                     # AbstractOMS
    │   ├── order_tracker.py           # State tracking
    │   └── documentation.md
    │
    ├── strategy/                      # Strategy layer
    │   ├── base_strategy.py           # SpotStrategy base class (hooks, EventBus)
    │   ├── test_strategy.py           # TestPrintStrategy (verification)
    │   ├── processor.py               # Legacy event processor
    │   ├── strategy_engine.py         # Legacy strategy engine
    │   └── documentation.md
    │
    └── utils/                         # Utilities
        ├── constants.py               # Constants
        ├── data_types.py              # Custom types
        ├── logger.py                  # Logging config
        ├── utils.py                   # General helpers
        ├── documentation.md
        └── logs/                      # Log files
```

## Installation

### Prerequisites

- Python 3.8+
- PostgreSQL 12+
- Redis 5.0+

### Setup

1. **Clone repository**
   ```bash
   git clone <repository-url>
   cd elysian
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

## Configuration

### Environment Variables (`.env`)

```bash
# Database
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=elysian
DATABASE_USER=postgres
DATABASE_PASSWORD=your_password

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Binance API
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret

# Aster API
ASTER_API_KEY=your_api_key
ASTER_API_SECRET=your_api_secret
```

### Trading Pairs (`config.json`)

```json
{
  "Spot Trading Pairs": {
    "Binance Pairs": ["ETHUSDT", "BTCUSDT"],
    "Binance Symbols": ["ETH/USDT", "BTC/USDT"],
    "Aster Pairs": ["ETHUSDT"],
    "Aster Symbols": ["ETH/USDT"]
  },
  "Futures Trading Pairs": {
    "Binance Futures Pairs": ["ETHUSDT"],
    "Binance Futures Symbols": ["ETH/USDT"]
  },
  "Spot Total Tokens": ["ETH", "BTC"],
  "Futures Total Tokens": ["ETH"]
}
```

## Usage

### Running with the Test Strategy

```bash
# Runs TestPrintStrategy — prints every event hook invocation
python elysian_core/run_strategy.py
```

### Creating a Custom Strategy

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import (
    KlineEvent, OrderBookUpdateEvent, OrderUpdateEvent, BalanceUpdateEvent
)
from elysian_core.core.enums import Venue, Side

class MyStrategy(SpotStrategy):
    """Override only the hooks you need. All others are no-op."""

    async def on_start(self):
        """Called once when strategy starts. Initialize state here."""
        self.signal_count = 0
        logger.info("MyStrategy started")

    async def on_kline(self, event: KlineEvent):
        """Called on each closed kline candle."""
        price = event.kline.close
        vol = self.get_current_vol(event.symbol)

        if price > 3000 and vol and vol < 500:
            exchange = self.get_exchange(Venue.BINANCE)
            # Place order through exchange connector
            await exchange.place_limit_order(
                symbol=event.symbol, side=Side.BUY,
                quantity=0.01, price=price * 0.999
            )

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        """Called on each orderbook depth update (~10/sec per symbol).
        Keep this fast or use run_heavy() for CPU work."""
        if event.orderbook.spread_bps > 10.0:
            result = await self.run_heavy(compute_signal, event.orderbook.mid_price)
            # ... act on result

    async def on_order_update(self, event: OrderUpdateEvent):
        """Called when an order status changes."""
        logger.info(f"Order {event.order.id}: {event.order.status.value}")

    async def on_balance_update(self, event: BalanceUpdateEvent):
        """Called when account balance changes."""
        logger.info(f"Balance {event.asset}: delta={event.delta}")

    async def run_forever(self):
        """Optional: long-running background task (gathered with feeds)."""
        while True:
            await asyncio.sleep(60)
            # Periodic rebalancing, health checks, etc.


# Top-level picklable function for run_heavy()
def compute_signal(mid_price: float) -> float:
    # CPU-intensive calculation here
    return mid_price * 1.001
```

### Wiring a Strategy with StrategyRunner

```python
from elysian_core.run_strategy import StrategyRunner
from elysian_core.core.event_bus import EventBus

runner = StrategyRunner()
strategy = MyStrategy(exchanges={}, event_bus=EventBus())

# runner.run() re-wires strategy._event_bus and strategy._exchanges
# before calling strategy.start(), so initial values are placeholders
asyncio.run(runner.run(strategy=strategy))
```

## Core Components

### Event System (`core/events.py`, `core/event_bus.py`)

- **EventType**: Enum with `KLINE`, `ORDERBOOK_UPDATE`, `ORDER_UPDATE`, `BALANCE_UPDATE`
- **Event Dataclasses**: Frozen (immutable) dataclasses carrying typed payloads (`Kline`, `OrderBook`, `Order`)
- **EventBus**: In-process async pub/sub with `subscribe()`, `unsubscribe()`, `publish()`. Publish is awaited for natural backpressure

### Strategy (`strategy/base_strategy.py`)

- **SpotStrategy**: Base class with hook functions, exchange/feed access, price helpers, `run_heavy()` for CPU offloading, `run_forever()` for background tasks
- **TestPrintStrategy**: Verification strategy that logs every hook invocation

### Connectors (`connectors/`)

- **BinanceDataConnectors.py**: `BinanceKlineClientManager`, `BinanceOrderBookClientManager` — multiplex WebSocket managers that publish events to EventBus after mutating feeds
- **BinanceExchange.py**: `BinanceSpotExchange` — REST API for orders, `BinanceUserDataClientManager` — user data stream publishing `OrderUpdateEvent` and `BalanceUpdateEvent`
- **base.py**: `AbstractDataFeed`, `AbstractClientManager` with `set_event_bus()` method
- **AsterDataConnectors.py / AsterExchange.py**: Aster exchange equivalents
- **Futures connectors**: Binance futures and Aster perp variants

### Core Models (`core/`)

- **market_data.py**: `Kline` (OHLCV), `OrderBook` (bids/asks/spread)
- **order.py**: `Order`, `LimitOrder`, `RangeOrder`
- **portfolio.py**: `Portfolio`, `Position` with P&L tracking
- **enums.py**: `Side`, `Venue`, `OrderStatus`, `OrderType`, `TradeType`

### Order Management (`oms/`)

- **AbstractOMS**: Unified exchange interface for order placement/cancellation
- **OrderTracker**: Real-time order status and fill history

## Development

### Code Style

- Follow PEP 8
- Use type hints
- Document public APIs
- Use `logger` for all logging (via `elysian_core.utils.logger`)

### Adding a New Exchange

1. Extend `AbstractDataFeed` and `AbstractClientManager` in `base.py`
2. Implement kline/OB client managers with `set_event_bus()` and event emission
3. Create exchange class with `event_bus` constructor parameter
4. Add `Venue` enum value in `enums.py`
5. Add setup method in `run_strategy.py` and wire event_bus injection

### Adding a New Strategy

1. Subclass `SpotStrategy`
2. Override the `on_*` hooks you need
3. Optionally override `run_forever()` for background tasks
4. Use `run_heavy(fn, *args)` for CPU-bound calculations
5. Wire in `run_strategy.py` or a custom entry point

## Performance

### Network

- **Multiplex Sockets**: Single WebSocket connection per asset class
- **Queue-Based**: asyncio.Queue (maxsize=10k) decouples I/O from processing
- **Worker Pools**: 4 async workers per manager for parallel message processing

### Event System

- **Zero Serialization**: Events are direct object references (in-process)
- **Backpressure**: `publish()` is awaited, so slow strategies throttle workers
- **Error Isolation**: Each dispatch wrapper catches exceptions independently

### Compute

- **ProcessPoolExecutor**: Reserved for CPU-bound strategy calculations via `run_heavy()`
- **Single Event Loop**: No thread/process boundary overhead for the hot path

## Troubleshooting

### WebSocket Issues

**Cannot pickle asyncio task**
- Solution: All feeds run in the single event loop, not `ProcessPoolExecutor`

**WebSocket hangs**
- Solution: Pre-resolve DNS with `loop.getaddrinfo()`

### Strategy Issues

**on_orderbook_update is slow / causing backpressure**
- Solution: Keep hook fast, offload CPU work to `run_heavy(fn, *args)`

**Events carry stale data**
- Events carry references to live objects. Copy immediately if you need a consistent snapshot

### Database Issues

**PostgreSQL connection refused**
- Check: Server running and credentials correct

**Redis connection failed**
- Check: Redis server running on configured host/port

## License

See LICENSE file for details.
