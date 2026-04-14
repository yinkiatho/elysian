# Utils & Config Module Documentation

## Overview

The `elysian_core/utils/` and `elysian_core/config/` directories provide logging infrastructure, configuration management, data processing utilities, and external API helpers used throughout the Elysian trading system.

Key responsibilities:
- Structured, colored terminal logging with file persistence and optional Discord integration
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

`logger.success(msg)` is patched onto `logging.Logger` directly so it works on any logger returned by `setup_custom_logger`.

### `ColoredFormatter`

A `logging.Formatter` subclass that wraps each log line in ANSI escape codes.

| Level | Color |
|-------|-------|
| `DEBUG` | Blue (`\033[34m`) |
| `INFO` | Bright white (`\033[97m`) |
| `SUCCESS` | Bright green (`\033[92m`) |
| `WARNING` | Yellow (`\033[33m`) |
| `ERROR` | Red (`\033[31m`) |
| `CRITICAL` | White on red background (`\033[41m`) |

### `setup_custom_logger`

```python
def setup_custom_logger(name: str, log_level: int = logging.INFO) -> logging.Logger
```

Returns a logger for the given `name`. Logger instances are cached in the module-level `loggers` dict — calling twice with the same name returns the same object without adding duplicate handlers.

**Behavior:**
- Adds a `StreamHandler` with `ColoredFormatter` for terminal output (UTF-8, line-buffered)
- Adds a `FileHandler` writing to `elysian_core/utils/logs/<run_timestamp>/<name>.log`; the run timestamp (`_run_timestamp`) is set once at module import time (UTC+8)
- Log directory is created automatically
- Both handlers use format: `%(asctime)s - %(levelname)s - %(module)s - %(filename)s:%(lineno)d - %(message)s`
- If `_discord_bot_manager` is active, also adds a `DiscordHandler`

**Usage:**
```python
from elysian_core.utils.logger import setup_custom_logger

logger = setup_custom_logger("my_strategy_0")
logger.debug("Debug detail")
logger.info("Strategy initialized")
logger.success("Order executed successfully")
logger.warning("Approaching position limit")
logger.error("Connector disconnected")
```

### Discord Integration

```python
async def init_discord_logging(token: str, guild_id: int)
```

Initializes the `DiscordBotManager` globally and attaches a `DiscordHandler` to all existing loggers. After this call, new loggers created via `setup_custom_logger` also automatically get a Discord handler. The bot creates per-logger-name channels in the specified guild.

```python
async def shutdown_discord()
```

Flushes the send queue and stops the Discord bot. Should be called in the runner's cleanup path.

### `DiscordBotManager` (`elysian_core/utils/discord_risk_bot.py`)

Manages a Discord bot connection with an async queue for log messages:
- Non-blocking `enqueue_log(logger_name, message) -> bool` — returns `False` if queue is full
- Background `_sender` task drains the queue and dispatches to per-channel Discord channels
- Rate limiting (0.2s between sends per channel) to avoid Discord API limits
- Channels are created lazily — one channel per logger name (sanitized)
- `flush(timeout=5.0)` waits for queue to drain before shutdown

### `DiscordHandler`

A `logging.Handler` that enqueues messages to `DiscordBotManager`. Strips ANSI color codes before sending.

---

## Utility Functions (`elysian_core/utils/utils.py`)

### `load_config`

```python
def load_config(path: str) -> dict
```

Reads a YAML file at `path` and returns its contents as a dict using `yaml.safe_load`.

### `replace_placeholders`

```python
def replace_placeholders(cfg: dict, placeholders: dict) -> dict
```

Recursively walks a config dict and replaces `{key}` tokens in string values with values from `placeholders`. Handles nested dicts and lists.

```python
cfg = {"log_path": "logs/{strategy_name}.log"}
out = replace_placeholders(cfg, {"strategy_name": "momentum"})
# out == {"log_path": "logs/momentum.log"}
```

Used by `load_strategy_yaml()` to substitute `{strategy_name}` in YAML strings.

### `config_to_args`

```python
def config_to_args(cfg: dict, placeholders: dict = None) -> argparse.Namespace
```

Converts a config dict to an `argparse.Namespace` with recursive dot-access. Nested dicts become nested Namespaces; lists are preserved element-by-element.

```python
from elysian_core.utils.utils import load_config, config_to_args

cfg = load_config("config/config.yaml")
args = config_to_args(cfg)
args.regression.S3.window_size   # dot-access to nested values
```

### `DataFrameIterator`

```python
class DataFrameIterator:
    def __init__(self, args: Namespace, df: pd.DataFrame, stage: str = 'train')
```

An iterator that yields fixed-size sub-DataFrames from a market-data DataFrame. Used by ML training and inference pipelines.

**Constructor:**
- Filters `df` to the date range for `stage` (`train`, `test`, or `inference`) from `args.regression.S3`
- Sorts by `(Date, Ticker)`
- Batch size: `window_size` unique dates × `ticker_size` unique tickers

**Iteration order:** Steps through all ticker windows for each date window before advancing to the next date window.

---

## AppConfig (`elysian_core/config/app_config.py`)

