"""Repo sources. Clones come from a local git repo (file://), so no network is needed."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.repos.source import GitCloneSource, LocalPathSource, RepoSourceError

TOKEN = "ghp_testtoken1234567890abcdefghijklmnop"  # noqa: S105


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return done.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    (repo / "app").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "demo"}\n')
    (repo / "app" / "main.py").write_text("print('hi')\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", "main")
    return repo


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(_env_file=None, repo_workdir=tmp_path / "work", **overrides)


def _clone(origin: Path, tmp_path: Path, **kwargs: Any) -> GitCloneSource:
    settings = kwargs.pop("settings", None) or _settings(tmp_path)
    return GitCloneSource(
        f"file://{origin}", settings=settings, allowed_schemes=("file",), **kwargs
    )


def test_local_path_source(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    source = LocalPathSource(tmp_path, subpath="svc")
    with source as root:
        assert root == (tmp_path / "svc").resolve()
    assert (tmp_path / "svc").is_dir()  # never deleted
    assert source.describe() == f"local {tmp_path.resolve()} (subpath svc)"
    with pytest.raises(RepoSourceError, match="Not a directory"):
        LocalPathSource(tmp_path / "missing").materialize()
    with pytest.raises(RepoSourceError, match="Subpath"):
        LocalPathSource(tmp_path, subpath="../..").materialize()


def test_clone_materializes_and_always_cleans_up(origin: Path, tmp_path: Path) -> None:
    source = _clone(origin, tmp_path)
    with source as root:
        assert (root / "package.json").read_text() == '{"name": "demo"}\n'
        assert not (root / ".git").exists()  # the model only sees the working tree
        assert not (root / "feature.txt").exists()
        assert source.commit and len(source.commit) == 40
        assert f"({source.commit[:7]})" in source.describe()
    assert list((tmp_path / "work").iterdir()) == []


def test_clone_branch_and_subpath(origin: Path, tmp_path: Path) -> None:
    with _clone(origin, tmp_path, ref="feature") as root:
        assert (root / "feature.txt").exists()
    with _clone(origin, tmp_path, subpath="app") as root:
        assert (root / "main.py").exists()


def test_clone_failure_cleans_up_and_explains(origin: Path, tmp_path: Path) -> None:
    source = _clone(origin, tmp_path, ref="no-such-branch")
    with pytest.raises(RepoSourceError, match="git failed"):
        source.materialize()
    assert list((tmp_path / "work").iterdir()) == []


def test_clone_size_cap(origin: Path, tmp_path: Path) -> None:
    source = _clone(origin, tmp_path, settings=_settings(tmp_path, repo_max_bytes=10))
    with pytest.raises(RepoSourceError, match="REPO_MAX_BYTES"):
        source.materialize()
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc",  # not allowed by default
        "ssh://git@github.com/acme/shop.git",
        "git://github.com/acme/shop.git",
        "ext::sh -c touch% /tmp/pwned",
        "-uhttps://github.com/acme/shop",
        "https://user:pass@github.com/acme/shop",
        "https:///no-host",
    ],
)
def test_rejected_urls(url: str) -> None:
    with pytest.raises(RepoSourceError):
        GitCloneSource(url, settings=Settings(_env_file=None))


@pytest.mark.parametrize("ref", ["--upload-pack=x", "a..b", "a b", "x:y"])
def test_rejected_refs(ref: str) -> None:
    with pytest.raises(RepoSourceError, match="branch or tag"):
        GitCloneSource("https://github.com/acme/shop", ref=ref, settings=Settings(_env_file=None))


def test_token_goes_through_env_scoped_to_host_never_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs["env"]))
        return subprocess.CompletedProcess(args, 128, "", f"fatal: auth failed for {TOKEN}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = _settings(tmp_path, git_clone_token=SecretStr(TOKEN))
    source = GitCloneSource("https://github.com/acme/private", settings=settings)
    with pytest.raises(RepoSourceError) as exc_info:
        source.materialize()

    [(args, env)] = calls
    assert args[-2] == "https://github.com/acme/private"  # URL unchanged, after "--"
    assert args[-3] == "--"
    assert TOKEN not in " ".join(args)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert TOKEN not in str(exc_info.value)  # redacted even if git echoes it
    assert "github.com/acme/private" in source.describe()


def test_timeout_is_reported_and_cleaned_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", slow)
    source = GitCloneSource("https://github.com/acme/shop", settings=_settings(tmp_path))
    with pytest.raises(RepoSourceError, match="timed out"):
        source.materialize()
    assert list((tmp_path / "work").iterdir()) == []
