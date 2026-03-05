# Elysian - Cryptocurrency Trading System

A high-performance, event-driven cryptocurrency trading system built with Python and asyncio, featuring real-time market data aggregation from Binance and an extensible order management system.

## 📋 Table of Contents

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
- **Stream market data** efficiently from multiple cryptocurrency exchanges
- **Process orders** through a unified Order Management System (OMS)
- **Execute strategies** using an event-driven architecture
- **Manage state** with database persistence (PostgreSQL) and caching (Redis)
- **Scale horizontally** with multiplex WebSocket connections and thread pool execution

### Key Characteristics

- **Asynchronous**: Built on Python's asyncio for concurrent I/O operations
- **Modular**: Loosely-coupled components for exchange connectors, feeds, and strategies
- **Production-Ready**: Error handling, logging, and cleanup mechanisms
- **Extensible**: Abstract base classes for adding new exchanges and strategies

## Architecture Overview

The Elysian system uses a layered, event-driven architecture:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Strategy Runner (Main Entry)                 │
│              Orchestrates all trading components                │
└────────┬────────────────────────────────────────────────────────┘
         │
    ┌────┴──────────────────────────┬──────────────────────────────┐
    │                               │                              │
┌───▼──────────────────────┐  ┌─────▼──────────────┐  ┌───────────▼─┐
│   Market Data Layer      │  │   Order Layer      │  │   Core      │
├──────────────────────────┤  ├────────────────────┤  ├─────────────┤
│ • Binance Kline Feed     │  │ • OMS              │  │ • Kline     │
│ • Binance OrderBook Feed │  │ • OrderTracker     │  │ • OrderBook │
│ • Volatility Feed        │  │ • Portfolio        │  │ • Order     │
│ • Multiplex Managers     │  │ • Market Maker     │  │ • Trade     │
└────────┬─────────────────┘  └────────┬───────────┘  └─────┬───────┘
         │                            │                     │
         └────────────────┬───────────┘                     │
                          │                                  │
                    ┌─────▼──────────────────────────────────┘
                    │
            ┌───────▼───────────┐
            │  Strategy Engine  │
            │  & Processor      │
            └───────┬───────────┘
                    │
            ┌───────▼───────────────────────┐
            │  Persistence Layer            │
            ├───────────────────────────────┤
            │ • PostgreSQL (trade history)  │
            │ • Redis (cache/streaming)     │
            │ • File Storage (logs/data)    │
            └───────────────────────────────┘
```

## Features

### Market Data Ingestion

- **Multiplex WebSocket Streams**: Efficient socket sharing for multiple symbols
- **Real-time Kline Data**: 1-second candlestick updates for technical analysis
- **Order Book Snapshots**: 100ms depth data for market microstructure
- **Volatility Feeds**: Historical volatility data from Redis

### Order Management

- **Unified OMS Interface**: Abstraction across multiple exchanges
- **Order Tracking**: Real-time order status and fill monitoring
- **Portfolio Management**: Asset balance tracking and position sizing
- **Market Maker Support**: Range orders and algorithmic order types

### Execution & Strategy

- **Event-Driven Architecture**: React to market events in real-time
- **Strategy Engine**: Base classes for implementing custom trading logic
- **Processor Pipeline**: Chain-based processing for complex signal logic
- **State Management**: Maintain and persist trading state

## Project Structure

```
elysian/
├── README.md                           # Documentation
├── requirements.txt                    # Python dependencies
├── LICENSE                             # License
│
└── elysian_core/                       # Main package
    │
    ├── run_strategy.py                 # Main entry point
    │
    ├── config/                         # Configuration
    │   ├── config.yaml                 # Strategy config
    │   ├── config.json                 # Trading pairs
    │   └── __init__.py
    │
    ├── connectors/                     # Exchange connectors
    │   ├── base.py                     # AbstractDataFeed
    │   ├── BinanceExchange.py          # Binance connector
    │   ├── VolatilityFeed.py           # Volatility data
    │   └── old_code/                   # Deprecated connectors
    │
    ├── core/                           # Core models
    │   ├── enums.py                    # Status/Side/TradeType
    │   ├── market_data.py              # Kline, OrderBook
    │   ├── order.py                    # Order classes
    │   ├── portfolio.py                # Portfolio tracking
    │   ├── trade.py                    # Trade records
    │   └── __pycache__/
    │
    ├── db/                             # Database layer
    │   ├── database.py                 # PostgreSQL setup
    │   ├── models.py                   # ORM models
    │   └── __pycache__/
    │
    ├── market_maker/                   # MM utilities
    │   ├── order_book.py               # Analysis
    │   ├── order_management.py         # Lifecycle
    │   └── order_tracker.py            # Tracking
    │
    ├── oms/                            # Order Management
    │   ├── oms.py                      # AbstractOMS
    │   └── order_tracker.py            # State tracking
    │
    ├── strategy/                       # Strategy layer
    │   ├── processor.py                # Event processor
    │   └── strategy_engine.py          # Base engine
    │
    └── utils/                          # Utilities
        ├── constants.py                # Constants
        ├── data_types.py               # Custom types
        ├── logger.py                   # Logging config
        ├── utils.py                    # General helpers
        └── logs/                       # Log files
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
```

### Trading Pairs (`config.json`)

```json
{
  "Trading Pairs": {
    "Binance Pairs": ["ETHUSDT", "BTCUSDT"],
    "Binance Symbols": ["ETH/USDT", "BTC/USDT"]
  },
  "Total Tokens": ["ETH", "BTC"]
}
```

## Usage

### Running the Strategy

```bash
# Production mode
python elysian_core/run_strategy.py

