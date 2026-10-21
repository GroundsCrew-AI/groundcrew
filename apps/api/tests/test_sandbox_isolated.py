"""The isolated sandbox (the platform host's rootless gVisor build daemon): BuildKit runs in a
container of the run's own, never privileged and with no host mounts; the smoke container
publishes no port and is probed from inside the daemon; timeouts stop BuildKit; cleanup removes
the builder too."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.sandbox import IsolatedSandbox, SmokeEnv, get_sandbox
from app.sandbox import docker as docker_module
from app.sandbox.runner import CommandResult

from .test_sandbox import FakeDocker, _settings

pytestmark = pytest.mark.anyio

RUN = "4f1c2d3e-aaaa-bbbb-cccc-000000000001"


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_module, "_POLL_SECONDS", 0.01)


def _sandbox(fake: FakeDocker, **settings: Any) -> IsolatedSandbox:
    return IsolatedSandbox(_settings(sandbox_provider="isolated", **settings), fake)


def _flat(fake: FakeDocker) -> list[str]:
    return [" ".join(c.args) for c in fake.calls]


def test_the_isolated_provider_is_chosen_by_setting() -> None:
    assert isinstance(get_sandbox(_settings(sandbox_provider="isolated")), IsolatedSandbox)


async def test_build_starts_an_unprivileged_buildkit_and_loads_the_image(tmp_path: Path) -> None:
    fake = FakeDocker(
        replies={
            "inspect": CommandResult(1, "", "no such container"),
            "buildx": lambda args: CommandResult(1 if args[1] == "inspect" else 0, "", ""),
        }
    )
    result = await _sandbox(fake).build(tmp_path, tmp_path / "Dockerfile", "app:1", run_id=RUN)
    assert result.ok and result.image_ref == "app:1"
    (start,) = [c for c in fake.find("run")]
    assert start.args[:3] == ["run", "--detach", "--name"]
    assert start.args[3] == f"gcb-bk-{RUN}"
    assert "BUILDKITD_FLAGS=--oci-worker-no-process-sandbox" in start.args
    assert start.args[-1] == "moby/buildkit:v0.25.1-rootless"
    for forbidden in ("--privileged", "-v", "--volume", "--mount", "--network=host", "-p"):
        assert forbidden not in start.args
    calls = _flat(fake)
    assert (
        f"buildx create --name gcb-{RUN} --driver remote docker-container://gcb-bk-{RUN}" in calls
    )
    (build,) = [c for c in fake.find("buildx") if c.args[1] == "build"]
    assert build.args[2:4] == ["--builder", f"gcb-{RUN}"]
    assert "--load" in build.args and "linux/amd64" in build.args
    assert f"groundcrew.run={RUN}" in build.args
    assert build.env is not None and "BUILDX_CONFIG" in build.env


async def test_a_running_builder_is_reused(tmp_path: Path) -> None:
    fake = FakeDocker(replies={"inspect": CommandResult(0, "true\n", "")})
    await _sandbox(fake).build(tmp_path, tmp_path / "Dockerfile", "app:2", run_id=RUN)
    assert not fake.find("run")
    assert not [c for c in fake.find("buildx") if c.args[1] == "create"]


async def test_a_timed_out_build_stops_buildkit(tmp_path: Path) -> None:
    def buildx(args: list[str]) -> CommandResult:
        if args[1] == "build":
            return CommandResult(-9, "#5 RUN sleep 9999\n", "", timed_out=True)
        return CommandResult(0, "", "")

    fake = FakeDocker(replies={"inspect": CommandResult(0, "true", ""), "buildx": buildx})
    result = await _sandbox(fake, quota_max_build_seconds=30).build(
        tmp_path, tmp_path / "Dockerfile", "app:3", run_id=RUN
    )
    assert result.timed_out and not result.ok
    assert "timed out after 30s" in result.log_tail
    calls = _flat(fake)
    assert f"rm --force --volumes gcb-bk-{RUN}" in calls
    assert f"buildx rm gcb-{RUN}" in calls
    (build,) = [c for c in fake.find("buildx") if c.args[1] == "build"]
    assert build.timeout == 30


async def test_a_sandbox_that_cannot_start_is_a_failed_build(tmp_path: Path) -> None:
    fake = FakeDocker(
        replies={
            "inspect": CommandResult(1, "", ""),
            "run": CommandResult(125, "", "runsc: cannot start"),
        }
    )
    result = await _sandbox(fake).build(tmp_path, tmp_path / "Dockerfile", "app:4", run_id=RUN)
    assert not result.ok and result.unavailable
    assert "build sandbox did not start" in result.log_tail
    assert result.error_lines[0].startswith("Groundcrew's build service is not available")


def _smoke_docker(codes: list[str]) -> FakeDocker:
    answers = iter(codes)

    def inspect(args: list[str]) -> CommandResult:
        if "{{.State.Running}} {{.State.ExitCode}}" in args:
            return CommandResult(0, "true 0", "")
        return CommandResult(0, "172.17.0.5 \n", "")

    return FakeDocker(
        replies={
            "inspect": inspect,
            "exec": lambda args: CommandResult(0, next(answers, "200"), ""),
        }
    )


async def test_smoke_publishes_nothing_and_probes_from_inside() -> None:
    fake = _smoke_docker(["000", "502", "200", "404"])
    env = [SmokeEnv("DATABASE_URL", SecretStr("postgresql://u:p@db/x"), secret=True)]
    result = await _sandbox(fake).run_smoke("app:1", 8000, env, run_id=RUN, health_path="/health")
    assert result.ok, result.detail
    assert [(c.path, c.status_code) for c in result.checks] == [("/health", 200), ("/", 404)]
    app, probe = fake.find("run")
    assert "--publish" not in app.args and "-p" not in app.args
    assert "--privileged" not in app.args and "--cap-drop" in app.args
    assert "postgresql://u:p@db/x" not in " ".join(app.args)  # env values never in argv
    assert probe.args[3] == f"gcb-probe-{RUN}" and probe.args[-3:] == [
        "curlimages/curl:8.17.0",
        "sleep",
        "600",
    ]
    execs = fake.find("exec")
    assert execs[0].args[1] == f"gcb-probe-{RUN}"
    assert execs[0].args[-1] == "http://172.17.0.5:8000/health"
    removed = [c.args[-1] for c in fake.find("rm")]
    assert f"gcb-probe-{RUN}" in removed and any(n.startswith("gc-smoke-") for n in removed)


async def test_smoke_without_an_address_fails_clearly() -> None:
    fake = FakeDocker(replies={"inspect": CommandResult(0, " \n", "")})
    result = await _sandbox(fake).run_smoke("app:1", 8000, [], run_id=RUN)
    assert not result.ok and "no address" in result.detail


async def test_cleanup_removes_the_builder_then_everything_labelled() -> None:
    fake = FakeDocker(
        replies={
            "ps": CommandResult(0, "c1\nc2\n", ""),
            "images": CommandResult(0, "i1\n", ""),
        }
    )
    await _sandbox(fake).cleanup(RUN)
    calls = _flat(fake)
    assert calls[0] == f"buildx rm gcb-{RUN}"
    assert "rm --force --volumes c1 c2" in calls and "rmi --force i1" in calls
