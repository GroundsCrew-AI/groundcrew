"""Sandbox (Milestone 2): exact docker arguments, smoke container limits, secrets never in argv
or output, cleanup on every path, log tails, the failing-step parser, and registry pushes.
The unit tests fake the Docker CLI; `docker` tests use a real daemon; the `live` test builds
GroundsCrew-AI/legacy-flask-app with a naive Dockerfile and expects the psycopg2 failure."""

import base64
import itertools
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.sandbox import (
    DockerSandbox,
    NebiusSandbox,
    PushTarget,
    RegistryLogin,
    SandboxError,
    SmokeEnv,
    get_sandbox,
)
from app.sandbox import docker as docker_module
from app.sandbox.logs import Scrubber, error_lines, failing_step, tail
from app.sandbox.registry import ecr_login, ecr_target, kind_target
from app.sandbox.runner import CommandResult, run_docker

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "sandbox"
SECRET = "postgresql://app:s3cret-value-9@db:5432/app"  # noqa: S105 (test value)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "sandbox_smoke_host": "smoke.test",
        "sandbox_smoke_timeout_seconds": 0.5,
        "gc_kind_registry": "kind-registry:5000",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@dataclass
class Call:
    args: list[str]
    timeout: float
    stdin: str | None
    env: dict[str, str] | None


@dataclass
class FakeDocker:
    """Records every docker call; `replies` maps the subcommand to a result or a function."""

    replies: dict[str, CommandResult | Callable[[list[str]], CommandResult]] = field(
        default_factory=dict
    )
    calls: list[Call] = field(default_factory=list)

    async def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(Call(args, timeout, stdin, env))
        reply = self.replies.get(args[0], CommandResult(0, "", ""))
        return reply(args) if callable(reply) else reply

    def find(self, sub: str) -> list[Call]:
        return [c for c in self.calls if c.args[0] == sub]


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_module, "_POLL_SECONDS", 0.01)


# --- logs ---------------------------------------------------------------------------------

NAIVE_LOG = (FIXTURES / "psycopg2-build.log").read_text()


def test_failing_step_and_cause_from_buildkit_output() -> None:
    assert failing_step(NAIVE_LOG) == "RUN pip install --no-cache-dir -r requirements.txt"
    lines = error_lines(NAIVE_LOG)
    assert "Error: pg_config executable not found." in lines
    assert not any("originates from a subprocess" in line for line in lines)
    assert len(lines) == len(set(lines)) <= 12


def test_network_flake_log_is_kept_for_builder() -> None:
    """A transient PyPI failure (broken pipe while installing build dependencies), captured
    live. Builder (M4) must retry it without spending a fix attempt; the parser still finds
    the step."""
    log = (FIXTURES / "pip-network-flake.log").read_text()
    assert failing_step(log) == "RUN pip install --no-cache-dir -r requirements.txt"
    assert "Connection broken" in log and "pg_config" not in log


def test_failing_step_unknown_when_nothing_failed() -> None:
    assert failing_step("#5 [2/3] RUN echo hi\n#5 DONE 0.1s\n") is None


def test_tail_keeps_the_end_from_a_line_start() -> None:
    log = "\n".join(f"line {n}" for n in range(5000))
    out = tail(log, 200)
    assert out.endswith("line 4999")
    assert out.startswith("[... ") and "\nline " in out
    assert len(out) < 260


def test_scrubber_hides_exact_values_and_known_patterns() -> None:
    scrub = Scrubber([SECRET, "abc"])  # too short to scrub: would blank ordinary words
    text = f"url={SECRET} token ghp_{'a' * 36} abc"
    out = scrub(text)
    assert SECRET not in out and "ghp_" not in out
    assert out.endswith("abc")


# --- build --------------------------------------------------------------------------------


async def test_build_arguments_and_success(tmp_path: Path) -> None:
    fake = FakeDocker()
    result = await DockerSandbox(_settings(), fake).build(
        tmp_path, tmp_path / "Dockerfile", "gc-build/app:1", run_id="run-1"
    )
    assert result.ok and result.image_ref == "gc-build/app:1"
    (call,) = fake.calls
    assert call.args == [
        "build",
        "--progress=plain",
        "--platform",
        "linux/amd64",
        "--file",
        str(tmp_path / "Dockerfile"),
        "--tag",
        "gc-build/app:1",
        "--label",
        "groundcrew=1",
        "--label",
        "groundcrew.run=run-1",
        str(tmp_path),
    ]
    assert call.timeout == 900.0


