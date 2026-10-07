"""Read-only, model-facing views of a repository: a file listing and line-numbered excerpts.

Models call these through the list_files and read_file tools, so outputs are small and
deterministic: sorted paths, hard caps on entries, lines, and bytes, and an explicit marker
whenever something is cut. The same repository always gives the same text.

Safety: paths must stay inside the repository root (no absolute paths, no `..`, no symlinks
that point outside). .gitignore'd files, dependency and build folders, binaries, and secret
files (.env, private keys, credential files) are never read. Excerpts are redacted.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pathspec

from app.security.redact import REDACTED, redact

# --- Limits (the tools' arguments are capped to these) ---------------------------------
LIST_DEFAULT_ENTRIES = 200
LIST_MAX_ENTRIES = 500
LIST_MAX_BYTES = 16_000
SCAN_MAX_ENTRIES = 20_000  # stop walking a huge tree; the output says so
READ_DEFAULT_LINES = 200
READ_MAX_LINES = 400
READ_MAX_BYTES = 24_000
READ_MAX_LINE_CHARS = 400
READ_MAX_FILE_BYTES = 1_000_000
SNIFF_BYTES = 1024

# Never listed or read: dependencies, virtualenvs, caches, build output, VCS data.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        ".gradle",
        ".terraform",
        "dist",
        "build",
        "target",
        "coverage",
        ".idea",
        ".vscode",
    }
)

BINARY_SUFFIXES = frozenset(
    [
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".avif",
        ".tiff",
        ".psd",
        ".svgz",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar",
        ".jar",
        ".war",
        ".ear",
        ".whl",
        ".egg",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".a",
        ".o",
        ".obj",
        ".lib",
        ".bin",
        ".class",
        ".pyc",
        ".pyo",
        ".wasm",
        ".node",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".webm",
        ".wav",
        ".flac",
        ".ogg",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".parquet",
        ".pkl",
        ".pickle",
        ".npy",
        ".npz",
        ".h5",
        ".onnx",
        ".pt",
        ".ckpt",
    ]
)

# Files that hold secrets. Their names may be listed; their contents are never read.
SECRET_NAMES = frozenset(
    {".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials", ".htpasswd"}
)
SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"})
# .env.<something> is a secret unless it is clearly a template.
ENV_TEMPLATES = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"}
)


class RepoFileError(ValueError):
    """A path the tools will not list or read. The message is shown to the model."""


def is_secret_file(name: str) -> bool:
    lower = name.lower()
    if lower in SECRET_NAMES or lower.startswith(SECRET_PREFIXES):
        return True
    if lower.startswith(".env.") and lower not in ENV_TEMPLATES:
        return True
    return PurePosixPath(lower).suffix in SECRET_SUFFIXES


def is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(SNIFF_BYTES)
    except OSError:
        return True


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@dataclass
class IgnoreRules:
    """.gitignore files under the root, applied like git: the deepest file with an opinion
    about a path wins, and within one file the last matching pattern wins."""

    root: Path
    _specs: dict[PurePosixPath, pathspec.GitIgnoreSpec | None] = field(default_factory=dict)

    def _spec(self, directory: PurePosixPath) -> pathspec.GitIgnoreSpec | None:
        if directory not in self._specs:
            path = self.root / directory / ".gitignore"
            spec = None
            if path.is_file() and not path.is_symlink():
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    spec = pathspec.GitIgnoreSpec.from_lines(lines)
                except OSError:
                    spec = None
            self._specs[directory] = spec
        return self._specs[directory]

    def ignored(self, rel: PurePosixPath, *, is_dir: bool) -> bool:
        ancestors = [PurePosixPath(*rel.parts[:i]) for i in range(len(rel.parts) - 1, -1, -1)]
        for directory in ancestors:  # deepest first
            spec = self._spec(directory)
            if spec is None:
                continue
            inner = rel.relative_to(directory).as_posix() + ("/" if is_dir else "")
            result = spec.check_file(inner)
            if result.include is not None:
                return bool(result.include)
        return False


def _resolve(root: Path, rel_path: str, *, want_dir: bool) -> tuple[Path, PurePosixPath]:
    """The real path for a model-supplied relative path, or RepoFileError."""
    text = (rel_path or ".").strip().replace("\\", "/")
    rel = PurePosixPath(text)
    if rel.is_absolute() or ".." in rel.parts:
        raise RepoFileError(f"{rel_path!r}: use a path relative to the repository root.")
    root_real = root.resolve()
    target = (root_real / rel).resolve()
    if not target.is_relative_to(root_real):
        raise RepoFileError(f"{rel_path!r} points outside the repository.")
    if want_dir and not target.is_dir():
        raise RepoFileError(f"{rel_path!r} is not a directory in the repository.")
    if not want_dir and not target.is_file():
        raise RepoFileError(f"{rel_path!r} is not a file in the repository.")
    clean = PurePosixPath(target.relative_to(root_real).as_posix())
    if any(part in SKIP_DIRS for part in clean.parts):
        raise RepoFileError(f"{rel_path!r} is in a dependency or build folder, which is skipped.")
    return target, clean


def list_files(root: Path, path: str = ".", max_entries: int = LIST_DEFAULT_ENTRIES) -> str:
    """Files under `path` (relative to the root), sorted, with sizes, capped."""
    max_entries = max(1, min(max_entries, LIST_MAX_ENTRIES))
    start, start_rel = _resolve(root, path, want_dir=True)
    rules = IgnoreRules(root.resolve())
    if start_rel.parts and rules.ignored(start_rel, is_dir=True):
        raise RepoFileError(f"{path!r} is ignored by .gitignore.")

    files: list[tuple[str, str]] = []
    scanned = 0
    stopped = False
    stack = [start]
    while stack and not stopped:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > SCAN_MAX_ENTRIES:
                stopped = True
                break
            if entry.is_symlink():
                continue  # never followed: it may point outside the repository
            rel = PurePosixPath(Path(entry.path).resolve().relative_to(root.resolve()).as_posix())
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in SKIP_DIRS and not rules.ignored(rel, is_dir=True):
                    stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False) or rules.ignored(rel, is_dir=False):
                continue
            entry_path = Path(entry.path)
            if is_binary(entry_path):
                continue
            note = " [secret: name only, not readable]" if is_secret_file(entry.name) else ""
            size = entry.stat(follow_symlinks=False).st_size
            files.append((rel.as_posix(), f"{rel.as_posix()} ({human_size(size)}){note}"))

    files.sort()
    where = start_rel.as_posix() if start_rel.parts else "the repository root"
    lines = [
        f"{len(files)} files under {where} (gitignored, dependency, build, and binary files "
        "are left out)"
    ]
    shown = 0
    used = len(lines[0]) + 1
    for _, line in files:
        if shown >= max_entries or used + len(line) + 1 > LIST_MAX_BYTES:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(files):
        lines.append(
            f"[truncated: showing {shown} of {len(files)} files. List a subdirectory "
            "(path=...) to see the rest.]"
        )
    if stopped:
        lines.append(
            f"[stopped scanning after {SCAN_MAX_ENTRIES} entries: the tree is very large. "
            "List a subdirectory.]"
        )
    if not files:
        lines.append("(no files)")
    return "\n".join(lines)


def _redact_lines(lines: list[str]) -> list[str]:
    """Redact line by line so line numbers stay true. Private key blocks are blanked whole."""
    out: list[str] = []
    in_key = False
    for line in lines:
        if "PRIVATE KEY-----" in line and "-----BEGIN" in line:
            in_key = "-----END" not in line
            out.append(REDACTED)
            continue
        if in_key:
            if "-----END" in line:
                in_key = False
            out.append(REDACTED)
            continue
        out.append(redact(line))
    return out


def read_file(
    root: Path, path: str, start_line: int = 1, max_lines: int = READ_DEFAULT_LINES
) -> str:
    """Lines `start_line`.. of a text file, numbered, redacted, capped."""
    max_lines = max(1, min(max_lines, READ_MAX_LINES))
    start_line = max(1, start_line)
    target, rel = _resolve(root, path, want_dir=False)
    if is_secret_file(target.name):
        raise RepoFileError(f"{rel} may hold secrets and is never read. Read a template instead.")
    if IgnoreRules(root.resolve()).ignored(rel, is_dir=False):
        raise RepoFileError(f"{rel} is ignored by .gitignore and is not read.")
    size = target.stat().st_size
    if size > READ_MAX_FILE_BYTES:
        raise RepoFileError(
            f"{rel} is {human_size(size)}; files over "
            f"{human_size(READ_MAX_FILE_BYTES)} are not read."
        )
    if is_binary(target):
        raise RepoFileError(f"{rel} is a binary file.")

    all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(all_lines)
    if total == 0:
        return f"{rel} is empty."
    if start_line > total:
        raise RepoFileError(f"{rel} has {total} lines; start_line {start_line} is past the end.")

    end = min(total, start_line + max_lines - 1)
    selected = _redact_lines(all_lines[start_line - 1 : end])
    width = len(str(end))
    header = f"{rel}, lines {start_line}-{end} of {total}"
    out = [header]
    used = len(header) + 1
    last_shown = start_line - 1
    for number, text in enumerate(selected, start=start_line):
        if len(text) > READ_MAX_LINE_CHARS:
            text = (
                text[:READ_MAX_LINE_CHARS] + f" [... {len(text) - READ_MAX_LINE_CHARS} more chars]"
            )
        line = f"{number:>{width}}| {text}"
        if used + len(line) + 1 > READ_MAX_BYTES:
            break
        out.append(line)
        used += len(line) + 1
        last_shown = number
    if last_shown < total:
        out.append(
            f"[truncated: lines {last_shown + 1}-{total} not shown. "
            f"Call again with start_line={last_shown + 1}.]"
        )
    if last_shown < end:
        out[0] = f"{rel}, lines {start_line}-{last_shown} of {total}"
    return "\n".join(out)
