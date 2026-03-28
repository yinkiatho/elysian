# Utils Documentation

## Overview
The utils module provides utility functions, logging infrastructure, data processing tools, and configuration management for the Elysian trading system.

## Key Classes and Functions

### ColoredFormatter (logger.py)
**Custom logging formatter with ANSI color support for terminal output.**

**Color Mapping:**
- `DEBUG`: Blue
- `INFO`: Bright white
- `SUCCESS`: Bright green
- `WARNING`: Yellow
- `ERROR`: Red
- `CRITICAL`: White on red background

**Methods:**
- `format(record)`: Format log record with appropriate colors

### setup_custom_logger (logger.py)
**Initialize a custom logger with colored console output and file logging.**

**Parameters:**
- `name`: Logger name (string)
- `log_level`: Logging level (default: INFO)

**Features:**
- Colored console output
- File logging with timestamps
- Module and line number information
- SUCCESS logging level support

**Usage:**
```python
logger = setup_custom_logger("trading_engine")
logger.info("Strategy initialized")
logger.success("Order executed successfully")
```

### DataFrameIterator (utils.py)
**Iterator for processing large DataFrames in batches for machine learning pipelines.**

**Parameters:**
- `args`: Configuration namespace with regression settings
- `df`: Input DataFrame with Date and Ticker columns
- `stage`: Processing stage ("train", "test", "inference")

**Methods:**
- `__iter__()`: Return self as iterator
- `__next__()`: Return next batch DataFrame

**Batch Configuration:**
- `n_dates`: Number of unique dates per batch
- `n_tickers`: Number of unique tickers per batch

### load_config (utils.py)
**Load YAML configuration file into dictionary.**

**Parameters:**
- `path`: Path to YAML configuration file

**Returns:**
- Dictionary containing configuration data

### replace_placeholders (utils.py)
**Recursively replace placeholders in configuration dictionary.**

**Parameters:**
- `cfg`: Configuration dictionary
- `placeholders`: Dictionary of placeholder → value mappings

**Usage:**
```python
config = load_config("config.yaml")
config = replace_placeholders(config, {"API_KEY": os.getenv("API_KEY")})
```

### config_to_args (utils.py)
**Convert configuration dictionary to argparse Namespace.**

**Parameters:**
- `cfg`: Configuration dictionary
- `placeholders`: Optional placeholder replacement dictionary

**Returns:**
- argparse.Namespace object

**Features:**
- Recursive conversion of nested dictionaries
- List preservation
- Placeholder replacement support

## Data Types and Constants

### BinanceKline (data_types.py)
**Data structure for Binance kline/candlestick data.**

**Attributes:**
- `timestamp`: Event timestamp
- `symbol`: Trading pair
- `interval`: Kline interval
- `open`: Opening price
- `high`: Highest price
- `low`: Lowest price
- `close`: Closing price
- `volume`: Trading volume
- `quote_volume`: Quote asset volume
- `trades`: Number of trades
- `taker_buy_volume`: Taker buy volume
- `taker_buy_quote_volume`: Taker buy quote volume

### BinanceOrderBook (data_types.py)
**Data structure for Binance order book snapshots.**

**Attributes:**
- `timestamp`: Snapshot timestamp
- `symbol`: Trading pair
- `bids`: List of [price, quantity] bid levels
- `asks`: List of [price, quantity] ask levels
- `last_update_id`: Last update ID

### APICommands (data_types.py)
**Enum for Chainflip API commands.**

**Values:**
- `Empty`: No operation
- `Register`: Register with protocol
- `LiquidityDeposit`: Deposit liquidity
- `RegisterWithdrawalAddress`: Set withdrawal address
- `WithdrawAsset`: Withdraw assets
- `AssetBalances`: Query balances
- `SetRangeOrderByLiquidity`: Create range order by liquidity
- `SetRangeOrderByAmounts`: Create range order by amounts
- `UpdateLimitOrder`: Update existing limit order
- `SetLimitOrder`: Create limit order
- `GetOpenSwapChannels`: Query swap channels
- `CancelAllOrders`: Cancel all orders

### RPCCommands (data_types.py)
**Enum for Chainflip RPC commands.**

**Values:**
- `Empty`: No operation
- `AccountInfo`: Get account information
- `RequiredRatioForRangeOrder`: Calculate required ratio
- `PoolInfo`: Get pool information
- `PoolDepth`: Get pool depth
- `PoolLiquidity`: Get pool liquidity
- `PoolOrders`: Get pool orders
- `PoolRangeOrdersLiquidityValue`: Get range order liquidity
- `PoolScheduledSwaps`: Get scheduled swaps
- `AccountAssetBalances`: Get account asset balances

