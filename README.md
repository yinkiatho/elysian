# Elysian

Event-driven, multi-venue cryptocurrency trading system. Single asyncio event loop, multiplexed WebSocket feeds, weight-vector rebalancing with integrated risk management.

## Architecture

6-stage pipeline: **Market Data** → **EventBus** → **Strategy / Alpha** → **Risk** → **Execution** → **Exchange**

```
WebSocket Feeds ──► RedisEventBus ──► SpotStrategy.on_kline / on_orderbook_update
                                             │
                                             ▼
                                       RebalanceFSM
                                compute_weights() ──► TargetWeights
                                optimizer.validate() ──► ValidatedWeights
                                engine.execute()   ──► OrderIntents ──► Exchange REST
                                             │
                                             ▼
                                  RebalanceCompleteEvent (private bus)
```

## Deployment Model

Elysian runs as a set of Docker containers:

- **`elysian_market_data`** — owns all WebSocket feed managers; publishes `KlineEvent` / `OrderBookUpdateEvent` to Redis pub/sub
- **`elysian_strategy_N`** — one container per strategy; subscribes to Redis for market data, runs the full Stage 3–5 pipeline
- **`elysian_postgres`** — trade and snapshot persistence
- **`elysian_redis`** — market data transport between containers

## Quick Start

```bash
cp .env.example .env     # fill in API keys and DB credentials
docker compose build
make up                  # start postgres + redis
make strategy-0          # start strategy 0
```

Or run locally without Docker:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python elysian_core/run_strategy.py
```

## Writing a Strategy

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent
import time

class MyStrategy(SpotStrategy):

    async def on_start(self):
        self._last_rebalance = 0.0

    def compute_weights(self, **ctx) -> dict:
        """Pure function — no I/O, no side effects. Return {} to skip cycle."""
        if ctx.get("convert_all_base"):
            return {}
        return {"ETHUSDT": 0.4, "BTCUSDT": 0.5}   # 10% implicit cash

    async def on_kline(self, event: KlineEvent):
        now = time.monotonic()
        if now - self._last_rebalance > 60:
            self._last_rebalance = now
            await self.request_rebalance()

    async def run_forever(self):
        await self._stop_event.wait()
```

Key invariants:
- `compute_weights()` must be **pure** — no exchange calls, no EventBus publishes
- Return `{}` or `None` to skip a cycle; never return `{"USDT": 1.0}`
- Read position state from `self._shadow_book`, not `self.portfolio`
- `request_rebalance()` is silently ignored if the FSM is busy — safe to call freely

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys, DB and Redis credentials |
| `elysian_core/config/trading_config.yaml` | System-level risk limits, execution defaults, venue configs |
| `elysian_core/config/strategies/strategy_NNN.yaml` | Per-strategy class, symbols, risk overrides, params |
| `elysian_core/config/config.json` | Symbol lists and token lists per venue |

## Project Structure

```
elysian_core/
├── run_strategy.py          # StrategyRunner entry point (Redis transport)
├── run_market_data.py       # MarketDataService entry point
├── config/                  # YAML + JSON configuration
├── connectors/              # Exchange connectors + data feeds
│   ├── base.py              # Abstract base classes
│   ├── binance/             # Binance Spot + Futures connectors
│   └── aster/               # Aster Spot + Perp connectors
├── core/                    # Domain model + state machines
│   ├── enums.py             # Side, Venue, OrderStatus, RebalanceState, …
│   ├── events.py            # Frozen event dataclasses
│   ├── event_bus.py         # In-process async pub/sub
│   ├── rebalance_fsm.py     # RebalanceFSM (compute→validate→execute→cooldown)
│   ├── shadow_book.py       # Per-strategy position ledger
│   ├── portfolio.py         # Aggregate NAV monitor
│   └── signals.py           # TargetWeights, ValidatedWeights, OrderIntent
├── strategy/                # SpotStrategy base + example implementations
├── risk/                    # RiskConfig + PortfolioOptimizer
├── execution/               # ExecutionEngine
├── transport/               # Redis pub/sub event bus (publisher + subscriber)
├── services/                # MarketDataService
├── db/                      # Peewee ORM (PostgreSQL)
└── utils/                   # Logger, config helpers
```

## License

Apache 2.0 — see LICENSE.
