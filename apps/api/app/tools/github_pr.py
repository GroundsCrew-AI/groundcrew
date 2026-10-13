"""Fix pull requests through the GitHub App (Phase 5, docs/phase5-interfaces.md).

Medic's only write to a customer repository. It always lands on a new `groundcrew/fix-*`
branch and opens a pull request for a person to review and merge: never a commit to the default
branch, never a merge. It runs only with an `ApprovedAction` for tool `open_fix_pr` whose
`plan_hash` is `PullRequestDraft.plan_hash()`.

`GitHubFixPullRequests` makes the calls; `FakeFixPullRequests` records drafts for tests.
"""

import base64
import contextlib
import difflib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.connectors.github import (
    API_VERSION,
    GitHubApp,
    GitHubError,
    GitHubWriteRefusedError,
    check_repo_name,
    get_github_app,
)
from app.db.session import SessionFactory
from app.policy.actions import OPEN_FIX_PR, ApprovedAction, consume_approved_action
from app.policy.engine import PolicyViolationError
from app.security.redact import redact

FIX_BRANCH_PREFIX = "groundcrew/fix-"
_FIX_BRANCH = re.compile(r"^groundcrew/fix-[a-z0-9][a-z0-9-]{0,60}$")


class PullRequestDraft(BaseModel):
    """A fix PR, ready to approve. The approval card shows `files` as diffs."""

    model_config = ConfigDict(frozen=True)

    installation_id: int
    repo: str = Field(description="owner/name")
    # Base branch (the repository's default branch unless the fix says otherwise).
    base: str
    # New branch, always under FIX_BRANCH_PREFIX.
    branch: str
    title: str
    body: str
    # Full new contents per path. A path with no current file is created.
    files: dict[str, str]

    def plan_hash(self) -> str:
        body = {
            "installation_id": self.installation_id,
            "repo": self.repo,
            "base": self.base,
            "branch": self.branch,
            "title": self.title,
            "body": self.body,
            "files": {
                path: hashlib.sha256(text.encode()).hexdigest()
                for path, text in sorted(self.files.items())
            },
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class PullRequestRef(BaseModel):
    repo: str
    number: int
    url: str
    branch: str
    head_sha: str
    # Refreshed from GitHub when an incident is read; "open" when Medic opens it.
    state: Literal["open", "closed", "merged"] = "open"
    # When it merged: Medic's verify waits for a deploy that started after this.
    merged_at: datetime | None = None


class FixPullRequests(Protocol):
    async def open_fix_pr(
        self, draft: PullRequestDraft, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> PullRequestRef:
        """Create the branch from `base`, commit `files`, open the PR. Consumes the token
        before the first write; raises PolicyViolationError without a valid one."""
        ...


class FakeFixPullRequests:
    """Records PRs instead of calling GitHub, with the real approval check. For tests, and for
    Medic until the GitHub calls land."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory
        self.opened: list[PullRequestDraft] = []

    async def open_fix_pr(
        self, draft: PullRequestDraft, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> PullRequestRef:
        check_fix_branch(draft.branch)
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=run_id,
            action=OPEN_FIX_PR,
            plan_hash=draft.plan_hash(),
        )
        self.opened.append(draft)
        number = len(self.opened)
        return PullRequestRef(
            repo=draft.repo,
            number=number,
            url=f"https://github.com/{draft.repo}/pull/{number}",
            branch=draft.branch,
            head_sha=draft.plan_hash()[:40],
        )


def check_fix_branch(branch: str) -> str:
    """Fix branches are always new `groundcrew/fix-*` branches, never the default branch."""
    if not _FIX_BRANCH.match(branch):
        raise PolicyViolationError(f"Fix PRs go on a {FIX_BRANCH_PREFIX}* branch, not {branch!r}.")
    return branch


# --- the real client -------------------------------------------------------------------------

# The only writes Medic makes on GitHub. Each is also checked against its body below: refs only
# under refs/heads/groundcrew/fix-*, contents only on that branch, pulls only from it.
_WRITES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/repos/[^/]+/[^/]+/git/refs$")),
    ("PUT", re.compile(r"^/repos/[^/]+/[^/]+/contents/.+$")),
    ("POST", re.compile(r"^/repos/[^/]+/[^/]+/pulls$")),
)
MAX_FILES = 10
MAX_FILE_BYTES = 200_000
# Paths a fix PR never touches: secrets and the pipeline itself.
_FORBIDDEN = re.compile(r"(^|/)(\.env[^/]*|.*\.pem|.*\.key|id_rsa.*|\.github/workflows/.*)$")


class FixPullRequestError(Exception):
    """GitHub refused, or the draft breaks a rule. Safe to show; never holds a token."""


def check_draft(draft: PullRequestDraft) -> None:
    check_fix_branch(draft.branch)
    check_repo_name(draft.repo)
    if not draft.files:
        raise FixPullRequestError("A fix PR needs at least one file.")
    if len(draft.files) > MAX_FILES:
        raise FixPullRequestError(f"A fix PR changes at most {MAX_FILES} files.")
    for path, text in draft.files.items():
        parts = path.split("/")
        if path.startswith("/") or ".." in parts or not path or _FORBIDDEN.search(path):
            raise FixPullRequestError(f"A fix PR may not write {path!r}.")
        if len(text.encode()) > MAX_FILE_BYTES:
            raise FixPullRequestError(f"{path} is larger than {MAX_FILE_BYTES} bytes.")


class GitHubFixPullRequests:
    """Opens fix PRs with the GitHub App's installation token: branch from base, one commit per
    file (contents API), then the pull request. Nothing else is ever written."""

    def __init__(
        self,
        session_factory: SessionFactory,
        app: GitHubApp | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.app = app or get_github_app()
        self._transport = transport

    async def _call(
        self,
        method: str,
        path: str,
        token: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if method != "GET" and not any(m == method and p.match(path) for m, p in _WRITES):
            raise GitHubWriteRefusedError(f"Groundcrew does not call {method} {path} on GitHub.")
        async with httpx.AsyncClient(
            base_url=self.app.settings.github_api_url, transport=self._transport, timeout=20
        ) as client:
            try:
                response = await client.request(
                    method,
                    path,
                    json=body,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "groundcrew",
                    },
                )
            except httpx.HTTPError as exc:
                raise FixPullRequestError(f"GitHub {method} {path}: {type(exc).__name__}") from None
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code >= 400:
            message = ""
            with contextlib.suppress(ValueError, AttributeError):
                message = str(response.json().get("message", ""))
            raise FixPullRequestError(
                redact(f"GitHub {method} {path} returned {response.status_code}: {message}")
            )
        return response.json()

    async def open_fix_pr(
        self, draft: PullRequestDraft, token: ApprovedAction, *, run_id: uuid.UUID
    ) -> PullRequestRef:
        check_draft(draft)
        await consume_approved_action(
            self.session_factory,
            token,
            run_id=run_id,
            action=OPEN_FIX_PR,
            plan_hash=draft.plan_hash(),
        )
        bearer = await self.app.installation_token(draft.installation_id)
        repo = draft.repo
        base = await self._call("GET", f"/repos/{repo}/git/ref/heads/{draft.base}", bearer)
        if not base:
            raise FixPullRequestError(f"{repo} has no branch {draft.base}.")
        ref = f"refs/heads/{draft.branch}"
        await self._call(
            "POST", f"/repos/{repo}/git/refs", bearer, {"ref": ref, "sha": base["object"]["sha"]}
        )
        head = base["object"]["sha"]
        for path, text in sorted(draft.files.items()):
            current = await self._call(
                "GET", f"/repos/{repo}/contents/{path}", bearer, params={"ref": draft.branch}
            )
            body: dict[str, Any] = {
                "message": f"{draft.title}\n\n{path}",
                "content": base64.b64encode(text.encode()).decode(),
                "branch": draft.branch,
            }
            if isinstance(current, dict) and current.get("sha"):
                body["sha"] = current["sha"]
            written = await self._call("PUT", f"/repos/{repo}/contents/{path}", bearer, body)
            head = str(((written or {}).get("commit") or {}).get("sha") or head)
        pull = await self._call(
            "POST",
            f"/repos/{repo}/pulls",
            bearer,
            {"title": draft.title, "head": draft.branch, "base": draft.base, "body": draft.body},
        )
        return PullRequestRef(
            repo=repo,
            number=int(pull["number"]),
            url=str(pull["html_url"]),
            branch=draft.branch,
            head_sha=head,
        )


# --- reads Medic needs to write a fix ----------------------------------------------------------


@dataclass
class CommitFile:
    path: str
    status: str
    patch: str
    # The file as it is on the base branch now (None when it no longer exists).
    current: str | None


async def read_file(
    app: GitHubApp, installation_id: int, repo: str, path: str, ref: str
) -> str | None:
    try:
        data = await app.get(
            installation_id, f"/repos/{check_repo_name(repo)}/contents/{path}", {"ref": ref}
        )
    except GitHubError as exc:
        if "404" in str(exc):
            return None
        raise
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    return base64.b64decode(str(data.get("content", ""))).decode(errors="replace")


async def commit_files(
    app: GitHubApp, installation_id: int, repo: str, sha: str, base: str, *, limit: int = 6
) -> list[CommitFile]:
    """What a commit changed (patches) and each file's current contents on `base`."""
    detail = await app.get(installation_id, f"/repos/{check_repo_name(repo)}/commits/{sha}")
    out = []
    for f in detail.get("files", [])[:limit]:
        path = str(f.get("filename"))
        if _FORBIDDEN.search(path):
            continue
        out.append(
            CommitFile(
                path=path,
                status=str(f.get("status", "modified")),
                patch=str(f.get("patch") or "")[:6000],
                current=await read_file(app, installation_id, repo, path, base),
            )
        )
    return out


def unified_diff(path: str, before: str | None, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}" if before is not None else "/dev/null",
            tofile=f"b/{path}",
        )
    )
