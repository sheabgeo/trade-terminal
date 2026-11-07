#!/usr/bin/env bash
# One-time VM setup. Run from the project folder:  bash deploy/setup_vm.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Swap (only on small VMs, e.g. E2.1.Micro with 1 GB RAM)"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 3000 ] && ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  echo "Added 2 GB swap (RAM ${MEM_MB} MB)"
fi

echo "== OS packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip sqlite3 unzip
sudo timedatectl set-timezone UTC          # strategy runs on UTC days

echo "== Python venv"
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== Secrets file"
if [ ! -f .env ]; then cp .env.example .env; fi
chmod 600 .env
mkdir -p data

echo "== Tests"
.venv/bin/python -m pytest -q

echo
echo "Done. Next: nano .env  (paste PAPER keys), then:"
echo "  .venv/bin/python scripts/check_connection.py"
