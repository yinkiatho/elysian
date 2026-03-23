"""
Typed configuration for the Elysian trading system.

Merges three config sources into a single :class:`AppConfig` object:

- ``.env``         — secrets (API keys, DB credentials)
- ``config.yaml``  — parameters (risk limits, execution, portfolio, strategy)
- ``config.json``  — large repetitive data (venue symbols, token lists)

YAML sections that are well-defined (risk) get typed dataclasses.
All other YAML sections (portfolio, execution, strategy, etc.) are loaded as
:class:`DictConfig` — a dict with recursive dot-access so that any number of
arbitrary parameters work without changing this file.

Placeholders in the YAML (``{some_key}``) are resolved via
:func:`replace_placeholders` before building the config.

Usage::

    from elysian_core.config.app_config import load_app_config

    cfg = load_app_config()
    cfg.risk.max_weight_per_asset              # 0.25  (typed RiskConfig)
    cfg.portfolio.max_history                   # 10000 (DictConfig dot-access)
    cfg.execution.default_order_type            # "MARKET"
    cfg.strategy.max_heavy_workers              # 4
    cfg.strategy.my_custom_param                # anything you add to YAML
    cfg.symbols.symbols_for("binance", "spot")  # ["SUIUSDC", ...]
    cfg.secrets.binance.api_key                 # from .env
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

    # Any extra YAML top-level sections not captured above
    extra: DictConfig = field(default_factory=DictConfig)


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


def load_app_config(
    yaml_path: str = "elysian_core/config/config.yaml",
    json_path: str = "elysian_core/config/config.json",
    env_path: str = ".env",
    placeholders: Optional[Dict[str, str]] = None,
) -> AppConfig:
    """Load all three config sources and return a unified :class:`AppConfig`.

    Parameters
    ----------
    yaml_path:
        Path to the YAML parameters file.
    json_path:
        Path to the JSON symbols/venues file.
    env_path:
        Path to the ``.env`` secrets file.
    placeholders:
        Optional dict of ``{key: value}`` replacements applied to the
        YAML before parsing sections. Useful for injecting runtime values
        (e.g. run ID, timestamp) into config strings.
    """
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

    # ── 2. Load YAML ─────────────────────────────────────────────────────
    with open(yaml_path, "r", encoding="utf-8") as f:
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

    # Collect any YAML sections not explicitly handled above
    _KNOWN_KEYS = {
        "version_name", "strategy_id", "strategy_name",
        "spot", "futures", "pools",
        "risk", "portfolio", "execution", "strategy",
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
        extra=extra,
    )
