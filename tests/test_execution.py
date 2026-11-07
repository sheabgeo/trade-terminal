from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from terminal import config, db
from terminal.broker.fake import FakeBroker, downtrend_bars, uptrend_breakout_bars
from terminal.collector import run_cycle
from terminal.execution import client_order_id
from terminal.service import ServiceError, TradingService
from terminal.sizing import size_position

NOW = datetime(2026, 11, 5, 10, 1, 30, tzinfo=timezone.utc)
SIGNAL_BAR = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
BTC_PX = round(uptrend_breakout_bars(SIGNAL_BAR)[-1].close, 2)
ETH_PX = round(downtrend_bars(SIGNAL_BAR)[-1].close, 2)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def ctx():
    strat, risk = config.load_strategy(), replace(config.load_risk(), execution_mode="paper")
    conn = db.connect(":memory:")
    fb = FakeBroker(now=NOW)
    fb.set_bars("BTC/USD", uptrend_breakout_bars(SIGNAL_BAR))
    fb.set_bars("ETH/USD", downtrend_bars(SIGNAL_BAR))
    fb.set_quote("BTC/USD", BTC_PX, BTC_PX + 10, age_s=2)
    fb.set_quote("ETH/USD", ETH_PX, ETH_PX + 1, age_s=2)
    clock = Clock(NOW)
    svc = TradingService(conn, fb, strat, risk, clock=clock)
    svc.sleep = lambda s: None
    run_cycle(conn, fb, strat, risk, now=NOW)
    cid = "BTC/USD|breakout-v1|2026-11-05T09:00:00Z"
    snap = f"snapshot:{svc.inspect_candidate(cid)['snapshot']['id']}"
    return conn, fb, svc, clock, cid, snap


