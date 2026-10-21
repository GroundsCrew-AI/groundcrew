"""The startup check refuses to run when TMPDIR or REPO_WORKDIR is not writable, and says what
to fix, instead of failing later on the first repository clone."""

import os
from pathlib import Path

import pytest

from app.config import get_settings
from app.writable import NotWritableError, check_writable

from .test_api import _settings


def test_a_writable_tmpdir_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    check_writable(_settings(repo_workdir=tmp_path / "repos"))
    assert (tmp_path / "repos").is_dir() and list(tmp_path.glob(".groundcrew-*")) == []


def test_a_missing_tmpdir_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "missing"))
    with pytest.raises(NotWritableError, match=r"TMPDIR=.*missing is not writable by uid \d+"):
        check_writable(get_settings())


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through directory permissions")
def test_an_unwritable_repo_workdir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(NotWritableError, match=r"REPO_WORKDIR=.*Permission denied"):
            check_writable(_settings(repo_workdir=locked))
    finally:
        locked.chmod(0o700)
