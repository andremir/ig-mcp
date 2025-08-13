#!/usr/bin/env bash
set -euo pipefail

cd /Users/andrebacellardemiranda/ig-mcp
set -a; source .env; set +a
source venv/bin/activate
export PYTHONPATH="$PWD:${PYTHONPATH:-}"

python -m src.instagram_mcp_server
