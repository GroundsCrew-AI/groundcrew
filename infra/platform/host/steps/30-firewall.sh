# Firewall beside ufw: the build user's egress and which containers reach the metadata service.
set -euo pipefail
install -d /opt/groundcrew/bin
install -m 0755 "$HERE/bin/gc-firewall" /opt/groundcrew/bin/gc-firewall
install -m 0644 "$HERE/files/gc-firewall.service" /etc/systemd/system/gc-firewall.service
systemctl daemon-reload
systemctl enable gc-firewall.service >/dev/null 2>&1
systemctl restart gc-firewall.service
ufw status | grep -q "Status: active" || { echo "ufw is not active" >&2; exit 1; }
