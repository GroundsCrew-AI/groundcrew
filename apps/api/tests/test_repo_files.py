"""list_files / read_file: deterministic, capped, and safe."""

import uuid
from pathlib import Path

import pytest

from app.policy.engine import execute_tool
from app.repos import files
from app.repos.files import RepoFileError, list_files, read_file
from app.tools.registry import ToolContext, registry
from app.tools.repo_read import ListFilesArgs, ReadFileArgs


def _write(root: Path, rel: str, text: str | bytes = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, ".gitignore", "*.log\n!keep.log\n/secret_dir/\nbuild_out/\n")
    _write(root, "README.md", "# Demo\n")
    _write(root, "package.json", '{"name": "demo"}\n')
    _write(root, ".env", "DATABASE_URL=postgres://u:hunter2pass@db/app\n")
    _write(root, ".env.example", "DATABASE_URL=\nPORT=3000\n")
    _write(root, "debug.log", "noise\n")
    _write(root, "keep.log", "kept\n")
    _write(root, "secret_dir/a.txt")
    _write(root, "build_out/b.js")
    _write(root, "src/app.py", "print('hi')\n")
    _write(root, "src/.gitignore", "generated.py\n!important.log\n")
    _write(root, "src/generated.py")
    _write(root, "src/important.log", "important\n")
    _write(root, "node_modules/lib/index.js")
    _write(root, ".venv/lib/x.py")
    _write(root, "dist/bundle.js")
    _write(root, "logo.png", b"\x89PNG\r\n\x1a\n\0\0")
    _write(root, "data.bin2", b"abc\0def")
    _write(
        root, "certs/server.pem", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
    )
    (tmp_path / "outside.txt").write_text("outside\n")
    (root / "link_out.txt").symlink_to(tmp_path / "outside.txt")
    return root


def _listed(output: str) -> list[str]:
    return [line.split(" (")[0] for line in output.splitlines()[1:] if not line.startswith("[")]


def test_list_respects_gitignore_and_skips(repo: Path) -> None:
    out = list_files(repo)
    assert _listed(out) == [
        ".env",
        ".env.example",
        ".gitignore",
        "README.md",
        "certs/server.pem",
        "keep.log",
        "package.json",
        "src/.gitignore",
        "src/app.py",
        "src/important.log",  # re-included by the nested .gitignore
    ]
    assert out.splitlines()[0].startswith("10 files under the repository root")
    assert ".env (" in out and "[secret: name only, not readable]" in out


def test_list_is_deterministic(repo: Path) -> None:
    assert list_files(repo) == list_files(repo)
    assert list_files(repo, "src") == list_files(repo, "./src/")


def test_list_caps_entries_with_marker(repo: Path) -> None:
    for i in range(30):
        _write(repo, f"many/f{i:02}.txt")
    out = list_files(repo, "many", max_entries=5)
    assert _listed(out) == [f"many/f{i:02}.txt" for i in range(5)]
    assert out.splitlines()[-1] == (
        "[truncated: showing 5 of 30 files. List a subdirectory (path=...) to see the rest.]"
    )
    assert len(_listed(list_files(repo, "many", max_entries=10_000))) == 30  # capped at 500


def test_list_caps_bytes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "LIST_MAX_BYTES", 200)
    for i in range(30):
        _write(repo, f"many/{'long_name_' * 3}{i:02}.txt")
    out = list_files(repo, "many")
    assert len(out) <= 200 + 120  # the cap plus the truncation marker
    assert "[truncated: showing" in out


def test_list_refuses_bad_paths(repo: Path) -> None:
    for path, message in [
        ("../", "relative"),
        ("/etc", "relative"),
        ("node_modules", "dependency or build"),
        ("secret_dir", "ignored"),
        ("package.json", "not a directory"),
    ]:
        with pytest.raises(RepoFileError, match=message):
            list_files(repo, path)


