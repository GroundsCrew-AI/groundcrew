#!/usr/bin/env bash
# make kind-up: the groundcrew kind cluster, a local registry on localhost:5001 wired into it,
# ingress-nginx on 127.0.0.1:80/443, the groundcrew-deployer ServiceAccount with its RBAC and
# admission policy, and (METRICS=1) metrics-server. Safe to run again.
source "$(dirname "$0")/lib.sh"

command -v docker >/dev/null || die "Docker is not installed (docs/kind.md)."
docker info >/dev/null 2>&1 || die "Docker is not running, or this user cannot use it (docs/kind.md)."
ensure_tools

if ! cluster_exists; then
  for port in 80 443 "$REGISTRY_PORT"; do
    if ss -Hltn "sport = :$port" | grep -q .; then
      if [ "$port" != "$REGISTRY_PORT" ] || ! docker inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
        die "port $port on this machine is already in use; stop what uses it (docs/kind.md, Troubleshooting)."
      fi
    fi
  done
  log "Creating kind cluster $CLUSTER"
  kind create cluster --config "$ROOT/infra/kind/cluster.yaml" --wait 120s
else
  log "Cluster $CLUSTER already exists"
fi

# Local registry: localhost:$REGISTRY_PORT on the host, kind-registry:5000 on the kind network.
if [ "$(docker inspect -f '{{.State.Running}}' "$REGISTRY_NAME" 2>/dev/null)" != "true" ]; then
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  log "Starting registry $REGISTRY_NAME on localhost:$REGISTRY_PORT"
  docker pull -q "$REGISTRY_IMAGE" >/dev/null
  docker run -d --restart=always --name "$REGISTRY_NAME" \
    -p "127.0.0.1:$REGISTRY_PORT:5000" --label project=groundcrew "$REGISTRY_IMAGE" >/dev/null
fi
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REGISTRY_NAME")" = "null" ]; then
  docker network connect kind "$REGISTRY_NAME"
fi
# Nodes pull localhost:$REGISTRY_PORT/<image> (pushed from this machine) and
# $REGISTRY_NAME:5000/<image> (pushed from the dind build daemon) from the registry container.
for node in $(kind get nodes --name "$CLUSTER"); do
  for name in "localhost:$REGISTRY_PORT" "$REGISTRY_NAME:5000"; do
    dir="/etc/containerd/certs.d/$name"
    docker exec "$node" mkdir -p "$dir"
    printf '[host."http://%s:5000"]\n' "$REGISTRY_NAME" | docker exec -i "$node" cp /dev/stdin "$dir/hosts.toml"
  done
done
kadmin apply -f - >/dev/null <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:$REGISTRY_PORT"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
YAML

mkdir -p "$STATE"
log "Installing ingress-nginx"
fetch "$INGRESS_URL" "$INGRESS_SHA256" "$STATE/ingress-nginx.yaml"
kadmin apply -f "$STATE/ingress-nginx.yaml" >/dev/null
kadmin -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=180s

if [ "${METRICS:-0}" = "1" ]; then
  log "Installing metrics-server"
  fetch "$METRICS_URL" "$METRICS_SHA256" "$STATE/metrics-server.yaml"
  kadmin apply -f "$STATE/metrics-server.yaml" >/dev/null
  # kind's kubelets use self-signed serving certificates.
  kadmin -n kube-system patch deployment metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' \
    >/dev/null 2>&1 || true
  kadmin -n kube-system rollout status deployment/metrics-server --timeout=180s
fi

log "Applying the groundcrew-deployer RBAC and admission policy"
kadmin apply -f "$ROOT/infra/kind/rbac.yaml" -f "$ROOT/infra/kind/admission-policy.yaml" >/dev/null

log "Ready. Registry: localhost:$REGISTRY_PORT (GC_KIND_REGISTRY). Apps: http://<host>.localhost"
log "Next: make kind-kubeconfig, then make kind-check."
