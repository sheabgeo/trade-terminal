"""Wake a headless Claude run for a new candidate (article §8: event-driven, no empty polling).

The run can ONLY use the trading MCP tools (+ read files / load the skill). It is started in the background so the
collector never waits on it; output goes to data/agent_runs/<time>_<symbol>.json and an event row.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config, db


def load() -> dict:
    return yaml.safe_load((config.ROOT / "config/agent.yaml").read_text(encoding="utf-8"))


def build_command(cfg: dict, candidate_id: str, owner: str) -> list[str]:
    prompt = (f"Use the review-candidate skill to review candidate {candidate_id}. "
              f"Your owner id is {owner}. Follow the skill exactly and finish with its final message.")
    return [
        "timeout", str(cfg["timeout_seconds"]),
        cfg["command"], "-p", prompt,
        "--mcp-config", str(config.ROOT / ".mcp.json"), "--strict-mcp-config",
        "--allowedTools", ",".join(cfg["allowed_tools"]),
        "--disallowedTools", ",".join(cfg["disallowed_tools"]),
        "--permission-mode", "dontAsk",
        "--max-turns", str(cfg["max_turns"]),
        "--output-format", "json",
    ] + (["--model", cfg["model"]] if cfg.get("model") else [])


def make_waker(conn: sqlite3.Connection, cfg: dict | None = None, popen=subprocess.Popen):
    cfg = cfg or load()

    def wake(candidate_id: str) -> dict:
        now = datetime.now(timezone.utc)
        if not cfg.get("enabled"):
            db.log_event(conn, now.isoformat(), "agent_disabled", {"candidate_id": candidate_id})
            return {"started": False, "reason": "agent disabled in config/agent.yaml"}
        if not shutil.which(cfg["command"]):
            db.log_event(conn, now.isoformat(), "agent_missing", {"candidate_id": candidate_id})
            return {"started": False, "reason": f"'{cfg['command']}' not found on PATH"}
        owner = f"agent-{uuid.uuid4().hex[:8]}"
        out_dir = config.ROOT / "data/agent_runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        log = out_dir / f"{now:%Y%m%dT%H%M%S}_{candidate_id.split('|')[0].replace('/', '-')}_{owner}.json"
        with log.open("w") as f:
            proc = popen(build_command(cfg, candidate_id, owner), cwd=config.ROOT, stdin=subprocess.DEVNULL,
                         stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        db.log_event(conn, now.isoformat(), "agent_wake",
                     {"candidate_id": candidate_id, "owner": owner, "pid": proc.pid, "log": str(log.relative_to(config.ROOT))})
        return {"started": True, "owner": owner, "log": str(log)}

    return wake
