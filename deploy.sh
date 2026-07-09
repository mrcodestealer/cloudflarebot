#!/usr/bin/env bash
# One-shot deploy: pull latest code, install any new deps, restart the service.
# Run from the repo directory:  bash deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> git pull origin main"
git pull origin main

echo "==> pip install -r requirements.txt"
pip install -q -r requirements.txt

echo "==> restart cloudflarebot.service"
systemctl restart cloudflarebot.service
sleep 1

echo "==> recent logs"
journalctl -u cloudflarebot.service -n 15 --no-pager || true
echo "==> done (follow live logs with: journalctl -u cloudflarebot.service -f)"
