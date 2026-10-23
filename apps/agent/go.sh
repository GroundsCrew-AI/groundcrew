#!/usr/bin/env bash
# Run the Go toolchain in the golang image (no local Go needed): ./go.sh test ./...
# The image is golang:1.23-bookworm pinned by digest, the same one apps/web/Dockerfile builds
# releases with, so a release built here and on the server is byte for byte the same.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cache=${GO_CACHE_DIR:-$HOME/.cache/groundcrew-go}
mkdir -p "$cache"
exec docker run --rm -u "$(id -u):$(id -g)" -v "$here:/src" -w /src -v "$cache:/cache" \
  -e GOCACHE=/cache/build -e GOMODCACHE=/cache/mod -e GOFLAGS=-buildvcs=false \
  -e CGO_ENABLED=0 ${GOOS:+-e GOOS=$GOOS} ${GOARCH:+-e GOARCH=$GOARCH} \
  ${GO_IMAGE:-golang@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db} go "$@"