# Test kline feed for 60 seconds
python elysian_core/run_strategy.py test kline 60

# Test orderbook feed for 30 seconds
python elysian_core/run_strategy.py test orderbook 30
```

### Creating a Custom Strategy

```python
from elysian_core.strategy.strategy_engine import StrategyEngine
from elysian_core.core.enums import Side

class MyStrategy(StrategyEngine):
    async def on_event(self, event: dict):
        """Handle market events."""
        symbol = event.get('symbol')
        feed = self.get_feed(symbol)
        
        if feed:
            latest = feed.current
            # Your trading logic here
```

## Core Components

### Connectors (`connectors/`)

**BinanceExchange.py**
- `BinanceKlineFeed`: 1-second candlesticks
- `BinanceOrderBookFeed`: Real-time order books
- `BinanceKlineClientManager`: Multiplex socket for klines
- `BinanceOrderBookClientManager`: Multiplex socket for books
- `BinanceSpotExchange`: REST API for orders

**VolatilityFeed.py**
- Volatility data sourcing from Redis
- Integration with strategy signals

### Core Models (`core/`)

**market_data.py**
- `Kline`: OHLCV candle
- `OrderBook`: Bid/ask orders with spreads

**order.py**
- `Order`: Base order class
- `LimitOrder`: Standard limit orders
- `RangeOrder`: Range/MM orders

**portfolio.py**
- Asset tracking
- Position management
- PnL calculations

### Order Management (`oms/`)

**oms.py** - AbstractOMS
- Unified exchange interface
- Order placement/cancellation
- Balance tracking

**order_tracker.py** - OrderTracker
- Real-time order status
- Fill history

### Strategy (`strategy/`)

**strategy_engine.py** - StrategyEngine
- Event-driven base class
- Price queries
- Order helpers

**processor.py** - Event Processor
- Pipeline processing
- Chain of responsibility pattern

## Development

### Code Style

- Follow PEP 8
- Use type hints
- Document public APIs
- Use `logger` for all logging

### Adding a New Exchange

1. Extend `AbstractDataFeed`
2. Implement `create_new()`, `run()`, `stop()`
3. Create corresponding OMS class
4. Register in `run_strategy.py`

## Performance

### Network

- **Multiplex Sockets**: Share WebSocket connections
- **Queue-Based**: Decouple I/O from processing
- **Worker Pools**: 4 workers per manager for parallel events

### Database

- **Peewee ORM**: Lightweight PostgreSQL abstraction
- **LRU Cache**: 200-entry cache for orders
- **Batch Inserts**: Efficient bulk operations

## Troubleshooting

### WebSocket Issues

**Cannot pickle asyncio task**
- Solution: Use `ThreadPoolExecutor` not `ProcessPoolExecutor`

**WebSocket hangs**
- Solution: Pre-resolve DNS with `loop.getaddrinfo()`

### Database Issues

**PostgreSQL connection refused**
- Check: Server running and credentials correct

**Redis connection failed**
- Check: Redis server running on configured host/port

## License

See LICENSE file for details.