import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from terminal import config, db
from terminal.broker.base import Bar, OpenOrder, Position
from terminal.broker.fake import FakeBroker, downtrend_bars, uptrend_breakout_bars
from terminal.collector import run_cycle
from terminal.indicators import atr, ema, prior_high
from terminal.service import ServiceError, TradingService

NOW = datetime(2026, 11, 5, 10, 1, 30, tzinfo=timezone.utc)
SIGNAL_BAR = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
BTC_PX = round(uptrend_breakout_bars(SIGNAL_BAR)[-1].close, 2)
ETH_PX = round(downtrend_bars(SIGNAL_BAR)[-1].close, 2)


@pytest.fixture
def env():
    strat, risk = config.load_strategy(), config.load_risk()
    conn = db.connect(":memory:")
    fb = FakeBroker(now=NOW)
    fb.set_bars("BTC/USD", uptrend_breakout_bars(SIGNAL_BAR))
    fb.set_bars("ETH/USD", downtrend_bars(SIGNAL_BAR))
    fb.set_quote("BTC/USD", BTC_PX, BTC_PX + 10, age_s=2)
    fb.set_quote("ETH/USD", ETH_PX, ETH_PX + 1, age_s=2)
    svc = TradingService(conn, fb, strat, risk, clock=lambda: NOW)
    return conn, fb, strat, risk, svc


def cycle(env, now=NOW):
    conn, fb, strat, risk, _ = env
    return {r["symbol"]: r for r in run_cycle(conn, fb, strat, risk, now=now)}


# ---------- indicators ----------
def test_prior_high_excludes_signal_bar():
    assert prior_high([1, 2, 3, 99], lookback=3) == 3


def test_ema_and_atr_basic():
    assert ema([5.0] * 300, 200) == pytest.approx(5.0)
    assert atr([11.0] * 30, [9.0] * 30, [10.0] * 30, 14) == pytest.approx(2.0)


# ---------- collector ----------
def test_clean_breakout_creates_one_candidate(env):
    r = cycle(env)
    assert r["BTC/USD"]["signal"] and r["BTC/USD"]["blockers"] == []
    assert r["BTC/USD"]["candidate_id"] == "BTC/USD|breakout-v1|2026-11-05T09:00:00Z"
    assert r["ETH/USD"]["signal"] is False and r["ETH/USD"]["candidate_id"] is None


def test_unfinished_bar_is_ignored(env):
    conn, fb, *_ = env
    bars = uptrend_breakout_bars(SIGNAL_BAR)
    forming = Bar(SIGNAL_BAR + timedelta(hours=1), 1, 1_000_000, 1, 1_000_000, 1)  # 10:00 bar, still open
    fb.set_bars("BTC/USD", bars + [forming])
    r = cycle(env)
    snap = conn.execute("SELECT bar_ts, indicators FROM snapshots WHERE id=?", (r["BTC/USD"]["snapshot_id"],)).fetchone()
    assert snap["bar_ts"] == "2026-11-05T09:00:00Z"
    assert json.loads(snap["indicators"])["close"] == bars[-1].close


def test_stale_quote_blocks_enter_and_skip_is_recorded(env):
    conn, fb, strat, risk, svc = env
    fb.set_quote("BTC/USD", BTC_PX, BTC_PX + 10, age_s=40)
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    snap_id = svc.inspect_candidate(cid)["snapshot"]["id"]
    svc.claim_candidate(cid, "run-1")
    with pytest.raises(ServiceError, match="stale_quote"):
        svc.record_decision(cid, "run-1", "ENTER", "breakout passed all three checks", [f"snapshot:{snap_id}"])
    out = svc.record_decision(cid, "run-1", "SKIP", "stale_quote: 40s > 15s limit", [f"snapshot:{snap_id}"])
    assert out["action"] == "SKIP"


def test_missing_candle_in_window_blocks(env):
    _, fb, *_ = env
    fb.missing_bar_ts = {SIGNAL_BAR - timedelta(hours=30)}
    r = cycle(env)["BTC/USD"]
    assert any(b.startswith("missing_bar: gap") for b in r["blockers"])
    assert r["candidate_id"] is None  # no indicators computed on gappy data


def test_missing_latest_bar_blocks(env):
    _, fb, *_ = env
    fb.missing_bar_ts = {SIGNAL_BAR}
    r = cycle(env)["BTC/USD"]
    assert any("latest completed bar" in b for b in r["blockers"])
    assert r["candidate_id"] is None


def test_quote_unavailable_blocks(env):
    _, fb, *_ = env
    del fb._quotes["BTC/USD"]
    r = cycle(env)["BTC/USD"]
    assert any(b.startswith("quote_unavailable") for b in r["blockers"])


def test_duplicate_cycle_does_not_duplicate_candidate(env):
    conn = env[0]
    first = cycle(env)["BTC/USD"]["candidate_id"]
    second = cycle(env)["BTC/USD"]["candidate_id"]
    assert first and second is None
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


