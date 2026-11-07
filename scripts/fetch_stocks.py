"""Download split- and dividend-adjusted DAILY bars for the S1 ETF universe → data/history_stocks/<SYM>.csv

Usage:  .venv/bin/python scripts/fetch_stocks.py
Tries the consolidated SIP feed first; falls back to IEX if the account's data plan refuses SIP.
"""
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from alpaca.data.enums import Adjustment, DataFeed  # noqa: E402
from alpaca.data.historical import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402

from terminal import config  # noqa: E402


def fetch(client, sym, start, end, feed):
    req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=start, end=end,
                           adjustment=Adjustment.ALL, feed=feed)
    return client.get_stock_bars(req).data.get(sym, [])


def main():
    cfg = yaml.safe_load((config.ROOT / "config/backtest_stocks.yaml").read_text(encoding="utf-8"))
    config._load_env_file(config.ROOT / ".env")
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    out = config.ROOT / cfg["data_dir"]
    out.mkdir(parents=True, exist_ok=True)
    start = datetime.fromisoformat(cfg["start"]).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)   # free plans can't query the last 15 min of SIP
    feed = DataFeed.SIP
    for sym in cfg["universe"]:
        try:
            bars = fetch(client, sym, start, end, feed)
        except Exception as e:  # noqa: BLE001
            if feed == DataFeed.SIP and ("subscription" in str(e).lower() or "403" in str(e) or "sip" in str(e).lower()):
                print(f"SIP refused ({e}); falling back to IEX feed for all symbols")
                feed = DataFeed.IEX
                bars = fetch(client, sym, start, end, feed)
            else:
                raise
        # Normalise each daily bar to its trading DATE at 00:00 UTC (Alpaca stamps daily bars at ~04:00/05:00 UTC).
        rows = {}
        for b in bars:
            d = b.timestamp.astimezone(timezone.utc).date()   # stamped 04:00/05:00 UTC = midnight ET → same date
            rows[d] = b
        with (out / f"{sym}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
            for d in sorted(rows):
                b = rows[d]
                w.writerow([f"{d.isoformat()}T00:00:00Z", b.open, b.high, b.low, b.close, b.volume])
        first, last = (min(rows), max(rows)) if rows else (None, None)
        print(f"{sym}: {len(rows)} daily bars {first} → {last}  (feed {feed.value})")
    (out / "FEED.txt").write_text(feed.value)


if __name__ == "__main__":
    main()
