#!/usr/bin/env bash
# make kind-down: delete the groundcrew cluster, the local registry, and the kubeconfigs.
source "$(dirname "$0")/lib.sh"
ensure_tools
if cluster_exists; then
  log "Deleting kind cluster $CLUSTER"
  kind delete cluster --name "$CLUSTER"
fi
docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 && log "Removed registry $REGISTRY_NAME" || true
rm -f "$STATE"/groundcrew-deployer*.kubeconfig
log "Done."
