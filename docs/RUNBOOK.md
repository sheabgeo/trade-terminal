# RUNBOOK

Exact commands to install, verify and operate the terminal. Everything is **paper-only**. Status marks:
✅ verified (in the build sandbox or on the author's paper account, 2026-11-07) · ⏳ written but not yet run on a real host.

- [1. Prerequisites](#1-prerequisites)
- [2. Install (any Ubuntu 24.04 host)](#2-install-any-ubuntu-2404-host)
- [3. Verify](#3-verify)
- [4. Historical tests](#4-historical-tests)
- [5. Always-on collector + Claude agent (shadow mode)](#5-always-on-collector--claude-agent-shadow-mode)
- [6. Hosting](#6-hosting)
- [7. Operations](#7-operations)
- [8. Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites
- **Alpaca paper account** → dashboard switched to **Paper** → API Keys → Generate. Paper key IDs start with `PK`
  (26 characters as of 2026-11). Copy the secret immediately: it is shown once.
- **Ubuntu 24.04** host with Python 3.12 (a cloud VM or Cloud Shell; see [Hosting](#6-hosting)).
- **Claude Code** (only for the agent): `curl -fsSL https://claude.ai/install.sh | bash`.

## 2. Install (any Ubuntu 24.04 host)
```bash
unzip trade-terminal.zip && cd trade-terminal        # or: git clone <your repo> && cd trade-terminal
bash deploy/setup_vm.sh       # swap on small VMs, apt packages, UTC clock, venv, pinned deps, runs the test suite
nano .env                     # paste PAPER key + secret; Ctrl+O Enter Ctrl+X
```
No sudo / restricted host (e.g. a cloud shell)? Use `uv` for a user-space Python 3.12:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv venv --python 3.12 .venv && ~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
```

## 3. Verify
| Step | Command | Expect |
|---|---|---|
| Tests (offline, fake broker) ✅ | `.venv/bin/python -m pytest -q` | `73 passed` |
| Read-only connection ✅ | `.venv/bin/python scripts/check_connection.py` | `"environment": "paper"`, account `ACTIVE`, BTC/ETH min sizes, fresh quotes. **Places nothing.** |
| One collector cycle ✅ | `.venv/bin/python -m terminal.collector --once` | one JSON line per symbol; `signal: false` most hours is normal |
| Paper order cycle ✅ | `.venv/bin/python scripts/paper_smoke_test.py --yes` | buys ≈ $12.50 BTC on paper, repeats the submit (same order id = no duplicate), sells back; `"PASS": true` |

The smoke test uses its own throwaway database (`data/smoke.db`) and forces `execution_mode: paper` for itself only.

## 4. Historical tests
All tests reuse the live indicator and sizing code. Results are appended to `data/backtest_runs.jsonl`.
```bash
# Crypto (hourly data since 2022, ~2–4 min, resumable)
.venv/bin/python scripts/fetch_history.py
.venv/bin/python -m terminal.backtest --timeframes 1h,4h,1d
.venv/bin/python -m terminal.backtest --timeframes 1h,4h,1d --allocation 1000   # diagnostic: isolates the $10 minimum

# S1 — 14 ETFs, daily, split/dividend-adjusted, since 2016 (~1 min)
.venv/bin/python scripts/fetch_stocks.py
.venv/bin/python -m terminal.backtest_stocks
```
`--final` evaluates the held-out final period. **Run it once, only for a frozen rule.** It has never been run in this
project because no rule passed.

## 5. Always-on collector + Claude agent (shadow mode)
Only worth running once a rule passes its historical test (none has). Defaults are safe: `execution_mode: shadow`
(Claude decides, no orders) and `config/agent.yaml enabled: false`.
```bash
curl -fsSL https://claude.ai/install.sh | bash
claude                          # sign in once (subscription or API key), then /exit
nano config/agent.yaml          # enabled: true
bash deploy/install_service.sh  # systemd unit for THIS user/folder; starts at boot, restarts on crash
journalctl -u trade-collector -f
ls data/agent_runs/             # one JSON transcript per agent review
```
Agent command (verified ✅ with Claude Code 2.1.319 against the fake broker): headless `-p`; only the trading MCP server
(`--mcp-config .mcp.json --strict-mcp-config`); tools `mcp__trading,Skill,Read`; everything else denied
(`--permission-mode dontAsk`); 25 turns max; 5-minute hard timeout. Measured cost ≈ $0.11–0.22 per review (Sonnet, API billing).

To let shadow decisions become paper orders: set `execution_mode: paper` in `config/risk.yaml` and restart the service.
**Before that, build the position supervisor** (see README → Not built). Without it, a filled position has a planned stop/target that
nothing executes.

## 6. Hosting
**Google Cloud (used)** ✅: e2-small, us-central1-a, ≈ $13/month incl. 30 GB disk. In Google Cloud Shell:
```bash
bash trade-terminal/deploy/gcp_create_vm.sh                      # enables Compute API, picks Ubuntu 24.04 x86, creates VM
gcloud compute scp ~/trade-terminal.zip trade-terminal:~ --zone us-central1-a
gcloud compute ssh trade-terminal --zone us-central1-a
gcloud compute instances stop trade-terminal --zone us-central1-a    # pay only for the disk (~$3/mo) while idle
```
Run `gcloud` commands in **Cloud Shell** (`@cloudshell` prompt), not on the VM (`@trade-terminal`). The VM has no
permission to manage cloud resources by design.

**Oracle Cloud (alternative)**: `deploy/oci_create_vm.sh` builds the network and tries Always-Free Ampere A1, then
E2.1.Micro, across all availability domains. In us-chicago-1 (2026-11-07): A1 was out of capacity everywhere and Micro was not offered.
OCI Cloud Shell is FIPS-mode: use `ssh-keygen -t rsa -b 4096`, not ed25519.

**Testing without a VM**: OCI or Google Cloud Shell can run everything in sections 2–4 (not the always-on service).

## 7. Operations
| Task | Command |
|---|---|
| Service status / logs | `systemctl status trade-collector` · `journalctl -u trade-collector -f` |
| Restart after config change | `sudo systemctl restart trade-collector` |
| Inspect records | `sqlite3 data/terminal.db "select * from decisions order by id desc limit 5;"` |
| Pause everything | `sudo systemctl stop trade-collector` (shadow/paper only; nothing live exists) |
| Close a paper position by hand | Alpaca dashboard → Positions → Close |
| Update code, keep keys + data | `cp .env ~/env.bak && cp -r data ~/data.bak` → unzip/pull → restore both |

## 8. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `401 unauthorized` from Alpaca | Live (`AK…`) instead of paper (`PK…`) keys, or key/secret from different generations → regenerate on the **Paper** dashboard, paste both |
| `cost basis must be >= minimal amount of order 10` | Alpaca crypto minimum order is $10. Sizing now SKIPs below it; seen only with tiny test allocations |
| `ENTER refused … another ENTER is in progress` | A previewed/unconfirmed order exists. Settled no-fill intents don't block (fixed). For the smoke DB: it self-cleans each run |
| `quote_mismatch` blocker | Quote mid > ±2% from the bar close = bad data. Intended |
| `clock_skew` blocker | Host clock is off by > 2 s vs exchange timestamps → `timedatectl status` |
| pip stuck on "installing build dependencies" | No prebuilt wheel for that Python → use Python 3.12/3.13 64-bit (all deps have wheels; verified for x86 and ARM64) |
| `insufficient authentication scopes` running gcloud | You're on the VM; run it in Cloud Shell |
| Uploaded file is named `trade-terminal_(1).zip` | Cloud Shell doesn't overwrite on upload → `mv ~/"trade-terminal_(1).zip" ~/trade-terminal.zip` |
