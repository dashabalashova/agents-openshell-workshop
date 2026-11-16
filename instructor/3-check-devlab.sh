#!/usr/bin/env bash
# Step 3: do what a participant does (load the code from GitHub, run the notebook) through the Jupyter API and check 10 results. ~2 min.
# ./3-check-devlab.sh [devlab url]   (default: the Devlab DEVLAB_NAME). Needs python3.
set -euo pipefail
cd "$(dirname "$0")"
token=${JUPYTER_TOKEN:-}; [ -f config.env ] && source config.env; [ -z "$token" ] || JUPYTER_TOKEN=$token
: "${PROJECT_ID:=$(nebius config get parent-id 2>/dev/null || true)}" "${DEVLAB_NAME:=openshell-devlab}"
: "${CONTENT_REPO:=dashabalashova/agents-openshell-workshop}" "${NOTEBOOK:=radar-openshell.ipynb}"
URL=${1:-$(nebius ai devlab list --parent-id "$PROJECT_ID" --format json | jq -r --arg n "$DEVLAB_NAME" '[.items[]? | select(.metadata.name == $n)][0].status.public_endpoints[0] // empty')}
[ -n "$URL" ] || { echo "ERROR: no Devlab URL: pass it as an argument"; exit 1; }
[ -n "${JUPYTER_TOKEN:-}" ] || { echo "ERROR: JUPYTER_TOKEN is not set (config.env or the environment)"; exit 1; }
V=tools/.venv-tools
[ -x $V/bin/python ] || python3 -m venv $V
$V/bin/python -c "import websocket" 2>/dev/null || $V/bin/pip install -q websocket-client >/dev/null
JUPYTER_TOKEN="$JUPYTER_TOKEN" $V/bin/python tools/verify.py "$URL" "$CONTENT_REPO" "$NOTEBOOK"
