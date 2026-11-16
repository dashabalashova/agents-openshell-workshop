#!/usr/bin/env bash
# Step 1: temporary VM -> Devlab image -> smoke test -> registry -> push (tags: date and latest) -> VM deleted. ~10 min; prints the image address.
# ./1-build-image.sh [--delete: remove registry and leftover VM]   Settings: config.env. DRY_RUN=1 only checks, KEEP_VM=1 keeps the VM.
set -euo pipefail
cd "$(dirname "$0")"
[ -f config.env ] && source config.env
: "${PROJECT_ID:=$(nebius config get parent-id 2>/dev/null || true)}"
: "${REGION:=eu-west2}" "${REGISTRY_NAME:=openshell-devlab}" "${IMAGE_NAME:=openshell-devlab}" "${VM_NAME:=openshell-build-vm}"
: "${VM_IMAGE_FAMILY:=ubuntu24.04-driverless}" "${VM_PLATFORM:=cpu-d3}" "${VM_PRESET:=4vcpu-16gb}" "${VM_DISK_GIB:=100}"
: "${DEVLAB_PLATFORM:=cpu-d3}" "${DEVLAB_PRESET:=4vcpu-16gb}" "${DEVLAB_DISK:=100Gi}"
: "${IMAGE_SRC:=tools/devlab-image}" "${SSH_KEY:=$HOME/.ssh/id_ed25519.pub}" "${TAG:=$(date +%Y%m%d-%H%M%S)}"
: "${CR:=cr.$REGION.nebius.cloud}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30)

registry_id() { nebius registry get-by-name --parent-id "$PROJECT_ID" --name "$REGISTRY_NAME" --format json 2>/dev/null | jq -r '.metadata.id // empty' || true; }
vm_id()       { nebius compute instance get-by-name --parent-id "$PROJECT_ID" --name "$VM_NAME" --format json 2>/dev/null | jq -r '.metadata.id // empty' || true; }
delete_vm()   { local id; id=$(vm_id); [ -z "$id" ] || { say "Deleting the build VM"; nebius compute instance delete --id "$id" >/dev/null 2>&1 && echo "    deleted (its disk goes with it)" || echo "    could not delete $id: delete it by hand"; }; }

for c in nebius jq ssh rsync; do command -v "$c" >/dev/null || die "missing command: $c"; done
nebius iam whoami >/dev/null 2>&1 || die "the nebius CLI is not logged in (run: nebius iam whoami)"
[ -n "$PROJECT_ID" ] || die "set PROJECT_ID in config.env (see config.env.example)"

if [ "${1:-}" = "--delete" ]; then
  delete_vm
  reg=$(registry_id)
  if [ -n "$reg" ]; then
    say "Deleting the registry $REGISTRY_NAME with its images"
    for a in $(nebius registry image list --parent-id "$reg" --format json | jq -r '.items[]?.id'); do nebius registry image delete --id "$a" >/dev/null 2>&1 || true; done
    sleep 3; nebius registry delete --id "$reg" >/dev/null 2>&1 && echo "    deleted" || echo "    could not delete it (delete its images first)"
  fi
  exit 0
fi

# ---- checks
[ -f "$SSH_KEY" ] || die "public SSH key not found: $SSH_KEY (set SSH_KEY in config.env)"
for f in Dockerfile start.sh gateway.toml; do [ -f "$IMAGE_SRC/$f" ] || die "$IMAGE_SRC/$f is missing"; done
: "${SUBNET:=$(nebius vpc subnet list --parent-id "$PROJECT_ID" --format json | jq -r '.items[0].metadata.id // empty')}"
: "${VM_IMAGE_ID:=$(nebius compute image list-public --region "$REGION" --format json | jq -r --arg f "$VM_IMAGE_FAMILY" '[.items[]? | select(.spec.image_family == $f)][0].metadata.id // empty')}"
[ -n "$SUBNET" ] && [ -n "$VM_IMAGE_ID" ] || die "no subnet or no public image '$VM_IMAGE_FAMILY' found (set SUBNET / VM_IMAGE_ID in config.env)"
[ -z "$(vm_id)" ] || die "a VM named $VM_NAME already exists: ./1-build-image.sh --delete"
say "Plan: project $PROJECT_ID, region $REGION, VM image $VM_IMAGE_ID, image tag $TAG"
[ -z "${DRY_RUN:-}" ] || { echo "DRY_RUN: the checks passed, nothing was created"; exit 0; }
trap '[ -n "${KEEP_VM:-}" ] || delete_vm' EXIT          # the build VM never outlives the script

# ---- 1. the build VM
say "1/5 Build VM ($VM_PLATFORM $VM_PRESET, $VM_DISK_GIB GiB; Docker comes from cloud-init)"
ci=$(mktemp)
cat > "$ci" <<EOF
#cloud-config
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$SSH_KEY")
package_update: true
packages: [docker.io, rsync]
runcmd:
  - systemctl enable --now docker
  - usermod -aG docker ubuntu
EOF
nebius compute instance create --parent-id "$PROJECT_ID" --name "$VM_NAME" \
  --resources-platform "$VM_PLATFORM" --resources-preset "$VM_PRESET" \
  --boot-disk-attach-mode read_write --boot-disk-managed-disk-name "$VM_NAME-disk" \
  --boot-disk-managed-disk-type network_ssd --boot-disk-managed-disk-size-gibibytes "$VM_DISK_GIB" \
  --boot-disk-managed-disk-source-image-id "$VM_IMAGE_ID" \
  --network-interfaces '[{"name":"eth0","subnet_id":"'"$SUBNET"'","ip_address":{},"public_ip_address":{}}]' \
  --cloud-init-user-data "$(cat "$ci")" >/dev/null
