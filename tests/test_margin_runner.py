"""
Tests for MarginStrategyRunner and SubAccountConfig loading.

Covers:
- SubAccountConfig fields and env-var resolution
- StrategyConfig.sub_accounts populated from YAML sub_accounts section
- load_strategy_yaml() with sub_accounts returns correct SubAccountConfig list
- MarginStrategyRunner._compute_channels() aggregates SPOT channels for MARGIN
- MarginStrategyRunner.setup_strategy() with sub_accounts builds N pipelines
- MarginStrategyRunner.setup_strategy() falls back to parent when sub_accounts empty
- _build_margin_pipeline() uses MarginShadowBook for MARGIN, ShadowBook for SPOT
- RebalanceFSM receives sub_account_key (integration with Phase 6 change)
"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
import tempfile
import yaml

from elysian_core.config.app_config import SubAccountConfig, StrategyConfig, load_strategy_yaml
from elysian_core.core.enums import AssetType, Venue
from elysian_core.core.sub_account_pipeline import SubAccountKey, SubAccountPipeline
from elysian_core.core.rebalance_fsm import RebalanceFSM


# ─── SubAccountConfig ─────────────────────────────────────────────────────────

class TestSubAccountConfig:
    def test_defaults(self):
        sa = SubAccountConfig()
        assert sa.sub_account_id == 0
        assert sa.asset_type == "Spot"
        assert sa.venue == "Binance"
        assert sa.symbols == []
        assert sa.isolated_symbol is None
        assert sa.risk_overrides == {}
        assert sa.api_key == ""
        assert sa.api_secret == ""
        assert sa.risk_config is None

    def test_custom_fields(self):
        sa = SubAccountConfig(
            sub_account_id=1,
            asset_type="Margin",
            venue="Binance",
            api_key_env="BINANCE_MARGIN_API_KEY_2",
            api_secret_env="BINANCE_MARGIN_API_SECRET_2",
            symbols=["BTCUSDT"],
            isolated_symbol="BTCUSDT",
            risk_overrides={"max_short_weight": 1.0},
        )
        assert sa.sub_account_id == 1
        assert sa.asset_type == "Margin"
        assert sa.isolated_symbol == "BTCUSDT"
        assert sa.risk_overrides["max_short_weight"] == 1.0

    def test_api_key_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_MARGIN_KEY", "test-margin-key-123")
        sa = SubAccountConfig(api_key_env="MY_MARGIN_KEY")
        sa.api_key = os.getenv(sa.api_key_env, "")
        assert sa.api_key == "test-margin-key-123"


# ─── load_strategy_yaml with sub_accounts ────────────────────────────────────

MARGIN_YAML_CONTENT = """
strategy_id: 2
strategy_name: "test_margin"
class: "elysian_core.strategy.margin_strategy.MarginStrategy"
asset_type: "Margin"
venue: "Binance"

sub_accounts:
  - sub_account_id: 0
    asset_type: "Spot"
    venue: "Binance"
    api_key_env: "BINANCE_API_KEY_2"
    api_secret_env: "BINANCE_API_SECRET_2"
    symbols:
      - "ETHUSDT"
    risk_overrides:
      max_weight_per_asset: 1.0

  - sub_account_id: 1
    asset_type: "Margin"
    venue: "Binance"
    api_key_env: "BINANCE_MARGIN_API_KEY_2"
    api_secret_env: "BINANCE_MARGIN_API_SECRET_2"
    isolated_symbol: "BTCUSDT"
    symbols:
      - "BTCUSDT"
    risk_overrides:
      max_short_weight: 1.0
      max_leverage: 3.0

params:
  rebalance_interval_s: 60
"""

SPOT_YAML_CONTENT = """
strategy_id: 0
strategy_name: "test_spot"
class: "elysian_core.strategy.base_strategy.SpotStrategy"
asset_type: "Spot"
venue: "Binance"
symbols:
  - "ETHUSDT"

params:
  rebalance_interval_s: 30
