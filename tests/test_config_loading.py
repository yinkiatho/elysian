"""
Integration test: config loading from main entry point through to Strategy object.

Run from project root:
    python tests/test_config_loading.py

All tests use plain Python assertions (no pytest required).
Prints "PASS: <test name>" for each group and "ALL TESTS PASSED" at the end.
Exits with code 1 on the first failure.
"""

import sys
import os
import traceback

# Ensure the project root is on sys.path so imports resolve correctly.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _fail(test_name: str, exc: Exception) -> None:
    print(f"FAIL: {test_name}")
    traceback.print_exc()
    sys.exit(1)


# ---------------------------------------------------------------------------
# Test 1 — load_app_config() two-tier layout
# ---------------------------------------------------------------------------
def test_load_app_config_two_tier():
    from elysian_core.config.app_config import load_app_config

    cfg = load_app_config(
        trading_config_yaml='elysian_core/config/trading_config.yaml',
        strategy_config_yamls=['elysian_core/config/strategies/strategy_001_event_driven.yaml'],
    )

    assert cfg.risk.max_weight_per_asset == 0.25, (
        f"Expected 0.25, got {cfg.risk.max_weight_per_asset}"
    )
    assert cfg.portfolio.max_history == 10000, (
        f"Expected 10000, got {cfg.portfolio.max_history}"
    )
    assert cfg.execution.default_order_type == "MARKET", (
        f"Expected 'MARKET', got {cfg.execution.default_order_type}"
    )
    assert cfg.strategy.max_heavy_workers == 4, (
        f"Expected 4, got {cfg.strategy.max_heavy_workers}"
    )
    assert len(cfg.strategies) == 1, (
        f"Expected 1 strategy, got {len(cfg.strategies)}"
    )
    assert cfg.strategies[0].id == 1, (
        f"Expected id=1, got {cfg.strategies[0].id}"
    )
    assert cfg.strategies[0].name == "event_driven_momentum_binance_spot", (
        f"Expected 'event_driven_momentum_binance_spot', got {cfg.strategies[0].name}"
    )
    assert cfg.strategies[0].allocation == 1.0, (
        f"Expected allocation=1.0, got {cfg.strategies[0].allocation}"
    )
    assert cfg.strategies[0].params["rebalance_interval_s"] == 60, (
        f"Expected 60, got {cfg.strategies[0].params['rebalance_interval_s']}"
    )
    assert cfg.strategies[0].risk_overrides["max_weight_per_asset"] == 0.20, (
        f"Expected 0.20, got {cfg.strategies[0].risk_overrides['max_weight_per_asset']}"
    )
    assert cfg.strategies[0].risk_overrides["min_cash_weight"] == 0.10, (
        f"Expected 0.10, got {cfg.strategies[0].risk_overrides['min_cash_weight']}"
    )
    assert set(cfg.venue_configs.keys()) >= {"spot_binance", "perpetual_binance"}, (
        f"Missing venue keys, got {set(cfg.venue_configs.keys())}"
    )

    print("PASS: test_load_app_config_two_tier")
    return cfg


# ---------------------------------------------------------------------------
# Test 2 — effective_risk_for() priority chain
# ---------------------------------------------------------------------------
def test_effective_risk_for(cfg):
    # No strategy_id — global only
    r_global = cfg.effective_risk_for("spot", "binance")
    assert r_global.max_weight_per_asset == 0.25, (
        f"Expected 0.25 (global), got {r_global.max_weight_per_asset}"
    )

    # strategy_id=1 — strategy override wins
    r_strategy = cfg.effective_risk_for("spot", "binance", strategy_id=1)
    assert r_strategy.max_weight_per_asset == 0.20, (
        f"Expected 0.20 (strategy override), got {r_strategy.max_weight_per_asset}"
    )
    assert r_strategy.min_cash_weight == 0.10, (
        f"Expected 0.10 (strategy override), got {r_strategy.min_cash_weight}"
    )
    # Unchanged global value flows through
    assert r_strategy.min_order_notional == 10.0, (
        f"Expected 10.0 (global), got {r_strategy.min_order_notional}"
    )

    print("PASS: test_effective_risk_for")


