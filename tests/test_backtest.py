from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from terminal import config
from terminal.backtest import Costs, Params, Series, buy_and_hold, compute_signals, simulate, split_bounds
from terminal.broker.fake import uptrend_breakout_bars

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)
RISK = config.load_risk()
STRAT = config.load_strategy()
ZERO = Costs(fee_bps=0, spread_bps={"BTC/USD": 0, "ETH/USD": 0}, slippage_bps=0)
REAL = Costs(fee_bps=25, spread_bps={"BTC/USD": 4, "ETH/USD": 4}, slippage_bps=3)


def flat(sym="BTC/USD", n=60, px=100.0):
    ts = [T0 + k * H for k in range(n)]
    return Series(sym, ts, [px] * n, [px + 0.1] * n, [px - 0.1] * n, [px] * n)


def sig(s, i, atr=1.0):
    return {s.ts[i] + H: atr}


def run(series, signals, costs=ZERO, params=Params(2.0, 4.0, 24), risk=None):
    risk = risk or replace(RISK, entry_slippage_bps=0, min_notional_usd=0)
    end = max(t for s in series.values() for t in s.ts) + H
    r = simulate(series, signals, params, costs, risk, T0, end)
    return r, r["_trades"]


# ---------- signals: same code, no lookahead ----------
def test_signals_use_only_past_bars():
    end = T0 + 700 * H
    bars = uptrend_breakout_bars(end, n=700)
    full = Series("BTC/USD", [b.ts for b in bars], [b.open for b in bars], [b.high for b in bars],
                  [b.low for b in bars], [b.close for b in bars])
    cut = 650
    trunc = Series("BTC/USD", full.ts[:cut], full.o[:cut], full.h[:cut], full.l[:cut], full.c[:cut])
    s_full, s_trunc = compute_signals(full, STRAT), compute_signals(trunc, STRAT)
    assert {k: v for k, v in s_full.items() if k <= full.ts[cut - 1] + H} == s_trunc
    assert full.ts[-1] + H in s_full             # the engineered breakout on the last bar is detected


def test_gap_in_window_suppresses_signal():
    bars = uptrend_breakout_bars(T0 + 700 * H, n=700)
    keep = [b for k, b in enumerate(bars) if k != 600]      # one missing hour inside the 260-bar window
    s = Series("BTC/USD", [b.ts for b in keep], [b.open for b in keep], [b.high for b in keep],
               [b.low for b in keep], [b.close for b in keep])
    assert s.ts[-1] + H not in compute_signals(s, STRAT)


# ---------- execution model ----------
def test_entry_is_next_bar_open_and_target_exit():
    s = flat()
    s.o[11] = 100.0
    s.h[13], s.c[13] = 105.0, 104.0                 # target = 100 + 4*1 = 104 → hit
    r, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)})
    assert len(t) == 1 and t[0]["entry_ts"] == s.ts[11] and t[0]["reason"] == "target"
    assert t[0]["exit"] == pytest.approx(104.0)


def test_stop_checked_before_target_in_same_bar():
    s = flat()
    s.h[12], s.l[12] = 110.0, 90.0                  # both levels inside one candle
    _, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)})
    assert t[0]["reason"] == "stop" and t[0]["exit"] == pytest.approx(98.0)


def test_gap_through_stop_fills_at_open():
    s = flat()
    s.o[12], s.h[12], s.l[12], s.c[12] = 95.0, 95.5, 94.0, 95.0
    _, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)})
    assert t[0]["reason"] == "stop_gap" and t[0]["exit"] == pytest.approx(95.0)


def test_time_exit_after_24_bars():
    s = flat(n=80)
    _, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)})
    assert t[0]["reason"] == "time" and t[0]["exit_ts"] == s.ts[10] + 24 * H   # close of bar 34 = signal close + 24h
    assert t[0]["bars"] == 24


def test_ioc_cap_skips_gap_up_entry():
    s = flat()
    s.o[11] = 101.0                                  # opens 1% above signal close; cap is +0.1%
    risk = replace(RISK, entry_slippage_bps=10, min_notional_usd=0)
    _, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, risk=risk)
    assert t == []


def test_fee_math_on_one_trade():
    s = flat()
    s.h[12] = 200.0                                  # target hit next bar
    r, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, costs=REAL, risk=replace(RISK, min_notional_usd=0))
    x = t[0]
    fee = 0.0025
    expected = x["qty"] * (x["exit"] - x["entry"]) - x["qty"] * x["entry"] * fee - x["qty"] * x["exit"] * fee
    assert x["net"] == pytest.approx(expected)
    assert x["entry"] == pytest.approx(100 * (1 + 0.0002 + 0.0003))   # half spread 2 bps + slippage 3 bps


