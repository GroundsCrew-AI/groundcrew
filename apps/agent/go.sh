#!/usr/bin/env bash
# Run the Go toolchain in the golang image (no local Go needed): ./go.sh test ./...
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cache=${GO_CACHE_DIR:-$HOME/.cache/groundcrew-go}
mkdir -p "$cache"
exec docker run --rm -u "$(id -u):$(id -g)" -v "$here:/src" -w /src -v "$cache:/cache" \
  -e GOCACHE=/cache/build -e GOMODCACHE=/cache/mod -e GOFLAGS=-buildvcs=false \
  -e CGO_ENABLED=0 ${GOOS:+-e GOOS=$GOOS} ${GOARCH:+-e GOARCH=$GOARCH} \
  golang:1.23-bookworm go "$@"
