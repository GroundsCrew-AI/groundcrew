"""Where a repository comes from. Phase 4 uses local paths and public git URLs; Phase 3 adds a
GitHub App source (installation token) behind the same protocol.

    with GitCloneSource("https://github.com/acme/shop") as root:   # cloned, then always deleted
        ...

materialize() and cleanup() block (git runs as a subprocess). From async code, call them with
asyncio.to_thread. Sources only read: nothing here writes to the customer's repository.
"""

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings, get_settings
from app.security.redact import redact


class RepoSourceError(RuntimeError):
    """The repository could not be read. The message never includes a token."""


class RepoSource(Protocol):
    def materialize(self) -> Path:
        """A local directory with the repository's files (the working tree, not .git)."""
        ...

    def describe(self) -> str:
        """One line for the timeline, e.g. 'git https://github.com/acme/shop @ main (3f2a1c9)'."""
        ...

    def cleanup(self) -> None:
        """Delete anything materialize() created. Safe to call more than once."""
        ...


class _ContextManager:
    def materialize(self) -> Path:
        raise NotImplementedError

    def cleanup(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Path:
        return self.materialize()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()


class LocalPathSource(_ContextManager):
    """A directory on this machine. Used as is; never copied, changed, or deleted."""

    def __init__(self, path: str | Path, *, subpath: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.subpath = subpath

    def materialize(self) -> Path:
        if not self.path.is_dir():
            raise RepoSourceError(f"Not a directory: {self.path}")
        return _with_subpath(self.path, self.subpath)

    def describe(self) -> str:
        suffix = f" (subpath {self.subpath})" if self.subpath else ""
        return f"local {self.path}{suffix}"

    def cleanup(self) -> None:
        pass  # not ours to delete


ALLOWED_SCHEMES = ("https",)


class GitCloneSource(_ContextManager):
    """A shallow clone of a git URL into a temp directory that cleanup() always removes.

    Only https:// URLs by default (no ssh, file, or git:// transports, no `-option` injection).
    A token, if set (GIT_CLONE_TOKEN), is sent as an Authorization header scoped to the URL's
    host, passed through git's environment (GIT_CONFIG_*), so it is never in the URL, the
    process list, or an error message.
    """

    def __init__(
        self,
        url: str,
        *,
        ref: str | None = None,
        subpath: str | None = None,
        settings: Settings | None = None,
        allowed_schemes: tuple[str, ...] = ALLOWED_SCHEMES,
    ) -> None:
        self.settings = settings or get_settings()
        self.url = _check_url(url, allowed_schemes)
        self.ref = _check_ref(ref)
        self.subpath = subpath
        self.commit: str | None = None
        self._dir: Path | None = None

    def materialize(self) -> Path:
        if self._dir is not None:
            return _with_subpath(self._dir / "repo", self.subpath)
        root = self.settings.repo_workdir
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        self._dir = Path(tempfile.mkdtemp(prefix="groundcrew-repo-", dir=root))
        target = self._dir / "repo"
        try:
            args = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
            if self.ref:
                args += ["--branch", self.ref]
            args += ["--", self.url, str(target)]
            self._git(args, cwd=self._dir)
            self.commit = self._git(["git", "rev-parse", "HEAD"], cwd=target).strip()
            size = _dir_size(target, limit=self.settings.repo_max_bytes)
            if size > self.settings.repo_max_bytes:
                raise RepoSourceError(
                    f"Repository is over {self.settings.repo_max_bytes} bytes (REPO_MAX_BYTES)."
                )
            # The model only ever sees the working tree.
            shutil.rmtree(target / ".git", ignore_errors=True)
            return _with_subpath(target, self.subpath)
        except BaseException:
            self.cleanup()
            raise

    def describe(self) -> str:
        parts = [f"git {_without_credentials(self.url)}"]
        if self.ref:
            parts.append(f"@ {self.ref}")
        if self.commit:
            parts.append(f"({self.commit[:7]})")
        if self.subpath:
            parts.append(f"subpath {self.subpath}")
        return " ".join(parts)

    def cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def _env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - git needs a HOME
            "GIT_TERMINAL_PROMPT": "0",  # fail instead of waiting for a password
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        if "TMPDIR" in os.environ:  # the writable tmpfs on a read-only root filesystem
            env["TMPDIR"] = os.environ["TMPDIR"]
        token = self.settings.git_clone_token
        if token is not None:
            parts = urlsplit(self.url)
            basic = base64.b64encode(f"x-access-token:{token.get_secret_value()}".encode())
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.{parts.scheme}://{parts.netloc}/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic.decode()}",
                }
            )
        return env

    def _git(self, args: list[str], *, cwd: Path) -> str:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, URL validated, no shell
                args,
                cwd=cwd,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=self.settings.git_clone_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RepoSourceError(
                f"git timed out after {self.settings.git_clone_timeout_seconds:.0f}s "
                f"({_without_credentials(self.url)})"
            ) from None
        if done.returncode != 0:
            raise RepoSourceError(self._safe(f"git failed: {done.stderr.strip()[-500:]}"))
        return done.stdout

    def _safe(self, text: str) -> str:
        token = self.settings.git_clone_token
        if token is not None:
            text = text.replace(token.get_secret_value(), "[REDACTED]")
        return redact(text)


def _check_url(url: str, allowed_schemes: tuple[str, ...]) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if url.startswith("-") or parts.scheme not in allowed_schemes:
        raise RepoSourceError(
            f"Only {', '.join(s + '://' for s in allowed_schemes)} repository URLs are allowed."
        )
    if parts.scheme != "file" and not parts.hostname:
        raise RepoSourceError("Repository URL has no host.")
    if parts.username or parts.password:
        raise RepoSourceError("Put credentials in GIT_CLONE_TOKEN, not in the URL.")
    return url


def _check_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    ref = ref.strip()
    if not ref or ref.startswith("-") or any(c in ref for c in " \t\n~^:?*[\\") or ".." in ref:
        raise RepoSourceError(f"Not a valid branch or tag name: {ref!r}")
    return ref


def _without_credentials(url: str) -> str:
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _with_subpath(root: Path, subpath: str | None) -> Path:
    if not subpath:
        return root
    target = (root / subpath).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_dir():
        raise RepoSourceError(f"Subpath {subpath!r} is not a directory inside the repository.")
    return target


def _dir_size(path: Path, *, limit: int) -> int:
    """Total file size, stopping early once over `limit`."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).lstat().st_size
            except OSError:
                continue
            if total > limit:
                return total
    return total
