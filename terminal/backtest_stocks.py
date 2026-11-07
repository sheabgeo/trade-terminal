"""S1: breakout-v1 on commission-free US ETFs, daily bars (config/backtest_stocks.yaml).

Two views, declared before results:
  EDGE VIEW (decides pass/fail): every valid signal simulated on its own — no one-position rule, no daily halt —
            so the sample is large enough to judge the rule itself.
  ACCOUNT VIEW (realism): the real rulebook — $100, one position at a time, daily halt.
Same indicator + sizing code as live; signal at the daily close, entry at the next session's open,
stop checked before target inside a day, overnight gaps fill at the open, time exit after 24 trading days.
Usage:
  python -m terminal.backtest_stocks           # development + validation
  python -m terminal.backtest_stocks --final   # ALSO the untouched final period — once, after freezing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import yaml

from . import config
from .backtest import Costs, Params, Series, buy_and_hold, compute_signals, load_csv, simulate

DAY = timedelta(days=1)


def _d(x: str | None) -> datetime | None:
    return datetime.fromisoformat(x).replace(tzinfo=timezone.utc) if x else None


def _slice(s: Series, i: int, n: int) -> Series:
    return Series(s.symbol, s.ts[i:i + n], s.o[i:i + n], s.h[i:i + n], s.l[i:i + n], s.c[i:i + n])


def run_independent(series, signals, params, costs, risk, start, end, min_qty, gross=False) -> list[dict]:
    """Simulate EVERY signal in [start, end) on its own (no one-position rule). Signal keys = signal-bar close time."""
    trades = []
    horizon = (params.time_exit_exec_bars or 24) + 2
    for sym, sigs in signals.items():
        s = series[sym]
        for close_t, atr_v in sorted(sigs.items()):
            i = s.index.get(close_t - DAY)
            if i is None or not (start <= s.ts[i] < end):
                continue
            sub = _slice(s, i, horizon + 1)
            stop_at = min(end, sub.ts[-1] + DAY)
            r = simulate({sym: sub}, {sym: {close_t: atr_v}}, params, costs, risk, sub.ts[0], stop_at,
                         gross=gross, exec_bar=DAY, min_qty=min_qty)
            trades += r["_trades"]
    return trades


def baseline_signals(series: dict[str, Series], strat, every: int = 5) -> dict[str, dict[datetime, float]]:
    """'Just be long' baseline: an entry every `every` trading days on every symbol, NO breakout/trend condition.
    Stop/target use a full-history Wilder ATR (same definition as indicators.atr)."""
    out = {}
    p = strat.atr_period
    for sym, s in series.items():
        atr_v, sigs = None, {}
        for i in range(1, len(s.ts)):
            tr = max(s.h[i] - s.l[i], abs(s.h[i] - s.c[i - 1]), abs(s.l[i] - s.c[i - 1]))
            if i < p:
                atr_v = (atr_v or 0) + tr / p
                continue
            atr_v = (atr_v * (p - 1) + tr) / p
            if i >= strat.min_contiguous_bars and i % every == 0:
                sigs[s.ts[i] + DAY] = atr_v
        out[sym] = sigs
    return out


def pct_list(trades) -> list[float]:
    return [100 * t["net"] / t["notional"] for t in trades if t["notional"] > 0]


def welch_t(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((ma - mb) / se, 2) if se > 0 else None


def edge_view(series, signals, params, costs, risk, start, end, min_qty) -> dict:
    """Per-trade statistics (% of notional) for every signal, after costs, plus the same rule before costs."""
    trades = run_independent(series, signals, params, costs, risk, start, end, min_qty)
    gross = run_independent(series, signals, params, costs, risk, start, end, min_qty, gross=True)
    pct = pct_list(trades)
    n = len(pct)
    if n == 0:
        return {"trades": 0}
    mean = sum(pct) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pct) / (n - 1)) if n > 1 else 0.0
    wins = [x for x in pct if x > 0]
    losses = [-x for x in pct if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    gross_pct = pct_list(gross)
    return {
        "trades": n,
        "avg_net_pct_per_trade": round(mean, 3),
        "avg_pre_cost_pct_per_trade": round(sum(gross_pct) / len(gross_pct), 3) if gross_pct else None,
        "t_stat": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None,
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "breakeven_win_rate_pct": round(100 * al / (aw + al), 1) if wins and losses else None,
        "avg_win_pct": round(aw, 3),
        "avg_loss_pct": round(-al, 3),
        "by_symbol_avg_net_pct": {s: round(sum(100 * t["net"] / t["notional"] for t in trades if t["symbol"] == s)
                                           / max(1, sum(1 for t in trades if t["symbol"] == s)), 3)
                                  for s in sorted({t["symbol"] for t in trades})},
        "exit_reasons": {r: sum(1 for t in trades if t["reason"] == r) for r in sorted({t["reason"] for t in trades})},
        "_pct": pct,
    }


def verdict(ev: dict, base: dict | None = None, need_t: float = 0.0) -> str:
    """Amended rule (2026-11-07, before real data): positive after costs, win% > breakeven, ≥30 trades,
    AND beats the 'just be long' baseline per trade; in validation the beat must have Welch t ≥ 2."""
    if ev.get("trades", 0) < 30:
        return "INCONCLUSIVE (<30 trades)"
    ok = ev["avg_net_pct_per_trade"] > 0 and (ev["breakeven_win_rate_pct"] is None
                                               or ev["win_rate_pct"] > ev["breakeven_win_rate_pct"])
    if not ok:
        return "FAIL"
    if base is None:
        return "PASS"
    diff = ev["avg_net_pct_per_trade"] - base["avg_net_pct_per_trade"]
    t = welch_t(ev["_pct"], base["_pct"])
    if diff <= 0:
        return "FAIL (no better than just being long)"
    if need_t and (t is None or t < need_t):
        return f"FAIL (beats baseline, t={t} < {need_t})"
    return "PASS"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true", help="also evaluate the untouched final period (once, after freezing)")
    args = ap.parse_args()
    cfg = yaml.safe_load((config.ROOT / "config/backtest_stocks.yaml").read_text(encoding="utf-8"))
    strat = config.load_strategy()
    risk = replace(config.load_risk(), min_notional_usd=cfg["venue"]["min_notional_usd"])
    costs = Costs(**cfg["costs"])
    min_qty = {s: 0.0 for s in cfg["universe"]}
    data = config.ROOT / cfg["data_dir"]
    series, digest = {}, hashlib.sha256()
    for sym in cfg["universe"]:
        path = data / f"{sym}.csv"
        if not path.exists():
            raise SystemExit(f"missing {path} — run scripts/fetch_stocks.py first")
        digest.update(path.read_bytes())
        series[sym] = load_csv(path, sym)
    feed = (data / "FEED.txt").read_text().strip() if (data / "FEED.txt").exists() else "unknown"
    print(f"S1 universe: {len(series)} ETFs, daily bars, feed={feed}. "
          + ", ".join(f"{s} {v.ts[0]:%Y-%m-%d}" for s, v in list(series.items())[:3]) + " ...")

    max_gap = timedelta(days=cfg["max_gap_days"])
    signals = {sym: compute_signals(s, strat, DAY, max_gap) for sym, s in series.items()}
    print("Raw signals:", sum(len(v) for v in signals.values()), {k: len(v) for k, v in signals.items()})
    base_sigs = baseline_signals(series, strat, every=cfg.get("baseline_every_n_days", 5))

    base = Params(strat.stop_atr_mult, strat.target_atr_mult, strat.time_exit_bars, 24,
                  time_exit_exec_bars=strat.time_exit_bars)
    grid = [replace(base, stop_atr_mult=a, target_atr_mult=b)
            for a in cfg["grid"]["stop_atr_mult"] for b in cfg["grid"]["target_atr_mult"]]
    last = max(v.ts[-1] for v in series.values()) + DAY
    periods = {k: (_d(a), _d(b) or last) for k, (a, b) in cfg["splits"].items() if k != "final_test" or args.final}

    results = {"run_at": datetime.now(timezone.utc).isoformat(), "test": "S1", "data_sha256": digest.hexdigest()[:16],
               "feed": feed, "costs": cfg["costs"], "periods": {}}
    table = []
    for name, (a, b) in periods.items():
        print(f"\n================ {name.upper()}  {a:%Y-%m-%d} → {b:%Y-%m-%d}")
        ev = edge_view(series, signals, base, costs, risk, a, b, min_qty)
        bl = edge_view(series, base_sigs, base, costs, risk, a, b, min_qty)
        need_t = 2.0 if name in ("validation", "final_test") else 0.0
        v = verdict(ev, bl, need_t)
        t_vs = welch_t(ev.get("_pct", []), bl.get("_pct", []))
        print(f"EDGE VIEW (every signal, independent) → {v}")
        for k, val in ev.items():
            if k != "_pct":
                print(f"  {k:28} {val}")
        print(f"  {'BASELINE just-be-long':28} {bl.get('avg_net_pct_per_trade')}% per trade over {bl.get('trades')} entries "
              f"(win {bl.get('win_rate_pct')}%)")
        print(f"  {'breakout minus baseline':28} "
              f"{round(ev.get('avg_net_pct_per_trade', 0) - bl.get('avg_net_pct_per_trade', 0), 3)}% per trade, Welch t={t_vs}")
        acct = simulate(series, signals, base, costs, risk, a, b, exec_bar=DAY, min_qty=min_qty)
        acct.pop("_trades")
        print("ACCOUNT VIEW ($%.0f, one position, real rulebook)" % risk.allocation_usd)
        for k in ("trades", "net_usd", "win_rate_pct", "max_drawdown_usd", "exposure_pct", "signals_skipped_by_sizing"):
            print(f"  {k:28} {acct[k]}")
        bh = buy_and_hold(series["SPY"], costs, risk.allocation_usd, a, b)
        print(f"  {'buy & hold $100 SPY':28} {bh:+.2f}")
        rows = []
        if name != "final_test":
            print(f"\n  Declared grid, EDGE VIEW — avg net % per trade / trades / win% / t:")
            for p in grid:
                g = edge_view(series, signals, p, costs, risk, a, b, min_qty)
                g.pop("_pct", None)
                rows.append({"stop_atr_mult": p.stop_atr_mult, "target_atr_mult": p.target_atr_mult, **g})
                print(f"    stop {p.stop_atr_mult:>3} × target {p.target_atr_mult:>3}:  "
                      f"{g.get('avg_net_pct_per_trade', 0):+.3f}%  {g['trades']:4}  {g.get('win_rate_pct')}%  t={g.get('t_stat')}")
        table.append((name, ev, acct, bh, bl, v, t_vs))
        ev.pop("_pct", None)
        bl.pop("_pct", None)
        results["periods"][name] = {"from": a.isoformat(), "to": b.isoformat(), "edge_view": ev, "baseline": bl,
                                    "breakout_vs_baseline_t": t_vs, "verdict": v, "account_view": acct,
                                    "spy_buy_and_hold": bh, "grid": rows}

    print("\n================ S1 SUMMARY")
    print(f"  {'period':13}{'trades':>7}{'net%/tr':>9}{'pre-cost%':>10}{'baseline%':>10}{'diff t':>8}{'win%':>7}"
          f"{'b/e%':>7}{'acct $':>9}{'SPY B&H $':>11}   verdict")
    for name, ev, acct, bh, bl, v, t_vs in table:
        print(f"  {name:13}{ev.get('trades', 0):>7}{ev.get('avg_net_pct_per_trade', 0):>9}"
              f"{str(ev.get('avg_pre_cost_pct_per_trade')):>10}{str(bl.get('avg_net_pct_per_trade')):>10}{str(t_vs):>8}"
              f"{str(ev.get('win_rate_pct')):>7}{str(ev.get('breakeven_win_rate_pct')):>7}{acct['net_usd']:>9}{bh:>11}   {v}")
    log = config.ROOT / "data/backtest_runs.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(results, default=str) + "\n")
    print(f"\nSaved to {log.relative_to(config.ROOT)}. "
          + ("" if args.final else "Final period NOT evaluated — run --final only once the rule is frozen."))


if __name__ == "__main__":
    main()