async def test_build_failure_is_summarised_and_redacted(tmp_path: Path) -> None:
    log = NAIVE_LOG + f"\nAuthorization: Bearer ghp_{'b' * 36}\n"
    fake = FakeDocker({"build": CommandResult(1, "", log)})
    result = await DockerSandbox(_settings(), fake).build(
        tmp_path, tmp_path / "Dockerfile", "t:1", run_id="run-1"
    )
    assert not result.ok and result.image_ref is None
    assert result.failing_step == "RUN pip install --no-cache-dir -r requirements.txt"
    assert "Error: pg_config executable not found." in result.error_lines
    assert len(result.log_tail) <= 8000 + 60
    assert "ghp_" not in result.log_tail


async def test_build_timeout_is_reported(tmp_path: Path) -> None:
    fake = FakeDocker({"build": CommandResult(-9, "#4 [1/2] FROM x\n", "", timed_out=True)})
    result = await DockerSandbox(_settings(sandbox_build_timeout_seconds=5), fake).build(
        tmp_path, tmp_path / "Dockerfile", "t:1", run_id="run-1"
    )
    assert not result.ok and result.timed_out
    assert "build timed out after 5s" in result.log_tail


@pytest.mark.parametrize("run_id", ["", "a b", "x;rm", "-flag", "a" * 65])
async def test_bad_run_ids_are_refused(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="bad run id"):
        await DockerSandbox(_settings(), FakeDocker()).build(
            tmp_path, tmp_path / "Dockerfile", "t:1", run_id=run_id
        )


# --- smoke --------------------------------------------------------------------------------


def _smoke_docker(running: str = "true 0", port: str = "0.0.0.0:49153\n[::]:49153\n") -> FakeDocker:
    return FakeDocker(
        {
            "port": CommandResult(0, port, ""),
            "inspect": CommandResult(0, running, ""),
            "logs": CommandResult(0, f"booting with {SECRET}\nTraceback: boom\n", ""),
        }
    )


def _http(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response]
) -> list[str]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return answer(request)

    real = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


ENV = [SmokeEnv("DATABASE_URL", SecretStr(SECRET), secret=True), SmokeEnv("MODE", SecretStr("x"))]


async def test_smoke_container_is_locked_down_and_env_stays_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _http(monkeypatch, lambda r: httpx.Response(200 if r.url.path == "/health" else 404))
    fake = _smoke_docker()
    result = await DockerSandbox(_settings(), fake).run_smoke(
        "img:1", 8000, ENV, run_id="run-1", health_path="/health"
    )
    assert result.ok
    assert [(c.path, c.status_code, c.ok) for c in result.checks] == [
        ("/health", 200, True),
        ("/", 404, False),  # recorded, not required: the app has no / route
    ]
    assert seen[0] == "http://smoke.test:49153/health"

    (run,) = fake.find("run")
    args = run.args
    pairs = set(itertools.pairwise(args))
    for flag in (
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--cpus", "1"),
        ("--memory", "512m"),
        ("--pids-limit", "256"),
        ("--tmpfs", "/tmp:rw,nosuid,size=64m"),  # noqa: S108
        ("--publish", "8000"),
        ("--label", "groundcrew=1"),
        ("--label", "groundcrew.run=run-1"),
        ("--env", "DATABASE_URL"),
        ("--env", "MODE"),
    ):
        assert flag in pairs, flag
    assert "--privileged" not in args
    assert not any(SECRET in a for a in args)
    assert run.env == {"DATABASE_URL": SECRET, "MODE": "x"}
    assert fake.calls[-1].args[:2] == ["rm", "--force"]


async def test_smoke_without_health_path_uses_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _http(monkeypatch, lambda r: httpx.Response(302))
    result = await DockerSandbox(_settings(), _smoke_docker()).run_smoke(
        "img:1", 3000, [], run_id="run-1"
    )
    assert result.ok and [c.path for c in result.checks] == ["/"]


