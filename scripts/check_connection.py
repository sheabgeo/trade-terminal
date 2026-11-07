"""Read-only connection check (article section 3). Submits nothing.

Usage: .venv/bin/python scripts/check_connection.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal import config  # noqa: E402
from terminal.collector import make_broker  # noqa: E402

risk, strat = config.load_risk(), config.load_strategy()
b = make_broker()
try:
    acct = b.account()
except Exception as e:  # noqa: BLE001
    if "unauthorized" in str(e).lower() or "401" in str(e) or "403" in str(e):
        sys.exit("Alpaca rejected the keys (401/403). Check: key starts with PK (paper, not AK live), "
                 "no spaces, key + secret from the SAME generation. Regenerate on the PAPER dashboard and re-paste both.")
    raise
out = {
    "source": b.source,
    "account": vars(acct),
    "strategy_allocation_usd": risk.allocation_usd,
    "positions": [vars(p) for p in b.positions()],
    "open_orders": [vars(o) for o in b.open_orders()],
    "assets": {s: vars(b.asset(s)) for s in strat.symbols},
    "quotes": {},
}
for s in strat.symbols:
    q = b.latest_quote(s)
    out["quotes"][s] = {"market_ts": q.ts.isoformat(), "bid": q.bid, "ask": q.ask}
print(json.dumps(out, indent=2, default=str))
if acct.environment != "paper":
    sys.exit("NOT PAPER — stop.")
