"""Loads strategy/risk settings and refuses anything that could reach a live account."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Strategy:
    version: str
    symbols: list[str]
    timeframe_minutes: int
    breakout_lookback: int
    ema_fast: int
    ema_slow: int
    atr_period: int
    history_bars: int
    min_contiguous_bars: int
    stop_atr_mult: float
    target_atr_mult: float
    time_exit_bars: int


@dataclass(frozen=True)
class Risk:
    live_trading: bool
    execution_mode: str
    allocation_usd: float
    max_position_pct: float
    loss_budget_pct: float
    daily_loss_halt_pct: float
    max_open_positions: int
    quote_max_age_seconds: float
    quote_close_max_deviation_pct: float
    candidate_ttl_minutes: float
    fee_rate_bps: float
    entry_slippage_bps: float
    intent_ttl_seconds: float
    min_notional_usd: float


def _load_env_file(path: Path) -> None:
    """Minimal .env loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def load_strategy(path: Path | None = None) -> Strategy:
    data = yaml.safe_load((path or ROOT / "config/strategy.yaml").read_text(encoding="utf-8"))
    return Strategy(**data)


def load_risk(path: Path | None = None) -> Risk:
    data = yaml.safe_load((path or ROOT / "config/risk.yaml").read_text(encoding="utf-8"))
    risk = Risk(**data)
    if risk.live_trading:
        raise ConfigError("live_trading is true in risk.yaml — this build is paper-only. Refusing to start.")
    if risk.execution_mode not in ("shadow", "paper"):
        raise ConfigError("execution_mode must be 'shadow' or 'paper'")
    if risk.max_open_positions != 1:
        raise ConfigError("v1 supports exactly one open position.")
    return risk


def db_path() -> Path:
    _load_env_file(ROOT / ".env")
    p = Path(os.environ.get("TERMINAL_DB", "data/terminal.db"))
    return p if p.is_absolute() else ROOT / p


def broker_kind() -> str:
    _load_env_file(ROOT / ".env")
    return os.environ.get("BROKER", "alpaca")