def enter(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    svc.claim_candidate(cid, "run-1")
    svc.record_decision(cid, "run-1", "ENTER", "close > prior 20h high, > EMA200, EMA50 > EMA200", [snap])
    return svc.preview_order(cid, "run-1")


# ---------- sizing ----------
def test_article_worked_example():
    r = size_position(loss_budget_usd=0.5, entry_price=100, stop_price=98, cost_per_unit=0.6, max_position_usd=25,
                      cash_usd=1e5, qty_increment=1e-4, min_qty=1e-4)
    assert r.qty == pytest.approx(0.1923) and r.notional == pytest.approx(19.23) and r.reject is None


def test_position_cap_binds():
    r = size_position(loss_budget_usd=0.5, entry_price=100, stop_price=99.9, cost_per_unit=0.01, max_position_usd=25,
                      cash_usd=1e5, qty_increment=1e-4, min_qty=1e-4)
    assert r.limited_by == "position_cap" and r.notional <= 25


def test_below_venue_minimum_rejected():
    r = size_position(loss_budget_usd=0.5, entry_price=100, stop_price=50, cost_per_unit=0.6, max_position_usd=25,
                      cash_usd=1e5, qty_increment=1e-4, min_qty=0.5)
    assert r.reject and "below venue minimum" in r.reject


# ---------- preview ----------
def test_preview_requires_enter(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    svc.claim_candidate(cid, "run-1")
    svc.record_decision(cid, "run-1", "SKIP", "not taking this one for test", [snap])
    with pytest.raises(ServiceError, match="ENTER"):
        svc.preview_order(cid, "run-1")


def test_preview_enforces_limits(ctx):
    p = enter(ctx)
    s = p["sizing"]
    assert p["status"] == "PREVIEWED" and p["max_entry_price"] >= BTC_PX + 10
    assert s["notional_usd"] <= 25 + 1e-6
    assert s["planned_loss_usd"] <= 0.5 + 1e-6
    assert p["planned_stop"] < p["max_entry_price"] < p["planned_target"]
    assert p["client_order_id"] == client_order_id(ctx[4])


def test_preview_refuses_stale_quote(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    svc.claim_candidate(cid, "run-1")
    svc.record_decision(cid, "run-1", "ENTER", "close > prior 20h high, > EMA200, EMA50 > EMA200", [snap])
    fb.set_quote("BTC/USD", BTC_PX, BTC_PX + 10, age_s=30)
    with pytest.raises(ServiceError, match="stale_quote"):
        svc.preview_order(cid, "run-1")


# ---------- submit / reconcile ----------
def test_full_fill_opens_position_and_repeat_is_noop(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    p = enter(ctx)
    r = svc.submit_intent(p["intent_id"], "run-1")
    assert r["status"] == "FILLED" and fb.submit_calls == 1
    pos = r["position"]
    assert pos and pos["qty"] < r["broker"]["filled_qty"]   # fee taken in BTC → less sellable
    again = svc.submit_intent(p["intent_id"], "run-1")
    assert "no second order" in again["note"] and fb.submit_calls == 1
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_partial_fill_tracks_actual_qty(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    fb.fill_ratio = 0.4
    p = enter(ctx)
    r = svc.submit_intent(p["intent_id"], "run-1")
    assert r["status"] == "PARTIAL"
    assert r["position"]["qty"] <= r["broker"]["filled_qty"] < p["qty"]


def test_no_fill_creates_no_position(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    p = enter(ctx)
    fb.set_quote("BTC/USD", BTC_PX * 1.01, BTC_PX * 1.01 + 50, age_s=1)   # price ran 1% past our max entry
    r = svc.submit_intent(p["intent_id"], "run-1")
    assert r["status"] == "NO_FILL" and r["position"] is None


def test_timeout_after_send_is_recovered_from_broker(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    fb.fail_mode = "after_send"
    p = enter(ctx)
    r = svc.submit_intent(p["intent_id"], "run-1")
    assert r["status"] == "FILLED" and r["position"]          # broker had it; we adopted it
    assert "TimeoutError" in r["broker"]["error"]
    fb.fail_mode = None
    svc.submit_intent(p["intent_id"], "run-1")
    assert fb.submit_calls == 1


def test_timeout_before_send_marks_not_sent(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    fb.fail_mode = "before_send"
    p = enter(ctx)
    r = svc.submit_intent(p["intent_id"], "run-1")
    assert r["status"] == "NOT_SENT" and r["position"] is None


def test_expired_preview_cannot_submit(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    p = enter(ctx)
    clock.t = NOW + timedelta(seconds=120)
    with pytest.raises(ServiceError, match="expired"):
        svc.submit_intent(p["intent_id"], "run-1")
    assert fb.submit_calls == 0


def test_other_owner_cannot_submit(ctx):
    p = enter(ctx)
    with pytest.raises(ServiceError, match="belongs to"):
        ctx[2].submit_intent(p["intent_id"], "intruder")


def test_open_position_blocks_next_candidate(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    p = enter(ctx)
    svc.submit_intent(p["intent_id"], "run-1")
    later = NOW + timedelta(hours=1)
    fb.now = later
    fb.set_quote("ETH/USD", ETH_PX, ETH_PX + 1, age_s=1)
    fb.set_quote("BTC/USD", BTC_PX, BTC_PX + 10, age_s=1)
    res = {r["symbol"]: r for r in run_cycle(conn, fb, svc.strat, svc.risk, now=later)}
    assert any(b.startswith("position_open") for b in res["BTC/USD"]["blockers"])


def test_dust_does_not_block(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    from terminal.broker.base import Position
    fb.add_position(Position("BTC/USD", 0.00000005, 61_000, 0.003))   # below 1.317e-05 min
    acct = svc.read_account()
    assert acct["strategy_positions"] == [] and len(acct["dust_positions"]) == 1


def test_below_min_notional_rejected_in_sizing():
    r = size_position(loss_budget_usd=0.5, entry_price=75_000, stop_price=74_000, cost_per_unit=400, max_position_usd=2.5,
                      cash_usd=1e5, qty_increment=1e-9, min_qty=1.317e-05, min_notional_usd=10)
    assert r.reject and "below venue minimum $10" in r.reject


def test_broker_4xx_is_rejected_not_unknown(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    p = enter(ctx)
    conn.execute("UPDATE intents SET qty=0.0001 WHERE id=?", (p["intent_id"],))   # force a ~$6 order past sizing
    r = svc.submit_intent(p["intent_id"], "run-1")
    assert r["status"] == "REJECTED" and "minimal amount" in r["broker"]["error"]
    assert svc.submit_intent(p["intent_id"], "run-1")["status"] == "REJECTED" and fb.submit_calls == 1


def test_rejected_enter_does_not_block_next_enter(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    p = enter(ctx)
    conn.execute("UPDATE intents SET qty=0.0001 WHERE id=?", (p["intent_id"],))
    assert svc.submit_intent(p["intent_id"], "run-1")["status"] == "REJECTED"
    # a second candidate in the same window must not be blocked by the rejected one
    snap_id = int(snap.split(":")[1])
    cid2 = "BTC/USD|breakout-v1|second"
    conn.execute("INSERT INTO candidates (id, symbol, snapshot_id, created_at, expires_at, status) VALUES (?,?,?,?,?, 'PENDING')",
                 (cid2, "BTC/USD", snap_id, "2026-11-05T10:01:00Z", "2026-11-05T10:15:00Z"))
    svc.claim_candidate(cid2, "run-2")
    assert svc.record_decision(cid2, "run-2", "ENTER", "close > prior 20h high, > EMA200, EMA50 > EMA200", [snap])["action"] == "ENTER"


def test_in_flight_enter_still_blocks(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    enter(ctx)   # PREVIEWED, not yet submitted
    snap_id = int(snap.split(":")[1])
    cid2 = "BTC/USD|breakout-v1|second"
    conn.execute("INSERT INTO candidates (id, symbol, snapshot_id, created_at, expires_at, status) VALUES (?,?,?,?,?, 'PENDING')",
                 (cid2, "BTC/USD", snap_id, "2026-11-05T10:01:00Z", "2026-11-05T10:15:00Z"))
    svc.claim_candidate(cid2, "run-2")
    with pytest.raises(ServiceError, match="in progress"):
        svc.record_decision(cid2, "run-2", "ENTER", "close > prior 20h high, > EMA200, EMA50 > EMA200", [snap])


# ---------- shadow mode ----------
def test_shadow_mode_records_decision_but_never_orders(ctx):
    conn, fb, svc, clock, cid, snap = ctx
    svc.risk = replace(svc.risk, execution_mode="shadow")
    svc.claim_candidate(cid, "run-1")
    svc.record_decision(cid, "run-1", "ENTER", "close > prior 20h high, > EMA200, EMA50 > EMA200", [snap])
    with pytest.raises(ServiceError, match="SHADOW MODE"):
        svc.preview_order(cid, "run-1")
    assert fb.submit_calls == 0
    assert conn.execute("SELECT action FROM decisions WHERE candidate_id=?", (cid,)).fetchone()[0] == "ENTER"


def test_default_config_is_shadow():
    assert config.load_risk().execution_mode == "shadow"
