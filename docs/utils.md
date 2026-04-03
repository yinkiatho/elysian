# Utils Module Documentation

## Overview

The `elysian_core/utils/` and `elysian_core/config/` directories provide logging infrastructure, configuration management, data processing utilities, and external API helpers used throughout the Elysian trading system.

Key responsibilities:
- Structured, colored terminal logging with file persistence
- YAML/JSON configuration loading and typed config object construction
- DataFrame batch iteration for ML pipelines
- Fetching live symbol data from Aster exchange endpoints

---

## Logger (`elysian_core/utils/logger.py`)

### Custom Log Level

A custom `SUCCESS` level is registered at numeric value `21` (between `INFO=20` and `WARNING=30`).

```python
SUCCESS_LEVEL = 21
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")
```

`logger.success(msg)` is patched onto `logging.Logger` directly so it works on any logger instance returned by `setup_custom_logger`.

### `ColoredFormatter`

A `logging.Formatter` subclass that wraps each log line in ANSI escape codes for terminal colorization.

| Level | Color |
|-------|-------|
| `DEBUG` | Blue (`\033[34m`) |
| `INFO` | Bright white (`\033[97m`) |
| `SUCCESS` | Bright green (`\033[92m`) |
| `WARNING` | Yellow (`\033[33m`) |
| `ERROR` | Red (`\033[31m`) |
| `CRITICAL` | White on red background (`\033[41m`) |

**Method:** `format(record) -> str` — calls the parent formatter then wraps the result in the level's color code, always resetting with `\033[0m` at the end.

### `setup_custom_logger`

```python
def setup_custom_logger(name: str, log_level: int = logging.INFO) -> logging.Logger
```

Returns a logger for the given `name`. Logger instances are cached in the module-level `loggers` dict — calling this function twice with the same name returns the same object without adding duplicate handlers.

**Behavior:**
- Adds a `StreamHandler` with `ColoredFormatter` for terminal output.
- Adds a `FileHandler` writing to `elysian_core/utils/logs/<timestamp>.log`, where the timestamp is formatted in UTC+8 as `YYYY-MM-DD_HH-MM-SS`. The log directory is created automatically if it does not exist.
- Both handlers use the same format string: `%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s`
- File handler logs at the same level as the logger itself.

**Log format example:**
```
2024-01-15 14:30:25,123 - INFO - engine - engine.py:45 - Order executed
```

**Usage:**
```python
from elysian_core.utils.logger import setup_custom_logger

logger = setup_custom_logger("trading_engine")
logger.debug("Debug detail")
logger.info("Strategy initialized")
logger.success("Order executed successfully")
logger.warning("Approaching position limit")
logger.error("Connector disconnected")
logger.critical("Risk limit breached")
```

---

## Utility Functions (`elysian_core/utils/utils.py`)

### `load_config`

```python
def load_config(path: str) -> dict
```

Reads a YAML file at `path` and returns its contents as a Python dictionary using `yaml.safe_load`. The file is opened with UTF-8 encoding.

### `replace_placeholders`

```python
def replace_placeholders(cfg: dict, placeholders: dict) -> dict
```

Recursively walks a config dictionary and replaces `{key}` tokens in string values with the corresponding value from `placeholders`. Handles nested dicts and lists. Non-string values are returned unchanged.

**Example:**
```python
cfg = {"log_path": "logs/{run_id}/trading.log"}
out = replace_placeholders(cfg, {"run_id": "20240115_143025"})
# out == {"log_path": "logs/20240115_143025/trading.log"}
```

### `config_to_args`

```python
def config_to_args(cfg: dict, placeholders: dict = None) -> argparse.Namespace
```

Converts a config dictionary to an `argparse.Namespace` object. If `placeholders` is provided, `replace_placeholders` is applied first. Nested dictionaries are recursively converted to nested `Namespace` objects; lists are preserved element-by-element using the same recursion.

**Example:**
```python
from elysian_core.utils.utils import load_config, config_to_args

cfg = load_config("config/config.yaml")
args = config_to_args(cfg, placeholders={"run_id": "001"})
args.regression.S3.window_size   # dot-access to nested values
```

### `DataFrameIterator`

```python
class DataFrameIterator:
    def __init__(self, args: Namespace, df: pd.DataFrame, stage: str = 'train')
```

