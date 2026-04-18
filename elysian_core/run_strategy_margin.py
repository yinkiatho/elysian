"""
Margin strategy container entry point — Redis transport.

Extends :class:`StrategyRunner` to support multi-sub-account strategies that
mix SPOT and isolated-MARGIN pipelines in a single strategy instance.

Key differences from ``run_strategy.py``:
  - ``setup_strategy()`` iterates ``strat_cfg.sub_accounts`` and builds one
    :class:`SubAccountPipeline` per entry (SPOT or MARGIN).
  - Uses :class:`MarginShadowBook` for MARGIN sub-accounts, standard
    :class:`ShadowBook` for SPOT sub-accounts.
  - ``_compute_channels()`` is an instance method that aggregates channels
    across all sub-accounts (MARGIN maps to SPOT Redis channels).
  - Falls back to the parent single-pipeline behaviour when ``sub_accounts``
    is empty (backward-compatible with legacy SPOT-only strategy YAMLs).

Run with:
    python elysian_core/run_strategy_margin.py --strategy-yaml <path>
    STRATEGY_CONFIG_YAML=.../strategy_002.yaml python elysian_core/run_strategy_margin.py
"""

from typing import Dict, List, Optional, Any
import argparse
import asyncio
import datetime
import os
import sys
from pathlib import Path

from elysian_core.run_strategy import StrategyRunner, _compute_channels
from elysian_core.config.app_config import (
    load_app_config,
    load_strategy_class,
    StrategyConfig,
    SubAccountConfig,
)
from elysian_core.connectors.binance.BinanceExchange import BinanceSpotExchange
from elysian_core.connectors.binance.BinanceFuturesExchange import BinanceFuturesExchange
from elysian_core.connectors.binance.BinanceMarginExchange import BinanceMarginExchange
from elysian_core.connectors.aster.AsterExchange import AsterSpotExchange

