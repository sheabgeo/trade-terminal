"""Hourly collector: saves bars + quotes, builds snapshots, queues candidates.

Run once:   python -m terminal.collector --once
Run loop:   python -m terminal.collector          (wakes ~1 min after each hour)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import config, db
from .broker.base import Broker
from .indicators import breakout_signal

HOUR = timedelta(hours=1)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def account_state(broker: Broker, risk: config.Risk, symbols: list[str]) -> dict:
    acct = broker.account()
    held = [p for p in broker.positions() if p.symbol in symbols]
    # Dust = a remainder below the venue minimum; it can't be sold, so it must not block the one-position rule.
    mins = {s: broker.asset(s).min_order_size for s in {p.symbol for p in held}}
    positions = [p for p in held if p.qty >= mins[p.symbol]]
    dust = [p for p in held if p.qty < mins[p.symbol]]
    orders = [o for o in broker.open_orders() if o.symbol in symbols]
    return {
        "masked_id": acct.masked_id,
        "environment": acct.environment,
        "status": acct.status,
        "account_equity": acct.equity,
        "account_cash": acct.cash,
        "strategy_allocation": risk.allocation_usd,
        "strategy_positions": [vars(p) for p in positions],
        "dust_positions": [vars(p) for p in dust],
        "strategy_exposure_usd": round(sum(p.market_value for p in positions), 2),
        "unresolved_orders": [vars(o) for o in orders],
    }


def evaluate_symbol(
    conn: sqlite3.Connection,
    broker: Broker,
    strat: config.Strategy,
    risk: config.Risk,
    symbol: str,
    acct: dict,
    now: datetime,
) -> dict:
    expected_signal_bar = floor_hour(now) - HOUR  # last hour that has fully closed
    start = expected_signal_bar - (strat.history_bars - 1) * HOUR

    raw = broker.bars(symbol, start, floor_hour(now) + HOUR)
    received = iso(now)
    # Unfinished candle is dropped from EVERYTHING: a bar counts only if its hour has closed.
    completed = sorted((b for b in raw if b.ts + HOUR <= now), key=lambda b: b.ts)
    for b in completed:
        conn.execute(
            "INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?)",
            (symbol, iso(b.ts), b.open, b.high, b.low, b.close, b.volume, received),
        )

    blockers: list[str] = []
    bar_dicts = [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in completed]

    # Missing-candle checks
    if not completed or completed[-1].ts != expected_signal_bar:
        blockers.append(f"missing_bar: latest completed bar {iso(expected_signal_bar)} not received")
    tail = completed[-strat.min_contiguous_bars:]
    gaps = [iso(a.ts + HOUR) for a, b in zip(tail, tail[1:]) if b.ts - a.ts != HOUR]
    if gaps:
        blockers.append(f"missing_bar: gap(s) in indicator window starting {gaps[0]} ({len(gaps)} total)")
    if len(completed) < strat.min_contiguous_bars:
        blockers.append(f"insufficient_history: {len(completed)} bars < {strat.min_contiguous_bars}")

    # Quote freshness
    quote_id, quote_age = None, None
    try:
        q = broker.latest_quote(symbol)
        quote_age = round((now - q.ts).total_seconds(), 3)
        cur = conn.execute(
            "INSERT INTO quotes (symbol, market_ts, received_at, bid, ask) VALUES (?,?,?,?,?)",
            (symbol, iso(q.ts), received, q.bid, q.ask),
        )
        quote_id = cur.lastrowid
        if quote_age > risk.quote_max_age_seconds:
            blockers.append(f"stale_quote: age {quote_age:.1f}s > {risk.quote_max_age_seconds}s")
        if quote_age < -2:
            blockers.append(f"clock_skew: quote is {-quote_age:.1f}s in the future — check the host clock (timedatectl)")
        if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
            blockers.append(f"bad_quote: bid {q.bid} ask {q.ask}")
        elif completed:
            mid, last_close = (q.bid + q.ask) / 2, completed[-1].close
            if abs(mid / last_close - 1) > risk.quote_close_max_deviation_pct / 100:
                blockers.append(f"quote_mismatch: mid {mid:.2f} is {100 * (mid / last_close - 1):+.1f}% from bar close "
                                f"{last_close:.2f} (limit ±{risk.quote_close_max_deviation_pct}%)")
    except Exception as e:  # noqa: BLE001 — any quote failure blocks entry
        blockers.append(f"quote_unavailable: {type(e).__name__}: {e}")

    # Account-level blockers
    if acct["strategy_positions"]:
        blockers.append("position_open: one-position rule")
    if acct["unresolved_orders"]:
        blockers.append("unresolved_order: an open order exists for a strategy symbol")

    indicators: dict = {}
    signal = False
    data_ok = not any(b.startswith(("missing_bar", "insufficient_history")) for b in blockers)
    if data_ok:
        indicators = breakout_signal(bar_dicts, strat.breakout_lookback, strat.ema_fast, strat.ema_slow, strat.atr_period)
        signal = indicators["signal"]

    bar_ts = iso(completed[-1].ts) if completed else None
    cur = conn.execute(
        """INSERT INTO snapshots (symbol, strategy_version, bar_ts, created_at, source, quote_id, quote_age_s,
                                  indicators, account, signal, blockers)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (symbol, strat.version, bar_ts, received, broker.source, quote_id, quote_age,
         json.dumps(indicators), json.dumps(acct), int(signal), json.dumps(blockers)),
    )
    snapshot_id = cur.lastrowid

    candidate_id = None
    if signal:
        candidate_id = f"{symbol}|{strat.version}|{bar_ts}"
        bar_close = completed[-1].ts + HOUR
        expires = bar_close + timedelta(minutes=risk.candidate_ttl_minutes)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO candidates (id, symbol, snapshot_id, created_at, expires_at, status) VALUES (?,?,?,?,?,?)",
            (candidate_id, symbol, snapshot_id, received, iso(expires), "PENDING" if expires > now else "EXPIRED"),
        ).rowcount
        if not inserted:
            db.log_event(conn, received, "duplicate_candidate_ignored", {"candidate_id": candidate_id})
            candidate_id = None

    return {"symbol": symbol, "snapshot_id": snapshot_id, "signal": signal, "blockers": blockers, "candidate_id": candidate_id}


