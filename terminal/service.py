"""Trading service: the ONLY way the Bot touches records. Enforces rules in code, not prompts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from mcp.server.mcpserver.exceptions import ToolError

from . import config, db
from .broker.base import Broker
from .collector import account_state, expire_candidates, iso
from .execution import ExecutionMixin


class ServiceError(ToolError, ValueError):
    """Raised for any request the rules refuse. ToolError → message reaches the Bot verbatim.
    Any other exception is a crash and its text stays server-side."""


class TradingService(ExecutionMixin):
    def __init__(self, conn: sqlite3.Connection, broker: Broker, strat: config.Strategy, risk: config.Risk,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self.conn, self.broker, self.strat, self.risk, self.clock = conn, broker, strat, risk, clock

    # ---- read tools ----------------------------------------------------------
    def read_account(self) -> dict:
        acct = account_state(self.broker, self.risk, self.strat.symbols)
        acct["checked_at"] = iso(self.clock())
        acct["live_trading"] = False
        acct["execution_mode"] = self.risk.execution_mode
        return acct

    def list_candidates(self) -> list[dict]:
        expire_candidates(self.conn, self.clock())
        rows = self.conn.execute(
            "SELECT id, symbol, status, owner, expires_at FROM candidates WHERE status IN ('PENDING','CLAIMED') ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def inspect_candidate(self, candidate_id: str) -> dict:
        c = self._candidate(candidate_id)
        s = self.conn.execute("SELECT * FROM snapshots WHERE id=?", (c["snapshot_id"],)).fetchone()
        q = self.conn.execute("SELECT * FROM quotes WHERE id=?", (s["quote_id"],)).fetchone() if s["quote_id"] else None
        return {
            "candidate": dict(c),
            "snapshot": {
                "id": s["id"],
                "symbol": s["symbol"],
                "strategy_version": s["strategy_version"],
                "completed_bar_ts": s["bar_ts"],
                "source": s["source"],
                "created_at": s["created_at"],
                "quote": dict(q) if q else None,
                "quote_age_s_at_snapshot": s["quote_age_s"],
                "indicators": json.loads(s["indicators"]),
                "account": json.loads(s["account"]),
                "blockers": json.loads(s["blockers"]),
            },
            "rule_limits": {
                "allocation_usd": self.risk.allocation_usd,
                "max_position_usd": self.risk.allocation_usd * self.risk.max_position_pct / 100,
                "loss_budget_usd": self.risk.allocation_usd * self.risk.loss_budget_pct / 100,
                "quote_max_age_s": self.risk.quote_max_age_seconds,
            },
        }

    # ---- queue -------------------------------------------------------------------
    def claim_candidate(self, candidate_id: str, owner: str) -> dict:
        now = iso(self.clock())
        expire_candidates(self.conn, self.clock())
        updated = self.conn.execute(
            "UPDATE candidates SET status='CLAIMED', owner=?, claimed_at=? WHERE id=? AND status='PENDING' AND expires_at > ?",
            (owner, now, candidate_id, now),
        ).rowcount
        if not updated:
            c = self._candidate(candidate_id)
            if c["status"] == "CLAIMED" and c["owner"] == owner:
                return {"claimed": True, "candidate_id": candidate_id, "note": "already yours"}
            raise ServiceError(f"cannot claim {candidate_id}: status={c['status']} owner={c['owner']}. Finish without duplicating work.")
        db.log_event(self.conn, now, "candidate_claimed", {"candidate_id": candidate_id, "owner": owner})
        return {"claimed": True, "candidate_id": candidate_id}

    # ---- decision -------------------------------------------------------------
    def record_decision(self, candidate_id: str, owner: str, action: str, reason: str, evidence_ids: list[str]) -> dict:
        now_dt = self.clock()
        now = iso(now_dt)
        action = action.upper().strip()
        if action not in ("ENTER", "SKIP"):
            raise ServiceError("action must be ENTER or SKIP")
        if len(reason.strip()) < 15:
            raise ServiceError("reason must name the specific rule and evidence (min 15 chars)")
        if not evidence_ids:
            raise ServiceError("evidence_ids required (at least the snapshot id, e.g. 'snapshot:12')")

        expire_candidates(self.conn, now_dt)
        c = self._candidate(candidate_id)
        if c["status"] != "CLAIMED" or c["owner"] != owner:
            raise ServiceError(f"candidate must be CLAIMED by {owner}; it is {c['status']} (owner={c['owner']})")
        self._validate_evidence(evidence_ids, c["snapshot_id"])

        if action == "ENTER":
            failures = self.entry_blockers(candidate_id)
            if failures:
                raise ServiceError("ENTER refused — failed checks cannot be overridden: " + "; ".join(failures)
                                   + ". Record SKIP with these reasons.")

        self.conn.execute(
            "INSERT INTO decisions (candidate_id, action, reason, evidence, owner, decided_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (candidate_id, action, reason.strip(), json.dumps(evidence_ids), owner, now, c["expires_at"]),
        )
        self.conn.execute("UPDATE candidates SET status='DECIDED' WHERE id=?", (candidate_id,))
        db.log_event(self.conn, now, "decision", {"candidate_id": candidate_id, "action": action})
        note = "Next: preview_order(candidate_id, owner), then submit_intent(intent_id, owner)." if action == "ENTER" else ""
        return {"recorded": True, "candidate_id": candidate_id, "action": action, "expires_at": c["expires_at"], "note": note}

    def entry_blockers(self, candidate_id: str) -> list[str]:
        """Blockers saved at snapshot time PLUS a fresh account re-check now."""
        c = self._candidate(candidate_id)
        s = self.conn.execute("SELECT blockers FROM snapshots WHERE id=?", (c["snapshot_id"],)).fetchone()
        out = list(json.loads(s["blockers"]))
        live = account_state(self.broker, self.risk, self.strat.symbols)
        if live["strategy_positions"] and not any(b.startswith("position_open") for b in out):
            out.append("position_open: one-position rule (re-checked now)")
        if live["unresolved_orders"] and not any(b.startswith("unresolved_order") for b in out):
            out.append("unresolved_order: open order exists (re-checked now)")
        # Another ENTER only blocks while its order could still become a position. Settled-without-fill
        # intents (REJECTED, NOT_SENT, NO_FILL, EXPIRED) don't; filled ones show up as broker positions above.
        other = self.conn.execute(
            """SELECT d.candidate_id FROM decisions d LEFT JOIN intents i ON i.candidate_id = d.candidate_id
               WHERE d.action='ENTER' AND d.expires_at > ? AND d.candidate_id != ?
                 AND (i.id IS NULL OR i.status IN ('PREVIEWED','SUBMITTING','SUBMITTED','UNKNOWN'))""",
            (iso(self.clock()), candidate_id),
        ).fetchone()
        if other:
            out.append(f"one_position: another ENTER is in progress ({other['candidate_id']})")
        return out

    # ---- helpers ------------------------------------------------------------
    def _candidate(self, candidate_id: str) -> sqlite3.Row:
        c = self.conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not c:
            raise ServiceError(f"unknown candidate {candidate_id}")
        return c

    def _validate_evidence(self, evidence_ids: list[str], snapshot_id: int) -> None:
        if f"snapshot:{snapshot_id}" not in evidence_ids:
            raise ServiceError(f"evidence_ids must include the candidate's own snapshot: 'snapshot:{snapshot_id}'")
        for e in evidence_ids:
            kind, _, ident = e.partition(":")
            if kind == "snapshot":
                if not self.conn.execute("SELECT 1 FROM snapshots WHERE id=?", (ident,)).fetchone():
                    raise ServiceError(f"evidence {e} does not exist")
            elif kind != "url":
                raise ServiceError(f"evidence must be 'snapshot:<id>' or 'url:<link>' — got {e}")
