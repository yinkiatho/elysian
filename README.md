# Elysian

Event-driven, multi-venue cryptocurrency trading system. Single asyncio event loop, multiplexed WebSocket feeds, weight-vector rebalancing with integrated risk management.

## Architecture

6-stage pipeline: **Market Data** &rarr; **Feature Compute** &rarr; **Strategy / Alpha** &rarr; **Risk** &rarr; **Execution** &rarr; **Exchange**

See [`architecture_diagram.html`](architecture_diagram.html) for interactive diagrams and [`state_transitions.html`](state_transitions.html) for full FSM documentation.

```
WebSocket Feeds ──► EventBus ──► SpotStrategy.on_kline / on_orderbook_update
                                       │
                                       ▼
                                 RebalanceFSM
                          compute_weights() ──► TargetWeights
                          optimizer.validate() ──► ValidatedWeights
                          engine.execute()   ──► OrderIntents ──► Exchange REST
                                       │
                                       ▼
                              RebalanceCompleteEvent
```

## Quick Start

```bash
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # add API keys
python elysian_core/run_strategy.py
```

## Writing a Strategy

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent

class MyStrategy(SpotStrategy):

    def compute_weights(self, **ctx):
        """Return target portfolio weights. Called by RebalanceFSM each tick."""
        return {"ETHUSDT": 0.4, "BTCUSDT": 0.5}  # 10% implicit cash

    async def on_kline(self, event: KlineEvent):
        """React to market data. Optionally trigger rebalance."""
        if self.should_rebalance(event):
            await self.request_rebalance()

    async def run_forever(self):
        """Start periodic rebalancing via the FSM timer."""
        self._rebalance_fsm.start_timer(interval_s=60)
        await self._rebalance_fsm.wait()
```

Run it:
```python
from elysian_core.run_strategy import StrategyRunner
from elysian_core.core.event_bus import EventBus
import asyncio

runner = StrategyRunner()
strategy = MyStrategy(exchanges={}, event_bus=EventBus())
asyncio.run(runner.run(strategy=strategy))
```

## Configuration

- **`config.yaml`** &mdash; strategy params, risk limits, database, execution defaults
- **`config.json`** &mdash; trading pairs per venue
- **`.env`** &mdash; API keys, database credentials

## Project Structure

```
elysian_core/
├── run_strategy.py          # StrategyRunner: main entry point
├── config/                  # YAML + JSON config
├── connectors/              # Exchange connectors + data feeds
│   ├── base.py              # AbstractDataFeed, SpotExchangeConnector ABCs
│   ├── Binance*.py          # Spot + Futures
│   └── Aster*.py            # Spot + Perps
├── core/                    # Shared types + state machines
│   ├── enums.py             # Side, Venue, OrderStatus, RebalanceState, ...
│   ├── events.py            # Frozen event dataclasses
│   ├── event_bus.py         # Async pub/sub
│   ├── fsm.py               # BaseFSM + PeriodicTask
│   ├── rebalance_fsm.py     # RebalanceFSM (compute→validate→execute→cooldown)
│   ├── order_fsm.py         # Order lifecycle validation
│   ├── signals.py           # TargetWeights, ValidatedWeights, OrderIntent
│   ├── portfolio.py         # Portfolio + Position tracking
│   └── market_data.py       # Kline, OrderBook
├── strategy/                # SpotStrategy base + examples
├── risk/                    # RiskConfig + PortfolioOptimizer
├── execution/               # ExecutionEngine
├── db/                      # Peewee ORM (PostgreSQL)
└── utils/                   # Logger, config helpers
```

## License

See LICENSE file for details.
