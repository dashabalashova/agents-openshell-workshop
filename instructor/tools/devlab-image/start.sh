#!/usr/bin/env bash
# Start a private Docker daemon, the OpenShell gateway, then JupyterLab.
# Required env: JUPYTER_TOKEN. Expects a persistent volume at /workspace.
set -euo pipefail
: "${JUPYTER_TOKEN:?JUPYTER_TOKEN is required; refusing to start an unauthenticated Jupyter}"

umask 077
STATE=/workspace/.openshell
mkdir -p "$STATE"

# dockerd, containerd and the gateway never see the token or the API keys; only JupyterLab and its kernels do.
STRIP=(env -u JUPYTER_TOKEN -u NEBIUS_API_KEY -u TAVILY_API_KEY)

# mTLS certificates for the gateway (created once, kept on the volume).
if [ ! -d "$STATE/tls" ]; then
  tmp=$(mktemp -d -p "$STATE")
  "${STRIP[@]}" openshell-gateway generate-certs --output-dir "$tmp/tls"
  mv "$tmp/tls" "$STATE/tls" && rmdir "$tmp"
fi
client="$XDG_CONFIG_HOME/openshell/gateways/devlab/mtls"
mkdir -p "$client"
cp "$STATE/tls/ca.crt" "$STATE/tls/client/tls.crt" "$STATE/tls/client/tls.key" "$client/"
"${STRIP[@]}" openshell-gateway config preflight --path /etc/openshell/gateway.toml

wait_for() {  # wait_for <description> <command...>
  local what=$1; shift
  for _ in $(seq 90); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "timeout waiting for $what" >&2; exit 1
}

# On stop (SIGTERM from the platform) or when anything exits: stop the children and leave cleanly.
shutdown() {
  trap - EXIT TERM INT
  kill $(jobs -p) 2>/dev/null || true
  wait 2>/dev/null || true
}
trap 'shutdown; exit 0' TERM INT
trap shutdown EXIT

# Private Docker daemon (a stale runtime dir from a same-container restart is discarded).
rm -rf /run/openshell-docker /run/openshell-docker.pid /run/openshell-docker.sock
"${STRIP[@]}" dind dockerd --host=unix:///run/openshell-docker.sock --data-root=/workspace/.docker \
  --exec-root=/run/openshell-docker --pidfile=/run/openshell-docker.pid --log-level=warn &
wait_for "docker" docker info

"${STRIP[@]}" openshell-gateway --config /etc/openshell/gateway.toml &
wait_for "gateway" curl -sf http://127.0.0.1:17671/readyz

# Registration persists on the volume; `gateway add` rejects duplicates (list first: `list | grep -q` under pipefail can misreport).
gateways=$(openshell gateway list --output json)
if ! grep -q '"devlab"' <<<"$gateways"; then
  openshell gateway add https://127.0.0.1:17670 --local --name devlab
fi

# jupyter reads the token from JUPYTER_TOKEN, so it never appears in argv.
jupyter lab --allow-root --no-browser --ip=0.0.0.0 --port=8888 --ServerApp.root_dir=/workspace &

echo "Ready: JupyterLab on :8888, OpenShell gateway 'devlab'." >&2
status=0
wait -n || status=$?          # returns as soon as any of the three processes exits
echo "a runtime process exited (status $status); stopping the container" >&2
exit 1
