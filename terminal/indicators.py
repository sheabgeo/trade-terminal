"""Indicator math. The backtest (stage 7) must import these same functions."""
from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> float:
    """EMA seeded with the SMA of the first `period` values, alpha = 2/(period+1)."""
    if len(values) < period:
        raise ValueError(f"need {period} values for EMA, got {len(values)}")
    alpha = 2.0 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = alpha * v + (1 - alpha) * out
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> float:
    """Wilder ATR. True range needs the previous close, so the first bar is skipped."""
    n = len(closes)
    if n < period + 1:
        raise ValueError(f"need {period + 1} bars for ATR, got {n}")
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    out = sum(trs[:period]) / period
    for tr in trs[period:]:
        out = (out * (period - 1) + tr) / period
    return out


def prior_high(highs: Sequence[float], lookback: int) -> float:
    """Max high of the `lookback` bars BEFORE the last one (the signal bar is excluded)."""
    if len(highs) < lookback + 1:
        raise ValueError(f"need {lookback + 1} bars for prior high, got {len(highs)}")
    return max(highs[-(lookback + 1):-1])


def breakout_signal(bars: Sequence[dict], lookback: int, fast: int, slow: int, atr_period: int) -> dict:
    """bars: completed bars only, oldest first. Last bar is the signal bar."""
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    close = closes[-1]
    ema_f = ema(closes, fast)
    ema_s = ema(closes, slow)
    hi20 = prior_high(highs, lookback)
    a = atr(highs, lows, closes, atr_period)
    checks = {
        "close_above_prior_high": close > hi20,
        "close_above_ema_slow": close > ema_s,
        "ema_fast_above_slow": ema_f > ema_s,
    }
    return {
        "close": close,
        f"ema_{fast}": ema_f,
        f"ema_{slow}": ema_s,
        f"prior_{lookback}_high": hi20,
        f"atr_{atr_period}": a,
        "checks": checks,
        "signal": all(checks.values()),
    }
