"""Paper order-cycle smoke test (article section 5). Buys ~$12.50 of BTC on PAPER through the real
service path, repeats the submit to prove no duplicate, then sells the position back.

Uses a separate database (data/smoke.db) and a synthetic candidate tagged 'smoke-test',
so it never touches real strategy records.

Usage:  .venv/bin/python scripts/paper_smoke_test.py --yes
"""
import argparse
import json
import math
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal import config, db  # noqa: E402
from terminal.broker.base import TERMINAL_STATUSES, OrderNotFound  # noqa: E402
from terminal.collector import iso, make_broker, run_cycle  # noqa: E402
from terminal.execution import client_order_id  # noqa: E402
from terminal.service import TradingService  # noqa: E402

SYMBOL = "BTC/USD"


def show(title, obj):
    print(f"\n== {title}")
    print(json.dumps(obj, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="confirm: place ~$12.50 PAPER buy + sell")
    args = ap.parse_args()
    if not args.yes:
        sys.exit("Add --yes to place a ~$12.50 PAPER buy and sell it back.")

    strat = config.load_strategy()
    # Smoke-test allocation $50 → max position $12.50 (Alpaca's minimum order is $10), loss budget $0.25.
    risk = replace(config.load_risk(), allocation_usd=50.0, execution_mode="paper")
    broker = make_broker()
    acct = broker.account()
    if acct.environment != "paper":
        sys.exit("NOT PAPER — stop.")
    smoke_db = config.ROOT / "data/smoke.db"
    for f in smoke_db.parent.glob("smoke.db*"):   # throwaway DB: every run starts clean
        f.unlink()
    conn = db.connect(smoke_db)
    svc = TradingService(conn, broker, strat, risk)

    # 1. Fresh snapshot of real data
    now = datetime.now(timezone.utc)
    res = {r["symbol"]: r for r in run_cycle(conn, broker, strat, risk, now=now)}[SYMBOL]
    show("snapshot", res)
    data_blockers = [b for b in res["blockers"] if not b.startswith(("position_open", "unresolved_order"))]
    if data_blockers:
        sys.exit(f"data blockers present, try again in a minute: {data_blockers}")

    # 2. Synthetic candidate pointing at that real snapshot
    cid = f"{SYMBOL}|smoke-test|{iso(now)}"
    conn.execute(
        "INSERT INTO candidates (id, symbol, snapshot_id, created_at, expires_at, status) VALUES (?,?,?,?,?, 'PENDING')",
        (cid, SYMBOL, res["snapshot_id"], iso(now), iso(now + timedelta(minutes=10))),
    )
    owner = "smoke-test"
    svc.claim_candidate(cid, owner)
    svc.record_decision(cid, owner, "ENTER", "SMOKE TEST — synthetic candidate to verify the order cycle",
                        [f"snapshot:{res['snapshot_id']}"])

    # 3. Preview → submit → repeat submit
    p = svc.preview_order(cid, owner)
    show("preview (allocation $50 for smoke test)", {k: p[k] for k in ("intent_id", "qty", "max_entry_price",
                                                                       "planned_stop", "planned_target", "sizing")})
    r1 = svc.submit_intent(p["intent_id"], owner)
    show("submit #1 — broker result", {"status": r1["status"], "broker": r1["broker"], "position": r1["position"]})
    r2 = svc.submit_intent(p["intent_id"], owner)
    show("submit #2 — must NOT send a second order", {"status": r2["status"], "note": r2.get("note"),
                                                      "broker_order_id": r2["broker"]["order_id"]})
    assert r2["broker"]["order_id"] == r1["broker"]["order_id"], "DUPLICATE ORDER — investigate"

    if r1["status"] not in ("FILLED", "PARTIAL"):
        print(f"\nEntry ended {r1['status']} — nothing to sell. (NO_FILL can happen if price moved; rerun.)")
        return

    # 4. Sell back what the broker says we hold (fees may have been taken in BTC)
    held = sum(x.qty for x in broker.positions() if x.symbol == SYMBOL)
    qty = math.floor(held * 1e9) / 1e9
    q = broker.latest_quote(SYMBOL)
    limit = math.floor(q.bid * (1 - 0.003) * 100) / 100   # willing to give up 0.3% to get out
    exit_id = client_order_id(cid, "smoke-exit")
    print(f"\n== selling {qty} {SYMBOL} IOC limit {limit} (bid {q.bid})")
    try:
        broker.submit_limit_ioc(SYMBOL, "sell", qty, limit, exit_id)
    except Exception as e:  # noqa: BLE001
        print(f"submit raised {type(e).__name__}: {e} — checking broker state")
    o = None
    for _ in range(5):
        try:
            o = broker.get_order_by_client_id(exit_id)
            if o.status in TERMINAL_STATUSES:
                break
        except OrderNotFound:
            pass
        time.sleep(1)
    show("exit order", vars(o) if o else "NOT FOUND")
    if o and o.filled_qty > 0:
        conn.execute("UPDATE positions SET status='CLOSED' WHERE intent_id=?", (p["intent_id"],))

    left = sum(x.qty for x in broker.positions() if x.symbol == SYMBOL)
    entry_cost = (r1["broker"]["filled_qty"] or 0) * (r1["broker"]["filled_avg_price"] or 0)
    exit_value = (o.filled_qty * o.filled_avg_price) if (o and o.filled_avg_price) else 0
    show("result", {
        "entry_usd": round(entry_cost, 4),
        "exit_usd_before_fee": round(exit_value, 4),
        "approx_round_trip_cost_usd": round(entry_cost - exit_value * (1 - risk.fee_rate_bps / 10_000), 4),
        "btc_left_over (dust)": left,
        "PASS": bool(o and o.filled_qty > 0),
    })
    if left > 0:
        print("Note: a remainder below the venue minimum is 'dust' — it can't be sold and the strategy ignores it.")


if __name__ == "__main__":
    main()
