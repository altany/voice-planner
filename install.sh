#!/usr/bin/env bash
# Run this ON THE PI, from inside the voice-planner folder:  ./install.sh
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
RUN_USER="$(whoami)"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

echo "==> Creating Python virtual environment"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> Created .env from .env.example — YOU MUST EDIT IT (nano .env)"
fi

mkdir -p data google

echo "==> Installing systemd service"
sed -e "s|{{DIR}}|$PROJECT_DIR|g" -e "s|{{USER}}|$RUN_USER|g" voice-planner.service \
    | sudo tee /etc/systemd/system/voice-planner.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable voice-planner

echo
echo "Done. Next steps:"
echo "  1. Edit your secrets:        nano .env"
echo "  2. Start the service:        sudo systemctl start voice-planner"
echo "  3. Check it's running:       curl http://localhost:\${PLANNER_PORT:-8484}/health"
echo "  4. Google Tasks setup:       see README ('Google Tasks one-time setup')"
