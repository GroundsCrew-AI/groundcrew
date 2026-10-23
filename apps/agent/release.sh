#!/usr/bin/env bash
# Signed agent releases (docs/agent-plan.md "A5 plan"). Run from a laptop with Docker and OpenSSL 3.
#
#   apps/agent/release.sh key    once: make the Ed25519 release key in ops/agent-release.key
#                                (never committed) and put its public key in the agent and install.sh
#   apps/agent/release.sh sign   each release: build both binaries reproducibly, write
#                                release/SHA256SUMS and sign it to release/SHA256SUMS.sig (commit both)
#
# The web image builds the same binaries again and refuses to serve them unless they match the
# signed SHA256SUMS, so the server can only serve what the key holder signed.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
KEY=${GC_RELEASE_KEY:-$ROOT/ops/agent-release.key}
PUB=$HERE/internal/release/release.pub

die() { echo "release: $*" >&2; exit 1; }
openssl version | grep -q '^OpenSSL 3' || die "OpenSSL 3 is required (Ed25519 with -rawin)"

case ${1:-} in
key)
  [ -e "$KEY" ] && die "$KEY exists already; a new key needs the rotation steps in docs/agent-plan.md"
  mkdir -p "$(dirname "$KEY")"
  (umask 077 && openssl genpkey -algorithm ed25519 -out "$KEY")
  openssl pkey -in "$KEY" -pubout -out "$PUB"
  python3 - "$PUB" "$HERE/packaging/install.sh" <<'PY'
import re, sys
pub = open(sys.argv[1]).read().strip()
path = sys.argv[2]
text = open(path).read()
text, n = re.subn(r"RELEASE_PUB='.*?'", lambda _: f"RELEASE_PUB='{pub}'", text, count=1, flags=re.S)
assert n == 1, "install.sh has no RELEASE_PUB"
open(path, "w").write(text)
PY
  echo "Private key: $KEY (keep a copy offline; it is git-ignored)"
  echo "Public key written to internal/release/release.pub and packaging/install.sh:"
  cat "$PUB"
  ;;
sign)
  [ -r "$KEY" ] || die "no release key at $KEY (apps/agent/release.sh key)"
  [ -s "$PUB" ] || die "internal/release/release.pub is empty (apps/agent/release.sh key)"
  version=$(cat "$HERE/VERSION")
  out=$HERE/release
  mkdir -p "$out"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  for arch in amd64 arm64; do
    (cd "$HERE" && GOOS=linux GOARCH=$arch ./go.sh build -trimpath -buildvcs=false \
      -ldflags "-s -w -buildid= -X main.version=$version" \
      -o "bin/groundcrew-agent-linux-$arch" ./cmd/groundcrew-agent)
    cp "$HERE/bin/groundcrew-agent-linux-$arch" "$tmp/"
  done
  (cd "$tmp" && sha256sum groundcrew-agent-linux-* >"$out/SHA256SUMS")
  openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$out/SHA256SUMS" -out "$out/SHA256SUMS.sig"
  openssl pkeyutl -verify -pubin -inkey "$PUB" -rawin -in "$out/SHA256SUMS" -sigfile "$out/SHA256SUMS.sig" >/dev/null
  echo "Signed agent $version:"
  cat "$out/SHA256SUMS"
  echo "Commit apps/agent/release/SHA256SUMS and SHA256SUMS.sig with this version."
  ;;
*)
  sed -n '2,10p' "$0"; exit 2 ;;
esac
