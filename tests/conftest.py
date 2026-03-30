"""Shared pytest fixtures for the Elysian test suite."""
import os
import sys
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Change to project root so all relative config paths resolve
os.chdir(PROJECT_ROOT)


@pytest.fixture(scope="session")
def cfg():
    """AppConfig loaded from the two-tier config layout."""
    from elysian_core.config.app_config import load_app_config
    return load_app_config(
        trading_config_yaml='elysian_core/config/trading_config.yaml',
        strategy_config_yamls=['elysian_core/config/strategies/strategy_001_event_driven.yaml'],
    )


@pytest.fixture(scope="session")
def runner():
    """StrategyRunner constructed from the two-tier config layout."""
    from elysian_core.run_strategy import StrategyRunner
    return StrategyRunner(
        trading_config_yaml='elysian_core/config/trading_config.yaml',
        strategy_config_yamls=['elysian_core/config/strategies/strategy_001_event_driven.yaml'],
    )


@pytest.fixture(scope="session")
def strategy(runner):
    """EventDrivenStrategy with cfg/strategy_config injected (simulates setup_strategy)."""
    from elysian_core.strategy.example_weight_strategy_v2_event_driven import EventDrivenStrategy
    from elysian_core.core.event_bus import EventBus
    from elysian_core.core.enums import Venue, AssetType

    strat = EventDrivenStrategy(
        exchanges={},
        event_bus=EventBus(),
        rebalance_interval_s=60.0,
        asset_type=AssetType.SPOT,
        venues={Venue.BINANCE},
    )
    # Simulate what setup_strategy() does
    strat.cfg = runner.cfg
    strat.strategy_id = 1
    strat.strategy_name = runner.cfg.strategies[0].name
    strat.strategy_config = next(
        (s for s in runner.cfg.strategies if s.id == 1), None
    )
    return strat
