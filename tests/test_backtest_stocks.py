import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from terminal import config
from terminal.backtest import Costs, Params, Series, compute_signals, simulate
from terminal.backtest_stocks import edge_view, verdict

DAY = timedelta(days=1)
STRAT = config.load_strategy()
RISK = replace(config.load_risk(), min_notional_usd=1, entry_slippage_bps=0)
COSTS = Costs(fee_bps=0, spread_bps={"SPY": 0, "QQQ": 0}, slippage_bps=0)
NOQ = {"SPY": 0.0, "QQQ": 0.0}


def trading_days(start, n, skip=()):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += DAY
    return out


def series(sym="SPY", n=400, start=datetime(2017, 1, 2, tzinfo=timezone.utc), skip=(), breakout_at=None):
    ts = trading_days(start, n, skip)
    c = [100 * (1 + 0.001 * i) + 2 * math.sin(i / 4) for i in range(n)]
    o = [x - 0.2 for x in c]
    h = [x + 0.5 for x in c]
    lo = [x - 0.7 for x in c]
    if breakout_at is not None:
        top = max(h[breakout_at - 20:breakout_at])
        c[breakout_at], h[breakout_at] = top + 3, top + 3.5
    return Series(sym, ts, o, h, lo, c)


def test_weekends_do_not_break_contiguity_but_long_gap_does():
    s = series(breakout_at=399)
    sig = compute_signals(s, STRAT, DAY, timedelta(days=5))
    assert s.ts[399] + DAY in sig
    base = series(breakout_at=399)
    hole = set(base.ts[370:378])                                   # 8 missing sessions ≈ 12 calendar days
    s2 = series(n=400, skip=hole, breakout_at=399)                 # gap now sits inside the last 260 bars
    assert max(b - a for a, b in zip(s2.ts, s2.ts[1:])) > timedelta(days=5)
    assert s2.ts[399] + DAY not in compute_signals(s2, STRAT, DAY, timedelta(days=5))


def test_stock_time_exit_counts_trading_days_not_calendar():
    s = series(n=100)
    p = Params(50.0, 50.0, 24, 24, time_exit_exec_bars=24)      # far stop/target → time exit
    r = simulate({"SPY": s}, {"SPY": {s.ts[10] + DAY: 0.1}}, p, COSTS, RISK, s.ts[0], s.ts[-1] + DAY,
                 exec_bar=DAY, min_qty=NOQ)
    t = r["_trades"][0]
    assert t["entry_ts"] == s.ts[11] and t["reason"] == "time" and t["exit_ts"] == s.ts[11 + 23]


def test_friday_signal_enters_monday_open():
    s = series(n=60)
    fri = next(i for i, t in enumerate(s.ts) if t.weekday() == 4 and i > 10)
    r = simulate({"SPY": s}, {"SPY": {s.ts[fri] + DAY: 1.0}}, Params(2, 4, 24, 24, 24), COSTS, RISK,
                 s.ts[0], s.ts[-1] + DAY, exec_bar=DAY, min_qty=NOQ)
    assert r["_trades"][0]["entry_ts"].weekday() == 0


def test_edge_view_counts_every_signal_account_view_only_one_at_a_time():
    a, b = series("SPY", n=120), series("QQQ", n=120)
    sigs = {"SPY": {a.ts[10] + DAY: 1.0}, "QQQ": {b.ts[10] + DAY: 1.0}}   # same day, two symbols
    p = Params(2, 4, 24, 24, 24)
    ev = edge_view({"SPY": a, "QQQ": b}, sigs, p, COSTS, RISK, a.ts[0], a.ts[-1] + DAY, NOQ)
    acct = simulate({"SPY": a, "QQQ": b}, sigs, p, COSTS, RISK, a.ts[0], a.ts[-1] + DAY, exec_bar=DAY, min_qty=NOQ)
    assert ev["trades"] == 2 and acct["trades"] == 1


def test_verdict_rules():
    assert verdict({"trades": 10}) == "INCONCLUSIVE (<30 trades)"
    assert verdict({"trades": 40, "avg_net_pct_per_trade": 0.2, "win_rate_pct": 45, "breakeven_win_rate_pct": 40}) == "PASS"
    assert verdict({"trades": 40, "avg_net_pct_per_trade": -0.1, "win_rate_pct": 45, "breakeven_win_rate_pct": 40}) == "FAIL"
    assert verdict({"trades": 40, "avg_net_pct_per_trade": 0.1, "win_rate_pct": 35, "breakeven_win_rate_pct": 40}) == "FAIL"


def test_shipped_s1_config_is_as_declared():
    import yaml
    cfg = yaml.safe_load((config.ROOT / "config/backtest_stocks.yaml").read_text())
    assert len(cfg["universe"]) == 14 and cfg["costs"]["fee_bps"] == 0
    assert cfg["splits"]["development"] == ["2016-01-01", "2022-01-01"]
    assert cfg["splits"]["validation"] == ["2022-01-01", "2025-01-01"]


def test_verdict_requires_beating_baseline():
    ev = {"trades": 100, "avg_net_pct_per_trade": 0.5, "win_rate_pct": 45, "breakeven_win_rate_pct": 40,
          "_pct": [0.5 + (i % 5 - 2) for i in range(100)]}
    same = {"avg_net_pct_per_trade": 0.6, "_pct": [0.6 + (i % 5 - 2) for i in range(100)]}
    worse = {"avg_net_pct_per_trade": -0.5, "_pct": [-0.5 + (i % 5 - 2) for i in range(100)]}
    assert verdict(ev, same).startswith("FAIL (no better")
    assert verdict(ev, worse, need_t=2.0) == "PASS"
    close = {"avg_net_pct_per_trade": 0.45, "_pct": [0.45 + (i % 5 - 2) for i in range(100)]}
    assert verdict(ev, close, need_t=2.0).startswith("FAIL (beats baseline, t=")


def test_pre_cost_differs_from_net_when_spread_exists():
    s = series(n=100)
    costs = Costs(fee_bps=0, spread_bps={"SPY": 10}, slippage_bps=5)
    ev = edge_view({"SPY": s}, {"SPY": {s.ts[10] + DAY: 1.0, s.ts[40] + DAY: 1.0}}, Params(2, 4, 24, 24, 24),
                   costs, RISK, s.ts[0], s.ts[-1] + DAY, NOQ)
    assert ev["avg_pre_cost_pct_per_trade"] > ev["avg_net_pct_per_trade"]
