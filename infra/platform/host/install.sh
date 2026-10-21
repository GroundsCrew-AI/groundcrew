#!/usr/bin/env bash
# Set up (or bring up to date) the Groundcrew platform host from this directory. Idempotent:
# safe to run again after any change here, and the way to rebuild a host from scratch after
# `terraform apply` (docs/operations.md). Run as root on the host:
#
#   sudo ./install.sh            every step
#   sudo ./install.sh 40-nginx   one step
#
# From a laptop: infra/platform/host/push.sh <host> copies this directory and runs it.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
HERE=$(cd "$(dirname "$0")" && pwd)
export HERE
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || { echo "expects Ubuntu 24.04" >&2; exit 1; }

for step in "$HERE"/steps/*.sh; do
  name=$(basename "$step" .sh)
  if [ $# -gt 0 ] && [ "$1" != "$name" ]; then continue; fi
  echo "== $name"
  bash "$step"
done
echo "== done"
