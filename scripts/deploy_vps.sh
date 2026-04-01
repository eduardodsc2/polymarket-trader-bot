#!/usr/bin/env bash
# deploy_vps.sh — Deploy polymarket-trader-bot to a fresh VPS
#
# Usage:
#   ./scripts/deploy_vps.sh <user@host>
#
# Prerequisites (local machine):
#   - SSH access to the VPS
#   - .env file ready with all secrets
#
# What this script does:
#   1. Installs Docker + Docker Compose on the VPS (Ubuntu 22.04/24.04)
#   2. Clones the repo
#   3. Copies your local .env to the VPS
#   4. Starts bot + db + dashboard in production mode
#
# Example:
#   ./scripts/deploy_vps.sh root@123.456.789.0

set -euo pipefail

TARGET="${1:?Usage: $0 <user@host>}"
REPO_URL="https://github.com/eduardodsc2/polymarket-trader-bot.git"
REMOTE_DIR="/opt/polymarket-trader-bot"
ENV_FILE=".env"

echo "==> Deploying to $TARGET"

# ── 1. Check local .env exists ────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in secrets."
    exit 1
fi

# ── 2. Install Docker on VPS (idempotent) ─────────────────────────────────────
echo "==> Installing Docker on VPS..."
ssh "$TARGET" 'bash -s' <<'ENDSSH'
set -euo pipefail
if command -v docker &>/dev/null; then
    echo "Docker already installed: $(docker --version)"
    exit 0
fi
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable docker
systemctl start docker
echo "Docker installed: $(docker --version)"
ENDSSH

# ── 3. Clone or update repo ───────────────────────────────────────────────────
echo "==> Cloning/updating repo on VPS..."
ssh "$TARGET" "
if [[ -d '$REMOTE_DIR/.git' ]]; then
    cd '$REMOTE_DIR' && git pull --ff-only
else
    git clone '$REPO_URL' '$REMOTE_DIR'
fi
"

# ── 4. Copy .env to VPS ───────────────────────────────────────────────────────
echo "==> Copying .env to VPS..."
scp "$ENV_FILE" "$TARGET:$REMOTE_DIR/.env"

# ── 5. Build and start containers ─────────────────────────────────────────────
echo "==> Building images and starting services..."
ssh "$TARGET" "
cd '$REMOTE_DIR'
docker compose -f docker-compose.yml -f docker-compose.prod.yml build --pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d bot db dashboard
"

# ── 6. Verify ─────────────────────────────────────────────────────────────────
echo "==> Checking container status..."
ssh "$TARGET" "cd '$REMOTE_DIR' && docker compose ps"

echo ""
echo "==> Deploy complete!"
echo "    Dashboard: http://$(echo $TARGET | cut -d@ -f2):8080"
echo "    Logs:      ssh $TARGET 'cd $REMOTE_DIR && docker compose logs -f bot'"
echo "    Stop:      ssh $TARGET 'cd $REMOTE_DIR && docker compose down'"
