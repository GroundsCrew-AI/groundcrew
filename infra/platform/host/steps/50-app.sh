# Groundcrew's own files: Compose, the gc-* commands, secrets layout, the build sweeper.
set -euo pipefail
install -d -m 0755 /opt/groundcrew /opt/groundcrew/bin
install -m 0644 "$HERE/compose.yml" /opt/groundcrew/compose.yml
for cmd in gc-secrets gc-status gc-build-sweep gc-isolation-check gc-release; do
  install -m 0755 "$HERE/bin/$cmd" /opt/groundcrew/bin/$cmd
done
echo 'export PATH="$PATH:/opt/groundcrew/bin"' >/etc/profile.d/groundcrew.sh
install -d -m 0700 /var/lib/groundcrew /var/lib/groundcrew/releases
[ -f /opt/groundcrew/release.env ] || : >/opt/groundcrew/release.env
/opt/groundcrew/bin/gc-secrets init >/dev/null
[ -f /etc/groundcrew/config.env ] || install -m 0600 /dev/null /etc/groundcrew/config.env
install -m 0644 "$HERE/files/gc-build-sweep.service" "$HERE/files/gc-build-sweep.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gc-build-sweep.timer >/dev/null 2>&1
echo "secrets: $(/opt/groundcrew/bin/gc-secrets check | tr '\n' ';')"
