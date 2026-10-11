#!/usr/bin/env bash
# make dind-check: prove the sandbox build path end to end, from inside the worker container.
#   1. the worker talks to the dind service over TLS, and the host's Docker socket is not mounted
#   2. it builds demo/fixtures/express-redis in dind and smoke-tests it at $SANDBOX_SMOKE_HOST
#   3. it pushes the image to kind-registry:5000
#   4. as groundcrew-deployer: gc-dind-check namespace, a Deployment of that image (ready only
#      when GET /healthz answers), a Service and an Ingress; GET http://dind-check.localhost/healthz
# Everything is removed at the end (the image stays in the local registry). Needs make kind-up,
# make kind-kubeconfig, and make dev KIND=1 running. Exits non-zero on the first failure.
source "$(dirname "$0")/lib.sh"
ensure_tools
KCFG="${KUBECONFIG_FILE:-$STATE/groundcrew-deployer.host.kubeconfig}"
[ -f "$KCFG" ] || die "no $KCFG; run make kind-kubeconfig first."

COMPOSE=(docker compose -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.kind.yml")
FIXTURE="$ROOT/demo/fixtures/express-redis/repo"
RUN_ID="dind-check-$(date -u +%Y%m%d%H%M%S)"
IMAGE="kind-registry:5000/gc-dind-check:$RUN_ID"
CHECK_NS=gc-dind-check
HOSTNAME=dind-check.localhost
SMOKE=""

k() { kubectl --kubeconfig "$KCFG" "$@"; }
w() { "${COMPOSE[@]}" exec -T worker "$@"; }
step() { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok() { printf '  ok    %s\n' "$*"; }

cleanup() {
  step "Cleanup"
  [ -n "$SMOKE" ] && w docker rm -f "$SMOKE" >/dev/null 2>&1 && ok "removed smoke container"
  w docker image rm "$IMAGE" >/dev/null 2>&1 && ok "removed $IMAGE from dind"
  if k get namespace "$CHECK_NS" >/dev/null 2>&1; then
    k delete namespace "$CHECK_NS" --wait=true --timeout=120s >/dev/null && ok "deleted namespace $CHECK_NS"
  fi
}
trap cleanup EXIT

step "Worker -> dind"
[ "$("${COMPOSE[@]}" ps -q worker)" ] || die "the worker is not running; start make dev KIND=1."
mounts=$(docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' "$("${COMPOSE[@]}" ps -q worker)")
case "$mounts" in *docker.sock*) die "the worker mounts a Docker socket: $mounts" ;; esac
ok "no Docker socket mounted in the worker"
# shellcheck disable=SC2016  # expanded inside the worker
env_line=$(w sh -c 'echo "$DOCKER_HOST $DOCKER_TLS_VERIFY $DOCKER_CERT_PATH $SANDBOX_SMOKE_HOST $GC_KIND_REGISTRY"')
[ "$env_line" = "tcp://dind:2376 1 /certs/client dind kind-registry:5000" ] \
  || die "unexpected worker env: $env_line"
ok "DOCKER_HOST=tcp://dind:2376 with TLS, SANDBOX_SMOKE_HOST=dind, GC_KIND_REGISTRY=kind-registry:5000"
server=$(w docker info --format '{{.Name}} {{.ServerVersion}}') || die "the worker cannot reach dind over TLS"
host_name=$(docker info --format '{{.Name}}')
[ "${server%% *}" != "$host_name" ] || die "the worker reached the host's daemon ($host_name), not dind"
ok "docker info from the worker: $server (host daemon is $host_name)"
if w env DOCKER_TLS_VERIFY= DOCKER_HOST=tcp://dind:2375 docker info >/dev/null 2>&1; then
  die "dind answers without TLS on 2375"
fi
ok "dind refuses plain TCP on 2375"

step "Build demo/fixtures/express-redis in dind"
tmp=$(mktemp -d)
cp -r "$FIXTURE/." "$tmp/"
cat >"$tmp/Dockerfile" <<'EOF'
FROM node:20-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY server.js ./
USER node
EXPOSE 8080
CMD ["node", "server.js"]
EOF
start=$SECONDS
tar -C "$tmp" -c . | w docker build -q -t "$IMAGE" - >/dev/null
rm -rf "$tmp"
ok "built $IMAGE ($((SECONDS - start)) s)"

step "Smoke test in dind at \$SANDBOX_SMOKE_HOST"
SMOKE=$(w docker run -d -P --label project=groundcrew "$IMAGE")
port=$(w docker port "$SMOKE" 8080/tcp | head -1 | sed 's/.*://')
w python -c '
import os, sys, time, urllib.request
host = os.environ["SANDBOX_SMOKE_HOST"]
url = f"http://{host}:{sys.argv[1]}/healthz"
for _ in range(30):
    try:
        print(urllib.request.urlopen(url, timeout=2).read().decode()); break
    except OSError:
        time.sleep(1)
else:
    sys.exit(f"no answer from {url}")
' "$port" | sed 's/^/  ok    GET http:\/\/dind:'"$port"'\/healthz -> /'
w docker rm -f "$SMOKE" >/dev/null && SMOKE=""

step "Push to kind-registry:5000"
w docker push -q "$IMAGE" >/dev/null
ok "pushed $IMAGE"

step "Run it in kind as groundcrew-deployer"
k create -f - >/dev/null <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: $CHECK_NS
  labels: {project: groundcrew, groundcrew/deploy: $RUN_ID}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: groundcrew-deployer, namespace: $CHECK_NS}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: gc-namespace-admin}
subjects: [{kind: ServiceAccount, name: $SA, namespace: $NAMESPACE}]
YAML
ok "namespace $CHECK_NS and its RoleBinding"
k apply -n "$CHECK_NS" -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
  labels: {project: groundcrew, groundcrew/deploy: $RUN_ID}
spec:
  replicas: 1
  selector: {matchLabels: {app: dind-check}}
  template:
    metadata:
      labels: {app: dind-check, project: groundcrew, groundcrew/deploy: $RUN_ID}
    spec:
      containers:
        - name: app
          image: $IMAGE
          ports: [{containerPort: 8080}]
          readinessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 2}
          resources: {limits: {cpu: 250m, memory: 128Mi}}
---
apiVersion: v1
kind: Service
metadata:
  name: app
  labels: {project: groundcrew, groundcrew/deploy: $RUN_ID}
spec:
  selector: {app: dind-check}
  ports: [{port: 80, targetPort: 8080}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app
  labels: {project: groundcrew, groundcrew/deploy: $RUN_ID}
spec:
  ingressClassName: nginx
  rules:
    - host: $HOSTNAME
      http:
        paths: [{path: /, pathType: Prefix, backend: {service: {name: app, port: {number: 80}}}}]
YAML
start=$SECONDS
k -n "$CHECK_NS" rollout status deployment/app --timeout=180s >/dev/null \
  || { k -n "$CHECK_NS" get events --sort-by=.lastTimestamp | tail -5; die "the pod did not become ready"; }
ok "pod pulled $IMAGE and passed GET /healthz ($((SECONDS - start)) s)"
for _ in $(seq 30); do
  body=$(curl -fsS --max-time 2 "http://127.0.0.1/healthz" -H "Host: $HOSTNAME" 2>/dev/null) && break
  sleep 1
done
[ -n "${body:-}" ] || die "no answer from http://$HOSTNAME/healthz through ingress-nginx"
ok "GET http://$HOSTNAME/healthz -> $body"

step "PASS"