async def test_smoke_fails_when_the_container_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    _http(monkeypatch, lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    fake = _smoke_docker(running="false 3")
    result = await DockerSandbox(_settings(), fake).run_smoke(
        "img:1", 8000, ENV, run_id="run-1", health_path="/health"
    )
    assert not result.ok
    assert "exited with code 3" in result.detail
    assert result.logs_tail is not None and "Traceback: boom" in result.logs_tail
    assert SECRET not in result.logs_tail
    assert fake.calls[-1].args[:2] == ["rm", "--force"]


async def test_smoke_times_out_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _http(monkeypatch, lambda r: httpx.Response(500))
    result = await DockerSandbox(_settings(), _smoke_docker()).run_smoke(
        "img:1", 8000, [], run_id="run-1", health_path="/health"
    )
    assert not result.ok and "did not answer 2xx/3xx" in result.detail
    assert result.checks[0].status_code == 500


async def test_smoke_cleans_up_when_start_fails() -> None:
    fake = FakeDocker({"run": CommandResult(125, "", f"bad image; env {SECRET}")})
    result = await DockerSandbox(_settings(), fake).run_smoke("img:1", 8000, ENV, run_id="run-1")
    assert not result.ok and "did not start" in result.detail
    assert SECRET not in result.detail
    assert fake.calls[-1].args[:2] == ["rm", "--force"]


async def test_smoke_cleans_up_on_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected")

    _http(monkeypatch, boom)
    fake = _smoke_docker()
    with pytest.raises(RuntimeError):
        await DockerSandbox(_settings(), fake).run_smoke("img:1", 8000, [], run_id="run-1")
    assert fake.calls[-1].args[:2] == ["rm", "--force"]


async def test_bad_env_names_never_reach_docker() -> None:
    fake = FakeDocker()
    result = await DockerSandbox(_settings(), fake).run_smoke(
        "img:1", 8000, [SmokeEnv("A=B", SecretStr("x"))], run_id="run-1"
    )
    assert not result.ok and fake.calls == []


# --- push and registries ------------------------------------------------------------------

LOGIN = RegistryLogin("123.dkr.ecr.us-east-2.amazonaws.com", "AWS", SecretStr("ecr-password-xyz"))


async def test_push_to_kind_needs_no_login() -> None:
    fake = FakeDocker({"push": CommandResult(0, f"m2: digest: sha256:{'a' * 64} size: 1\n", "")})
    target = kind_target(_settings(), "legacy-flask-app", "abc")
    result = await DockerSandbox(_settings(), fake).push("gc-build/app:1", target)
    assert target.image == "kind-registry:5000/legacy-flask-app:abc"
    assert [c.args[0] for c in fake.calls] == ["tag", "push"]
    assert result.digest == f"sha256:{'a' * 64}"


async def test_ecr_login_goes_through_stdin_and_a_temp_config() -> None:
    fake = FakeDocker()
    await DockerSandbox(_settings(), fake).push(
        "gc-build/app:1", PushTarget(f"{LOGIN.registry}/groundcrew-app:abc", LOGIN)
    )
    login, tag, push = fake.calls
    assert login.args == ["login", "--username", "AWS", "--password-stdin", LOGIN.registry]
    assert login.stdin == "ecr-password-xyz"
    assert not any("ecr-password-xyz" in a for c in fake.calls for a in c.args)
    config = login.env and login.env["DOCKER_CONFIG"]
    assert config and tag.env == push.env == login.env
    assert not Path(config).exists()  # removed after the push


async def test_push_errors_never_show_the_password() -> None:
    fake = FakeDocker({"login": CommandResult(1, "", "denied for ecr-password-xyz")})
    with pytest.raises(SandboxError) as err:
        await DockerSandbox(_settings(), fake).push("i:1", PushTarget("r/x:1", LOGIN))
    assert "ecr-password-xyz" not in str(err.value)

    fake = FakeDocker({"push": CommandResult(1, "", "retrying ecr-password-xyz")})
    with pytest.raises(SandboxError, match="docker push r/x:1 failed") as err:
        await DockerSandbox(_settings(), fake).push("i:1", PushTarget("r/x:1", LOGIN))
    assert "ecr-password-xyz" not in str(err.value)


class _Ecr:
    def get_authorization_token(self) -> dict[str, Any]:
        token = base64.b64encode(b"AWS:ecr-password-xyz").decode()
        return {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://123.dkr.ecr.us-east-2.amazonaws.com",
                }
            ]
        }


class _Session:
    def client(self, name: str) -> _Ecr:
        assert name == "ecr"
        return _Ecr()


