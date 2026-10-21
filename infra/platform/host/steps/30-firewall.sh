# Firewall beside ufw: the build user's egress and which containers reach the metadata service.
set -euo pipefail
install -d /opt/groundcrew/bin
install -m 0755 "$HERE/bin/gc-firewall" /opt/groundcrew/bin/gc-firewall
install -m 0644 "$HERE/files/gc-firewall.service" /etc/systemd/system/gc-firewall.service
systemctl daemon-reload
systemctl enable gc-firewall.service >/dev/null 2>&1
systemctl restart gc-firewall.service
ufw status | grep -q "Status: active" || { echo "ufw is not active" >&2; exit 1; }

# SSH only from the admin addresses (the security group says the same). New ones first, so a
# session is never cut off.
if [ -n "${GC_ADMIN_CIDRS:-}" ]; then
  for cidr in $GC_ADMIN_CIDRS; do
    ufw allow from "${cidr%/32}" to any port 22 proto tcp >/dev/null
  done
  ufw status | awk '$1 == "22/tcp" && $2 == "ALLOW" {print $NF}' | while read -r from; do
    case " $GC_ADMIN_CIDRS " in *" $from/32 "*|*" $from "*) ;;
      *) ufw delete allow from "$from" to any port 22 proto tcp >/dev/null && echo "ssh: removed $from" ;; esac
  done
  echo "ssh allowed from: $GC_ADMIN_CIDRS"
fi