The central typed configuration object for the Elysian system. Merges four config sources into a single `AppConfig` dataclass.

### Config Sources and Priority

| Source | Contents |
|--------|----------|
| `.env` | API keys, DB credentials, Redis connection |
| `trading_config.yaml` | Risk limits, portfolio params, execution settings, venue overrides |
| `strategies/*.yaml` | Per-strategy: id, class, venue, symbols, risk/execution overrides, params |
| `config.json` | Venue symbol lists, token lists |

**Override priority for risk/execution (highest wins):**
1. Strategy `risk:` section
2. `venue_configs["{asset_type}_{venue}"]`
3. Global `risk:` section

### Key Classes

#### `DictConfig`

A `dict` subclass with recursive dot-access for nested YAML sections:

```python
class DictConfig(dict):
    def __getattr__(self, key) -> Any: ...    # nested dicts auto-wrapped as DictConfig
    def __setattr__(self, key, value): ...
    def __delattr__(self, key): ...
```

Supports arbitrary keys — no schema changes needed when new params are added to YAML.

#### `ExchangeSecrets` / `SecretsConfig`

Frozen dataclasses holding credentials loaded from `.env`:

```python
@dataclass(frozen=True)
class SecretsConfig:
    binance: ExchangeSecrets      # BINANCE_API_KEY, BINANCE_API_SECRET (main account, not sub-accounts)
    aster: ExchangeSecrets        # ASTER_API_KEY, ASTER_API_SECRET
    binance_wallet_address: str   # BINANCE_WALLET_ADDRESS
    postgres_user: str            # POSTGRES_USER
    postgres_password: str        # POSTGRES_PASSWORD
    postgres_host: str            # POSTGRES_HOST (default: "localhost")
    postgres_port: int            # POSTGRES_PORT (default: 5432)
    postgres_database: str        # POSTGRES_DATABASE
    redis_host: str               # REDIS_HOST (default: "localhost")
    redis_port: int               # REDIS_PORT (default: 6379)
```

**Note:** Sub-account API keys for strategies are not stored here — they are resolved directly in `load_strategy_yaml()` using the pattern `{VENUE}_API_KEY_{strategy_id}`.

#### `SymbolsConfig`

Frozen dataclass loaded from `config.json`:

```python
@dataclass(frozen=True)
class SymbolsConfig:
    venues: Dict[str, VenueSymbols]  # "binance" → {spot: [...], futures: [...]}
    spot_tokens: List[str]
    futures_tokens: List[str]
    package_targets: List[str]        # DEX contract targets
    pools: List[str]                  # DEX pool identifiers
```

Key method: `symbols_for(venue: str, market: str = "spot") -> List[str]` — case-insensitive venue lookup.

#### `StrategyConfig`

Typed config for a single strategy instance, loaded from a per-strategy YAML:

```python
@dataclass
class StrategyConfig:
    strategy_id: int
    strategy_name: str
    class_name: str            # fully qualified Python class path
    asset_type: str            # "Spot" or "Perpetuals"
    venue: str
    venues: List[str]
    symbols: List[str]         # empty = load from config.json
    params: Dict[str, Any]     # strategy-specific parameters
    risk_overrides: Dict[str, Any]
    execution_overrides: Dict[str, Any]
    portfolio_overrides: Dict[str, Any]
    sub_account_api_key: str   # from env: {VENUE}_API_KEY_{strategy_id}
    sub_account_api_secret: str
    risk_config: RiskConfig    # fully resolved (global + venue + strategy overrides)
```

#### `AppConfig`

Top-level composite dataclass:

```python
@dataclass
class AppConfig:
    meta: MetaConfig
    risk: RiskConfig           # global risk defaults
    portfolio: DictConfig
    execution: DictConfig
    strategy: DictConfig       # global strategy params
    secrets: SecretsConfig
    symbols: SymbolsConfig
    strategies: Dict[int, StrategyConfig]  # keyed by strategy_id
    venue_configs: Dict[str, VenueConfig]  # keyed by "{asset_type}_{venue}"
    extra: DictConfig          # unrecognized top-level YAML sections
```

**Key methods:**

`effective_risk_for(asset_type=None, venue=None, strategy_id=None) -> RiskConfig`

If `strategy_id` is provided, returns that strategy's pre-resolved `risk_config` directly. Otherwise merges global risk with venue-level overrides:

```python
cfg.effective_risk_for(strategy_id=0)              # per-strategy (preferred)
cfg.effective_risk_for(asset_type="spot", venue="binance")  # venue-level
```

`effective_execution_for(asset_type=None, venue=None) -> DictConfig`

Returns merged execution config applying global defaults then venue overrides.

### Factory Functions

#### `load_app_config`

```python
def load_app_config(
    trading_config_yaml: str = "elysian_core/config/trading_config.yaml",
    strategy_config_yamls: Optional[List[str]] = None,
    json_path: str = "elysian_core/config/config.json",
    env_path: str = ".env",
    placeholders: Optional[Dict[str, str]] = None,
    yaml_path: Optional[str] = None,   # backward-compat alias for trading_config_yaml
) -> AppConfig
```