# ---------------------------------------------------------------------------
# Test 3 — effective_execution_for() returns DictConfig
# ---------------------------------------------------------------------------
def test_effective_execution_for(cfg):
    exec_global = cfg.effective_execution_for("spot", "binance")
    assert exec_global.get("default_order_type") == "MARKET", (
        f"Expected 'MARKET', got {exec_global.get('default_order_type')}"
    )

    exec_strategy = cfg.effective_execution_for("spot", "binance", strategy_id=1)
    assert exec_strategy.get("default_order_type") == "MARKET", (
        f"Expected 'MARKET', got {exec_strategy.get('default_order_type')}"
    )

    print("PASS: test_effective_execution_for")


# ---------------------------------------------------------------------------
# Test 4 — effective_portfolio_for() returns DictConfig
# ---------------------------------------------------------------------------
def test_effective_portfolio_for(cfg):
    port = cfg.effective_portfolio_for("spot", "binance")
    assert port.get("max_history") == 10000, (
        f"Expected 10000, got {port.get('max_history')}"
    )

    print("PASS: test_effective_portfolio_for")


# ---------------------------------------------------------------------------
# Test 5 — backward compat: yaml_path kwarg (points to trading_config.yaml)
# ---------------------------------------------------------------------------
def test_load_app_config_legacy_yaml_path():
    from elysian_core.config.app_config import load_app_config

    cfg_legacy = load_app_config(yaml_path='elysian_core/config/trading_config.yaml')
    assert cfg_legacy.risk.max_weight_per_asset == 0.25, (
        f"Expected 0.25, got {cfg_legacy.risk.max_weight_per_asset}"
    )

    print("PASS: test_load_app_config_legacy_yaml_path")


# ---------------------------------------------------------------------------
# Test 6 — load_strategy_yaml() standalone loader
# ---------------------------------------------------------------------------
def test_load_strategy_yaml():
    from elysian_core.config.app_config import load_strategy_yaml

    sc = load_strategy_yaml('elysian_core/config/strategies/strategy_001_event_driven.yaml')
    assert sc.id == 1, f"Expected id=1, got {sc.id}"
    assert sc.class_name == "EventDrivenStrategy", (
        f"Expected 'EventDrivenStrategy', got {sc.class_name}"
    )
    assert sc.risk_overrides["max_weight_per_asset"] == 0.20, (
        f"Expected 0.20, got {sc.risk_overrides['max_weight_per_asset']}"
    )
    # Sub-account fields default to empty (no sub-account configured in default strategy)
    assert sc.sub_account_api_key == "", (
        f"Expected empty sub_account_api_key, got {sc.sub_account_api_key!r}"
    )
    assert sc.sub_account_api_secret == "", (
        f"Expected empty sub_account_api_secret, got {sc.sub_account_api_secret!r}"
    )

    print("PASS: test_load_strategy_yaml")


# ---------------------------------------------------------------------------
# Test 7 — StrategyRunner.__init__() new API
# ---------------------------------------------------------------------------
def test_strategy_runner_new_api():
    from elysian_core.run_strategy import StrategyRunner
    from elysian_core.config.app_config import AppConfig

    runner = StrategyRunner(
        trading_config_yaml='elysian_core/config/trading_config.yaml',
        strategy_config_yamls=['elysian_core/config/strategies/strategy_001_event_driven.yaml'],
    )
    assert isinstance(runner.cfg, AppConfig), (
        f"Expected AppConfig instance, got {type(runner.cfg)}"
    )
    assert len(runner.cfg.strategies) == 1, (
        f"Expected 1 strategy, got {len(runner.cfg.strategies)}"
    )
    assert runner.cfg.strategies[0].id == 1, (
        f"Expected id=1, got {runner.cfg.strategies[0].id}"
    )

    print("PASS: test_strategy_runner_new_api")
    return runner


# ---------------------------------------------------------------------------
# Test 8 — StrategyRunner.__init__() backward compat config_yaml kwarg
# ---------------------------------------------------------------------------
def test_strategy_runner_legacy_config_yaml():
    from elysian_core.run_strategy import StrategyRunner

    runner_legacy = StrategyRunner(config_yaml='elysian_core/config/trading_config.yaml')
    assert runner_legacy.cfg.risk.max_weight_per_asset == 0.25, (
        f"Expected 0.25, got {runner_legacy.cfg.risk.max_weight_per_asset}"
    )

    print("PASS: test_strategy_runner_legacy_config_yaml")


