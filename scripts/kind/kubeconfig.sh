#!/usr/bin/env bash
# make kind-kubeconfig: kubeconfigs for the groundcrew-deployer ServiceAccount (never
# cluster-admin), from its token Secret.
#   .kind/groundcrew-deployer.kubeconfig       server https://groundcrew-control-plane:6443,
#                                              for Groundcrew in Docker Compose (kind network)
#   .kind/groundcrew-deployer.host.kubeconfig  server https://127.0.0.1:<port>, for kubectl here
source "$(dirname "$0")/lib.sh"
ensure_tools
cluster_exists || die "no kind cluster $CLUSTER; run make kind-up first."

token=""
for _ in $(seq 1 30); do
  token=$(kadmin -n "$NAMESPACE" get secret groundcrew-deployer-token -o jsonpath='{.data.token}' 2>/dev/null || true)
  [ -n "$token" ] && break
  sleep 1
done
[ -n "$token" ] || die "the groundcrew-deployer token was not issued; run make kind-up again."
token=$(printf '%s' "$token" | base64 -d)
ca=$(kadmin -n "$NAMESPACE" get secret groundcrew-deployer-token -o jsonpath='{.data.ca\.crt}')
host_server=$(kind get kubeconfig --name "$CLUSTER" | awk '/server:/ {print $2; exit}')
internal_server=$(kind get kubeconfig --name "$CLUSTER" --internal | awk '/server:/ {print $2; exit}')

write() {
  local file=$1 server=$2
  umask 077
  cat > "$file" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: $CLUSTER
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $SA
    user:
      token: $token
contexts:
  - name: $SA@$CLUSTER
    context:
      cluster: $CLUSTER
      user: $SA
      namespace: default
current-context: $SA@$CLUSTER
YAML
}
mkdir -p "$STATE"
write "$STATE/groundcrew-deployer.kubeconfig" "$internal_server"
write "$STATE/groundcrew-deployer.host.kubeconfig" "$host_server"
log "Wrote .kind/groundcrew-deployer.kubeconfig ($internal_server): register it as a Kubernetes connection."
log "Wrote .kind/groundcrew-deployer.host.kubeconfig ($host_server): for kubectl on this machine."
