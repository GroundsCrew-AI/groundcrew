"""Read-only, cached view of a repository for Scout's detectors and evidence checks.

Uses the same safety rules as the repo tools (app.repos.files): no .gitignore'd files, no
dependency or build folders, no binaries, and secret files (.env, keys) are never read.
"""

import os
from functools import cached_property
from pathlib import Path, PurePosixPath

from app.repos.files import SKIP_DIRS, IgnoreRules, is_binary, is_secret_file

MAX_FILES = 2_000
MAX_SOURCE_FILES = 300
MAX_FILE_BYTES = 200_000
SOURCE_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".py",
        ".go",
        ".java",
        ".kt",
        ".rb",
        ".rs",
        ".php",
        ".cs",
    }
)


class RepoView:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._rules = IgnoreRules(self.root)
        self._lines: dict[str, list[str] | None] = {}

    @cached_property
    def files(self) -> list[str]:
        """Readable file paths (posix, relative), sorted. At most MAX_FILES."""
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            here = Path(dirpath)
            rel_dir = PurePosixPath(here.relative_to(self.root).as_posix())
            kept = []
            for name in sorted(dirnames):
                rel = rel_dir / name if rel_dir.parts else PurePosixPath(name)
                path = here / name
                if name in SKIP_DIRS or path.is_symlink() or self._rules.ignored(rel, is_dir=True):
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in sorted(filenames):
                rel = rel_dir / name if rel_dir.parts else PurePosixPath(name)
                path = here / name
                if path.is_symlink() or self._rules.ignored(rel, is_dir=False):
                    continue
                found.append(rel.as_posix())
                if len(found) >= MAX_FILES:
                    return sorted(found)
        return sorted(found)

    def exists(self, rel: str) -> bool:
        return rel in self._file_set

    @cached_property
    def _file_set(self) -> frozenset[str]:
        return frozenset(self.files)

    def lines(self, rel: str) -> list[str] | None:
        """The file's lines, or None if it is missing, secret, binary, or too big."""
        if rel not in self._lines:
            self._lines[rel] = self._read(rel)
        return self._lines[rel]

    def _read(self, rel: str) -> list[str] | None:
        if rel not in self._file_set or is_secret_file(PurePosixPath(rel).name):
            return None
        path = self.root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES or is_binary(path):
                return None
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None

    def text(self, rel: str) -> str | None:
        lines = self.lines(rel)
        return None if lines is None else "\n".join(lines)

    def line(self, rel: str, number: int) -> str | None:
        lines = self.lines(rel)
        if lines is None or not 1 <= number <= len(lines):
            return None
        return lines[number - 1]

    def find(self, rel: str, needle: str) -> int | None:
        """First line number (1-based) containing `needle`, or None."""
        for number, text in enumerate(self.lines(rel) or [], start=1):
            if needle in text:
                return number
        return None

    @cached_property
    def source_files(self) -> list[str]:
        return [f for f in self.files if PurePosixPath(f).suffix in SOURCE_SUFFIXES][
            :MAX_SOURCE_FILES
        ]
