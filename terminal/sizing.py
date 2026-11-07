"""Position sizing. Pure function, no I/O — the backtest (stage 8) uses the same code."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeResult:
    qty: float
    notional: float
    risk_per_unit: float
    planned_loss_usd: float       # if the stop is hit, incl. estimated costs
    limited_by: str               # "loss_budget" | "position_cap" | "cash"
    reject: str | None            # reason no order is allowed, else None


def floor_to(x: float, inc: float) -> float:
    if inc <= 0:
        return x
    return math.floor(x / inc + 1e-9) * inc


def size_position(
    *,
    loss_budget_usd: float,
    entry_price: float,
    stop_price: float,
    cost_per_unit: float,
    max_position_usd: float,
    cash_usd: float,
    qty_increment: float,
    min_qty: float,
    min_notional_usd: float = 0.0,
) -> SizeResult:
    """qty = loss budget / (stop distance + costs per unit), capped by position cap and cash,
    rounded DOWN to the venue increment. Below venue minimum → reject."""
    stop_distance = entry_price - stop_price
    if stop_distance <= 0:
        return SizeResult(0, 0, 0, 0, "loss_budget", "stop is not below entry")
    risk_per_unit = stop_distance + cost_per_unit
    caps = {
        "loss_budget": loss_budget_usd / risk_per_unit,
        "position_cap": max_position_usd / entry_price,
        "cash": cash_usd / entry_price,
    }
    limited_by = min(caps, key=caps.get)
    qty = floor_to(caps[limited_by], qty_increment)
    qty = round(qty, 9)
    if qty < min_qty or qty <= 0:
        return SizeResult(qty, qty * entry_price, risk_per_unit, qty * risk_per_unit, limited_by,
                          f"qty {qty:.9f} below venue minimum {min_qty} — SKIP")
    if qty * entry_price < min_notional_usd:
        return SizeResult(qty, qty * entry_price, risk_per_unit, qty * risk_per_unit, limited_by,
                          f"order value ${qty * entry_price:.2f} below venue minimum ${min_notional_usd:.2f} — SKIP")
    return SizeResult(qty, qty * entry_price, risk_per_unit, qty * risk_per_unit, limited_by, None)
