# Packages: Docker (main daemon and rootless extras), gVisor, nginx, certbot; journald and
# Docker defaults.
set -euo pipefail
install -m 0755 -d /etc/apt/keyrings
[ -f /etc/apt/keyrings/docker.gpg ] || curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
[ -f /etc/apt/keyrings/gvisor.gpg ] || curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /etc/apt/keyrings/gvisor.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" >/etc/apt/sources.list.d/docker.list
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/gvisor.gpg] https://storage.googleapis.com/gvisor/releases release main" >/etc/apt/sources.list.d/gvisor.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
  docker-compose-plugin docker-ce-rootless-extras uidmap dbus-user-session slirp4netns \
  runsc nginx certbot xfsprogs jq acl >/dev/null

install -D -m 0644 "$HERE/files/journald.conf" /etc/systemd/journald.conf.d/60-groundcrew.conf
systemctl restart systemd-journald

if ! cmp -s "$HERE/files/docker-daemon.json" /etc/docker/daemon.json; then
  install -m 0644 "$HERE/files/docker-daemon.json" /etc/docker/daemon.json
  systemctl restart docker
fi
systemctl enable --now docker >/dev/null 2>&1
echo "docker $(docker version --format '{{.Server.Version}}'), $(runsc --version | head -1)"
