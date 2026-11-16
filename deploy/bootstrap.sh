#!/usr/bin/env bash
# First-time VM setup. Run from the repo root after cloning:
#   bash deploy/bootstrap.sh
#
# Installs system packages, creates a venv, installs Python deps, and sets up
# the .env file (you still have to fill in real secret values by hand).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 git unzip curl

if ! command -v aws >/dev/null 2>&1; then
  echo "==> Installing AWS CLI v2 (Ubuntu 24.04 dropped the apt 'awscli' package,"
  echo "    so this uses AWS's own installer instead)"
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64) AWS_CLI_ARCH="aarch64" ;;
    x86_64)  AWS_CLI_ARCH="x86_64" ;;
    *) echo "Unrecognized arch '$ARCH' — install AWS CLI manually, see https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" >&2
       AWS_CLI_ARCH="" ;;
  esac
  if [ -n "$AWS_CLI_ARCH" ]; then
    TMP_DIR="$(mktemp -d)"
    curl "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_CLI_ARCH}.zip" -o "$TMP_DIR/awscliv2.zip"
    unzip -q "$TMP_DIR/awscliv2.zip" -d "$TMP_DIR"
    sudo "$TMP_DIR/aws/install"
    rm -rf "$TMP_DIR"
  fi
else
  echo "==> AWS CLI already installed, skipping"
fi

echo "==> Creating virtualenv"
python3 -m venv venv
source venv/bin/activate

echo "==> Installing Python dependencies"
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example — fill in real values before running anything"
  cp .env.example .env
else
  echo "==> .env already exists, leaving it alone"
fi

echo ""
echo "Done. Next steps:"
echo "  1. nano .env                      # fill in GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, etc."
echo "  2. cp deploy/backup.env.example deploy/backup.env && nano deploy/backup.env"
echo "  3. See deploy/README.md for the backup timer setup."
