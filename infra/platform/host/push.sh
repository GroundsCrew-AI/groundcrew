#!/usr/bin/env bash
# From a laptop: copy infra/platform/host to the platform host and run install.sh there.
#   infra/platform/host/push.sh [step]     (host from `terraform output -raw elastic_ip`)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=${GC_HOST:-$(cd "$HERE/.." && terraform output -raw elastic_ip)}
KEY=${GC_SSH_KEY:-$HOME/.ssh/id_ed25519}
# The SSH allowlist for ufw, the same as the security group's (Terraform admin_cidrs).
CIDRS=${GC_ADMIN_CIDRS:-$(cd "$HERE/.." && terraform output -json admin_cidrs | jq -r 'join(" ")')}
rsync -a --delete -e "ssh -i $KEY" "$HERE/" "ubuntu@$HOST:/tmp/gc-host/"
ssh -i "$KEY" "ubuntu@$HOST" "sudo GC_ADMIN_CIDRS='$CIDRS' bash /tmp/gc-host/install.sh $*"
