# Shared settings for the kind scripts: pinned versions, checksums, and tool lookup.
# Sourced, not run. Tools are downloaded once into .tools/bin (git-ignored), so nothing is
# installed system-wide; kind or kubectl already on PATH at the pinned version are used as is.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLS="$ROOT/.tools/bin"
STATE="$ROOT/.kind"
CLUSTER=groundcrew
CONTEXT="kind-$CLUSTER"
REGISTRY_NAME=kind-registry
REGISTRY_PORT="${GC_KIND_REGISTRY_PORT:-5001}"
REGISTRY_IMAGE="registry:3.0.0"
NAMESPACE=groundcrew-system
SA=groundcrew-deployer

KIND_VERSION=v0.33.0
KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
KUBECTL_VERSION=v1.37.0
KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
INGRESS_URL=https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml
INGRESS_SHA256=2a3ae008c8786431115502644e77ab398fdebfb721a5d1195ed3089cde3299df
METRICS_URL=https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml
METRICS_SHA256=1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b

log() { printf '\033[1m==>\033[0m %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# fetch URL SHA256 DEST: download and refuse anything whose checksum differs.
fetch() {
  local url=$1 sum=$2 dest=$3 tmp
  tmp=$(mktemp)
  curl -fsSL --retry 3 -o "$tmp" "$url" || { rm -f "$tmp"; die "download failed: $url"; }
  echo "$sum  $tmp" | sha256sum --check --status \
    || { rm -f "$tmp"; die "checksum mismatch for $url (expected $sum)"; }
  mv "$tmp" "$dest"
}

arch() {
  case "$(uname -m)" in
    x86_64) echo amd64 ;;
    *) die "only linux/amd64 has pinned checksums; install kind $KIND_VERSION and kubectl $KUBECTL_VERSION yourself (docs/kind.md)" ;;
  esac
}

ensure_tools() {
  mkdir -p "$TOOLS"
  if [ ! -x "$TOOLS/kind" ]; then
    if command -v kind >/dev/null && kind version | grep -q "$KIND_VERSION "; then
      ln -sf "$(command -v kind)" "$TOOLS/kind"
    else
      log "Downloading kind $KIND_VERSION into .tools/bin"
      fetch "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-$(arch)" \
        "$KIND_SHA256" "$TOOLS/kind"
      chmod +x "$TOOLS/kind"
    fi
  fi
  if [ ! -x "$TOOLS/kubectl" ]; then
    if command -v kubectl >/dev/null && kubectl version --client 2>/dev/null | grep -q "$KUBECTL_VERSION"; then
      ln -sf "$(command -v kubectl)" "$TOOLS/kubectl"
    else
      log "Downloading kubectl $KUBECTL_VERSION into .tools/bin"
      fetch "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$(arch)/kubectl" \
        "$KUBECTL_SHA256" "$TOOLS/kubectl"
      chmod +x "$TOOLS/kubectl"
    fi
  fi
  export PATH="$TOOLS:$PATH"
}

# kubectl as cluster admin, always against this cluster's context (never the current one).
kadmin() { kubectl --context "$CONTEXT" "$@"; }

cluster_exists() { kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; }