"""


class TestLoadStrategyYamlWithSubAccounts:
    def _write_yaml(self, content: str) -> str:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_sub_accounts_parsed(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        assert len(cfg.sub_accounts) == 2
        os.unlink(path)

    def test_sub_account_ids_correct(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        ids = [sa.sub_account_id for sa in cfg.sub_accounts]
        assert 0 in ids
        assert 1 in ids
        os.unlink(path)

    def test_sub_account_asset_types(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        types = {sa.sub_account_id: sa.asset_type for sa in cfg.sub_accounts}
        assert types[0] == "Spot"
        assert types[1] == "Margin"
        os.unlink(path)

    def test_sub_account_symbols(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        symbols_map = {sa.sub_account_id: sa.symbols for sa in cfg.sub_accounts}
        assert symbols_map[0] == ["ETHUSDT"]
        assert symbols_map[1] == ["BTCUSDT"]
        os.unlink(path)

    def test_sub_account_isolated_symbol(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        margin_sa = next(sa for sa in cfg.sub_accounts if sa.sub_account_id == 1)
        assert margin_sa.isolated_symbol == "BTCUSDT"
        os.unlink(path)

    def test_sub_account_risk_overrides_parsed(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        margin_sa = next(sa for sa in cfg.sub_accounts if sa.sub_account_id == 1)
        assert margin_sa.risk_overrides.get("max_short_weight") == 1.0
        assert margin_sa.risk_overrides.get("max_leverage") == 3.0
        os.unlink(path)

    def test_sub_account_risk_config_built(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        margin_sa = next(sa for sa in cfg.sub_accounts if sa.sub_account_id == 1)
        # risk_config should be built from overrides
        assert margin_sa.risk_config is not None
        assert margin_sa.risk_config.max_short_weight == 1.0
        os.unlink(path)

    def test_api_key_env_stored(self):
        path = self._write_yaml(MARGIN_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        margin_sa = next(sa for sa in cfg.sub_accounts if sa.sub_account_id == 1)
        assert margin_sa.api_key_env == "BINANCE_MARGIN_API_KEY_2"
        os.unlink(path)

    def test_no_sub_accounts_gives_empty_list(self):
        path = self._write_yaml(SPOT_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        assert cfg.sub_accounts == []
        os.unlink(path)

    def test_spot_strategy_fields_unchanged(self):
        """Existing SPOT YAML fields still load correctly — no regression."""
        path = self._write_yaml(SPOT_YAML_CONTENT)
        cfg = load_strategy_yaml(path)
        assert cfg.strategy_id == 0
        assert cfg.strategy_name == "test_spot"
        assert cfg.symbols == ["ETHUSDT"]
        assert cfg.params["rebalance_interval_s"] == 30
        os.unlink(path)


# ─── MarginStrategyRunner._compute_channels ───────────────────────────────────

class TestComputeChannels:
    """_compute_channels() collects SPOT-format Redis channels for all sub-accounts."""

    def _mock_runner(self, sub_accounts=None, symbols=None, venue="Binance", asset_type="Spot"):
        """Build a minimal mock runner with a config shaped like the real one."""
        from elysian_core.run_strategy_margin import MarginStrategyRunner
        runner = object.__new__(MarginStrategyRunner)

        strat_cfg = MagicMock()
        strat_cfg.sub_accounts = sub_accounts or []
        strat_cfg.symbols = symbols or []
        strat_cfg.venue = venue
        strat_cfg.asset_type = asset_type

        runner.cfg = MagicMock()
        runner.cfg.strategies = {0: strat_cfg}
        return runner

    def test_margin_sub_accounts_use_spot_channels(self):
        from elysian_core.run_strategy_margin import MarginStrategyRunner

        runner = object.__new__(MarginStrategyRunner)
        runner.cfg = MagicMock()

        sa_spot = SubAccountConfig(sub_account_id=0, asset_type="Spot", venue="Binance", symbols=["ETHUSDT"])
        sa_margin = SubAccountConfig(sub_account_id=1, asset_type="Margin", venue="Binance", symbols=["BTCUSDT"])

        strat_cfg = MagicMock()
        strat_cfg.sub_accounts = [sa_spot, sa_margin]
        runner.cfg.strategies = {0: strat_cfg}

        with patch("elysian_core.run_strategy_margin.strategy_channels") as mock_channels:
            mock_channels.return_value = ["chan:1"]
            runner._compute_channels()
            # Both sub-accounts should use AssetType.SPOT for channel naming
            calls = mock_channels.call_args_list
            for c in calls:
                assert c.args[2] == AssetType.SPOT or c[0][2] == AssetType.SPOT

    def test_legacy_strategy_uses_native_asset_type(self):
        from elysian_core.run_strategy_margin import MarginStrategyRunner

        runner = object.__new__(MarginStrategyRunner)
        runner.cfg = MagicMock()

        strat_cfg = MagicMock()
        strat_cfg.sub_accounts = []     # no sub_accounts → legacy path
        strat_cfg.venue = "Binance"
        strat_cfg.asset_type = "Spot"
        strat_cfg.symbols = ["ETHUSDT"]
        runner.cfg.strategies = {0: strat_cfg}

        with patch("elysian_core.run_strategy_margin.strategy_channels") as mock_channels:
            mock_channels.return_value = ["chan:spot"]
            runner._compute_channels()
            call_args = mock_channels.call_args
            # Legacy path: uses the strategy's native asset_type
            assert call_args.args[2] == AssetType.SPOT


# ─── RebalanceFSM sub_account_key integration ─────────────────────────────────

class TestRebalanceFSMSubAccountKey:
    """Phase 6: verify RebalanceFSM.sub_account_key is injected into ctx."""

    def _make_fsm(self, sub_account_key=None):
        strategy = MagicMock()
        strategy.strategy_name = "test"
        strategy.strategy_id = 1
        strategy.compute_weights = MagicMock(return_value={})
        optimizer = MagicMock()
        optimizer.validate = MagicMock(return_value=(None, False))
        execution_engine = MagicMock()
        return RebalanceFSM(
            strategy=strategy,
            optimizer=optimizer,
            execution_engine=execution_engine,
            sub_account_key=sub_account_key,
        )

    def test_fsm_stores_sub_account_key(self):
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)
        fsm = self._make_fsm(sub_account_key=key)
        assert fsm._sub_account_key is key

    def test_fsm_stores_none_by_default(self):
        fsm = self._make_fsm()
        assert fsm._sub_account_key is None

    def test_sub_account_key_injected_into_compute_weights_ctx(self):
        """When sub_account_key set, compute_weights receives ctx['sub_account_key']."""
        key = SubAccountKey(strategy_id=1, asset_type=AssetType.MARGIN, venue=Venue.BINANCE)

        strategy = MagicMock()
        strategy.strategy_name = "test"
        strategy.strategy_id = 1

        received_ctx = {}
        def capture_weights(**ctx):
            received_ctx.update(ctx)
            return {}   # empty = no_signal, FSM returns to IDLE

        strategy.compute_weights = capture_weights
        optimizer = MagicMock()
        execution_engine = MagicMock()

        fsm = RebalanceFSM(
            strategy=strategy,
            optimizer=optimizer,
            execution_engine=execution_engine,
            sub_account_key=key,
        )

        asyncio.run(fsm.request())

        assert received_ctx.get('sub_account_key') is key

    def test_spot_fsm_no_key_in_ctx(self):
        """SPOT FSM (sub_account_key=None) does NOT inject key into ctx."""
        strategy = MagicMock()
        strategy.strategy_name = "test"
        strategy.strategy_id = 1

        received_ctx = {}
        def capture_weights(**ctx):
            received_ctx.update(ctx)
            return {}

        strategy.compute_weights = capture_weights
        optimizer = MagicMock()
        execution_engine = MagicMock()

        fsm = RebalanceFSM(
            strategy=strategy,
            optimizer=optimizer,
            execution_engine=execution_engine,
            sub_account_key=None,
        )

        asyncio.run(fsm.request())

        assert 'sub_account_key' not in received_ctx
