#!/usr/bin/env bash
# Installs the collector as an always-on systemd service for THIS user and THIS folder
# (Google Cloud usernames aren't 'ubuntu', so paths are filled in here, not hard-coded).
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"; ME="$(id -un)"; HOMEDIR="$HOME"
CLAUDE_BIN="$(command -v claude || echo "$HOMEDIR/.local/bin/claude")"
RW="$DIR/data"
# Claude Code keeps its sign-in + session files here; the agent needs to write them.
for p in "$HOMEDIR/.claude" "$HOMEDIR/.claude.json"; do [ -e "$p" ] && RW="$RW $p"; done
sed -e "s#^User=.*#User=$ME#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=$DIR#" \
    -e "s#^ExecStart=.*#ExecStart=$DIR/.venv/bin/python -m terminal.collector#" \
    -e "s#^ReadWritePaths=.*#ReadWritePaths=$RW#" \
    deploy/trade-collector.service > /tmp/trade-collector.service
echo "Environment=PATH=$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin" >> /tmp/trade-collector.service.env
sed -i "/^\[Service\]/r /tmp/trade-collector.service.env" /tmp/trade-collector.service
rm -f /tmp/trade-collector.service.env
sudo cp /tmp/trade-collector.service /etc/systemd/system/trade-collector.service
sudo systemctl daemon-reload
sudo systemctl enable --now trade-collector
sudo systemctl restart trade-collector
sleep 3
systemctl --no-pager status trade-collector | head -12
echo "Logs: journalctl -u trade-collector -f"