An iterator that yields fixed-size sub-DataFrames from a market-data DataFrame, used by ML training and inference pipelines.

**Constructor parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `args` | `argparse.Namespace` | Config namespace containing `args.regression.S3.*` settings |
| `df` | `pd.DataFrame` | Input DataFrame with `Date` and `Ticker` columns |
| `stage` | `str` | One of `"train"`, `"test"`, or `"inference"` |

The constructor filters `df` to the date range configured for `stage` on `args.regression.S3`:
- `train` → `train_start_date` / `train_end_date`
- `test` → `test_start_date` / `test_end_date`
- `inference` → `inference_start_date` / `inference_end_date`

It then sorts the filtered frame by `(Date, Ticker)` and builds unique-date and unique-ticker lists.

**Batch parameters (read from `args.regression.S3`):**

| Config key | Description |
|------------|-------------|
| `window_size` | Number of unique dates per batch (`n_dates`) |
| `ticker_size` | Number of unique tickers per batch (`n_tickers`) |

**Methods:**

- `__iter__() -> Iterator[pd.DataFrame]` — returns `self`
- `__next__() -> pd.DataFrame` — returns the next batch slice. Advances through tickers first, then dates. Raises `StopIteration` when all dates are exhausted.

**Iteration order:** For each date window the iterator steps through all ticker windows before advancing to the next date window.

**Usage:**
```python
from elysian_core.utils.utils import DataFrameIterator
import pandas as pd

df = pd.read_csv("market_data.csv")
for batch in DataFrameIterator(args, df, stage="train"):
    model.train(batch)
```

---

## AsterApiCalls (`elysian_core/config/aster_api_calls.py`)

A standalone script (also importable as a module) that fetches live trading symbol data from the Aster exchange REST API and serializes it to a JSON file. Intended to be run as a one-off to refresh `config.json` with the current active symbol list.

**Base URLs:**

| Market | Base URL |
|--------|----------|
| Futures | `https://fapi.asterdex.com` |
| Spot | `https://sapi.asterdex.com` |

### `fetch_aster_symbols`

```python
def fetch_aster_symbols() -> dict
```

Hits the Aster exchange info endpoints and returns a dictionary with keys:

| Key | Description |
|-----|-------------|
| `spot_symbols` | USDT-quoted spot pairs with status `TRADING` |
| `spot_pairs` | Same list (alias for `spot_symbols`) |
| `futures_symbols` | USDT-quoted perpetual futures with status `TRADING` and `contractType == "PERPETUAL"` |
| `futures_pairs` | Same list (alias for `futures_symbols`) |
| `spot_tokens` | Unique base and quote tokens from active spot symbols |
| `futures_tokens` | Unique base and quote tokens from active futures symbols |

### `build_config`

```python
def build_config(output_path: str) -> dict
```

Calls `fetch_aster_symbols`, writes a JSON file to `output_path` containing `"Spot Tokens"` and `"Futures Tokens"` keys, and returns the resulting dict. Prints counts of spot and futures symbols to stdout.

**Usage (direct execution):**
```bash
# Run from the config directory
python -m elysian_core.config.aster_api_calls
# Writes aster_exchange_info.json in the config directory
```

**Usage (import):**
```python
from elysian_core.config.aster_api_calls import fetch_aster_symbols, build_config

symbols = fetch_aster_symbols()
print(symbols["futures_symbols"])   # ["BTCUSDT", "ETHUSDT", ...]

build_config("elysian_core/config/aster_exchange_info.json")
```

---

## AppConfig (`elysian_core/config/app_config.py`)

The central typed configuration object for the Elysian system. Merges four config sources into a single `AppConfig` dataclass.

**Config sources and priority:**

| Source | Contents |
|--------|----------|
| `.env` | API keys, DB credentials, Redis connection |
| `trading_config.yaml` | Risk limits, portfolio params, execution settings, venue overrides |
| `strategies/*.yaml` | Per-strategy: id, class, symbols, risk/execution overrides, params |
| `config.json` | Venue symbol lists, token lists, pools |

**Override priority for risk/execution (highest wins):**
1. Strategy `risk_overrides`
2. `venue_configs["{asset_type}_{venue}"]`
3. Global `risk` section