# ---------- queue / decisions ----------
def test_second_owner_cannot_claim(env):
    svc = env[4]
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    svc.claim_candidate(cid, "run-1")
    assert svc.claim_candidate(cid, "run-1")["note"] == "already yours"
    with pytest.raises(ServiceError, match="owner=run-1"):
        svc.claim_candidate(cid, "run-2")


def test_expired_candidate_cannot_be_claimed(env):
    conn, fb, strat, risk, _ = env
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    late = TradingService(conn, fb, strat, risk, clock=lambda: NOW + timedelta(minutes=30))
    with pytest.raises(ServiceError, match="EXPIRED"):
        late.claim_candidate(cid, "run-1")


def test_enter_refused_when_position_opens_after_snapshot(env):
    _, fb, _, _, svc = env
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    snap = f"snapshot:{svc.inspect_candidate(cid)['snapshot']['id']}"
    svc.claim_candidate(cid, "run-1")
    fb.add_position(Position("ETH/USD", 0.01, 2800, 28))
    with pytest.raises(ServiceError, match="position_open"):
        svc.record_decision(cid, "run-1", "ENTER", "breakout passed all three checks", [snap])


def test_enter_refused_with_unresolved_order(env):
    _, fb, _, _, svc = env
    fb.add_open_order(OpenOrder("o1", "c1", "BTC/USD", "buy", 0.001, "new"))
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    snap = f"snapshot:{svc.inspect_candidate(cid)['snapshot']['id']}"
    svc.claim_candidate(cid, "run-1")
    with pytest.raises(ServiceError, match="unresolved_order"):
        svc.record_decision(cid, "run-1", "ENTER", "breakout passed all three checks", [snap])


def test_clean_enter_is_recorded_once(env):
    conn, _, _, _, svc = env
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    snap = f"snapshot:{svc.inspect_candidate(cid)['snapshot']['id']}"
    svc.claim_candidate(cid, "run-1")
    out = svc.record_decision(cid, "run-1", "ENTER", "close > prior 20h high, > EMA200, EMA50 > EMA200", [snap])
    assert out["action"] == "ENTER"
    with pytest.raises(ServiceError):
        svc.record_decision(cid, "run-1", "ENTER", "close > prior 20h high again", [snap])
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_decision_requires_own_snapshot_evidence(env):
    svc = env[4]
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    svc.claim_candidate(cid, "run-1")
    with pytest.raises(ServiceError, match="must include"):
        svc.record_decision(cid, "run-1", "SKIP", "not convinced by the setup", ["url:https://example.com"])


def test_unclaimed_decision_refused(env):
    svc = env[4]
    cid = cycle(env)["BTC/USD"]["candidate_id"]
    snap = f"snapshot:{svc.inspect_candidate(cid)['snapshot']['id']}"
    with pytest.raises(ServiceError, match="CLAIMED"):
        svc.record_decision(cid, "run-1", "SKIP", "skipping for test purposes", [snap])


# ---------- safety ----------
def test_live_trading_true_refuses_to_start(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text((config.ROOT / "config/risk.yaml").read_text(encoding="utf-8").replace("live_trading: false", "live_trading: true"))
    with pytest.raises(config.ConfigError):
        config.load_risk(p)


def test_alpaca_missing_credentials_errors(monkeypatch):
    from terminal.broker.alpaca_paper import AlpacaPaper, BrokerConfigError

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(BrokerConfigError):
        AlpacaPaper()


def test_alpaca_client_is_pinned_to_paper(monkeypatch):
    from terminal.broker.alpaca_paper import PAPER_URL, AlpacaPaper

    monkeypatch.setenv("ALPACA_API_KEY", "PKFAKEKEY123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "fakesecret")
    b = AlpacaPaper()  # constructs without network
    assert b._trading._base_url.value == PAPER_URL


def test_mcp_exposes_only_trading_tools():
    from terminal.mcp_server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"read_account", "list_candidates", "inspect_candidate", "claim_candidate", "record_decision",
                     "preview_order", "submit_intent", "get_intent"}


def test_env_file_with_bom_and_crlf(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes("\ufeffBROKER=fake\r\nTERMINAL_DB=\"data/x.db\"\r\n".encode("utf-8"))
    monkeypatch.delenv("BROKER", raising=False)
    monkeypatch.delenv("TERMINAL_DB", raising=False)
    config._load_env_file(env)
    import os
    assert os.environ["BROKER"] == "fake" and os.environ["TERMINAL_DB"] == "data/x.db"


def test_quote_far_from_close_blocks(env):
    _, fb, *_ = env
    fb.set_quote("BTC/USD", BTC_PX * 0.6, BTC_PX * 0.6 + 10, age_s=1)    # 40% below bar close
    r = cycle(env)["BTC/USD"]
    assert any(b.startswith("quote_mismatch") for b in r["blockers"])
