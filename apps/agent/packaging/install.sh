#!/bin/sh
# Install the Groundcrew agent on a Linux host (systemd), and enroll it if a join token is given:
#
#   curl -fsSL https://groundkrew.com/agent/install.sh | sudo sh -s -- --token gcj_...
#
# Options:
#   --token TOKEN   the join token from Connections > Agent (enrolls and starts the agent)
#   --url URL       Groundcrew's API (default https://api.groundkrew.com)
#   --no-docker     do not add the agent's user to the docker group
#
# What it does: downloads groundcrew-agent for this CPU and checks it against SHA256SUMS,
# installs it to /usr/local/bin, creates the system user groundcrew-agent, writes
# /etc/groundcrew-agent/agent.yaml (only if missing), installs the systemd unit, then enrolls
# and starts the agent. Run it again to upgrade; the configuration is kept.
set -eu

DOWNLOADS=${GROUNDCREW_DOWNLOADS:-https://groundkrew.com/agent}
URL=https://api.groundkrew.com
TOKEN=
DOCKER=yes
while [ $# -gt 0 ]; do
  case $1 in
    --token) TOKEN=$2; shift 2 ;;
    --url) URL=$2; shift 2 ;;
    --no-docker) DOCKER=no; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "groundcrew: $*"; }
die() { printf '%s\n' "groundcrew: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ "$(uname -s)" = Linux ] || die "the agent runs on Linux"
command -v systemctl >/dev/null || die "systemd is required"
case $(uname -m) in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) die "unsupported CPU $(uname -m) (amd64 and arm64 are supported)" ;;
esac
if command -v curl >/dev/null; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null; then
  fetch() { wget -q "$1" -O "$2"; }
else
  die "curl or wget is required"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
BIN=groundcrew-agent-linux-$ARCH
say "downloading $BIN"
fetch "$DOWNLOADS/$BIN" "$TMP/$BIN"
fetch "$DOWNLOADS/SHA256SUMS" "$TMP/SHA256SUMS"
(cd "$TMP" && grep " $BIN\$" SHA256SUMS | sha256sum -c - >/dev/null) || die "checksum mismatch for $BIN"
install -m 0755 "$TMP/$BIN" /usr/local/bin/groundcrew-agent

if ! id groundcrew-agent >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /var/lib/groundcrew-agent --shell /usr/sbin/nologin groundcrew-agent
fi
if [ "$DOCKER" = yes ] && getent group docker >/dev/null; then
  usermod -aG docker groundcrew-agent
  say "added groundcrew-agent to the docker group, so it can read containers."
  say "note: docker group members can control Docker, which is root-equivalent on this host."
fi
if getent group systemd-journal >/dev/null; then
  # Reads the journal of the systemd units you list (their error lines go to Groundcrew).
  usermod -aG systemd-journal groundcrew-agent
fi
install -d -m 0700 -o groundcrew-agent -g groundcrew-agent /var/lib/groundcrew-agent
install -d -m 0755 /etc/groundcrew-agent
CONFIG=/etc/groundcrew-agent/agent.yaml
if [ ! -f "$CONFIG" ]; then
  fetch "$DOWNLOADS/agent.example.yaml" "$TMP/agent.yaml"
  sed "s#^  url: .*#  url: $URL#" "$TMP/agent.yaml" > "$CONFIG"
  say "wrote $CONFIG: list your services there"
fi
chown root:groundcrew-agent "$CONFIG"
chmod 0640 "$CONFIG"
fetch "$DOWNLOADS/groundcrew-agent.service" /etc/systemd/system/groundcrew-agent.service
systemctl daemon-reload

if [ -n "$TOKEN" ]; then
  runuser -u groundcrew-agent -- /usr/local/bin/groundcrew-agent enroll --config "$CONFIG" --token "$TOKEN"
  systemctl enable --now groundcrew-agent
  systemctl restart groundcrew-agent
  say "the agent is running: systemctl status groundcrew-agent"
elif [ -f /var/lib/groundcrew-agent/enrollment.json ]; then
  systemctl restart groundcrew-agent
  say "upgraded to $(/usr/local/bin/groundcrew-agent version); the agent was restarted"
  exit 0
else
  say "installed. Enroll it with the token from Groundcrew:"
  say "  sudo runuser -u groundcrew-agent -- groundcrew-agent enroll --token TOKEN"
  say "  sudo systemctl enable --now groundcrew-agent"
fi
say "next: add your services to $CONFIG, run sudo groundcrew-agent check --config $CONFIG,"
say "then sudo systemctl restart groundcrew-agent"
