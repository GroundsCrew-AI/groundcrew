#!/usr/bin/env bash
# From a laptop: copy infra/platform/host to the platform host and run install.sh there.
#   infra/platform/host/push.sh [step]     (host from `terraform output -raw elastic_ip`)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=${GC_HOST:-$(cd "$HERE/.." && terraform output -raw elastic_ip)}
KEY=${GC_SSH_KEY:-$HOME/.ssh/groundkrew}
rsync -a --delete -e "ssh -i $KEY" "$HERE/" "ubuntu@$HOST:/tmp/gc-host/"
ssh -i "$KEY" "ubuntu@$HOST" "sudo bash /tmp/gc-host/install.sh $*"