# ---------------------------------------------------------------------------
# Test 9 — SpotStrategy has strategy_config attribute
# ---------------------------------------------------------------------------
def test_spot_strategy_has_strategy_config():
    from elysian_core.strategy.base_strategy import SpotStrategy
    from elysian_core.core.event_bus import EventBus
    from elysian_core.core.enums import Venue, AssetType

    s = SpotStrategy(
        exchanges={},
        event_bus=EventBus(),
        asset_type=AssetType.SPOT,
        venue=Venue.BINANCE,
    )
    assert hasattr(s, 'strategy_config'), "SpotStrategy missing 'strategy_config' attribute"
    assert s.strategy_config is None, (
        f"Expected None, got {s.strategy_config}"
    )

    print("PASS: test_spot_strategy_has_strategy_config")


# ---------------------------------------------------------------------------
# Test 10 — EventDrivenStrategy construction — no errors
# ---------------------------------------------------------------------------
def test_event_driven_strategy_construction():
    from elysian_core.strategy.example_weight_strategy_v2_event_driven import EventDrivenStrategy
    from elysian_core.core.event_bus import EventBus
    from elysian_core.core.enums import Venue, AssetType

    strategy = EventDrivenStrategy(
        exchanges={},
        event_bus=EventBus(),
        rebalance_interval_s=60.0,
        asset_type=AssetType.SPOT,
        venues={Venue.BINANCE},
    )
    assert hasattr(strategy, 'strategy_config'), (
        "EventDrivenStrategy missing 'strategy_config' attribute"
    )
    assert strategy.strategy_config is None, (
        f"Expected None, got {strategy.strategy_config}"
    )

    print("PASS: test_event_driven_strategy_construction")
    return strategy


# ---------------------------------------------------------------------------
# Test 11 — Manual cfg injection into strategy (simulating setup_strategy())
# ---------------------------------------------------------------------------
def test_manual_cfg_injection(strategy, runner):
    strategy.cfg = runner.cfg
    strategy.strategy_id = 1
    strategy.strategy_name = runner.cfg.strategies[0].name
    strategy.strategy_config = next(
        (s for s in runner.cfg.strategies if s.id == 1), None
    )

    assert strategy.strategy_config is not None, "strategy_config is None after injection"
    assert strategy.strategy_config.params["rebalance_interval_s"] == 60, (
        f"Expected 60, got {strategy.strategy_config.params['rebalance_interval_s']}"
    )
    assert strategy.cfg.risk.max_weight_per_asset == 0.25, (
        f"Expected 0.25, got {strategy.cfg.risk.max_weight_per_asset}"
    )
    assert strategy.strategy_config.risk_overrides["max_weight_per_asset"] == 0.20, (
        f"Expected 0.20, got {strategy.strategy_config.risk_overrides['max_weight_per_asset']}"
    )

    print("PASS: test_manual_cfg_injection")


# ---------------------------------------------------------------------------
# Test 12 — symbols_for still works from cfg
# ---------------------------------------------------------------------------
def test_symbols_for(strategy):
    symbols = strategy.cfg.symbols.symbols_for("binance", "spot")
    assert isinstance(symbols, list), (
        f"Expected list, got {type(symbols)}"
    )

    print("PASS: test_symbols_for")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Change to project root so all relative paths resolve correctly
    os.chdir(PROJECT_ROOT)

    try:
        cfg = test_load_app_config_two_tier()
        test_effective_risk_for(cfg)
        test_effective_execution_for(cfg)
        test_effective_portfolio_for(cfg)
        test_load_app_config_legacy_yaml_path()
        test_load_strategy_yaml()
        runner = test_strategy_runner_new_api()
        test_strategy_runner_legacy_config_yaml()
        test_spot_strategy_has_strategy_config()
        strategy = test_event_driven_strategy_construction()
        test_manual_cfg_injection(strategy, runner)
        test_symbols_for(strategy)
    except AssertionError as exc:
        print(f"\nASSERTION ERROR: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\nUNEXPECTED ERROR: {exc}")
        traceback.print_exc()
        sys.exit(1)

    print("\nALL TESTS PASSED")
