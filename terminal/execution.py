"""Stage 4: order preview → submit → reconcile. The Bot decides; this code decides HOW MUCH.

Lifecycle of an entry intent (one per candidate):
  PREVIEWED ──submit──▶ SUBMITTING ──broker ok──▶ SUBMITTED ──reconcile──▶ FILLED | PARTIAL | NO_FILL
                              └──timeout/error──▶ UNKNOWN ──reconcile──▶ (found → as above) | NOT_SENT
                              └──broker 4xx──────▶ REJECTED (nothing placed)
  PREVIEWED ──ttl passes──▶ EXPIRED
Rules:
  * client_order_id is derived from the candidate id and saved BEFORE the broker call.
  * Status flips PREVIEWED→SUBMITTING atomically, so a repeat submit can never send a second order.
  * After a timeout we ask the broker what happened before doing anything else.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timedelta

from . import db
from .broker.base import TERMINAL_STATUSES, OrderNotFound
from .collector import account_state, iso
from .sizing import size_position

SETTLED = {"FILLED", "PARTIAL", "NO_FILL", "NOT_SENT", "REJECTED", "EXPIRED"}


def is_definite_rejection(e: Exception) -> bool:
    """4xx from the broker = it refused the order; nothing was placed. Timeouts / 5xx / 429 stay UNKNOWN."""
    code = getattr(e, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)
NEEDS_RECONCILE = {"SUBMITTING", "SUBMITTED", "UNKNOWN"}


def client_order_id(candidate_id: str, leg: str = "entry") -> str:
    return "tt-" + hashlib.sha256(f"{candidate_id}|{leg}".encode()).hexdigest()[:24]


def ceil_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class ExecutionMixin:
    """Mixed into TradingService; uses self.conn, self.broker, self.strat, self.risk, self.clock."""

    sleep = staticmethod(time.sleep)
    poll_attempts = 5

    # ---------------------------------------------------------------- preview
    def preview_order(self, candidate_id: str, owner: str) -> dict:
        from .service import ServiceError

        now = self.clock()
        if self.risk.execution_mode == "shadow":
            db.log_event(self.conn, iso(now), "shadow_no_order", {"candidate_id": candidate_id})
            raise ServiceError("SHADOW MODE: your decision is recorded; no order is placed. Report your decision and stop.")
        c = self._candidate(candidate_id)
        d = self.conn.execute("SELECT * FROM decisions WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not d or d["action"] != "ENTER":
            raise ServiceError("preview requires a recorded ENTER decision for this candidate")
        if d["owner"] != owner:
            raise ServiceError(f"decision belongs to {d['owner']}, not {owner}")
        existing = self.conn.execute("SELECT * FROM intents WHERE candidate_id=?", (candidate_id,)).fetchone()
        if existing and existing["status"] != "PREVIEWED":
            return {"note": "intent already past preview — no new order possible for this candidate",
                    **self._intent_view(existing)}
        if _parse(d["expires_at"]) <= now:
            raise ServiceError(f"decision expired at {d['expires_at']} — candidates are not revived")

        live = account_state(self.broker, self.risk, self.strat.symbols)
        if live["strategy_positions"] or live["unresolved_orders"]:
            raise ServiceError("one-position rule: a strategy position or open order exists")

        q = self.broker.latest_quote(c["symbol"])
        age = (now - q.ts).total_seconds()
        if age < -2:
            raise ServiceError(f"clock_skew: quote is {-age:.1f}s in the future — host clock is wrong; fix before trading")
        if age > self.risk.quote_max_age_seconds:
            raise ServiceError(f"stale_quote at preview: {age:.1f}s > {self.risk.quote_max_age_seconds}s — try again shortly")
        if q.ask <= 0 or q.bid <= 0 or q.ask < q.bid:
            raise ServiceError(f"bad quote bid={q.bid} ask={q.ask}")

        snap = self.conn.execute("SELECT indicators, bar_ts FROM snapshots WHERE id=?", (c["snapshot_id"],)).fetchone()
        ind = json.loads(snap["indicators"])
        atr_val = ind[f"atr_{self.strat.atr_period}"]
        asset = self.broker.asset(c["symbol"])

        fee = self.risk.fee_rate_bps / 10_000
        limit = ceil_cents(q.ask * (1 + self.risk.entry_slippage_bps / 10_000))
        stop = round(limit - self.strat.stop_atr_mult * atr_val, 2)
        target = round(limit + self.strat.target_atr_mult * atr_val, 2)
        cost_per_unit = limit * fee * 2 + (q.ask - q.bid) + (limit - q.ask)   # fees both sides + spread + slippage
        alloc = self.risk.allocation_usd
        sz = size_position(
            loss_budget_usd=alloc * self.risk.loss_budget_pct / 100,
            entry_price=limit,
            stop_price=stop,
            cost_per_unit=cost_per_unit,
            max_position_usd=alloc * self.risk.max_position_pct / 100,
            cash_usd=min(live["account_cash"], alloc),
            qty_increment=asset.min_trade_increment,
            min_qty=asset.min_order_size,
            min_notional_usd=self.risk.min_notional_usd,
        )
        sizing = {
            "quote": {"bid": q.bid, "ask": q.ask, "age_s": round(age, 3)},
            "atr": atr_val,
            "fee_rate_bps": self.risk.fee_rate_bps,
            "est_cost_per_unit": round(cost_per_unit, 4),
            "risk_per_unit": round(sz.risk_per_unit, 4),
            "qty": sz.qty,
            "notional_usd": round(sz.notional, 4),
            "planned_loss_usd": round(sz.planned_loss_usd, 4),
            "est_round_trip_fees_usd": round(sz.notional * fee * 2, 4),
            "limited_by": sz.limited_by,
            "limits": {
                "loss_budget_usd": alloc * self.risk.loss_budget_pct / 100,
                "max_position_usd": alloc * self.risk.max_position_pct / 100,
                "venue_min_qty": asset.min_order_size,
                "venue_min_notional_usd": self.risk.min_notional_usd,
                "qty_increment": asset.min_trade_increment,
            },
        }
        if sz.reject:
            db.log_event(self.conn, iso(now), "preview_rejected", {"candidate_id": candidate_id, "reason": sz.reject})
            raise ServiceError(f"preview rejected: {sz.reject}")

        bar_close = _parse(snap["bar_ts"]) + timedelta(hours=1)
        time_exit = iso(bar_close + timedelta(hours=self.strat.time_exit_bars))
        expires = iso(now + timedelta(seconds=self.risk.intent_ttl_seconds))
        coid = client_order_id(candidate_id)
        if existing:   # re-price a still-unsent preview, same client_order_id
            self.conn.execute(
                """UPDATE intents SET qty=?, limit_price=?, stop_price=?, target_price=?, sizing=?, expires_at=?, updated_at=?
                   WHERE id=? AND status='PREVIEWED'""",
                (sz.qty, limit, stop, target, json.dumps(sizing), expires, iso(now), existing["id"]),
            )
            intent_id = existing["id"]
        else:
            intent_id = self.conn.execute(
                """INSERT INTO intents (candidate_id, client_order_id, owner, symbol, side, qty, limit_price, stop_price,
                       target_price, time_exit_at, sizing, created_at, expires_at, status, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'PREVIEWED',?)""",
                (candidate_id, coid, owner, c["symbol"], "buy", sz.qty, limit, stop, target, time_exit,
                 json.dumps(sizing), iso(now), expires, iso(now)),
            ).lastrowid
        db.log_event(self.conn, iso(now), "preview", {"intent_id": intent_id, "qty": sz.qty, "limit": limit})
        return self._intent_view(self._intent(intent_id))

    # ----------------------------------------------------------------- submit
    def submit_intent(self, intent_id: int, owner: str) -> dict:
        from .service import ServiceError

        now = self.clock()
        it = self._intent(intent_id)
        if it["owner"] != owner:
            raise ServiceError(f"intent belongs to {it['owner']}, not {owner}")
        if it["status"] != "PREVIEWED":
            # Idempotent: a repeat submit only reports/reconciles the existing order.
            view = self.reconcile_intent(intent_id)
            view["note"] = "already submitted — no second order was sent"
            return view
        if _parse(it["expires_at"]) <= now:
            self._set(intent_id, status="EXPIRED")
            raise ServiceError("preview expired — request a fresh preview (same order id is kept)")
        live = account_state(self.broker, self.risk, self.strat.symbols)
        if live["strategy_positions"] or live["unresolved_orders"]:
            raise ServiceError("one-position rule: a strategy position or open order exists")

        # Write-ahead: claim the send atomically. If another run got here first, rowcount is 0.
        if not self.conn.execute(
            "UPDATE intents SET status='SUBMITTING', updated_at=? WHERE id=? AND status='PREVIEWED'", (iso(now), intent_id)
        ).rowcount:
            return {"note": "another run is submitting this intent", **self._intent_view(self._intent(intent_id))}
        db.log_event(self.conn, iso(now), "submit_attempt", {"intent_id": intent_id, "client_order_id": it["client_order_id"]})
        try:
            o = self.broker.submit_limit_ioc(it["symbol"], "buy", it["qty"], it["limit_price"], it["client_order_id"])
            self._set(intent_id, status="SUBMITTED", broker_order_id=o.id, broker_status=o.status)
        except Exception as e:  # noqa: BLE001
            if is_definite_rejection(e):
                self._set(intent_id, status="REJECTED", error=f"{type(e).__name__}: {e}")
                db.log_event(self.conn, iso(self.clock()), "submit_rejected", {"intent_id": intent_id, "error": str(e)})
                return self._intent_view(self._intent(intent_id))
            # The order may or may not have reached the broker → ask before doing anything else.
            self._set(intent_id, status="UNKNOWN", error=f"{type(e).__name__}: {e}")
            db.log_event(self.conn, iso(self.clock()), "submit_uncertain", {"intent_id": intent_id, "error": str(e)})
        return self.reconcile_intent(intent_id)

    # -------------------------------------------------------------- reconcile
    def reconcile_intent(self, intent_id: int) -> dict:
        it = self._intent(intent_id)
        if it["status"] not in NEEDS_RECONCILE:
            return self._intent_view(it)
        for attempt in range(self.poll_attempts):
            try:
                o = self.broker.get_order_by_client_id(it["client_order_id"])
            except OrderNotFound:
                if attempt < self.poll_attempts - 1:
                    self.sleep(1)
                    continue
                if it["status"] in ("UNKNOWN", "SUBMITTING"):
                    self._set(intent_id, status="NOT_SENT")
                    db.log_event(self.conn, iso(self.clock()), "reconciled_not_sent", {"intent_id": intent_id})
                return self._intent_view(self._intent(intent_id))
            self._set(intent_id, broker_order_id=o.id, broker_status=o.status,
                      filled_qty=o.filled_qty, filled_avg_price=o.filled_avg_price)
            if o.status in TERMINAL_STATUSES:
                if o.filled_qty <= 0:
                    status = "NO_FILL"
                elif o.filled_qty >= o.qty - 1e-12:
                    status = "FILLED"
                else:
                    status = "PARTIAL"
                self._set(intent_id, status=status)
                if status in ("FILLED", "PARTIAL"):
                    self._open_position(intent_id)
                db.log_event(self.conn, iso(self.clock()), "reconciled", {"intent_id": intent_id, "status": status,
                                                                         "filled_qty": o.filled_qty})
                break
            self._set(intent_id, status="SUBMITTED")
            self.sleep(1)
        return self._intent_view(self._intent(intent_id))

    def get_intent(self, intent_id: int) -> dict:
        return self.reconcile_intent(intent_id)

    # ---------------------------------------------------------------- helpers
    def _open_position(self, intent_id: int) -> None:
        it = self._intent(intent_id)
        if self.conn.execute("SELECT 1 FROM positions WHERE intent_id=?", (intent_id,)).fetchone():
            return
        held = sum(p.qty for p in self.broker.positions() if p.symbol == it["symbol"])
        sellable = min(it["filled_qty"], held) if held > 0 else it["filled_qty"]   # fee may be taken in the asset
        self.conn.execute(
            """INSERT INTO positions (intent_id, symbol, qty, entry_price, stop_price, target_price, time_exit_at, opened_at, status)
               VALUES (?,?,?,?,?,?,?,?, 'OPEN')""",
            (intent_id, it["symbol"], sellable, it["filled_avg_price"], it["stop_price"], it["target_price"],
             it["time_exit_at"], iso(self.clock())),
        )

    def _intent(self, intent_id: int):
        from .service import ServiceError

        it = self.conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        if not it:
            raise ServiceError(f"unknown intent {intent_id}")
        return it

    def _set(self, intent_id: int, **fields) -> None:
        fields["updated_at"] = iso(self.clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE intents SET {cols} WHERE id=?", (*fields.values(), intent_id))

    def _intent_view(self, it) -> dict:
        pos = self.conn.execute("SELECT * FROM positions WHERE intent_id=?", (it["id"],)).fetchone()
        return {
            "intent_id": it["id"],
            "candidate_id": it["candidate_id"],
            "client_order_id": it["client_order_id"],
            "status": it["status"],
            "symbol": it["symbol"],
            "side": it["side"],
            "qty": it["qty"],
            "max_entry_price": it["limit_price"],
            "planned_stop": it["stop_price"],
            "planned_target": it["target_price"],
            "time_exit_at": it["time_exit_at"],
            "expires_at": it["expires_at"],
            "sizing": json.loads(it["sizing"]),
            "broker": {"order_id": it["broker_order_id"], "status": it["broker_status"],
                       "filled_qty": it["filled_qty"], "filled_avg_price": it["filled_avg_price"], "error": it["error"]},
            "position": dict(pos) if pos else None,
        }
