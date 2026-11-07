"""Historical test of the mechanical rule (article section 9).

Same code as live:  indicators.breakout_signal (same 600-bar window, same 260-bar contiguity rule)
                    sizing.size_position       (same loss budget, position cap, venue minimums)
Execution model (conservative):
  * signal on a COMPLETED bar → entry at the NEXT bar's open + half spread + slippage
  * IOC cap: if that fill would exceed limit = close*(1+half spread)*(1+entry_slippage) → no fill
  * stop/target levels measured from the limit price, exactly like preview_order
  * stop checked BEFORE target inside a bar (an hourly candle can't tell which came first)
  * gap through a level → fill at the open; time exit at the close of the 24th bar
  * fees on both sides; one position across both symbols; daily realized-loss halt
Usage:
  python -m terminal.backtest              # development + validation, baseline + declared grid
  python -m terminal.backtest --timeframes 1h,4h,1d   # signals on 4h / daily bars; fills/exits still on hourly bars
  python -m terminal.backtest --final      # ALSO the untouched final period — run once, after freezing the rule
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config
from .indicators import breakout_signal
from .sizing import size_position

HOUR = timedelta(hours=1)
VENUE_MIN_QTY = {"BTC/USD": 1.317e-05, "ETH/USD": 0.000445692}   # from your paper account, 2026-11-07


# ----------------------------------------------------------------------------------------- data
@dataclass
class Series:
    symbol: str
    ts: list[datetime]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741
    c: list[float]
    index: dict[datetime, int] = field(default_factory=dict)

    def __post_init__(self):
        self.index = {t: i for i, t in enumerate(self.ts)}


def load_csv(path: Path, symbol: str) -> Series:
    rows = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            rows[t] = r   # dedupe: last wins
    ts = sorted(rows)
    return Series(symbol, ts, [float(rows[t]["open"]) for t in ts], [float(rows[t]["high"]) for t in ts],
                  [float(rows[t]["low"]) for t in ts], [float(rows[t]["close"]) for t in ts])


def resample(s: Series, hours: int) -> Series:
    """Hourly → N-hour bars aligned to UTC midnight (4h: 00,04,...; 24h: daily). Bar ts = bucket OPEN time."""
    if hours == 1:
        return s
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    buckets: dict[datetime, list[int]] = {}
    for i, t in enumerate(s.ts):
        b = epoch + timedelta(hours=((t - epoch) // HOUR) // hours * hours)
        buckets.setdefault(b, []).append(i)
    ts = sorted(buckets)
    return Series(s.symbol, ts,
                  [s.o[buckets[b][0]] for b in ts], [max(s.h[k] for k in buckets[b]) for b in ts],
                  [min(s.l[k] for k in buckets[b]) for b in ts], [s.c[buckets[b][-1]] for b in ts])


# ---------------------------------------------------------------------------------------- signals
def compute_signals(s: Series, strat: config.Strategy, bar: timedelta = HOUR,
                    max_gap: timedelta | None = None) -> dict[datetime, float]:
    """{signal-bar CLOSE time: ATR} for every completed bar where the live rule would create a candidate.
    Uses only bars up to and including that bar — the same window and contiguity rule as the collector.
    max_gap: largest normal spacing between bars (crypto: = bar; stocks: weekends/holidays → 5 days)."""
    gap_ok = max_gap or bar
    out: dict[datetime, float] = {}
    run = 0
    lb = strat.breakout_lookback
    for i in range(len(s.ts)):
        run = run + 1 if i > 0 and s.ts[i] - s.ts[i - 1] <= gap_ok else 1
        if run < strat.min_contiguous_bars or i < lb:
            continue
        if s.c[i] <= max(s.h[i - lb:i]):        # cheap pre-check; breakout_signal re-checks identically
            continue
        lo = max(0, i - strat.history_bars + 1)
        window = [{"high": s.h[k], "low": s.l[k], "close": s.c[k]} for k in range(lo, i + 1)]
        r = breakout_signal(window, lb, strat.ema_fast, strat.ema_slow, strat.atr_period)
        if r["signal"]:
            out[s.ts[i] + bar] = r[f"atr_{strat.atr_period}"]
    return out


# ------------------------------------------------------------------------------------- simulation
@dataclass(frozen=True)
class Params:
    stop_atr_mult: float
    target_atr_mult: float
    time_exit_bars: int
    bar_hours: int = 1          # signal timeframe; fills/exits are simulated on the execution series' bars
    time_exit_exec_bars: int | None = None   # stocks: exit after N TRADING bars (calendar deadline can't skip weekends)


@dataclass
class Costs:
    fee_bps: float
    spread_bps: dict
    slippage_bps: float

    def half_spread(self, sym: str) -> float:
        if sym not in self.spread_bps:
            raise KeyError(f"no spread configured for {sym} in config/backtest.yaml — refusing to guess a cost")
        return self.spread_bps[sym] / 2 / 10_000


def simulate(series: dict[str, Series], signals: dict[str, dict[datetime, float]], params: Params, costs: Costs,
             risk: config.Risk, start: datetime, end: datetime, gross: bool = False,
             exec_bar: timedelta = HOUR, min_qty: dict | None = None) -> dict:
    """Event loop over HOURLY timestamps in [start, end). `series` are hourly; `signals` are keyed by signal-bar
    close time (any timeframe). gross=True → no fees/spread/slippage (for comparison)."""
    fee = 0 if gross else costs.fee_bps / 10_000
    slip = 0 if gross else costs.slippage_bps / 10_000
    hs = (lambda sym: 0.0) if gross else costs.half_spread
    alloc = risk.allocation_usd
    timeline = sorted({t for s in series.values() for t in s.ts if start <= t < end})
    trades, equity_curve = [], []
    pos = None              # open position
    pending = None          # entry to execute at the next bar's open
    realized = 0.0
    day, day_pnl = None, 0.0
    in_pos_bars = 0
    skipped: dict[str, int] = {}

    def close_pos(t, px, reason):
        nonlocal pos, realized, day_pnl
        net = pos["qty"] * (px - pos["entry"]) - pos["qty"] * pos["entry"] * fee - pos["qty"] * px * fee
        grs = pos["qty"] * (px - pos["entry"])
        trades.append({"symbol": pos["sym"], "entry_ts": pos["ts"], "exit_ts": t, "entry": pos["entry"], "exit": px,
                       "qty": pos["qty"], "notional": pos["qty"] * pos["entry"], "net": net, "gross_move": grs,
                       "reason": reason, "bars": pos["bars"]})
        realized += net
        day_pnl += net
        pos = None

    for t in timeline:
        if t.date() != day:
            day, day_pnl = t.date(), 0.0

        # (a) pending entry fills at this bar's open
        if pending and pending["sym"] in series and t in series[pending["sym"]].index:
            sym, s = pending["sym"], series[pending["sym"]]
            i = s.index[t]
            fill = s.o[i] * (1 + hs(sym) + slip)
            if fill <= pending["limit"]:
                pos = {"sym": sym, "ts": t, "entry": fill, "qty": pending["qty"], "stop": pending["stop"],
                       "target": pending["target"], "deadline": pending["deadline"], "bars": 0}
            pending = None

        # (b) manage the open position on this bar
        if pos and t in series[pos["sym"]].index:
            s, sym = series[pos["sym"]], pos["sym"]
            i = s.index[t]
            pos["bars"] += 1
            in_pos_bars += 1
            out = 1 - hs(sym) - slip
            if s.o[i] <= pos["stop"]:
                close_pos(t, s.o[i] * out, "stop_gap")
            elif s.l[i] <= pos["stop"]:
                close_pos(t, pos["stop"] * out, "stop")
            elif s.o[i] >= pos["target"]:
                close_pos(t, s.o[i] * (1 - hs(sym)), "target_gap")
            elif s.h[i] >= pos["target"]:
                close_pos(t, pos["target"] * (1 - hs(sym)), "target")
            elif (pos["bars"] >= params.time_exit_exec_bars) if params.time_exit_exec_bars \
                    else (t + exec_bar >= pos["deadline"]):
                close_pos(t, s.c[i] * out, "time")

        # (c) mark to market
        mtm = 0.0
        if pos and t in series[pos["sym"]].index:
            mtm = pos["qty"] * (series[pos["sym"]].c[series[pos["sym"]].index[t]] - pos["entry"])
        equity_curve.append(realized + mtm)

        # (d) new candidates at this bar's close (one position across all symbols; daily halt)
        if pos or pending or day_pnl <= -alloc * risk.daily_loss_halt_pct / 100:
            continue
        close_t = t + exec_bar                   # this execution bar's close
        for sym in sorted(series):
            s = series[sym]
            i = s.index.get(t)
            if i is None or close_t not in signals.get(sym, {}):
                continue
            atr_v = signals[sym][close_t]
            ask = s.c[i] * (1 + hs(sym))
            limit = ask * (1 + risk.entry_slippage_bps / 10_000)   # same IOC cap in gross mode
            stop = limit - params.stop_atr_mult * atr_v
            target = limit + params.target_atr_mult * atr_v
            cost_pu = limit * fee * 2 + 2 * hs(sym) * s.c[i] + (limit - ask)
            sz = size_position(loss_budget_usd=alloc * risk.loss_budget_pct / 100, entry_price=limit, stop_price=stop,
                               cost_per_unit=cost_pu, max_position_usd=alloc * risk.max_position_pct / 100,
                               cash_usd=alloc, qty_increment=1e-9,
                               min_qty=(VENUE_MIN_QTY if min_qty is None else min_qty).get(sym, 0),
                               min_notional_usd=risk.min_notional_usd)
            if sz.reject:
                key = "below_min_notional" if "$" in sz.reject else "below_min_qty" if "venue minimum" in sz.reject else sz.reject
                skipped[key] = skipped.get(key, 0) + 1
                continue
            pending = {"sym": sym, "qty": sz.qty, "limit": limit, "stop": stop, "target": target,
                       "deadline": close_t + params.time_exit_bars * params.bar_hours * HOUR}
            break

    if pos:   # force-close at the end of the period, at the last close
        s = series[pos["sym"]]
        last = max(k for k in s.ts if k < end)
        close_pos(last, s.c[s.index[last]] * (1 - hs(pos["sym"]) - slip), "period_end")
        equity_curve.append(realized)

    out = summarize(trades, equity_curve, alloc, len(timeline), in_pos_bars)
    out["signals_skipped_by_sizing"] = skipped
    return out


def summarize(trades, curve, alloc, n_bars, in_pos_bars) -> dict:
    nets = [x["net"] for x in trades]
    wins = [n for n in nets if n > 0]
    peak, mdd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    avg_win = sum(wins) / len(wins) if wins else 0
    losses = [n for n in nets if n <= 0]
    avg_loss = -sum(losses) / len(losses) if losses else 0
    return {
        "trades": len(trades),
        "net_usd": round(sum(nets), 2),
        "net_pct_of_alloc": round(100 * sum(nets) / alloc, 2),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
        "breakeven_win_rate_pct": round(100 * avg_loss / (avg_win + avg_loss), 1) if wins and losses else None,
        "avg_net_trade_usd": round(sum(nets) / len(trades), 4) if trades else None,
        "avg_win_usd": round(avg_win, 4),
        "avg_loss_usd": round(-avg_loss, 4),
        "max_drawdown_usd": round(mdd, 2),
        "exposure_pct": round(100 * in_pos_bars / n_bars, 1) if n_bars else 0,
        "turnover_x": round(sum(2 * x["notional"] for x in trades) / alloc, 1),
        "exit_reasons": {r: sum(1 for x in trades if x["reason"] == r) for r in sorted({x["reason"] for x in trades})},
        "_trades": trades,
    }


def buy_and_hold(s: Series, costs: Costs, alloc: float, start: datetime, end: datetime) -> float:
    idx = [i for i, t in enumerate(s.ts) if start <= t < end]
    if not idx:
        return 0.0
    fee = costs.fee_bps / 10_000
    buy = s.o[idx[0]] * (1 + costs.half_spread(s.symbol))
    sell = s.c[idx[-1]] * (1 - costs.half_spread(s.symbol))
    qty = alloc * (1 - fee) / buy
    return round(qty * sell * (1 - fee) - alloc, 2)


# --------------------------------------------------------------------------------------- runner
def split_bounds(timeline: list[datetime], splits: dict) -> dict[str, tuple[datetime, datetime]]:
    n = len(timeline)
    out, k = {}, 0
    names = list(splits)
    for j, name in enumerate(names):
        k2 = n if j == len(names) - 1 else k + int(n * splits[name])
        out[name] = (timeline[k], timeline[k2 - 1] + HOUR)
        k = k2
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--final", action="store_true", help="also report the untouched final period (run once, after freezing)")
    ap.add_argument("--trades", action="store_true", help="print every baseline trade")
    ap.add_argument("--timeframes", default="1h", help="comma list of signal timeframes: 1h,4h,1d")
    ap.add_argument("--allocation", type=float, help="override allocation_usd (diagnostic: isolates edge from the $10 minimum)")
    args = ap.parse_args()

    cfg = yaml.safe_load((config.ROOT / "config/backtest.yaml").read_text(encoding="utf-8"))
    strat, risk = config.load_strategy(), config.load_risk()
    if args.allocation:
        from dataclasses import replace
        risk = replace(risk, allocation_usd=args.allocation)
        print(f"NOTE: allocation overridden to ${args.allocation:,.0f} (diagnostic). Compare % columns, not $.")
    costs = Costs(**cfg["costs"])
    data_dir = config.ROOT / cfg["data_dir"]
    series, digest = {}, hashlib.sha256()
    for sym in strat.symbols:
        path = data_dir / f"{sym.replace('/', '-')}.csv"
        if not path.exists():
            raise SystemExit(f"missing {path} — run scripts/fetch_history.py first")
        digest.update(path.read_bytes())
        series[sym] = load_csv(path, sym)
    print("Data:", ", ".join(f"{s}: {len(v.ts)} bars {v.ts[0]:%Y-%m-%d}→{v.ts[-1]:%Y-%m-%d}" for s, v in series.items()))

    timeline = sorted({t for s in series.values() for t in s.ts})
    bounds = split_bounds(timeline, cfg["splits"])
    periods = [p for p in bounds if p != "final_test" or args.final]
    tf_hours = {"1h": 1, "4h": 4, "1d": 24}
    log = config.ROOT / "data/backtest_runs.jsonl"
    summary = []
    for tf in [x.strip() for x in args.timeframes.split(",") if x.strip()]:
        if tf not in tf_hours:
            raise SystemExit(f"unknown timeframe {tf}; use 1h, 4h or 1d")
        summary += run_timeframe(tf, tf_hours[tf], series, strat, risk, cfg, costs, bounds, periods, args, digest, log)
    if len(summary) > 1:
        print("\n================ SUMMARY (baseline, after costs)")
        print(f"  {'timeframe':10}{'period':13}{'trades':>7}{'skipped':>8}{'net %':>8}{'pre-cost %':>11}{'win%':>7}"
              f"{'b/e win%':>9}{'max dd %':>9}")
        a = risk.allocation_usd
        for r in summary:
            print(f"  {r[0]:10}{r[1]:13}{r[2]:>7}{r[8]:>8}{100*r[3]/a:>8.1f}{100*r[4]/a:>11.1f}{str(r[5]):>7}"
                  f"{str(r[6]):>9}{100*r[7]/a:>9.1f}")
    print(f"\nSaved to {log.relative_to(config.ROOT)} (every run is kept).")
    if not args.final:
        print("Final test period NOT evaluated. Run with --final only once the rule is frozen.")


def run_timeframe(tf, hours, series, strat, risk, cfg, costs, bounds, periods, args, digest, log) -> list:
    bar = hours * HOUR
    print(f"\n#################### SIGNAL TIMEFRAME {tf}  (fills/exits simulated on hourly bars)")
    signals = {sym: compute_signals(resample(s, hours), strat, bar) for sym, s in series.items()}
    print("Raw signals:", {k: len(v) for k, v in signals.items()})
    base = Params(strat.stop_atr_mult, strat.target_atr_mult, strat.time_exit_bars, hours)
    grid = [Params(a, b, strat.time_exit_bars, hours) for a in cfg["grid"]["stop_atr_mult"]
            for b in cfg["grid"]["target_atr_mult"]]
    summary = []
    results = {"run_at": datetime.now(timezone.utc).isoformat(), "data_sha256": digest.hexdigest()[:16],
               "strategy": strat.version, "timeframe": tf, "costs": cfg["costs"],
               "allocation_usd": risk.allocation_usd, "periods": {}}
    for name in periods:
        a, b = bounds[name]
        print(f"\n================ {name.upper()}  {a:%Y-%m-%d} → {b:%Y-%m-%d}")
        net = simulate(series, signals, base, costs, risk, a, b)
        grs = simulate(series, signals, base, costs, risk, a, b, gross=True)
        trades = net.pop("_trades")
        grs.pop("_trades")
        bh = {sym: buy_and_hold(series[sym], costs, risk.allocation_usd, a, b) for sym in series}
        print(f"Baseline [{tf}] (stop {base.stop_atr_mult}×ATR, target {base.target_atr_mult}×ATR, "
              f"time {base.time_exit_bars} bars) "
              f"on ${risk.allocation_usd:.0f}:")
        for k, v in net.items():
            print(f"  {k:24} {v}")
        print(f"  {'net_usd BEFORE costs':24} {grs['net_usd']}   ← same rule with zero fees/spread/slippage")
        print(f"  {'cash':24} 0.0")
        print(f"  {'buy & hold $' + str(int(risk.allocation_usd)):24} " + ", ".join(f"{k} {v:+.2f}" for k, v in bh.items()))
        if args.trades:
            for x in trades:
                print(f"    {x['symbol']} {x['entry_ts']:%Y-%m-%d %H:%M} → {x['exit_ts']:%m-%d %H:%M} "
                      f"{x['reason']:10} net {x['net']:+.4f}")
        rows = []
        if name != "final_test":
            print(f"\n  Declared grid ({len(grid)} combos) — net_usd / trades / win% :")
            for p in grid:
                r = simulate(series, signals, p, costs, risk, a, b)
                r.pop("_trades")
                rows.append({"stop_atr_mult": p.stop_atr_mult, "target_atr_mult": p.target_atr_mult, **r})
                print(f"    stop {p.stop_atr_mult:>3} × target {p.target_atr_mult:>3}:  {r['net_usd']:+8.2f}  "
                      f"{r['trades']:4}  {r['win_rate_pct']}%  dd {r['max_drawdown_usd']}")
        results["periods"][name] = {"from": a.isoformat(), "to": b.isoformat(), "baseline": net,
                                    "baseline_gross_net_usd": grs["net_usd"], "buy_and_hold": bh, "grid": rows}
        summary.append((tf, name, net["trades"], net["net_usd"], grs["net_usd"], net["win_rate_pct"],
                        net["breakeven_win_rate_pct"], net["max_drawdown_usd"],
                        sum(net["signals_skipped_by_sizing"].values())))

    with log.open("a") as f:
        f.write(json.dumps(results, default=str) + "\n")
    return summary


if __name__ == "__main__":
    main()
