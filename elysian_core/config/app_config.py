"""
Typed configuration for the Elysian trading system.

Supports a two-tier config layout:

  trading_config.yaml          — system-level: risk, portfolio, execution, venue configs
  strategies/strategy_N.yaml   — per-strategy: id, class, venue, allocation, symbols, overrides

Legacy single-file layout (config.yaml) is still supported via the ``yaml_path`` kwarg.

Merges three config sources into a single :class:`AppConfig` object:

- ``.env``                    — secrets (API keys, DB credentials)
- ``trading_config.yaml``     — system parameters (risk limits, execution, portfolio, strategy)
- ``strategies/*.yaml``       — per-strategy parameters (one file per strategy)
- ``config.json``             — large repetitive data (venue symbols, token lists)

Priority for risk/execution overrides (highest wins):
  strategy risk_overrides > venue_configs["{asset_type}_{venue}"] > global risk

YAML sections that are well-defined (risk) get typed dataclasses.
All other YAML sections (portfolio, execution, strategy, etc.) are loaded as
:class:`DictConfig` — a dict with recursive dot-access so that any number of
arbitrary parameters work without changing this file.

Usage::

    from elysian_core.config.app_config import load_app_config

    cfg = load_app_config(
        trading_config_yaml="elysian_core/config/trading_config.yaml",
        strategy_config_yamls=["elysian_core/config/strategies/strategy_001_event_driven.yaml"],
    )
    cfg.risk.max_weight_per_asset              # 0.25  (typed RiskConfig)
    cfg.portfolio.max_history                   # 10000 (DictConfig dot-access)
    cfg.execution.default_order_type            # "MARKET"
    cfg.strategy.max_heavy_workers              # 4
    cfg.symbols.symbols_for("binance", "spot")  # ["SUIUSDC", ...]
    cfg.secrets.binance.api_key                 # from .env
    cfg.effective_risk_for("spot", "binance")   # merged RiskConfig for Binance Spot
    cfg.strategies[0].params["rebalance_interval_s"]  # 60
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from elysian_core.risk.risk_config import RiskConfig
from elysian_core.utils.utils import replace_placeholders


# ── DictConfig — flexible dot-access config ──────────────────────────────────

class DictConfig(dict):
    """Dict subclass with recursive dot-access for nested YAML sections.

    Supports arbitrary keys — no schema changes needed when new params
    are added to the YAML::

        cfg.strategy.my_new_param        # just add it to config.yaml
        cfg.strategy.get("optional", 42) # dict.get still works
    """

    def __getattr__(self, key: str) -> Any:
        try:
            val = self[key]
        except KeyError:
            raise AttributeError(
                f"Config section has no key '{key}'"
            )
        if isinstance(val, dict) and not isinstance(val, DictConfig):
            return DictConfig(val)
        return val

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def __delattr__(self, key: str) -> None:
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key)


# ── Secrets (from .env) ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExchangeSecrets:
    api_key: str = ""
    api_secret: str = ""


@dataclass(frozen=True)
class SecretsConfig:
    binance: ExchangeSecrets = field(default_factory=ExchangeSecrets)
    aster: ExchangeSecrets = field(default_factory=ExchangeSecrets)
    binance_wallet_address: str = ""
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_database: str = ""
    redis_host: str = "localhost"
    redis_port: int = 6379


# ── Venue symbols (from config.json) ────────────────────────────────────────

@dataclass(frozen=True)
class VenueSymbols:
    spot: List[str] = field(default_factory=list)
    futures: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SymbolsConfig:
    venues: Dict[str, VenueSymbols] = field(default_factory=dict)
    spot_tokens: List[str] = field(default_factory=list)
    futures_tokens: List[str] = field(default_factory=list)
    package_targets: List[str] = field(default_factory=list)
    pools: List[str] = field(default_factory=list)

    def symbols_for(self, venue: str, market: str = "spot") -> List[str]:
        """Get symbol list for a venue and market type.

        ``venue`` is case-insensitive (e.g. ``"Binance"`` or ``"binance"``).
        ``market`` is ``"spot"`` or ``"futures"``.
        """
        v = self.venues.get(venue.lower())
        if v is None:
            return []
        return getattr(v, market, [])

    @property
    def all_spot_pairs(self) -> List[str]:
        out = []
        for v in self.venues.values():
            out.extend(v.spot)
        return out

    @property
    def all_futures_pairs(self) -> List[str]:
        out = []
        for v in self.venues.values():
            out.extend(v.futures)
        return out


# ── Meta (top-level YAML scalars) ───────────────────────────────────────────

@dataclass
class MetaConfig:
    version_name: str = ""
    strategy_id: int = 0
    strategy_name: str = ""
    spot_venues: List[str] = field(default_factory=list)
    futures_venues: List[str] = field(default_factory=list)


# ── Per-strategy config ────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    """Typed config for a single strategy instance.

    Loaded from an individual strategy YAML file or from the ``strategies``
    list in a monolithic config.yaml.  Strategy YAML example::

        id: 1
        name: "event_driven_momentum_binance_spot"
        class: "EventDrivenStrategy"
        asset_type: "Spot"
        venue: "Binance"
        venues: ["Binance"]
        allocation: 1.0
        symbols: []            # empty = load from config.json
        risk_overrides:
          max_weight_per_asset: 0.20
        execution_overrides: {}
        portfolio_overrides: {}
        params:
          rebalance_interval_s: 60
    """
    id: int = 0
    name: str = ""
    class_name: str = ""
    asset_type: str = "Spot"
    venue: str = "Binance"
    venues: List[str] = field(default_factory=lambda: ["Binance"])
    allocation: float = 1.0
    symbols: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    # Per-strategy config overrides (applied on top of venue + global defaults)
    risk_overrides: Dict[str, Any] = field(default_factory=dict)
    execution_overrides: Dict[str, Any] = field(default_factory=dict)
    portfolio_overrides: Dict[str, Any] = field(default_factory=dict)


# ── Per-(asset_type, venue) config overrides ─────────────────────────────────

@dataclass
class VenueConfig:
    """Config overrides scoped to a specific (asset_type, venue) pair.

    Key format in trading_config.yaml: ``"{asset_type}_{venue}"`` (lowercase),
    e.g. ``"spot_binance"``, ``"perpetual_binance"``, ``"spot_aster"``.

    Fields here override the global risk/execution defaults for that pairing.
    Strategy ``risk_overrides`` still take priority over these.
    """
    risk_overrides: Dict[str, Any] = field(default_factory=dict)
    execution_overrides: Dict[str, Any] = field(default_factory=dict)
    portfolio_overrides: Dict[str, Any] = field(default_factory=dict)


# ── Top-level composite ─────────────────────────────────────────────────────

@dataclass
class AppConfig:
    meta: MetaConfig = field(default_factory=MetaConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    portfolio: DictConfig = field(default_factory=DictConfig)
    execution: DictConfig = field(default_factory=DictConfig)
    strategy: DictConfig = field(default_factory=DictConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    symbols: SymbolsConfig = field(default_factory=SymbolsConfig)
    strategies: List[StrategyConfig] = field(default_factory=list)
    
    # Per-(asset_type, venue) config overrides from venue_configs YAML section
    venue_configs: Dict[str, VenueConfig] = field(default_factory=dict)

    # Any extra YAML top-level sections not captured above
    extra: DictConfig = field(default_factory=DictConfig)

    def effective_risk_for(
        self,
        asset_type: Optional[str] = None,
        venue: Optional[str] = None,
        strategy_id: Optional[int] = None,
    ) -> RiskConfig:
        """Return a merged :class:`RiskConfig` for the given context.

        Priority (highest wins):
          1. Strategy ``risk_overrides`` (if ``strategy_id`` is provided)
          2. Per-(asset_type, venue) overrides from ``venue_configs``
          3. Global ``self.risk``
        """
        # Flatten global risk to a plain dict
        base: Dict[str, Any] = {
            f: getattr(self.risk, f)
            for f in self.risk.__dataclass_fields__
        }
        # Apply venue-level overrides
        if asset_type and venue:
            venue_key = f"{asset_type.lower()}_{venue.lower()}"
            vc = self.venue_configs.get(venue_key)
            if vc and vc.risk_overrides:
                base.update({k: v for k, v in vc.risk_overrides.items() if v is not None})
        # Apply strategy-level overrides
        if strategy_id is not None:
            sc = next((s for s in self.strategies if s.id == strategy_id), None)
            if sc and sc.risk_overrides:
                base.update({k: v for k, v in sc.risk_overrides.items() if v is not None})
        return _build_risk_config(base)

    def effective_execution_for(
        self,
        asset_type: Optional[str] = None,
        venue: Optional[str] = None,
        strategy_id: Optional[int] = None,
    ) -> DictConfig:
        """Return a merged execution :class:`DictConfig` for the given context.

        Priority (highest wins):
          1. Strategy ``execution_overrides``
          2. Per-(asset_type, venue) overrides from ``venue_configs``
          3. Global ``self.execution``
        """
        base = dict(self.execution)
        if asset_type and venue:
            venue_key = f"{asset_type.lower()}_{venue.lower()}"
            vc = self.venue_configs.get(venue_key)
            if vc and vc.execution_overrides:
                base.update(vc.execution_overrides)
        if strategy_id is not None:
            sc = next((s for s in self.strategies if s.id == strategy_id), None)
            if sc and sc.execution_overrides:
                base.update(sc.execution_overrides)
        return DictConfig(base)

    def effective_portfolio_for(
        self,
        asset_type: Optional[str] = None,
        venue: Optional[str] = None,
        strategy_id: Optional[int] = None,
    ) -> DictConfig:
        """Return a merged portfolio :class:`DictConfig` for the given context."""
        base = dict(self.portfolio)
        if asset_type and venue:
            venue_key = f"{asset_type.lower()}_{venue.lower()}"
            vc = self.venue_configs.get(venue_key)
            if vc and vc.portfolio_overrides:
                base.update(vc.portfolio_overrides)
        if strategy_id is not None:
            sc = next((s for s in self.strategies if s.id == strategy_id), None)
            if sc and sc.portfolio_overrides:
                base.update(sc.portfolio_overrides)
        return DictConfig(base)


# ── Factory ──────────────────────────────────────────────────────────────────

def _build_risk_config(risk_section: dict) -> RiskConfig:
    """Build a RiskConfig from the YAML risk dict, handling type conversions."""
    kwargs = {}

    _SCALAR_FIELDS = {
        "max_weight_per_asset", "min_weight_per_asset",
        "max_total_exposure", "min_cash_weight",
        "max_turnover_per_rebalance", "max_leverage",
        "max_short_weight", "min_order_notional",
        "max_order_notional", "min_rebalance_interval_ms",
        "min_weight_delta",
    }
    for field_name in _SCALAR_FIELDS:
        val = risk_section.get(field_name)
        if val is not None:
            kwargs[field_name] = val

    # YAML list/null → frozenset
    allowed = risk_section.get("allowed_symbols")
    if allowed is not None and isinstance(allowed, list):
        kwargs["allowed_symbols"] = frozenset(allowed)

    blocked = risk_section.get("blocked_symbols")
    if blocked is not None and isinstance(blocked, list) and blocked:
        kwargs["blocked_symbols"] = frozenset(blocked)

    return RiskConfig(**kwargs)


def load_strategy_yaml(yaml_path: str) -> StrategyConfig:
    """Load a single strategy config from a YAML file.

    The file should follow the schema documented in :class:`StrategyConfig`.
    """
    with open(yaml_path, "r", encoding="utf-8") as f:
        s = yaml.safe_load(f) or {}
    return StrategyConfig(
        id=s.get("id", 0),
        name=s.get("name", ""),
        class_name=s.get("class", ""),
        asset_type=s.get("asset_type", "Spot"),
        venue=s.get("venue", "Binance"),
        venues=s.get("venues", [s.get("venue", "Binance")]),
        allocation=float(s.get("allocation", 1.0)),
        symbols=s.get("symbols", []) or [],
        params=s.get("params", {}) or {},
        risk_overrides=s.get("risk_overrides", {}) or {},
        execution_overrides=s.get("execution_overrides", {}) or {},
        portfolio_overrides=s.get("portfolio_overrides", {}) or {},
    )


def load_app_config(
    trading_config_yaml: str = "elysian_core/config/trading_config.yaml",
    strategy_config_yamls: Optional[List[str]] = None,
    json_path: str = "elysian_core/config/config.json",
    env_path: str = ".env",
    placeholders: Optional[Dict[str, str]] = None,
    # Backward-compat alias — takes precedence over trading_config_yaml if provided
    yaml_path: Optional[str] = None,
) -> AppConfig:
    """Load all config sources and return a unified :class:`AppConfig`.

    Two-tier layout (preferred)::

        load_app_config(
            trading_config_yaml="elysian_core/config/trading_config.yaml",
            strategy_config_yamls=["elysian_core/config/strategies/strategy_001_event_driven.yaml"],
        )

    Legacy single-file layout (backward compat)::

        load_app_config(yaml_path="elysian_core/config/config.yaml")

    Parameters
    ----------
    trading_config_yaml:
        Path to the system-level YAML (risk, portfolio, execution, venue_configs).
    strategy_config_yamls:
        List of per-strategy YAML file paths.  Each file is parsed into a
        :class:`StrategyConfig` and appended to ``AppConfig.strategies``.
    json_path:
        Path to the JSON symbols/venues file.
    env_path:
        Path to the ``.env`` secrets file.
    placeholders:
        Optional ``{key: value}`` replacements applied to the YAML before parsing.
    yaml_path:
        Backward-compat alias for ``trading_config_yaml``.
    """
    if yaml_path is not None:
        trading_config_yaml = yaml_path
    # ── 1. Load .env ─────────────────────────────────────────────────────
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()

    secrets = SecretsConfig(
        binance=ExchangeSecrets(
            api_key=os.getenv("BINANCE_API_KEY", ""),
            api_secret=os.getenv("BINANCE_API_SECRET", ""),
        ),
        aster=ExchangeSecrets(
            api_key=os.getenv("ASTER_API_KEY", ""),
            api_secret=os.getenv("ASTER_API_SECRET", ""),
        ),
        binance_wallet_address=os.getenv("BINANCE_WALLET_ADDRESS", ""),
        postgres_user=os.getenv("POSTGRES_USER", ""),
        postgres_password=os.getenv("POSTGRES_PASSWORD", ""),
        postgres_host=os.getenv("POSTGRES_HOST", "localhost"),
        postgres_port=int(os.getenv("POSTGRES_PORT", "5432")),
        postgres_database=os.getenv("POSTGRES_DATABASE", ""),
        redis_host=os.getenv("REDIS_HOST", "localhost"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
    )

    # ── 2. Load trading config YAML ───────────────────────────────────────
    with open(trading_config_yaml, "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}

    # Apply placeholder substitutions if provided
    if placeholders:
        y = replace_placeholders(y, placeholders)

    # Typed: meta
    meta = MetaConfig(
        version_name=y.get("version_name", ""),
        strategy_id=y.get("strategy_id", 0),
        strategy_name=y.get("strategy_name", ""),
        spot_venues=y.get("spot", {}).get("venues", []) or [],
        futures_venues=y.get("futures", {}).get("venues", []) or [],
    )

    # Typed: risk (well-defined constraint set)
    risk = _build_risk_config(y.get("risk", {}))

    # Flexible: portfolio, execution, strategy — DictConfig for arbitrary params
    portfolio = DictConfig(y.get("portfolio", {}))
    execution = DictConfig(y.get("execution", {}))
    strategy_cfg = DictConfig(y.get("strategy", {}))

    # Parse per-(asset_type, venue) config overrides
    venue_configs: Dict[str, VenueConfig] = {}
    for key, vc_data in (y.get("venue_configs", {}) or {}).items():
        if isinstance(vc_data, dict):
            venue_configs[key] = VenueConfig(
                risk_overrides=vc_data.get("risk", {}) or {},
                execution_overrides=vc_data.get("execution", {}) or {},
                portfolio_overrides=vc_data.get("portfolio", {}) or {},
            )

    # Typed: strategies list from inline YAML (legacy single-file layout)
    strategies_list: List[StrategyConfig] = []
    for s in y.get("strategies", []) or []:
        strategies_list.append(StrategyConfig(
            id=s.get("id", 0),
            name=s.get("name", ""),
            class_name=s.get("class", ""),
            asset_type=s.get("asset_type", "Spot"),
            venue=s.get("venue", "Binance"),
            venues=s.get("venues", [s.get("venue", "Binance")]),
            allocation=float(s.get("allocation", 1.0)),
            symbols=s.get("symbols", []) or [],
            params=s.get("params", {}) or {},
            risk_overrides=s.get("risk_overrides", {}) or {},
            execution_overrides=s.get("execution_overrides", {}) or {},
            portfolio_overrides=s.get("portfolio_overrides", {}) or {},
        ))

    # Load additional per-strategy YAML files (two-tier layout)
    for strat_yaml_path in (strategy_config_yamls or []):
        strategies_list.append(load_strategy_yaml(strat_yaml_path))

    # Collect any YAML sections not explicitly handled above
    _KNOWN_KEYS = {
        "version_name", "strategy_id", "strategy_name",
        "spot", "futures", "pools",
        "risk", "portfolio", "execution", "strategy", "strategies",
        "venue_configs",
    }
    extra = DictConfig({k: v for k, v in y.items() if k not in _KNOWN_KEYS})

    # ── 3. Load JSON ─────────────────────────────────────────────────────
    with open(json_path, "r", encoding="utf-8") as f:
        j = json.load(f)

    venues_raw = j.get("venues", {})
    venue_symbols = {
        name: VenueSymbols(
            spot=v.get("spot", []),
            futures=v.get("futures", []),
        )
        for name, v in venues_raw.items()
    }

    tokens_raw = j.get("tokens", {})
    symbols = SymbolsConfig(
        venues=venue_symbols,
        spot_tokens=tokens_raw.get("spot", []),
        futures_tokens=tokens_raw.get("futures", []),
        package_targets=j.get("package_targets", []),
        pools=j.get("pools", []),
    )

    return AppConfig(
        meta=meta,
        risk=risk,
        portfolio=portfolio,
        execution=execution,
        strategy=strategy_cfg,
        secrets=secrets,
        symbols=symbols,
        strategies=strategies_list,
        venue_configs=venue_configs,
        extra=extra,
    )
