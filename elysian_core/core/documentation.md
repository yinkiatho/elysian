# Core Documentation

## Overview
The core module contains fundamental data structures, enums, and business logic for trading operations. It provides normalized representations of market data, orders, portfolios, and trading primitives that are exchange-agnostic.

## Key Classes and Functions

### OrderBook (market_data.py)
**Normalized order book snapshot with bid/ask management.**

**Methods:**
- `__init__(last_update_id, last_timestamp, ticker, interval, bid_orders, ask_orders)`: Initialize order book
- `from_lists(last_update_id, last_timestamp, ticker, interval, bid_levels, ask_levels)`: Create from bid/ask lists
- `apply_update(side, price, qty)`: Apply single price level update
- `apply_both_updates(last_timestamp, new_update_id, bid_levels, ask_levels)`: Apply concurrent bid/ask updates (async)

**Properties:**
- `best_bid_price`: Highest bid price
- `best_bid_amount`: Quantity at best bid
- `best_ask_price`: Lowest ask price
- `best_ask_amount`: Quantity at best ask
- `mid_price`: Midpoint price ((best_bid + best_ask) / 2)
- `spread`: Bid-ask spread
- `spread_bps`: Spread in basis points

### Kline (market_data.py)
**OHLCV candlestick data structure.**

**Attributes:**
- `ticker`: Trading pair symbol
- `interval`: Time interval (e.g., "1m", "1h")
- `start_time`: Candle start timestamp
- `end_time`: Candle end timestamp
- `open`: Opening price
- `high`: Highest price
- `low`: Lowest price
- `close`: Closing price
- `volume`: Trading volume

### Order (order.py)
**Generic exchange order representation.**

**Attributes:**
- `id`: Unique order identifier
- `symbol`: Trading pair
- `side`: BUY or SELL
- `order_type`: LIMIT, MARKET, or RANGE
- `quantity`: Order quantity
- `price`: Limit price (None for market orders)
- `filled_qty`: Quantity already filled
- `status`: Current order status
- `timestamp`: Order creation time
- `venue`: Exchange venue
- `client_order_id`: Client-specified order ID
- `fee`: Trading fee amount
- `fee_currency`: Fee currency

**Properties:**
- `remaining_qty`: Unfilled quantity
- `is_filled`: Whether order is completely filled
- `is_active`: Whether order is still active

### LimitOrder (order.py)
**Liquidity provider limit order for AMM-style exchanges.**

**Attributes:**
- `id`: Order ID
- `base_asset`: Base asset symbol
- `quote_asset`: Quote asset symbol
- `side`: BUY or SELL
- `price`: Limit price
- `amount`: Order amount
- `lp_account`: Liquidity provider account
- `timestamp`: Creation timestamp
- `volatility`: Associated volatility

### RangeOrder (order.py)
**Liquidity provider range order (concentrated liquidity).**

**Attributes:**
- `id`: Order ID
- `base_asset`: Base asset
- `quote_asset`: Quote asset
- `lower_price`: Lower price bound
- `upper_price`: Upper price bound
- `order_type`: Range order type
- `amount`: Liquidity amount
- `min_amounts`: Minimum amounts tuple
- `max_amounts`: Maximum amounts tuple
- `lp_account`: LP account
- `timestamp`: Creation timestamp

### Position (portfolio.py)
**Single asset position tracking.**

**Attributes:**
- `symbol`: Asset symbol
- `quantity`: Position size (>0 long, <0 short)
- `avg_entry_price`: Average entry price
- `realized_pnl`: Realized profit/loss

**Methods:**
- `unrealized_pnl(mark_price)`: Calculate unrealized P&L at given price
- `notional(mark_price)`: Position notional value
- `is_flat()`: Check if position is closed

### Portfolio (portfolio.py)
**Multi-asset portfolio management.**

**Methods:**
- `__init__(initial_cash)`: Initialize with starting cash
- `position(symbol)`: Get or create position for symbol
- `positions`: Property returning all positions dict

**Properties:**
- `cash`: Available cash balance
- `total_value`: Total portfolio value (cash + positions)

## Enums

### Side (enums.py)
- `BUY`: Buy side
- `SELL`: Sell side

**Methods:**
- `opposite()`: Return opposite side

### AssetType (enums.py)
- `SPOT`: Spot trading
- `PERPETUAL`: Perpetual futures

### OrderType (enums.py)
- `LIMIT`: Limit order
- `MARKET`: Market order
- `RANGE`: Range/liquidity order

### OrderStatus (enums.py)
- `PENDING`: Order submitted but not yet active
- `OPEN`: Order active on exchange
- `PARTIALLY_FILLED`: Partially executed
- `FILLED`: Completely filled
- `CANCELLED`: Cancelled by user or system
- `REJECTED`: Rejected by exchange

**Methods:**
- `is_active()`: Check if order can still be filled

### Venue (enums.py)
- `BINANCE`: Binance exchange
- `BYBIT`: Bybit exchange
- `OKX`: OKX exchange
- `CETUS`: Cetus protocol
- `TURBOS`: Turbos protocol
- `DEEPBOOK`: Deepbook protocol
- `ASTER`: Aster exchange

### Chain (enums.py)
- `SUI`: Sui blockchain
- `TON`: TON blockchain
- `ETHEREUM`: Ethereum blockchain
- `BITCOIN`: Bitcoin blockchain
- `SOLANA`: Solana blockchain

**Methods:**
- `to_dict()`: Convert to dictionary

### TradeType (enums.py)
- `REAL`: Real trading
- `SIMULATED`: Paper trading

### SwapType (enums.py)
- `BLUEFIN`: Bluefin protocol
- `CETUS`: Cetus protocol
- `FLOW_X`: Flow X protocol
- `TURBOS`: Turbos protocol

### WalletType (enums.py)
- `WALLET`: Sui wallet
- `CEX`: Centralized exchange

## Exchange-Specific Classes

### AsterOrderBook (market_data.py)
**Aster exchange-specific order book with async update methods.**

**Methods:**
- `apply_update_lists(side, price_levels)`: Apply list of price level updates
- `apply_both_updates(last_timestamp, new_update_id, bid_levels, ask_levels)`: Apply bid and ask updates concurrently

### BinanceOrderBook (market_data.py)
**Binance exchange-specific order book with async update methods.**

**Methods:**
- `apply_update_lists(side, price_levels)`: Apply list of price level updates
- `apply_both_updates(last_timestamp, new_update_id, bid_levels, ask_levels)`: Apply bid and ask updates concurrently

## Data Flow

1. **Market Data**: OrderBook and Kline objects created from exchange feeds
2. **Order Management**: Order objects track trade lifecycle
3. **Portfolio Tracking**: Position objects updated with fills
4. **P&L Calculation**: Realized/unrealized P&L computed from positions

## Key Design Patterns

- **Dataclasses**: Immutable data structures with automatic methods
- **Enums**: Type-safe constants with methods
- **Properties**: Computed attributes for derived values
- **Inheritance**: Exchange-specific subclasses extend base classes
- **Async Methods**: Non-blocking updates for high-frequency data

## Validation and Constraints

- Price/quantity validation in order constructors
- Status transition validation
- Position size limits
- Timestamp ordering checks

## Performance Considerations

- Efficient data structures (SortedDict for order books)
- Minimal object creation in hot paths
- Lazy computation of derived properties
- Memory-efficient storage of historical data</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\core\documentation.md