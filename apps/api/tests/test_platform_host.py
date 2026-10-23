"""The platform host's setup (infra/platform/host) keeps its isolation and hardening: Compose
services are non-root, read-only, unprivileged, published only on 127.0.0.1, with limits and
health checks; Postgres is reachable only on the internal network; only the API and worker may
reach the metadata service; the build daemon is rootless gVisor with public DNS; nginx exposes
only the API's public routes; the scripts parse."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

HOST = Path(__file__).resolve().parents[3] / "infra" / "platform" / "host"


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((HOST / "compose.yml").read_text())
    return data


def test_every_service_is_locked_down() -> None:
    services = _compose()["services"]
    assert set(services) == {"postgres", "migrate", "api", "worker", "web", "registry"}
    for name, svc in services.items():
        assert svc.get("privileged") is not True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["read_only"] is True, name
        user = str(svc["user"])
        assert not user.startswith("0") and user != "root", name
        assert svc["logging"]["driver"] == "journald", name
        assert "mem_limit" in svc, name
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
        for volume in svc.get("volumes", []):
            assert "/var/run/docker.sock" not in volume, (name, volume)
        if name != "migrate":
            assert svc["restart"] == "unless-stopped", name
            assert "healthcheck" in svc, name


def test_postgres_is_only_on_the_internal_network() -> None:
    compose = _compose()
    pg = compose["services"]["postgres"]
    assert "ports" not in pg and pg["networks"] == ["backend"]
    assert compose["networks"]["backend"]["internal"] is True
    assert "scram-sha-256" in pg["environment"]["POSTGRES_INITDB_ARGS"]


def test_only_api_and_worker_may_reach_the_metadata_service() -> None:
    services = _compose()["services"]
    addresses = {
        name: services[name]["networks"]["egress"]["ipv4_address"]
        for name in ("api", "worker", "web")
    }
    firewall = (HOST / "bin" / "gc-firewall").read_text()
    allowed = re.search(r"METADATA_ALLOWED=\(([^)]*)\)", firewall)
    assert allowed is not None
    assert set(allowed.group(1).split()) == {addresses["api"], addresses["worker"]}
    assert addresses["web"] not in allowed.group(1)


def test_the_worker_builds_only_through_the_rootless_daemon() -> None:
    worker = _compose()["services"]["worker"]
    assert worker["environment"]["SANDBOX_PROVIDER"] == "isolated"
    assert worker["environment"]["DOCKER_HOST"] == "unix:///run/gcbuild/docker.sock"
    assert "/run/user/1500/docker.sock:/run/gcbuild/docker.sock" in worker["volumes"]
    assert worker["group_add"] == ["1500"]
    # The worker reaches the socket through that group. Rootless dockerd gives its socket the
    # group named here, inside its user namespace, where root is the host's gcbuild (1500);
    # its default, "docker", lands on a sub-gid the worker is not in (EACCES on connect).
    daemon = json.loads((HOST / "files" / "rootless-daemon.json").read_text())
    assert daemon["group"] == "root"


def test_the_build_daemon_is_rootless_gvisor_with_its_own_limits() -> None:
    daemon = json.loads((HOST / "files" / "rootless-daemon.json").read_text())
    assert daemon["default-runtime"] == "runsc"
    assert daemon["runtimes"]["runsc"]["path"] == "/usr/bin/runsc"
    assert daemon["dns"] == ["1.1.1.1", "8.8.8.8"]
    assert daemon["data-root"].startswith("/var/lib/gcbuild/")
    sl = (HOST / "files" / "gcbuild-slice.conf").read_text()
    for line in ("MemoryMax=", "TasksMax=", "CPUQuota="):
        assert line in sl
    firewall = (HOST / "bin" / "gc-firewall").read_text()
    for net in ("169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"):
        assert net in firewall
    assert "--uid-owner" in firewall


def test_nginx_exposes_only_the_apis_public_routes() -> None:
    conf = (HOST / "nginx" / "groundcrew.conf").read_text()
    api = conf[conf.index("server_name __API_DOMAIN__;", conf.index("listen 443")) :]
    locations = re.findall(r"location ([^{]+)\{", api)
    assert [loc.strip() for loc in locations] == [
        "~ ^/runs/[0-9a-fA-F-]+/events$",
        "= /github/webhook",
        "/v2/",
        "= /_registry_auth",
        "@registry_401",
        "/agent/v1/",
        "= /health",
        "= /ready",
        "/",
    ]
    assert "return 404;" in api.split("location / {")[1]
    # The registry: pulls only, each one checked by the API; its auth location is internal.
    v2 = api.split("location /v2/ {")[1].split("location = /_registry_auth")[0]
    assert "limit_except GET HEAD { deny all; }" in v2 and "auth_request /_registry_auth;" in v2
    assert "internal;" in api.split("location = /_registry_auth {")[1].split("}")[0]
    # Agent calls are signed and checked by the API; nginx limits each address.
    assert "limit_req zone=gc_agent" in api.split("location /agent/v1/ {")[1].split("}")[0]
    assert "proxy_buffering off;" in api and "proxy_read_timeout 3600s;" in api
    common_tls = (HOST / "nginx" / "groundcrew-common.conf").read_text()
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in common_tls
    assert conf.count("Strict-Transport-Security") >= 2
    # The client address is the connection's, never what a client sends.
    assert "$proxy_add_x_forwarded_for" not in conf
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in conf
    common = (HOST / "nginx" / "groundcrew-common.conf").read_text()
    assert "client_max_body_size 1m;" in common and "server_tokens off;" in common


@pytest.mark.parametrize(
    "script",
    sorted(
        str(p.relative_to(HOST))
        for p in [*HOST.glob("bin/*"), *HOST.glob("*.sh"), *HOST.glob("steps/*.sh")]
    ),
)
def test_scripts_parse(script: str) -> None:
    subprocess.run(["bash", "-n", str(HOST / script)], check=True)  # noqa: S603, S607


def test_certificates_need_the_operators_consent() -> None:
    certs = (HOST / "bin" / "gc-certs").read_text()
    assert "agreed=no" in certs and "pass --agree-tos" in certs


def _tmpfs_options(svc: dict[str, Any], mount: str) -> dict[str, str] | None:
    for entry in svc.get("tmpfs", []):
        path, _, opts = str(entry).partition(":")
        if path == mount:
            return dict(o.split("=", 1) for o in opts.split(",") if "=" in o)
    return None


@pytest.mark.parametrize("name", ["api", "worker"])
def test_temp_files_go_to_a_tmpfs_the_service_user_owns(name: str) -> None:
    """The root filesystem is read-only: TMPDIR (and HOME, for git and the Docker CLI) must be a
    tmpfs owned by the uid the service runs as, or clones and kubeconfigs fail with EACCES."""
    svc = _compose()["services"][name]
    env = svc["environment"]
    assert env["TMPDIR"] == env["HOME"] == "/scratch", name
    opts = _tmpfs_options(svc, "/scratch")
    assert opts is not None, f"{name}: /scratch is not a tmpfs"
    uid, _, gid = str(svc["user"]).partition(":")
    assert (opts.get("uid"), opts.get("gid")) == (uid, gid), name
    assert opts.get("mode") == "0700" and "size" in opts, name
    assert not any(str(v).split(":")[-1] == "/scratch" for v in svc.get("volumes", [])), name