from elysian_core.core.enums import AssetType, Venue, RunnerState
from elysian_core.core.event_bus import EventBus
from elysian_core.core.shadow_book import ShadowBook
from elysian_core.core.sub_account_pipeline import SubAccountKey, SubAccountPipeline
from elysian_core.core.portfolio import Portfolio
from elysian_core.execution.engine import ExecutionEngine
from elysian_core.risk.optimizer import PortfolioOptimizer
from elysian_core.strategy.margin_strategy import MarginStrategy
from elysian_core.transport.event_serializer import strategy_channels
from elysian_core.transport.redis_event_bus import RedisEventBusSubscriber
from elysian_core.utils.logger import setup_custom_logger, shutdown_discord, init_discord_logging
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class MarginStrategyRunner(StrategyRunner):
    """Strategy runner for multi-sub-account margin strategies.

    Overrides :class:`StrategyRunner` to build one pipeline per sub-account
    declared in the strategy YAML ``sub_accounts:`` section.  Falls back to
    the parent single-pipeline behaviour when ``sub_accounts`` is empty.
    """

    # ── Strategy wiring (Stage 3-5, multi-sub-account) ───────────────────────

    async def setup_strategy(
        self,
        strategy,
        strategy_id: int,
        strategy_name: str,
    ) -> None:
        """Wire a MarginStrategy with one pipeline per sub-account.

        If the strategy YAML has no ``sub_accounts:`` section, falls back to
        the parent's single-pipeline setup (standard SPOT behaviour).
        """
        strat_cfg = self.cfg.strategies[strategy_id]

        if not strat_cfg.sub_accounts:
            # Legacy / SPOT-only strategy — delegate to parent
            await super().setup_strategy(strategy, strategy_id, strategy_name)
            return

        for sub_cfg in strat_cfg.sub_accounts:
            asset_type = AssetType[sub_cfg.asset_type.upper()]
            venue = Venue[sub_cfg.venue.upper()]
            pipeline = await self._build_margin_pipeline(
                sub_cfg, strategy_id, strategy_name, strat_cfg, asset_type, venue
            )
            strategy._pipelines[pipeline.key] = pipeline

        # Wire common strategy fields (not set by parent since we bypass it)
        strategy.strategy_id = strategy_id
        strategy.strategy_name = strategy_name
        strategy._shared_event_bus = self.shared_event_bus
        strategy.cfg = self.cfg
        strategy.strategy_config = strat_cfg

        await strategy.start()

        self.logger.info(
            f"[MarginRunner] Wired: {strategy.__class__.__name__} (id={strategy_id})"
            f" — {len(strategy._pipelines)} sub-account pipeline(s)"
        )

    async def _build_margin_pipeline(
        self,
        sub_cfg: SubAccountConfig,
        strategy_id: int,
        strategy_name: str,
        strat_cfg: StrategyConfig,
        asset_type: AssetType,
        venue: Venue,
    ) -> SubAccountPipeline:
        """Build one complete Stage 3-5 pipeline for a single sub-account.

        Returns a :class:`SubAccountPipeline` with ``rebalance_fsm=None``.
        The FSM is created later in ``MarginStrategy.start()`` so it can hold
        a reference to the fully initialised strategy instance.
        """
        # ── Exchange + private bus ────────────────────────────────────────
        private_bus = EventBus()
        exchange_cls = self._exchange_connector_callables[(asset_type, venue)]
        exchange = exchange_cls(
            args=self.cfg,
            api_key=sub_cfg.api_key,
            api_secret=sub_cfg.api_secret,
            symbols=sub_cfg.symbols,
            private_event_bus=private_bus,
            strategy_config=strat_cfg,
        )
        exchange.start(self.shared_event_bus)
        await exchange.run()

        # Track for cleanup
        exchange_key = f"{strategy_id}_{sub_cfg.sub_account_id}"
        self._sub_account_exchange_dict[exchange_key] = exchange

        # ── ShadowBook ────────────────────────────────────────────────────
        if asset_type == AssetType.MARGIN:
            from elysian_core.core.margin_shadow_book import MarginShadowBook
            shadow_book = MarginShadowBook(
                strategy_id=strategy_id,
                strategy_name=strategy_name,
                venue=venue,
                strategy_config=strat_cfg,
            )
        else:
            shadow_book = ShadowBook(
                strategy_id=strategy_id,
                strategy_name=strategy_name,
                venue=venue,
                strategy_config=strat_cfg,
            )

        shadow_book.sync_from_exchange(exchange, feeds={})
        shadow_book.start(self.shared_event_bus, private_event_bus=private_bus)
        self._start_snapshot_task(
            shadow_book.save_snapshot,
            f"shadow_book_{strategy_id}_{sub_cfg.sub_account_id}",
        )

        # ── Portfolio (NAV aggregation) ───────────────────────────────────
        portfolio = self._portfolio_dict[asset_type].setdefault(
            venue, Portfolio(venue=venue, cfg=self.cfg)
        )
        portfolio.register_shadow_book(shadow_book)

        # ── Risk config: sub-account overrides on top of strategy-level risk ─
        risk_cfg = (
            sub_cfg.risk_config
            if sub_cfg.risk_config is not None
            else self.cfg.effective_risk_for(
                asset_type=sub_cfg.asset_type.lower(),
                venue=sub_cfg.venue.lower(),
                strategy_id=strategy_id,
            )
        )

        # ── Optimizer + ExecutionEngine ───────────────────────────────────
        optimizer = PortfolioOptimizer(
            risk_config=risk_cfg,
            portfolio=shadow_book,
            cfg=self.cfg,
        )
        execution_engine = ExecutionEngine(
            exchanges={venue: exchange},
            portfolio=shadow_book,
            default_venue=venue,
            cfg=self.cfg,
            asset_type=asset_type,
        )
        execution_engine.start(self.shared_event_bus)

        key = SubAccountKey(
            strategy_id=strategy_id,
            asset_type=asset_type,
            venue=venue,
            sub_account_id=sub_cfg.sub_account_id,
        )

        self.logger.info(
            f"[MarginRunner] Pipeline built: {key} "
            f"({asset_type.value}/{venue.value}) "
            f"symbols={sub_cfg.symbols}"
        )

        return SubAccountPipeline(
            key=key,
            exchange=exchange,
            private_event_bus=private_bus,
            shadow_book=shadow_book,
            optimizer=optimizer,
            execution_engine=execution_engine,
            rebalance_fsm=None,      # created in MarginStrategy.start()
            symbols=list(sub_cfg.symbols),
        )

    # ── Channel computation — collects from all sub-accounts ─────────────────

    def _compute_channels(self) -> List[str]:
        """Return the union of Redis channels needed across all sub-accounts.

        MARGIN sub-accounts subscribe to SPOT channels (Binance serves
        isolated-margin market data on the same streams).
        """
        channels: List[str] = []
        for strat_cfg in self.cfg.strategies.values():
            if strat_cfg.sub_accounts:
                for sub_cfg in strat_cfg.sub_accounts:
                    venue = Venue[sub_cfg.venue.upper()]
                    # MARGIN reuses SPOT channel format (event_serializer maps MARGIN→SPOT)
                    channels.extend(
                        strategy_channels(sub_cfg.symbols, venue, AssetType.SPOT)
                    )
            else:
                # Legacy single-sub-account strategy
                venue = Venue[strat_cfg.venue.upper()]
                asset_type = AssetType[strat_cfg.asset_type.upper()]
                channels.extend(strategy_channels(strat_cfg.symbols, venue, asset_type))
        return list(set(channels))

    # ── Main run — override only the channel computation call ────────────────

    async def run(self) -> None:
        enable_discord = self.cfg.extra.get('logging', {}).get('enable_discord', False)
        if enable_discord:
            token = os.getenv('DISCORD_TOKEN')
            guild_id_str = os.getenv('DISCORD_GUILD_ID')
            if token and guild_id_str:
                await init_discord_logging(token=token, guild_id=int(guild_id_str))

        strategy = load_strategy_class(self.strat_cfg.class_name)(
            strategy_name=self.strat_cfg.strategy_name,
            strategy_id=self.strat_cfg.strategy_id,
        )

        try:
            self.logger.info("[MarginRunner] ── Margin strategy runner starting ──")

            self._state = RunnerState.CONFIGURING
            self.shared_event_bus = RedisEventBusSubscriber(redis_url=self._redis_url)
            self.logger.info(
                f"[MarginRunner] RedisEventBusSubscriber ready ({self._redis_url})"
            )

            self._state = RunnerState.CONNECTING

            try:
                self._state = RunnerState.SYNCING
                await self._setup_pipeline()
                await self.setup_strategy(
                    strategy,
                    strategy_id=self.strat_cfg.strategy_id,
                    strategy_name=self.strat_cfg.strategy_name,
                )

                # Use instance method so margin sub-account channels are included
                channels = self._compute_channels()
                await self.shared_event_bus.start_listener(channels)
                self.logger.info(
                    f"[MarginRunner] Subscribed to {len(channels)} Redis channels"
                )

                coros: List[Any] = []
                if self.shared_event_bus._listener_task:
                    coros.append(self.shared_event_bus._listener_task)
                coros.append(strategy.run_forever())

                self._state = RunnerState.RUNNING
                self.logger.info(
                    f"[MarginRunner] Main loop: {len(coros)} coroutine(s)"
                )
                await asyncio.gather(*coros)

            finally:
                self._state = RunnerState.STOPPING
                await strategy.stop()

        except asyncio.CancelledError:
            self.logger.warning("[MarginRunner] Cancelled")
        except Exception as e:
            self._state = RunnerState.FAILED
            self.logger.error(f"[MarginRunner] Fatal: {e}", exc_info=True)
            raise
        finally:
            await self._cleanup()
            if self._state != RunnerState.FAILED:
                self._state = RunnerState.STOPPED
            self.logger.info(f"[MarginRunner] Stopped (state={self._state.name})")


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_strategy_margin(
    trading_config_yaml: str,
    strategy_config_yaml: str,
    redis_url: str,
) -> None:
    runner = MarginStrategyRunner(
        trading_config_yaml=trading_config_yaml,
        strategy_config_yaml=strategy_config_yaml,
        redis_url=redis_url,
    )
    await runner.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Elysian Margin Strategy Runner")
    parser.add_argument(
        "--trading-config",
        default="elysian_core/config/trading_config.yaml",
    )
    parser.add_argument(
        "--strategy-yaml",
        default=os.environ.get(
            "STRATEGY_CONFIG_YAML",
            "elysian_core/config/strategies/strategy_002_margin_example.yaml",
        ),
    )
    parser.add_argument(
        "--redis-url",
        default=(
            f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:"
            f"{os.environ.get('REDIS_PORT', '6379')}"
        ),
    )
    args = parser.parse_args()

    asyncio.run(run_strategy_margin(
        trading_config_yaml=args.trading_config,
        strategy_config_yaml=args.strategy_yaml,
        redis_url=args.redis_url,
    ))
