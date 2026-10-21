# The build daemon: rootless Docker as user gcbuild (uid and gid 1500), gVisor for every
# container, data on its own 30 GB XFS filesystem, limits on its systemd slice.
set -euo pipefail
UID_=1500
HOME_=/var/lib/gcbuild-home
DATA=/var/lib/gcbuild
IMG=/var/lib/gcbuild.img
as_build() { sudo -u gcbuild env XDG_RUNTIME_DIR=/run/user/$UID_ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_/bus HOME=$HOME_ "$@"; }

# User and group with fixed ids (the worker container joins gid 1500 to use the socket).
if getent group gcbuild >/dev/null && [ "$(getent group gcbuild | cut -d: -f3)" != $UID_ ]; then
  old=$(getent group gcbuild | cut -d: -f3)
  systemctl stop user@$UID_.service 2>/dev/null || true
  groupmod -g $UID_ gcbuild
  find "$HOME_" "$DATA" -xdev -group "$old" -exec chgrp -h $UID_ {} + 2>/dev/null || true
fi
getent group gcbuild >/dev/null || groupadd --system -g $UID_ gcbuild
id gcbuild >/dev/null 2>&1 || useradd --system --uid $UID_ --gid $UID_ --create-home \
  --home-dir "$HOME_" --shell /usr/sbin/nologin gcbuild
grep -q "^gcbuild:" /etc/subuid || echo "gcbuild:300000:65536" >>/etc/subuid
grep -q "^gcbuild:" /etc/subgid || echo "gcbuild:300000:65536" >>/etc/subgid

install -m 0644 "$HERE/files/apparmor-rootlesskit" /etc/apparmor.d/usr.bin.rootlesskit
apparmor_parser -r /etc/apparmor.d/usr.bin.rootlesskit

# Its own filesystem: a build can fill it, never the root volume Postgres lives on.
if [ ! -f "$IMG" ]; then
  fallocate -l 30G "$IMG"; chmod 0600 "$IMG"; mkfs.xfs -q "$IMG"
fi
install -d -m 0700 -o gcbuild -g gcbuild "$DATA"
grep -q " $DATA " /etc/fstab || echo "$IMG $DATA xfs loop,nodev,nosuid 0 0" >>/etc/fstab
mountpoint -q "$DATA" || mount "$DATA"
chown gcbuild:gcbuild "$DATA"; chmod 0700 "$DATA"

install -d /etc/systemd/system/user-$UID_.slice.d
install -m 0644 "$HERE/files/gcbuild-slice.conf" /etc/systemd/system/user-$UID_.slice.d/50-groundcrew.conf
systemctl daemon-reload
loginctl enable-linger gcbuild
systemctl start user@$UID_.service
for _ in $(seq 1 30); do [ -S /run/user/$UID_/bus ] && break; sleep 1; done

if [ ! -f "$HOME_/.config/systemd/user/docker.service" ]; then
  as_build dockerd-rootless-setuptool.sh install >/dev/null
fi
rm -f "$HOME_/.config/systemd/user/docker.service.d/runsc.conf"
install -d -o gcbuild -g gcbuild "$HOME_/.config/docker"
# daemon.json sets "group": "root": in the daemon's user namespace that is the host's gcbuild
# group, which the worker container has (group_add), so the worker can use the socket. The
# default, "docker", maps to a sub-gid nobody else is in.
install -m 0644 -o gcbuild -g gcbuild "$HERE/files/rootless-daemon.json" "$HOME_/.config/docker/daemon.json"
as_build systemctl --user daemon-reload
as_build systemctl --user enable docker >/dev/null 2>&1
as_build systemctl --user restart docker
for _ in $(seq 1 30); do [ -S /run/user/$UID_/docker.sock ] && break; sleep 1; done
# The worker bind-mounts the socket file, which stays pinned to the one that existed when it
# started; the restart above made a new one. Restarting the worker mounts the new socket.
if docker inspect groundcrew-worker-1 >/dev/null 2>&1; then
  docker restart groundcrew-worker-1 >/dev/null
  echo "worker restarted to pick up the new build daemon socket"
fi
# The previous data root, if any, is not used any more.
rm -rf "$HOME_/.local/share/docker"

D="as_build docker --host unix:///run/user/$UID_/docker.sock"
$D info --format 'build daemon: rootless={{range .SecurityOptions}}{{if eq . "name=rootless"}}yes{{end}}{{end}} default-runtime={{.DefaultRuntime}} data={{.DockerRootDir}}'
for image in moby/buildkit:v0.25.1-rootless curlimages/curl:8.17.0; do $D pull -q "$image" >/dev/null; done