def test_costs_make_net_worse_than_gross():
    s = flat()
    s.h[12] = 200.0
    end = s.ts[-1] + H
    risk = replace(RISK, min_notional_usd=0)
    net = simulate({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, Params(2, 4, 24), REAL, risk, T0, end)
    gross = simulate({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, Params(2, 4, 24), REAL, risk, T0, end, gross=True)
    assert net["net_usd"] < gross["net_usd"]


def test_one_position_across_symbols():
    a, b = flat("BTC/USD"), flat("ETH/USD")
    _, t = run({"BTC/USD": a, "ETH/USD": b}, {"BTC/USD": sig(a, 10), "ETH/USD": {**sig(b, 10), **sig(b, 12)}})
    entries = sorted(x["entry_ts"] for x in t)
    assert all(e2 >= e1 for e1, e2 in zip(entries, entries[1:]))
    for x, y in zip(t, t[1:]):
        assert y["entry_ts"] > x["exit_ts"] or y["entry_ts"] == x["exit_ts"] + H or y["entry_ts"] >= x["exit_ts"]
    assert len({x["entry_ts"] for x in t}) == len(t)


def test_min_notional_skips_trade():
    s = flat()
    risk = replace(RISK, entry_slippage_bps=0, min_notional_usd=1_000)
    _, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, risk=risk)
    assert t == []


def test_position_never_exceeds_cap_and_loss_budget():
    s = flat()
    s.l[12] = 50.0                                     # stop hit
    _, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, costs=REAL, risk=replace(RISK, min_notional_usd=0))
    assert t[0]["notional"] <= RISK.allocation_usd * RISK.max_position_pct / 100 + 1e-6
    assert t[0]["net"] >= -RISK.allocation_usd * RISK.loss_budget_pct / 100 - 1e-6


# ---------- splits / benchmarks ----------
def test_splits_are_chronological_and_disjoint():
    tl = [T0 + k * H for k in range(1000)]
    b = split_bounds(tl, {"development": 0.6, "validation": 0.2, "final_test": 0.2})
    assert b["development"][1] == b["validation"][0] and b["validation"][1] == b["final_test"][0]
    assert b["final_test"][1] == tl[-1] + H


def test_buy_and_hold_includes_costs():
    s = flat()
    assert buy_and_hold(s, REAL, 100, T0, s.ts[-1] + H) < 0      # flat price → costs make it negative


def test_missing_spread_refuses_to_guess():
    with pytest.raises(KeyError):
        Costs(fee_bps=25, spread_bps={}, slippage_bps=3).half_spread("BTC/USD")


# ---------- higher timeframes ----------
def test_resample_4h_and_daily():
    from terminal.backtest import resample
    s = flat(n=48)
    s.h[5], s.l[6], s.c[7] = 130.0, 70.0, 111.0
    r4 = resample(s, 4)
    assert len(r4.ts) == 12 and r4.ts[1] == T0 + 4 * H
    assert r4.o[1] == s.o[4] and r4.h[1] == 130.0 and r4.l[1] == 70.0 and r4.c[1] == 111.0
    assert len(resample(s, 24).ts) == 2


def test_daily_signal_enters_after_day_close_and_time_exit_scales():
    s = flat(n=24 * 30)
    day_close = T0 + 24 * H                      # signal on day 1's completed daily bar
    params = Params(2.0, 4.0, 2, bar_hours=24)   # time exit after 2 daily bars
    risk = replace(RISK, entry_slippage_bps=0, min_notional_usd=0)
    r = simulate({"BTC/USD": s}, {"BTC/USD": {day_close: 1.0}}, params, ZERO, risk, T0, s.ts[-1] + H)
    t = r["_trades"][0]
    assert t["entry_ts"] == day_close            # first hourly bar AFTER the daily close
    assert t["exit_ts"] + H == day_close + 48 * H and t["reason"] == "time"


def test_min_notional_skips_are_counted():
    s = flat()
    risk = replace(RISK, entry_slippage_bps=0, min_notional_usd=1_000)
    r, t = run({"BTC/USD": s}, {"BTC/USD": sig(s, 10)}, risk=risk)
    assert t == [] and r["signals_skipped_by_sizing"] == {"below_min_notional": 1}
