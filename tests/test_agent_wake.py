from terminal import agent_wake, db

CFG = {"enabled": True, "command": "sh", "timeout_seconds": 300, "max_turns": 25,
       "allowed_tools": ["mcp__trading", "Skill", "Read"],
       "disallowed_tools": ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"]}


def test_command_is_locked_down():
    cmd = agent_wake.build_command(CFG, "BTC/USD|breakout-v1|2026-11-05T09:00:00Z", "agent-x")
    assert cmd[:2] == ["timeout", "300"]
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__trading,Skill,Read"
    assert "Bash" in cmd[cmd.index("--disallowedTools") + 1]
    assert "BTC/USD|breakout-v1|2026-11-05T09:00:00Z" in cmd[cmd.index("-p") + 1]


def test_wake_starts_background_run_and_logs(tmp_path, monkeypatch):
    conn = db.connect(":memory:")
    started = {}

    class P:
        pid = 4242

    def fake_popen(cmd, **kw):
        started["cmd"], started["kw"] = cmd, kw
        return P()

    monkeypatch.setattr(agent_wake.config, "ROOT", tmp_path)
    (tmp_path / ".mcp.json").write_text("{}")
    r = agent_wake.make_waker(conn, CFG, popen=fake_popen)("BTC/USD|breakout-v1|t")
    assert r["started"] and started["kw"]["start_new_session"] is True
    import subprocess
    assert started["kw"]["stdin"] is subprocess.DEVNULL
    ev = conn.execute("SELECT kind FROM events").fetchone()[0]
    assert ev == "agent_wake"


def test_disabled_or_missing_binary_does_not_start():
    conn = db.connect(":memory:")
    assert not agent_wake.make_waker(conn, {**CFG, "enabled": False})("c")["started"]
    assert not agent_wake.make_waker(conn, {**CFG, "command": "definitely-not-installed-xyz"})("c")["started"]


def test_shipped_config_is_disabled_and_locked():
    cfg = agent_wake.load()
    assert cfg["enabled"] is False
    assert set(cfg["allowed_tools"]) == {"mcp__trading", "Skill", "Read"}
