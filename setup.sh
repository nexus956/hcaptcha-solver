#!/bin/bash
# setup.sh — One-time setup for the AI hCaptcha Solver
# Run: chmod +x setup.sh && ./setup.sh

set -e
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo -e "${BOLD}=== AI hCaptcha Solver Setup ===${RESET}\n"

# ── 1. Install Python dependencies ───────────────────────────────────────────
echo -e "${YELLOW}[1/2] Installing Python packages...${RESET}"
pip install -r requirements.txt --break-system-packages -q
echo -e "${GREEN}   Done.${RESET}"

# ── 2. Install Playwright browsers ───────────────────────────────────────────
echo -e "${YELLOW}[2/2] Installing Playwright Chromium browser...${RESET}"
python -m playwright install chromium
echo -e "${GREEN}   Done.${RESET}"

echo -e "\n${BOLD}${GREEN}Setup complete!${RESET}\n"
echo -e "Next steps:"
echo -e "  1. Run AI solver:   ${BOLD}python ai_solver.py${RESET}"
echo -e "  2. Or start API:    ${BOLD}python api_server.py${RESET}"
echo -e "  3. Or show browser: ${BOLD}python ai_solver.py --no-headless${RESET}"
echo -e "  4. API docs:        ${BOLD}http://localhost:8000/docs${RESET}\n"