## Configuration Management

### YAML Configuration Structure
```yaml
# Example configuration file
binance:
  api_key: ${BINANCE_API_KEY}
  api_secret: ${BINANCE_API_SECRET}
  testnet: true

trading:
  symbols: ["BTCUSDT", "ETHUSDT"]
  position_limit: 0.1
  risk_multiplier: 2.0

logging:
  level: INFO
  file: logs/trading.log
```

### Environment Variable Integration
```python
import os
from utils import load_config, replace_placeholders

# Load config with environment variable replacement
config = load_config("config.yaml")
config = replace_placeholders(config, {
    "BINANCE_API_KEY": os.getenv("BINANCE_API_KEY"),
    "BINANCE_API_SECRET": os.getenv("BINANCE_API_SECRET"),
})
```

## Logging Infrastructure

### Logger Configuration
```python
# Setup application logger
logger = setup_custom_logger("elysian")

# Log different levels
logger.debug("Detailed debug information")
logger.info("General information")
logger.success("Successful operation")
logger.warning("Warning condition")
logger.error("Error condition")
logger.critical("Critical error")
```

### Log Format
```
2024-01-15 14:30:25,123 - INFO - trading_engine - strategy.py:45 - Order executed: BTCUSDT BUY 0.001 @ 50000.0
```

### File Logging
- Automatic log file creation
- Timestamped filenames
- Rotation based on size/time
- Configurable log levels

## Data Processing Utilities

### DataFrame Operations
```python
from utils import DataFrameIterator
import pandas as pd

# Create iterator for ML training
df = pd.read_csv("market_data.csv")
iterator = DataFrameIterator(args, df, stage="train")

# Process in batches
for batch in iterator:
    # Train model on batch
    model.train(batch)
```

### Batch Processing Configuration
```python
# Configuration namespace
args.regression.S3.window_size = 30  # dates per batch
args.regression.S3.ticker_size = 100  # tickers per batch
args.regression.S3.train_start_date = "2023-01-01"
args.regression.S3.train_end_date = "2023-12-31"
```

## Error Handling

### Configuration Errors
```python
try:
    config = load_config("config.yaml")
except FileNotFoundError:
    logger.error("Configuration file not found")
    raise
except yaml.YAMLError as e:
    logger.error(f"Invalid YAML configuration: {e}")
    raise
```

### Logging Errors
```python
try:
    # Risky operation
    result = execute_trade(order)
except Exception as e:
    logger.error(f"Trade execution failed: {e}", exc_info=True)
    # Continue with error recovery
```

## Performance Considerations

### Memory Management
- Efficient DataFrame processing for large datasets
- Iterator-based batch processing
- Garbage collection awareness

### I/O Optimization
- Buffered file operations
- Asynchronous logging where possible
- Connection pooling for external services

### Configuration Caching
- Load configuration once at startup
- Cache compiled regular expressions
- Reuse logger instances

## Testing Utilities

### Mock Configuration
```python
def create_test_config():
    return {
        "binance": {
            "api_key": "test_key",
            "api_secret": "test_secret",
            "testnet": True
        },
        "trading": {
            "symbols": ["BTCUSDT"],
            "position_limit": 0.01
        }
    }
```

### Logger Testing
```python
import io
import sys
from contextlib import redirect_stderr

# Capture log output for testing
log_stream = io.StringIO()
with redirect_stderr(log_stream):
    logger = setup_custom_logger("test")
    logger.info("Test message")

assert "Test message" in log_stream.getvalue()
```

## Integration Examples

### Full Application Setup
```python
import os
from utils import load_config, replace_placeholders, config_to_args, setup_custom_logger

# Setup logging
logger = setup_custom_logger("elysian")

# Load configuration
config = load_config("config/elysian.yaml")
config = replace_placeholders(config, {
    "API_KEY": os.getenv("BINANCE_API_KEY"),
    "API_SECRET": os.getenv("BINANCE_API_SECRET"),
})
args = config_to_args(config)

# Initialize components
logger.info("Elysian trading system initialized")
```

### Data Processing Pipeline
```python
from utils import DataFrameIterator
import pandas as pd

def process_market_data(args, data_path):
    # Load raw data
    df = pd.read_csv(data_path)
    
    # Create processing iterator
    iterator = DataFrameIterator(args, df, stage="inference")
    
    results = []
    for batch in iterator:
        # Process batch
        processed = preprocess_batch(batch)
        predictions = model.predict(processed)
        results.extend(predictions)
    
    return results
```</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\utils\documentation.md