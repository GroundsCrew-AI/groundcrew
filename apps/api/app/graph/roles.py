"""What a resource is for: part of the app, or tooling around it.

Tooling is infrastructure that manages or observes the app but is not used by it: Terraform
state buckets and lock tables, the bucket holding Groundcrew's CloudFormation template,
Groundcrew's own read-only role, and Resource Explorer. The summary leaves tooling out (so
the model cannot claim the app stores data in a state bucket), and the Map folds it into a
"Tooling" group. An edge to a tooling resource still shows it.

A customer marks their own: the AWS tag `groundcrew:role` (or the Kubernetes label
`groundcrew/role`) set to `tooling` puts a resource in Tooling, and `app` keeps it in the app
whatever its name says. The marker wins; the name rules below are only the fallback.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from app.discovery.types import DiscoveredResource

Role = Literal["app", "tooling"]

_TOOLING_BUCKET = re.compile(
    r"(tf-?state|terraform-?state|terraform-locks?|tf-locks?|cloudtrail|"
    r"^groundcrew-templates-|cdk-.*-assets|cf-templates-)",
    re.IGNORECASE,
)
_TOOLING_TABLE = re.compile(r"(terraform|tf)-?(state-)?locks?", re.IGNORECASE)
_TOOLING_ROLE = re.compile(r"^(groundcrew-|GroundcrewReadOnly$)")

# AWS tag keys may contain ":"; Kubernetes label keys use "<prefix>/<name>".
ROLE_KEYS = ("groundcrew:role", "groundcrew/role")
ROLES: tuple[Role, ...] = ("app", "tooling")


def marked_role(tags: Mapping[str, Any] | None) -> Role | None:
    """The role a person set with the tag or label, if any (case and spaces ignored)."""
    for key in ROLE_KEYS:
        value = str((tags or {}).get(key) or "").strip().lower()
        if value in ROLES:
            return value
    return None


def role_of(resource_type: str, name: str, tags: Mapping[str, Any] | None = None) -> Role:
    marked = marked_role(tags)
    if marked is not None:
        return marked
    if resource_type == "s3:bucket" and _TOOLING_BUCKET.search(name):
        return "tooling"
    if resource_type == "dynamodb:table" and _TOOLING_TABLE.search(name):
        return "tooling"
    if resource_type == "iam:role" and _TOOLING_ROLE.search(name):
        return "tooling"
    if resource_type.startswith(("resource-explorer-2:", "cloudformation:")):
        return "tooling"
    return "app"


def role(resource: DiscoveredResource) -> Role:
    return role_of(resource.type, resource.name, resource.tags)


def linked_to_app(roles: Mapping[str, Role], edges: Iterable[tuple[str, str]]) -> set[str]:
    """Keys with an edge to an app resource. Edges among tooling (a Lambda, its role, its log
    group, its secret) do not make tooling part of the app."""
    out: set[str] = set()
    for a, b in edges:
        if roles.get(b) == "app":
            out.add(a)
        if roles.get(a) == "app":
            out.add(b)
    return out
