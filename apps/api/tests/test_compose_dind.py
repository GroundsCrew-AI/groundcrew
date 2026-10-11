"""The dind build daemon in docker-compose.yml, checked offline. The live proof is
`make dind-check` (docs/deploy-live.md)."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.kind.yml"]


def _compose(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / name).read_text())
    return data


def _mount_sources(service: dict[str, Any]) -> list[str]:
    sources = []
    for v in service.get("volumes", []):
        sources.append(v.split(":")[0] if isinstance(v, str) else str(v.get("source", "")))
    return sources


def test_no_service_mounts_a_docker_socket() -> None:
    for name in COMPOSE_FILES:
        for svc_name, svc in _compose(name)["services"].items():
            for source in _mount_sources(svc):
                assert "docker.sock" not in source, f"{name}: {svc_name} mounts {source}"
    assert "docker.sock" not in (ROOT / "docker-compose.yml").read_text().replace(
        "unix:///var/run/docker.sock", ""
    ), "only dind's own health check may name its socket"


def test_dind_uses_tls_and_publishes_no_ports() -> None:
    dind = _compose("docker-compose.yml")["services"]["dind"]
    assert dind["privileged"] is True  # dind cannot run otherwise; local dev only
    assert dind["hostname"] == "dind"  # the server certificate's name, checked by the worker
    assert "ports" not in dind and "network_mode" not in dind
    assert dind["environment"]["DOCKER_TLS_CERTDIR"] == "/certs"
    assert "dind-certs-client:/certs/client" in dind["volumes"]
    assert "dind-data:/var/lib/docker" in dind["volumes"]
    for limit in ("cpus", "mem_limit", "pids_limit"):
        assert limit in dind


def test_worker_reaches_dind_only_over_tls() -> None:
    services = _compose("docker-compose.yml")["services"]
    env = services["worker"]["environment"]
    assert env["DOCKER_HOST"] == "tcp://dind:2376"
    assert env["DOCKER_TLS_VERIFY"] == "1"
    assert env["DOCKER_CERT_PATH"] == "/certs/client"
    assert env["SANDBOX_SMOKE_HOST"] == "dind"
    assert "dind-certs-client:/certs/client:ro" in services["worker"]["volumes"]
    assert services["worker"]["depends_on"]["dind"]["condition"] == "service_healthy"
    # The API never builds.
    assert "DOCKER_HOST" not in services["api"]["environment"]


def test_cli_and_daemon_versions_match() -> None:
    dind_image = _compose("docker-compose.yml")["services"]["dind"]["image"]
    m = re.fullmatch(r"docker:(\d+\.\d+\.\d+)-dind", dind_image)
    assert m, f"dind image is not pinned to an exact version: {dind_image}"
    dockerfile = (ROOT / "apps" / "api" / "Dockerfile").read_text()
    assert f"docker:{m.group(1)}-cli" in dockerfile


def test_kind_overlay_wires_dind_to_the_registry() -> None:
    overlay = _compose("docker-compose.kind.yml")["services"]
    assert "kind" in overlay["dind"]["networks"]
    assert overlay["worker"]["environment"]["GC_KIND_REGISTRY"] == "kind-registry:5000"
    daemon = json.loads((ROOT / "infra" / "dind" / "daemon.json").read_text())
    assert daemon["insecure-registries"] == ["kind-registry:5000"]
    # kind nodes pull the name dind pushes (make kind-up writes this mirror).
    up = (ROOT / "scripts" / "kind" / "up.sh").read_text()
    assert '"$REGISTRY_NAME:5000"' in up
