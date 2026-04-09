#!/usr/bin/env bash
# ════════════════════════════════════════════════════════
# setup.sh — Kalshi EV Bot environment setup
# ════════════════════════════════════════════════════════
set -euo pipefail

BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

echo ""
echo -e "${CYAN}${BOLD}⬡ Kalshi EV Bot — Setup${RESET}"
echo -e "${CYAN}══════════════════════════════════════${RESET}"
echo ""

# ── Step 1: Python version check ───────────────────────
echo -e "${BOLD}[1/5] Checking Python version...${RESET}"
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3; do
    if command -v $cmd &>/dev/null; then
        version=$($cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo $version | cut -d. -f1)
        minor=$(echo $version | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_CMD=$cmd
            echo -e "  ${GREEN}✓ Found Python ${version} at $(which $cmd)${RESET}"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "  ${RED}✗ Python 3.11+ not found. Install from https://python.org${RESET}"
    exit 1
fi

# ── Step 2: Create virtual environment ─────────────────
echo ""
echo -e "${BOLD}[2/5] Creating virtual environment...${RESET}"
if [ ! -d "venv" ]; then
    $PYTHON_CMD -m venv venv
    echo -e "  ${GREEN}✓ Created venv/${RESET}"
else
    echo -e "  ${YELLOW}✓ venv/ already exists — skipping creation${RESET}"
fi

# Activate
source venv/bin/activate
echo -e "  ${GREEN}✓ Activated venv (Python: $(python --version))${RESET}"

# ── Step 3: Install dependencies ───────────────────────
echo ""
echo -e "${BOLD}[3/5] Installing dependencies...${RESET}"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo -e "  ${GREEN}✓ All packages installed${RESET}"

# ── Step 4: Copy .env.example → .env ───────────────────
echo ""
echo -e "${BOLD}[4/5] Setting up environment file...${RESET}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "  ${GREEN}✓ Created .env from .env.example${RESET}"
    echo -e "  ${YELLOW}  → Edit .env and add your Kalshi API credentials${RESET}"
else
    echo -e "  ${YELLOW}✓ .env already exists — keeping existing file${RESET}"
fi

# ── Step 5: Create runtime directories ─────────────────
echo ""
echo -e "${BOLD}[5/5] Creating runtime directories...${RESET}"
mkdir -p logs data
touch logs/.gitkeep data/.gitkeep
echo -e "  ${GREEN}✓ logs/ and data/ directories ready${RESET}"

# ── Done ────────────────────────────────────────────────
echo ""
echo -e "${CYAN}══════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}✓ Setup complete!${RESET}"
echo ""
echo -e "${BOLD}Next steps:${RESET}"
echo ""
echo -e "  ${CYAN}1.${RESET} Edit ${BOLD}.env${RESET} and add your credentials:"
echo -e "       KALSHI_API_KEY=<your-key-id>"
echo -e "       KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem"
echo -e "       DISCORD_WEBHOOK_URL=<optional>"
echo ""
echo -e "  ${CYAN}2.${RESET} Run the backtest to validate the strategy:"
echo -e "       ${BOLD}source venv/bin/activate && python backtest.py${RESET}"
echo ""
echo -e "  ${CYAN}3.${RESET} Run the test suite:"
echo -e "       ${BOLD}pytest tests/ -v${RESET}"
echo ""
echo -e "  ${CYAN}4.${RESET} Start the bot in paper mode (safe — no real orders):"
echo -e "       ${BOLD}python -m bot.main${RESET}"
echo "       or one single scan cycle:"
echo -e "       ${BOLD}python -m bot.main --single${RESET}"
echo ""
echo -e "  ${CYAN}5.${RESET} Once confident, set ${BOLD}PAPER_MODE=false${RESET} in .env to go live."
echo ""
echo -e "${YELLOW}⚠  Risk reminder: Never risk more than you can afford to lose.${RESET}"
echo -e "${YELLOW}   Review all parameters in ${BOLD}bot/config.py${RESET}${YELLOW} before going live.${RESET}"
echo ""
