"""Read-tier repository tools for Scout (and later Builder and Medic).

Outputs are small and deterministic (see app.repos.files for the caps and safety rules), so
token costs are predictable. A refused path comes back as a short error the model can act on.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from app.db.models import ToolPolicy
from app.repos.files import (
    LIST_DEFAULT_ENTRIES,
    LIST_MAX_ENTRIES,
    READ_DEFAULT_LINES,
    READ_MAX_LINES,
    RepoFileError,
    list_files,
    read_file,
)
from app.tools.registry import ToolContext, tool


class ListFilesArgs(BaseModel):
    path: str = Field(
        default=".", description="Directory relative to the repository root, e.g. 'src'."
    )
    max_entries: int = Field(
        default=LIST_DEFAULT_ENTRIES,
        ge=1,
        le=LIST_MAX_ENTRIES,
        description="How many files to show at most.",
    )


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to the repository root, e.g. 'package.json'.")
    start_line: int = Field(default=1, ge=1, description="First line to show (1-based).")
    max_lines: int = Field(
        default=READ_DEFAULT_LINES, ge=1, le=READ_MAX_LINES, description="How many lines to show."
    )


def _root(ctx: ToolContext) -> Path:
    if ctx.repo_root is None:
        raise RepoFileError("No repository is attached to this run.")
    return ctx.repo_root


@tool(
    name="list_files",
    description=(
        "List the repository's files with sizes, sorted by path. Leaves out .gitignore'd files, "
        "dependency and build folders, and binaries. Capped; says when it is cut."
    ),
    args=ListFilesArgs,
    policy=ToolPolicy.read,
)
async def list_files_tool(ctx: ToolContext, args: ListFilesArgs) -> str:
    try:
        return await asyncio.to_thread(list_files, _root(ctx), args.path, args.max_entries)
    except RepoFileError as exc:
        return f"error: {exc}"


@tool(
    name="read_file",
    description=(
        "Read a text file from the repository as numbered lines, starting at start_line. "
        "Capped; says how to read the rest. Secret files (.env, keys) are never read."
    ),
    args=ReadFileArgs,
    policy=ToolPolicy.read,
)
async def read_file_tool(ctx: ToolContext, args: ReadFileArgs) -> str:
    try:
        return await asyncio.to_thread(
            read_file, _root(ctx), args.path, args.start_line, args.max_lines
        )
    except RepoFileError as exc:
        return f"error: {exc}"
