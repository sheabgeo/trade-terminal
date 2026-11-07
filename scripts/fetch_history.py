"""Download completed hourly bars from Alpaca into data/history/<SYMBOL>.csv (resumable).

Usage:  .venv/bin/python scripts/fetch_history.py              # from config/backtest.yaml start
        .venv/bin/python scripts/fetch_history.py --start 2023-01-01
"""
import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from terminal import config  # noqa: E402
from terminal.collector import make_broker  # noqa: E402

HOUR = timedelta(hours=1)


def main():
    cfg = yaml.safe_load((config.ROOT / "config/backtest.yaml").read_text(encoding="utf-8"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=cfg["start"])
    args = ap.parse_args()
    out_dir = config.ROOT / cfg["data_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    broker = make_broker()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)   # last bar that has closed starts at now-1h

    for symbol in config.load_strategy().symbols:
        path = out_dir / f"{symbol.replace('/', '-')}.csv"
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        last_ts = None
        if path.exists():   # resume after the last saved bar
            last = path.read_text().strip().splitlines()[-1].split(",")[0]
            if last != "ts":
                last_ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
                start = last_ts + HOUR
        new = 0
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if f.tell() == 0:
                w.writerow(["ts", "open", "high", "low", "close", "volume"])
            chunk = start
            while chunk < now:
                end = min(chunk + timedelta(days=90), now)
                bars = [b for b in broker.bars(symbol, chunk, end) if b.ts + HOUR <= now]   # completed only
                bars = [b for b in bars if last_ts is None or b.ts > last_ts]   # API end may be inclusive
                if bars:
                    last_ts = bars[-1].ts
                for b in bars:
                    w.writerow([b.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), b.open, b.high, b.low, b.close, b.volume])
                new += len(bars)
                print(f"{symbol}: {chunk:%Y-%m-%d} → {end:%Y-%m-%d}  +{len(bars)} bars", flush=True)
                chunk = end
        print(f"{symbol}: saved {new} new bars → {path}")


if __name__ == "__main__":
    main()
