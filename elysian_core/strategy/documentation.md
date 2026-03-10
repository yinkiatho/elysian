# Strategy Documentation

## Overview
The strategy module provides a framework for implementing event-driven trading strategies. It includes base classes for strategy development and signal processing components.

## Key Classes and Functions

### StrategyEngine (strategy_engine.py)
**Abstract base class for trading strategies with event-driven execution.**

**Methods:**
- `__init__(feeds, exchange)`: Initialize with data feeds and exchange client
- `get_feed(symbol)`: Get data feed for specific symbol
- `get_current_price(pair, side)`: Get current market price with fallback logic
- `get_current_vol(symbol)`: Get rolling volatility for symbol
- `_split_pair(pair)`: Parse trading pair into base/quote components
- `on_event(event)`: Abstract method for strategy logic (must be implemented)
- `run(event_sources)`: Start strategy execution with event sources

**Price Resolution Logic:**
1. Direct feed lookup (bid/ask/mid based on side)
2. Inverted pair lookup
3. Stablecoin proxy (e.g., ETHUSDC → ETHUSDT)
4. Synthetic construction via USDT legs

**Attributes:**
- `_feeds`: Dict of symbol → AbstractDataFeed
- `_exchange`: Exchange client for order execution
- `_trade_id`: Incremental trade identifier

### Processor (processor.py)
**Event processing and signal generation component.**

**Methods:**
- `__init__(datafeed, tokenlist, eventfilter, bidBuilder, poolManager, sui_event_listener, tq_hedger, vol_feed, timestamp)`: Initialize processor with dependencies
- `process_event(event)`: Process incoming market events
- `generate_signals()`: Generate trading signals from processed data
- `execute_signals()`: Execute generated signals through order management

**Event Processing Pipeline:**
1. **Pre-processing**: Filter and normalize incoming events
2. **Signal Generation**: Apply strategy logic to generate signals
3. **Risk Management**: Apply position limits and risk checks
4. **Order Generation**: Create orders from signals
5. **Post-processing**: Log and monitor execution

## Strategy Implementation

### Basic Strategy Template
```python
from elysian_core.strategy.strategy_engine import StrategyEngine
from elysian_core.core.enums import Side

class MyStrategy(StrategyEngine):
    async def on_event(self, event: dict):
        # Get current price
        price = await self.get_current_price("BTCUSDT")
        
        # Get volatility
        vol = self.get_current_vol("BTCUSDT")
        
        # Strategy logic
        if price > self.target_price and vol < self.vol_threshold:
            # Place buy order
            await self._exchange.place_order(
                symbol="BTCUSDT",
                side=Side.BUY,
                order_type="LIMIT",
                quantity=0.001,
                price=price * 0.99  # 1% below current price
            )
```

### Event Types
- **Market Data Events**: Price updates, order book changes
- **Order Events**: Order fills, cancellations, rejections
- **Portfolio Events**: Balance updates, position changes
- **System Events**: Connection status, error notifications

## Signal Processing

### Signal Types
- **Entry Signals**: Open new positions
- **Exit Signals**: Close existing positions
- **Adjustment Signals**: Modify position sizes
- **Hedge Signals**: Manage risk exposures

### Risk Management
- **Position Limits**: Maximum position sizes
- **Volatility Filters**: Avoid trading in high volatility
- **Drawdown Limits**: Stop trading after losses
- **Correlation Checks**: Diversify across assets

## Configuration

### Strategy Parameters
```yaml
strategy:
  name: "momentum_strategy"
  symbols: ["BTCUSDT", "ETHUSDT"]
  position_limit: 0.1
  vol_threshold: 0.05
  rebalance_interval: 300
```

### Feed Configuration
```yaml
feeds:
  BTCUSDT:
    interval: "1s"
    depth: 20
  ETHUSDT:
    interval: "1s"
    depth: 20
```

## Performance Monitoring

### Metrics Collection
- **Execution Time**: Event processing latency
- **Signal Accuracy**: Win/loss ratio
- **Slippage**: Realized vs expected prices
- **Position Turnover**: Trading frequency

### Logging
```python
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("strategy")

# Log signal generation
logger.info(f"Generated {len(signals)} signals")

# Log order execution
logger.info(f"Executed order: {order.id} {order.side} {order.quantity}")
```

## Error Handling

### Exception Types
- **FeedErrors**: Data feed connectivity issues
- **ExchangeErrors**: Order execution failures
- **ValidationErrors**: Invalid order parameters
- **RiskErrors**: Risk limit violations

### Recovery Strategies
- **Circuit Breakers**: Pause trading on errors
- **Fallback Feeds**: Switch to backup data sources
- **Order Retries**: Retry failed orders with backoff
- **Position Reconciliation**: Sync positions after outages

## Testing and Simulation

### Backtesting Framework
```python
class BacktestStrategy(MyStrategy):
    def __init__(self, historical_data):
        self.historical_data = historical_data
        self.current_index = 0
    
    async def on_event(self, event):
        # Process historical events
        current_data = self.historical_data[self.current_index]
        await super().on_event(current_data)
        self.current_index += 1
```

### Paper Trading
```python
class PaperTradingStrategy(MyStrategy):
    async def execute_order(self, order):
        # Simulate order execution without real trading
        simulated_fill = self.simulate_fill(order)
        await self.update_portfolio(simulated_fill)
        return simulated_fill
```

## Advanced Features

### Multi-Asset Strategies
```python
class CrossAssetStrategy(StrategyEngine):
    async def on_event(self, event):
        # Monitor correlations between assets
        btc_price = await self.get_current_price("BTCUSDT")
        eth_price = await self.get_current_price("ETHUSDT")
        
        # Trade based on relative strength
        if btc_price / eth_price > self.threshold:
            await self._exchange.place_order("ETHUSDT", Side.BUY, ...)
```

### Machine Learning Integration
```python
class MLStrategy(StrategyEngine):
    def __init__(self, model_path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = self.load_model(model_path)
    
    async def on_event(self, event):
        features = self.extract_features(event)
        prediction = self.model.predict(features)
        
        if prediction > self.threshold:
            await self.place_order(...)
```

## Best Practices

### Code Organization
- Separate signal generation from execution
- Use configuration files for parameters
- Implement comprehensive logging
- Add unit tests for strategy logic

### Performance Optimization
- Minimize synchronous operations
- Use async/await for I/O operations
- Cache frequently accessed data
- Profile and optimize hot paths

### Risk Management
- Implement position size limits
- Add volatility-based filters
- Monitor drawdowns and stop losses
- Diversify across multiple strategies

### Monitoring and Alerting
- Track key performance metrics
- Set up alerts for anomalies
- Log all trading decisions
- Regular strategy reviews</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\strategy\documentation.md