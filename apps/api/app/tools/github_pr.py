"""Fix pull requests through the GitHub App (Phase 5, docs/phase5-interfaces.md).

Medic's only write to a customer repository. It always lands on a new `groundcrew/fix-*`
branch and opens a pull request for a person to review and merge: never a commit to the default
branch, never a merge. It runs only with an `ApprovedAction` for tool `open_fix_pr` whose
`plan_hash` is `PullRequestDraft.plan_hash()`.

The contract and a fake land in M0; the GitHub calls land with Medic (M3).
"""

import hashlib
import json
import re
import uuid
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.db.session import SessionFactory
from app.policy.actions import OPEN_FIX_PR, ApprovedAction, consume_approved_action
from app.policy.engine import PolicyViolationError

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