async def test_ecr_login_from_the_deploy_session() -> None:
    login = await ecr_login(_Session())
    assert login.registry == "123.dkr.ecr.us-east-2.amazonaws.com"
    assert login.username == "AWS"
    assert login.password.get_secret_value() == "ecr-password-xyz"
    assert "ecr-password-xyz" not in repr(login)
    target = await ecr_target(_Session(), "groundcrew-app", "abc")
    assert target.image == "123.dkr.ecr.us-east-2.amazonaws.com/groundcrew-app:abc"


# --- cleanup and providers ----------------------------------------------------------------


async def test_cleanup_removes_only_this_runs_containers_and_images() -> None:
    fake = FakeDocker(
        {
            "ps": CommandResult(0, "c1\nc2\n", ""),
            "images": CommandResult(0, "i1\ni1\ni2\n", ""),
        }
    )
    await DockerSandbox(_settings(), fake).cleanup("run-1")
    assert fake.calls[0].args == [
        "ps",
        "--all",
        "--quiet",
        "--filter",
        "label=groundcrew.run=run-1",
    ]
    assert fake.find("rm")[0].args == ["rm", "--force", "--volumes", "c1", "c2"]
    assert fake.find("rmi")[0].args == ["rmi", "--force", "i1", "i2"]


async def test_provider_switch_and_nebius_stub(tmp_path: Path) -> None:
    assert isinstance(get_sandbox(_settings()), DockerSandbox)
    nebius = get_sandbox(_settings(sandbox_provider="nebius"))
    assert isinstance(nebius, NebiusSandbox)
    with pytest.raises(SandboxError, match="TODO\\(nebius-sandbox\\)"):
        await nebius.build(tmp_path, tmp_path / "Dockerfile", "t:1", run_id="r")


async def test_runner_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = await run_docker(["info"], timeout=5)
    assert result.returncode == 127 and not result.ok


# --- real daemon ---------------------------------------------------------------------------

TINY = """FROM busybox:1.37
RUN mkdir -p /www && echo ok > /www/health && echo home > /www/index.html
EXPOSE 8080
CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]
"""


@pytest.mark.docker
async def test_build_and_smoke_on_a_real_daemon(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(TINY)
    sandbox = DockerSandbox(
        _settings(sandbox_smoke_host="localhost", sandbox_smoke_timeout_seconds=20)
    )
    run_id = "test-sandbox-real"
    try:
        built = await sandbox.build(
            tmp_path, tmp_path / "Dockerfile", "gc-test/tiny:1", run_id=run_id
        )
        assert built.ok, built.log_tail
        smoke = await sandbox.run_smoke(
            "gc-test/tiny:1", 8080, [], run_id=run_id, health_path="/health"
        )
        assert smoke.ok, smoke.detail
        assert [c.status_code for c in smoke.checks] == [200, 200]

        (tmp_path / "Dockerfile").write_text("FROM busybox:1.37\nRUN exit 7\n")
        broken = await sandbox.build(
            tmp_path, tmp_path / "Dockerfile", "gc-test/tiny:2", run_id=run_id
        )
        assert not broken.ok and broken.failing_step == "RUN exit 7"
    finally:
        await sandbox.cleanup(run_id)
    leftovers = subprocess.run(  # noqa: S603
        ["docker", "ps", "-aq", "--filter", f"label=groundcrew.run={run_id}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    assert leftovers.stdout.strip() == ""


# --- live: GroundsCrew-AI/legacy-flask-app ------------------------------------------------

NAIVE_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]
"""


@pytest.mark.live
async def test_legacy_flask_app_naive_build_fails_on_psycopg2(tmp_path: Path) -> None:
    """Expected failure: psycopg2 builds from source and python:3.12-slim has no pg_config.
    Builder's automatic fix (M4) starts from exactly this error."""
    repo = tmp_path / "legacy-flask-app"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "clone",
            "--depth=1",
            "https://github.com/GroundsCrew-AI/legacy-flask-app.git",
            str(repo),
        ],
        check=True,
        capture_output=True,
    )
    (repo / "Dockerfile").write_text(NAIVE_DOCKERFILE)
    sandbox = DockerSandbox(_settings())
    try:
        result = await sandbox.build(
            repo, repo / "Dockerfile", "gc-test/legacy:naive", run_id="live-naive"
        )
    finally:
        await sandbox.cleanup("live-naive")
    assert not result.ok
    assert result.failing_step == "RUN pip install --no-cache-dir -r requirements.txt"
    assert any("pg_config executable not found" in line for line in result.error_lines)