def expire_candidates(conn: sqlite3.Connection, now: datetime) -> int:
    return conn.execute(
        "UPDATE candidates SET status='EXPIRED' WHERE status IN ('PENDING','CLAIMED') AND expires_at <= ?", (iso(now),)
    ).rowcount


def run_cycle(
    conn: sqlite3.Connection,
    broker: Broker,
    strat: config.Strategy,
    risk: config.Risk,
    now: datetime | None = None,
    on_candidate: Callable[[str], None] | None = None,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    expire_candidates(conn, now)
    acct = account_state(broker, risk, strat.symbols)
    results = [evaluate_symbol(conn, broker, strat, risk, s, acct, now) for s in strat.symbols]
    db.log_event(conn, iso(now), "collector_cycle", {"results": results})
    if on_candidate:
        for r in results:
            if r["candidate_id"]:
                on_candidate(r["candidate_id"])  # wakes one headless Claude review run (agent_wake.py)
    return results


def make_broker() -> Broker:
    kind = config.broker_kind()
    if kind == "fake":
        from .broker.fake import FakeBroker, uptrend_breakout_bars

        fb = FakeBroker()
        end = floor_hour(fb.now) - HOUR
        fb.set_bars("BTC/USD", uptrend_breakout_bars(end))
        fb.set_bars("ETH/USD", uptrend_breakout_bars(end, base=3000))
        for sym in ("BTC/USD", "ETH/USD"):
            last = fb._bars[sym][-1].close
            fb.set_quote(sym, last * 0.99995, last * 1.00005)
        return fb
    from .broker.alpaca_paper import AlpacaPaper

    return AlpacaPaper()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one cycle now and exit")
    args = ap.parse_args()
    strat, risk = config.load_strategy(), config.load_risk()
    conn = db.connect(config.db_path())
    broker = make_broker()
    from .agent_wake import make_waker
    wake = make_waker(conn)
    while True:
        for r in run_cycle(conn, broker, strat, risk, on_candidate=lambda cid: print(json.dumps(wake(cid)))):
            print(json.dumps(r))
        if args.once:
            return
        now = datetime.now(timezone.utc)
        wake = floor_hour(now) + HOUR + timedelta(seconds=65)  # give the bar time to publish
        time.sleep(max(5, (wake - now).total_seconds()))


if __name__ == "__main__":
    main()