def test_read_numbers_lines_and_ranges(repo: Path) -> None:
    _write(repo, "big.txt", "".join(f"line {i}\n" for i in range(1, 451)))
    out = read_file(repo, "big.txt")
    lines = out.splitlines()
    assert lines[0] == "big.txt, lines 1-200 of 450"
    assert lines[1] == "  1| line 1"
    assert lines[200] == "200| line 200"
    assert lines[-1] == "[truncated: lines 201-450 not shown. Call again with start_line=201.]"
    tail = read_file(repo, "big.txt", start_line=441, max_lines=50)
    assert tail.splitlines()[0] == "big.txt, lines 441-450 of 450"
    assert tail.splitlines()[-1] == "450| line 450"  # no marker at the end
    assert read_file(repo, "big.txt", max_lines=10_000).splitlines()[0].endswith("1-400 of 450")
    with pytest.raises(RepoFileError, match="past the end"):
        read_file(repo, "big.txt", start_line=451)


def test_read_caps_long_lines_and_bytes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo, "wide.js", "x" * 1000 + "\n" + "y\n")
    out = read_file(repo, "wide.js")
    assert "x" * 400 + " [... 600 more chars]" in out
    monkeypatch.setattr(files, "READ_MAX_BYTES", 300)
    _write(repo, "many.txt", "".join(f"some longer line number {i}\n" for i in range(100)))
    out = read_file(repo, "many.txt")
    assert len(out) <= 300 + 100
    first = out.splitlines()[0]
    shown_to = int(first.split("lines 1-")[1].split(" ")[0])
    assert out.splitlines()[-1].startswith(f"[truncated: lines {shown_to + 1}-100 not shown")


def test_read_refuses_secrets_ignored_binary_and_escapes(repo: Path) -> None:
    for path, message in [
        (".env", "secrets"),
        ("certs/server.pem", "secrets"),
        ("debug.log", "ignored"),
        ("src/generated.py", "ignored"),
        ("logo.png", "binary"),
        ("data.bin2", "binary"),
        ("link_out.txt", "outside"),
        ("../outside.txt", "relative"),
        ("node_modules/lib/index.js", "dependency or build"),
        ("src", "not a file"),
    ]:
        with pytest.raises(RepoFileError, match=message):
            read_file(repo, path)
    assert "PORT=3000" in read_file(repo, ".env.example")  # templates are fine


def test_read_redacts_and_keeps_line_numbers(repo: Path) -> None:
    _write(
        repo,
        "config.py",
        "API_KEY = 'sk-live-abcdefghijklmnop123'\n"
        "KEY = '''\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n'''\n"
        "PORT = 8000\n",
    )
    out = read_file(repo, "config.py")
    assert "abcdefghijklmnop123" not in out and "MIIEow" not in out
    assert out.splitlines()[-1] == "7| PORT = 8000"


def test_read_too_large(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "READ_MAX_FILE_BYTES", 10)
    with pytest.raises(RepoFileError, match="not read"):
        read_file(repo, "package.json")


@pytest.mark.anyio
async def test_tools_run_through_the_policy_engine(repo: Path) -> None:
    ctx = ToolContext(run_id=uuid.uuid4(), workspace_id=uuid.uuid4(), repo_root=repo)
    listing = registry.get("list_files")
    reading = registry.get("read_file")
    assert listing.policy == reading.policy == "read"
    out = await execute_tool(None, ctx, listing, ListFilesArgs(path="src"))  # type: ignore[arg-type]
    assert out.splitlines()[0].startswith("3 files under src")
    out = await execute_tool(None, ctx, reading, ReadFileArgs(path=".env"))  # type: ignore[arg-type]
    assert out.startswith("error:") and "secrets" in out
    no_repo = ToolContext(run_id=uuid.uuid4(), workspace_id=uuid.uuid4())
    out = await execute_tool(None, no_repo, reading, ReadFileArgs(path="README.md"))  # type: ignore[arg-type]
    assert out == "error: No repository is attached to this run."


def test_tool_argument_caps() -> None:
    with pytest.raises(ValueError):
        ListFilesArgs(max_entries=501)
    with pytest.raises(ValueError):
        ReadFileArgs(path="a", max_lines=401)
    with pytest.raises(ValueError):
        ReadFileArgs(path="a", start_line=0)
    schema = registry.get("read_file").openai_schema()["function"]["parameters"]
    assert schema["properties"]["max_lines"]["maximum"] == 400