### Key Classes

#### `DictConfig`

A `dict` subclass that provides recursive dot-access to nested YAML sections. Supports arbitrary keys without schema changes.

```python
cfg.strategy.my_new_param       # dot-access
cfg.strategy.get("opt", 42)     # dict.get still works
```

#### `ExchangeSecrets`

Frozen dataclass holding `api_key: str` and `api_secret: str` for one exchange.

#### `SecretsConfig`

Frozen dataclass aggregating all secrets loaded from `.env`:

| Field | Env var | Default |
|-------|---------|---------|
| `binance.api_key` | `BINANCE_API_KEY_0` | `""` |
| `binance.api_secret` | `BINANCE_API_SECRET_0` | `""` |
| `aster.api_key` | `ASTER_API_KEY_0` | `""` |
| `aster.api_secret` | `ASTER_API_SECRET_0` | `""` |
| `binance_wallet_address` | `BINANCE_WALLET_ADDRESS` | `""` |
| `postgres_user` | `POSTGRES_USER` | `""` |
| `postgres_password` | `POSTGRES_PASSWORD` | `""` |
| `postgres_host` | `POSTGRES_HOST` | `"localhost"` |
| `postgres_port` | `POSTGRES_PORT` | `5432` |
| `postgres_database` | `POSTGRES_DATABASE` | `""` |
| `redis_host` | `REDIS_HOST` | `"localhost"` |
| `redis_port` | `REDIS_PORT` | `6379` |

#### `VenueSymbols`

Frozen dataclass with `spot: List[str]` and `futures: List[str]`.

#### `SymbolsConfig`

Frozen dataclass loaded from `config.json`.

| Field | Type | Description |
|-------|------|-------------|
| `venues` | `Dict[str, VenueSymbols]` | Per-venue spot/futures symbol lists |
| `spot_tokens` | `List[str]` | All spot tokens |
| `futures_tokens` | `List[str]` | All futures tokens |
| `package_targets` | `List[str]` | Target packages |
| `pools` | `List[str]` | Pool identifiers |

Methods:
- `symbols_for(venue: str, market: str = "spot") -> List[str]` — returns the symbol list for the given venue (case-insensitive) and market type (`"spot"` or `"futures"`).
- `all_spot_pairs` (property) — flat list of all spot pairs across all venues.
- `all_futures_pairs` (property) — flat list of all futures pairs across all venues.

#### `MetaConfig`

| Field | Type | Description |
|-------|------|-------------|
| `version_name` | `str` | Config version label |
| `strategy_id` | `int` | Default strategy ID |
| `strategy_name` | `str` | Default strategy name |
| `spot_venues` | `List[str]` | Active spot venues |
| `futures_venues` | `List[str]` | Active futures venues |

#### `StrategyConfig`

Typed config for a single strategy instance, loaded from a per-strategy YAML file.

| Field | Type | Description |
|-------|------|-------------|
| `strategy_id` | `int` | Unique strategy identifier |
| `strategy_name` | `str` | Human-readable strategy name |
| `class_name` | `str` | Fully-qualified class path (e.g. `elysian_core.strategy.EventDrivenStrategy`) |
| `asset_type` | `str` | `"Spot"` or `"Perpetuals"` |
| `venue` | `str` | Primary venue name |
| `venues` | `List[str]` | All venues this strategy uses |
| `symbols` | `List[str]` | Symbol list; empty list means load from `config.json` |
| `params` | `Dict[str, Any]` | Arbitrary strategy parameters (e.g. `rebalance_interval_s`) |
| `risk_overrides` | `Dict[str, Any]` | Risk config overrides applied on top of global/venue risk |
| `execution_overrides` | `Dict[str, Any]` | Execution config overrides |
| `portfolio_overrides` | `Dict[str, Any]` | Portfolio config overrides |
| `sub_account_api_key` | `str` | Read from env var `{VENUE}_API_KEY_{strategy_id}` |
| `sub_account_api_secret` | `str` | Read from env var `{VENUE}_API_SECRET_{strategy_id}` |
| `risk_config` | `RiskConfig` | Fully resolved risk config for this strategy |

#### `VenueConfig`

