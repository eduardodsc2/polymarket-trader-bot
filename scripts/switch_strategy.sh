#!/bin/bash
# switch_strategy.sh <strategy_name>
# Troca PAPER_STRATEGY no .env e reinicia o bot
# Chamado via cron para automatizar a rotação de estratégias

STRATEGY="$1"
DEPLOY_DIR="/opt/polymarket-trader-bot"
LOG="/var/log/polymarket_cron.log"

if [[ -z "$STRATEGY" ]]; then
    echo "Uso: $0 <market_maker|sum_to_one_arb|calibration_betting>" >&2
    exit 1
fi

echo "[$(date -u)] Trocando para: $STRATEGY" >> "$LOG"

# Atualiza ou adiciona PAPER_STRATEGY no .env
if grep -q "^PAPER_STRATEGY=" "$DEPLOY_DIR/.env"; then
    sed -i "s/^PAPER_STRATEGY=.*/PAPER_STRATEGY=$STRATEGY/" "$DEPLOY_DIR/.env"
else
    echo "PAPER_STRATEGY=$STRATEGY" >> "$DEPLOY_DIR/.env"
fi

# Reinicia bot e dashboard (up -d re-lê .env, restart não)
cd "$DEPLOY_DIR"
docker compose up -d bot dashboard >> "$LOG" 2>&1

echo "[$(date -u)] Bot e dashboard reiniciados com estrategia: $STRATEGY" >> "$LOG"