Primary entry point. Reads `.env`, trading config YAML, optional per-strategy YAMLs, and the JSON symbols file.

```python
from elysian_core.config.app_config import load_app_config

cfg = load_app_config(
    trading_config_yaml="elysian_core/config/trading_config.yaml",
    strategy_config_yamls=[
        "elysian_core/config/strategies/strategy_000_event_driven.yaml",
    ],
)
cfg.risk.max_weight_per_asset           # 0.25
cfg.portfolio.snapshot_interval_s       # DictConfig dot-access
cfg.execution.default_order_type        # "MARKET"
cfg.strategies[0].params["rebalance_interval_s"]  # 60
cfg.symbols.symbols_for("binance", "spot")        # ["SUIUSDT", …]
cfg.secrets.postgres_host               # from .env
cfg.effective_risk_for(strategy_id=0)
```

#### `load_strategy_yaml`

```python
def load_strategy_yaml(yaml_path: str) -> StrategyConfig
```

Loads a single strategy YAML into a `StrategyConfig`. Applies `replace_placeholders()` to substitute `{strategy_name}` tokens. Resolves sub-account credentials from env vars: `{VENUE}_API_KEY_{strategy_id}` and `{VENUE}_API_SECRET_{strategy_id}`.

#### `load_strategy_class`

```python
def load_strategy_class(class_path: str) -> type
```

Dynamically imports and returns a strategy class from a fully-qualified dotted path.

```python
cls = load_strategy_class("elysian_core.strategy.example_weight_strategy_v2_event_driven.EventDrivenStrategy")
strategy = cls(strategy_name="...", strategy_id=0)
```

#### `_build_risk_config` (internal)

```python
def _build_risk_config(risk_section: dict) -> RiskConfig
```

Constructs a `RiskConfig` from a raw YAML dict, extracting only recognized scalar risk fields and ignoring unknown keys.

---

## `AsterApiCalls` (`elysian_core/config/aster_api_calls.py`)

A standalone utility that fetches live trading symbol data from the Aster exchange REST API and serializes it to a JSON file. Run to refresh `aster_exchange_info.json` with the current active symbol list.

**Base URLs:**

| Market | Base URL |
|--------|----------|
| Futures | `https://fapi.asterdex.com` |
| Spot | `https://sapi.asterdex.com` |

### `fetch_aster_symbols`

```python
def fetch_aster_symbols() -> dict
```

Fetches `exchangeInfo` for both spot and futures and returns:

| Key | Description |
|-----|-------------|
| `spot_symbols` / `spot_pairs` | USDT-quoted spot pairs with status `TRADING` |
| `futures_symbols` / `futures_pairs` | USDT-quoted perpetuals with status `TRADING` and `contractType == "PERPETUAL"` |
| `spot_tokens` | Unique base and quote tokens from active spot symbols |
| `futures_tokens` | Unique base and quote tokens from active futures symbols |

### `build_config`

```python
def build_config(output_path: str) -> dict
```

Calls `fetch_aster_symbols()` and writes the result to a JSON file at `output_path`.

```bash
# Run directly to refresh the symbol list
python -m elysian_core.config.aster_api_calls
# Writes aster_exchange_info.json in the config directory
```

---

## `async_helpers` (`elysian_core/utils/async_helpers.py`)

### `cancel_tasks`

```python
async def cancel_tasks(
    reader_task: Optional[asyncio.Task],
    worker_tasks: List[asyncio.Task],
) -> None
```

Gracefully cancels a reader task and a list of worker tasks, awaiting each to consume the `CancelledError`. Used by all client managers in their `stop()` method to ensure clean shutdown without lingering background tasks.

---

## `NumpySeries` (`elysian_core/objs/numpy_series.py`)

A fixed-length ring buffer backed by a NumPy array, designed to replace `collections.deque` for rolling numeric state in strategies. Provides O(1) appends and pops from both ends, with full slicing support returning NumPy arrays (enabling vectorized operations).

```python
from elysian_core.objs.numpy_series import NumpySeries

s = NumpySeries(maxlen=60)
s.append(100.0)
s.append(101.0)
last_30 = s[-30:]          # numpy array, zero-copy slice when contiguous
mean_30 = last_30.mean()
s.to_array()               # full logical array as ndarray (handles wrap-around)
```

**Key methods:**

| Method | Description |
|--------|-------------|
| `append(value)` | Add to right (newest); drop leftmost if full |
| `appendleft(value)` | Add to left (oldest); drop rightmost if full |
| `pop()` | Remove and return rightmost |
| `popleft()` | Remove and return leftmost |
| `clear()` | Remove all elements |
| `to_array()` | Return full logical array as `np.ndarray` (handles wrap-around correctly) |
| `__getitem__(key)` | Supports integer and slice indexing; slices return `np.ndarray` |

**Properties:** `maxlen`, `__len__`, `__bool__`, `__iter__`

Used in `EventDrivenStrategy` for all rolling price and return series:
```python
self._price_series = {sym: NumpySeries(maxlen=60*240) for sym in self._symbols}
# Access last 30 values as numpy array:
sma = self._price_series[sym][-30:].mean()
```
