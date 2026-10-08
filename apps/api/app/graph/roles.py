"""What a resource is for: part of the app, or tooling around it.

Tooling is infrastructure that manages or observes the app but is not used by it: Terraform
state buckets and lock tables, the bucket holding Groundcrew's CloudFormation template,
Groundcrew's own read-only role, and Resource Explorer. The summary leaves tooling out (so
the model cannot claim the app stores data in a state bucket), and the Map folds it into a
"Tooling" group. An edge to a tooling resource still shows it.
"""

import re
from typing import Literal

from app.discovery.types import DiscoveredResource

Role = Literal["app", "tooling"]

_TOOLING_BUCKET = re.compile(
    r"(tf-?state|terraform-?state|terraform-locks?|tf-locks?|cloudtrail|"
    r"^groundcrew-templates-|cdk-.*-assets|cf-templates-)",
    re.IGNORECASE,
)
_TOOLING_TABLE = re.compile(r"(terraform|tf)-?(state-)?locks?", re.IGNORECASE)
_TOOLING_ROLE = re.compile(r"^(groundcrew-|GroundcrewReadOnly$)")


def role_of(resource_type: str, name: str) -> Role:
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
    return role_of(resource.type, resource.name)
