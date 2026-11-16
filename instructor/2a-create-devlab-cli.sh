#!/usr/bin/env bash
# Step 2a: create a Devlab from the :latest registry image and wait until it runs. ~4 min; prints the URL.
# ./2a-create-devlab-cli.sh [--delete]   Another one: DEVLAB_NAME=ws-alice JUPYTER_TOKEN=... ./2a-create-devlab-cli.sh
# Settings and secrets: config.env (a JUPYTER_TOKEN in the environment wins). DRY_RUN=1 only checks; IMAGE=... overrides the image.
# The token and keys are command-line arguments: visible in `ps` on this machine while the CLI runs.
set -euo pipefail
cd "$(dirname "$0")"
token=${JUPYTER_TOKEN:-}; [ -f config.env ] && source config.env; [ -z "$token" ] || JUPYTER_TOKEN=$token
: "${PROJECT_ID:=$(nebius config get parent-id 2>/dev/null || true)}"
: "${REGION:=eu-west2}" "${CR:=cr.$REGION.nebius.cloud}" "${REGISTRY_NAME:=openshell-devlab}" "${IMAGE_NAME:=openshell-devlab}"
: "${DEVLAB_NAME:=openshell-devlab}" "${DEVLAB_PLATFORM:=cpu-d3}" "${DEVLAB_PRESET:=4vcpu-16gb}" "${DEVLAB_DISK:=100Gi}"
:

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
devlab() { nebius ai devlab list --parent-id "$PROJECT_ID" --format json 2>/dev/null | jq -c --arg n "$DEVLAB_NAME" '[.items[]? | select(.metadata.name == $n)][0] // {}'; }

for c in nebius jq; do command -v "$c" >/dev/null || die "missing command: $c"; done
nebius iam whoami >/dev/null 2>&1 || die "the nebius CLI is not logged in (run: nebius iam whoami)"
[ -n "$PROJECT_ID" ] || die "set PROJECT_ID in config.env (see config.env.example)"

if [ "${1:-}" = "--delete" ]; then
  id=$(devlab | jq -r '.metadata.id // empty'); [ -n "$id" ] || { echo "no Devlab named $DEVLAB_NAME"; exit 0; }
  nebius ai devlab delete --id "$id" --async >/dev/null; echo "deleting $id ..."
  for _ in $(seq 60); do [ -z "$(devlab | jq -r '.metadata.id // empty')" ] && break; sleep 10; done; echo "deleted"; exit 0
fi

# ---- checks
for k in JUPYTER_TOKEN NEBIUS_API_KEY TAVILY_API_KEY; do [ -n "${!k:-}" ] || die "$k is missing: put it in config.env (see config.env.example) or the environment"; done
[ "${#JUPYTER_TOKEN}" -ge 16 ] || die "JUPYTER_TOKEN must be at least 16 characters (openssl rand -hex 16)"
[ -z "$(devlab | jq -r '.metadata.id // empty')" ] || die "a Devlab named $DEVLAB_NAME already exists: ./2a-create-devlab-cli.sh --delete (or change DEVLAB_NAME)"
: "${SUBNET:=$(nebius vpc subnet list --parent-id "$PROJECT_ID" --format json | jq -r '.items[0].metadata.id // empty')}"
if [ -z "${IMAGE:-}" ]; then   # the :latest image of the registry
  reg=$(nebius registry get-by-name --parent-id "$PROJECT_ID" --name "$REGISTRY_NAME" --format json 2>/dev/null | jq -r '.metadata.id // empty') || true
  [ -n "$reg" ] || die "no registry $REGISTRY_NAME: run ./1-build-image.sh first"
  nebius registry image list --parent-id "$reg" --format json | jq -e '[.items[]? | select((.tags // []) | index("latest"))] | length > 0' >/dev/null || die "no :latest image in the registry: run ./1-build-image.sh first"
  IMAGE="$CR/${reg#registry-}/$IMAGE_NAME:latest"
fi
say "Devlab $DEVLAB_NAME: project $PROJECT_ID, image $IMAGE"

# ---- create and wait
nebius ai devlab create --parent-id "$PROJECT_ID" --name "$DEVLAB_NAME" --image "$IMAGE" \
  --platform "$DEVLAB_PLATFORM" --preset "$DEVLAB_PRESET" --subnet-id "$SUBNET" --public \
  --primary-route-port 8888 --disk-size "$DEVLAB_DISK" --workspace-path /workspace \
  --env "JUPYTER_TOKEN=$JUPYTER_TOKEN" --env "NEBIUS_API_KEY=$NEBIUS_API_KEY" --env "TAVILY_API_KEY=$TAVILY_API_KEY" \
  ${DRY_RUN:+--dry-run} --async >/dev/null
[ -z "${DRY_RUN:-}" ] || { echo "DRY_RUN: the request is valid, nothing was created"; exit 0; }
for _ in $(seq 90); do
  state=$(devlab | jq -r '.status.state // "?"'); URL=$(devlab | jq -r '.status.public_endpoints[0] // empty'); echo "    state: $state"
  case "$state" in RUNNING) break ;; FAILED|ERROR) die "the Devlab failed to start: nebius ai devlab logs <id>" ;; esac
  sleep 10
done
[ "$state" = RUNNING ] || die "timed out waiting for the Devlab"

cat <<EOT

Devlab: $URL
Token:  JUPYTER_TOKEN (without it the URL answers 403)
Next:   ./3-load-and-run.sh "$URL"      (by hand: ../workshop/participant.txt, step 3)
Delete: DEVLAB_NAME=$DEVLAB_NAME ./2a-create-devlab-cli.sh --delete
EOT
