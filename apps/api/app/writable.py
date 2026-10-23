"""Startup check: the directories Groundcrew writes to are writable by the user it runs as.

In production the containers have a read-only root filesystem and run as an unprivileged uid
(infra/platform/host/compose.yml), so every temporary file (repository clones, kubeconfigs,
Docker client configs for registry logins, Dockerfiles written by Builder, build contexts) goes
under TMPDIR, a tmpfs owned by that uid. Python's tempfile quietly falls back to another
directory when TMPDIR is not usable, so this checks TMPDIR itself and fails loudly instead.
"""

import contextlib
import os
import tempfile
from pathlib import Path

from app.config import Settings


class NotWritableError(RuntimeError):
    pass


def _probe(name: str, directory: Path) -> None:
    try:
        fd, path = tempfile.mkstemp(prefix=".groundcrew-write-check-", dir=directory)
    except OSError as exc:
        raise NotWritableError(
            f"{name}={directory} is not writable by uid {os.getuid()}: {exc.strerror}. "
            "Groundcrew writes repository clones, kubeconfigs, and build files there. Mount a "
            "directory this user owns, such as a tmpfs with uid and gid set to it "
            "(infra/platform/host/compose.yml has the production setup)."
        ) from None
    os.close(fd)
    os.unlink(path)


def check_writable(settings: Settings) -> None:
    """Raise NotWritableError, with what to fix, if a directory Groundcrew writes to is not
    writable. TMPDIR when it is set (otherwise the directory tempfile picked), and REPO_WORKDIR."""
    tmpdir = os.environ.get("TMPDIR")
    _probe("TMPDIR", Path(tmpdir) if tmpdir else Path(tempfile.gettempdir()))
    if settings.repo_workdir is not None:
        with contextlib.suppress(OSError):  # the probe below says why
            settings.repo_workdir.mkdir(parents=True, exist_ok=True)
        _probe("REPO_WORKDIR", settings.repo_workdir)