rm -f "$ci"
IP=$(nebius compute instance get-by-name --parent-id "$PROJECT_ID" --name "$VM_NAME" --format json | jq -r '.status.network_interfaces[0].public_ip_address.address' | cut -d/ -f1)
[ -n "$IP" ] && [ "$IP" != null ] || die "the VM has no public IP"
echo "    VM $IP: waiting for SSH and cloud-init"
for _ in $(seq 60); do "${SSH[@]}" "ubuntu@$IP" true 2>/dev/null && break; sleep 10; done
"${SSH[@]}" "ubuntu@$IP" 'cloud-init status --wait >/dev/null; docker --version' || die "the VM did not become ready"

# ---- 2. registry
say "2/5 Registry $REGISTRY_NAME"
[ -n "$(registry_id)" ] || nebius registry create --parent-id "$PROJECT_ID" --name "$REGISTRY_NAME" >/dev/null
reg=$(registry_id); [ -n "$reg" ] || die "the registry was not created"
LATEST="$CR/${reg#registry-}/$IMAGE_NAME:latest"; IMAGE="$CR/${reg#registry-}/$IMAGE_NAME:$TAG"; echo "    image: $IMAGE"

# ---- 3. build
say "3/5 Build the image on the VM"
rsync -az -e "${SSH[*]}" --delete --exclude '.ipynb_checkpoints' "$IMAGE_SRC"/ "ubuntu@$IP:~/devlab-image/"
"${SSH[@]}" "ubuntu@$IP" "cd ~/devlab-image && sudo docker build -t '$IMAGE' . 2>&1 | tail -3"

# ---- 4. smoke test: start, secrets isolation, token, restart, stop
say "4/5 Smoke test of the image"
"${SSH[@]}" "ubuntu@$IP" "sudo bash -s '$IMAGE'" <<'REMOTE' || die "the image smoke test failed (see above)"
set -u
IMAGE=$1; N=smoke-test; TOKEN=smoketest123456
fail() { echo "SMOKE FAIL: $*"; docker logs "$N" 2>&1 | tail -15; docker rm -f "$N" >/dev/null 2>&1; exit 1; }
ready() { for _ in $(seq 90); do [ "$(docker logs "$N" 2>&1 | grep -c 'Ready:')" -ge "$1" ] && return 0; docker ps -q -f name="$N" | grep -q . || return 1; sleep 2; done; return 1; }
docker rm -f "$N" >/dev/null 2>&1; rm -rf /root/ws; mkdir -p /root/ws
docker run -d --name "$N" --privileged -e JUPYTER_TOKEN=$TOKEN -e NEBIUS_API_KEY=fake-nebius -e TAVILY_API_KEY=fake-tavily -v /root/ws:/workspace "$IMAGE" >/dev/null
ready 1 || fail "first start"; echo "  start: Ready"
leaks=$(docker exec "$N" sh -c 'for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "$c" in dockerd*|containerd\ *|openshell-gateway*) tr "\0" "\n" < $p/environ 2>/dev/null | grep -E "^(JUPYTER_TOKEN|NEBIUS_API_KEY|TAVILY_API_KEY)=" ;; esac; done' | wc -l)
[ "$leaks" = 0 ] || fail "dockerd/containerd/gateway see $leaks secret variable(s)"; echo "  secrets: dockerd, containerd and the gateway do not see the token or the keys"
[ "$(docker exec "$N" curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/api/status)" = 403 ] || fail "jupyter answers without the token"
[ "$(docker exec "$N" curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8888/api/status?token=$TOKEN")" = 200 ] || fail "jupyter rejects the right token"; echo "  jupyter: 403 without the token, 200 with it"
docker exec "$N" openshell status 2>/dev/null | grep -q Connected || fail "gateway not connected"; echo "  gateway: Connected"
docker restart -t 30 "$N" >/dev/null; ready 2 || fail "restart"
[ "$(docker exec "$N" openshell gateway list 2>/dev/null | grep -c devlab)" = 1 ] || fail "gateway registration duplicated after the restart"; echo "  restart: ready again, one gateway entry"
docker stop -t 30 "$N" >/dev/null; [ "$(docker inspect -f '{{.State.ExitCode}}' "$N")" = 0 ] || fail "unclean stop"; echo "  stop: exit code 0"
docker rm -f "$N" >/dev/null; rm -rf /root/ws
REMOTE

# ---- 5. push, then the VM is not needed any more
say "5/5 Push the image"
nebius iam get-access-token | "${SSH[@]}" "ubuntu@$IP" "sudo docker login $CR --username iam --password-stdin" 2>&1 | grep -q Succeeded || die "registry login failed"
"${SSH[@]}" "ubuntu@$IP" "sudo docker tag '$IMAGE' '$LATEST' && sudo docker push '$IMAGE' 2>&1 | tail -1 && sudo docker push '$LATEST' 2>&1 | tail -1; sudo docker logout $CR >/dev/null 2>&1"
nebius registry image list --parent-id "$reg" --format json | jq -e '[.items[]? | select((.tags // []) | index("latest"))] | length > 0' >/dev/null || die "the image is not in the registry"
delete_vm; trap - EXIT

cat <<EOF

Image ready: $LATEST  (also $IMAGE)

For the participants (Devlab creation form in the console):
  Image     $LATEST
  Port      8888 (HTTP)      Workspace path  /workspace      Public IP  yes
  Machine   $DEVLAB_PLATFORM $DEVLAB_PRESET      Disk  $DEVLAB_DISK
  Variables JUPYTER_TOKEN (their own, 16+ characters), NEBIUS_API_KEY, TAVILY_API_KEY      (../workshop/participant.txt)
Your own Devlab:  ./2a-create-devlab-cli.sh   (participants: ../workshop/participant.txt, step 2b)
EOF