Overrides scoped to a specific `(asset_type, venue)` pair. Key format in YAML: `"{asset_type}_{venue}"` (lowercase), e.g. `"spot_binance"`, `"perpetual_binance"`.

| Field | Type | Description |
|-------|------|-------------|
| `risk_overrides` | `Dict[str, Any]` | Risk parameter overrides |
| `execution_overrides` | `Dict[str, Any]` | Execution parameter overrides |
| `portfolio_overrides` | `Dict[str, Any]` | Portfolio parameter overrides |

#### `AppConfig`

Top-level composite dataclass.

| Field | Type | Description |
|-------|------|-------------|
| `meta` | `MetaConfig` | System version and venue metadata |
| `risk` | `RiskConfig` | Typed global risk configuration |
| `portfolio` | `DictConfig` | Portfolio parameters (dot-access) |
| `execution` | `DictConfig` | Execution parameters (dot-access) |
| `strategy` | `DictConfig` | Strategy system parameters (dot-access) |
| `secrets` | `SecretsConfig` | All loaded secrets |
| `symbols` | `SymbolsConfig` | Venue symbol/token lists |
| `strategies` | `Dict[int, StrategyConfig]` | Per-strategy configs keyed by strategy ID |
| `venue_configs` | `Dict[str, VenueConfig]` | Per-(asset_type, venue) overrides |
| `extra` | `DictConfig` | Any unrecognized top-level YAML sections |

**Key methods:**

`effective_risk_for(asset_type=None, venue=None, strategy_id=None) -> RiskConfig`
Returns a merged `RiskConfig`. If `strategy_id` is given, returns that strategy's pre-resolved `risk_config` directly. Otherwise merges global risk with venue-level overrides.

`effective_execution_for(asset_type=None, venue=None) -> DictConfig`
Returns a merged execution `DictConfig` applying global execution then venue-level execution overrides.

### Factory Functions

#### `load_app_config`

```python
def load_app_config(
    trading_config_yaml: str = "elysian_core/config/trading_config.yaml",
    strategy_config_yamls: Optional[List[str]] = None,
    json_path: str = "elysian_core/config/config.json",
    env_path: str = ".env",
    placeholders: Optional[Dict[str, str]] = None,
    yaml_path: Optional[str] = None,   # backward-compat alias
) -> AppConfig
```

Primary entry point for loading configuration. Reads `.env`, the trading config YAML, optional per-strategy YAMLs, and the JSON symbols file, then assembles and returns an `AppConfig`.

**Two-tier layout (preferred):**
```python
from elysian_core.config.app_config import load_app_config

cfg = load_app_config(
    trading_config_yaml="elysian_core/config/trading_config.yaml",
    strategy_config_yamls=[
        "elysian_core/config/strategies/strategy_000_event_driven.yaml",
    ],
)
cfg.risk.max_weight_per_asset           # 0.25
cfg.portfolio.max_history               # dot-access DictConfig
cfg.execution.default_order_type        # "MARKET"
cfg.strategies[0].params["rebalance_interval_s"]  # 60
cfg.symbols.symbols_for("binance", "spot")        # ["SUIUSDC", ...]
cfg.secrets.binance.api_key             # from .env
cfg.effective_risk_for("spot", "binance")
```

**Legacy single-file layout:**
```python
cfg = load_app_config(yaml_path="elysian_core/config/config.yaml")
```

#### `load_strategy_yaml`

```python
def load_strategy_yaml(yaml_path: str) -> StrategyConfig
```

Loads a single strategy YAML file into a `StrategyConfig`. Sub-account credentials are read from environment variables using the pattern `{VENUE}_API_KEY_{strategy_id}` and `{VENUE}_API_SECRET_{strategy_id}`.

#### `load_strategy_class`

```python
def load_strategy_class(class_path: str) -> type
```

Dynamically imports and returns a strategy class from a fully-qualified dotted path.

```python
cls = load_strategy_class("elysian_core.strategy.example_weight_strategy_v2_event_driven.EventDrivenStrategy")
instance = cls(cfg, ...)
```

#### `_build_risk_config` (internal)

```python
def _build_risk_config(risk_section: dict) -> RiskConfig
```

Constructs a `RiskConfig` from a raw YAML dict, extracting only recognized scalar risk fields and ignoring unknown keys.
