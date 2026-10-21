"""AWS access: short-lived sessions from a customer's role, and a read-only guard on every call.

Groundcrew never stores a customer's AWS keys. A connection stores the role ARN, the
ExternalId, and the account ID. Groundcrew's platform identity (the IAM user
groundcrew-platform, whose only permission is sts:AssumeRole on role/groundcrew-*) assumes
the role with the ExternalId; the temporary session is cached in memory until shortly before
it expires and is never written anywhere. The platform identity comes only from
GROUNDCREW_AWS_ACCESS_KEY_ID / GROUNDCREW_AWS_SECRET_ACCESS_KEY, else the default credential
chain without profiles or ~/.aws (a hosted deployment's task or instance role).

Every client made from a customer session carries a `before-parameter-build` hook that refuses any
operation not in AWS_READ_OPERATIONS, so a coding mistake cannot write, read a secret value,
or read customer data, even if the role would allow it. Regional clients are only made for
DISCOVERY_REGIONS; IAM and STS are global.
"""

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import boto3
import botocore.session
from botocore import xform_name
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings, get_settings
from app.security.redact import redact

# Operations a customer session may call, as "<botocore service name>:<Operation>". Reads
# only: Describe*, List*, Get*, Search, FilterLogEvents. tests/test_aws_connector.py fails if
# anything else is added, and if any operation that returns secret values or data is.
AWS_READ_OPERATIONS: frozenset[str] = frozenset(
    {
        "sts:GetCallerIdentity",
        "resource-explorer-2:Search",
        "resource-explorer-2:GetDefaultView",
        "resource-explorer-2:ListIndexes",
        "resourcegroupstaggingapi:GetResources",
        "cloudcontrol:GetResource",
        "cloudcontrol:ListResources",
        "ecs:ListClusters",
        "ecs:DescribeClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "elbv2:DescribeLoadBalancers",
        "elbv2:DescribeTargetGroups",
        "elbv2:DescribeTargetHealth",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeNetworkInterfaces",
        "lambda:ListFunctions",
        "lambda:ListTags",
        "lambda:GetFunctionConfiguration",
        "lambda:ListVersionsByFunction",
        "lambda:ListAliases",
        "lambda:GetAlias",
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "logs:DescribeLogGroups",
        "logs:FilterLogEvents",
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms",
        "secretsmanager:ListSecrets",
        "secretsmanager:DescribeSecret",
        "iam:GetRole",
        "iam:ListRoles",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
    }
)

# Operations a deploy session (the groundcrew-deploy role, Tier=deploy) may call. The IAM
# policy in infra/aws-connect.yaml limits them to groundcrew-* resources tagged
# project=groundcrew; this allowlist keeps code from calling anything else even so (no IAM,
# no RDS, no secret values, no task definition deregistration). Image pushes go through the
# registry with the ECR token, not here.
AWS_DEPLOY_OPERATIONS: frozenset[str] = frozenset(
    {
        "sts:GetCallerIdentity",
        "resourcegroupstaggingapi:GetResources",
        "ecs:CreateService",
        "ecs:UpdateService",
        "ecs:DeleteService",
        "ecs:DescribeServices",
        "ecs:DescribeClusters",
        "ecs:ListServices",
        "ecs:ListTasks",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:DescribeTasks",
        "ecs:RegisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTaskDefinitions",
        "ecs:TagResource",
        "ecr:GetAuthorizationToken",
        "ecr:CreateRepository",
        "ecr:TagResource",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "ecr:ListImages",
        "ecr:BatchDeleteImage",
        "ecr:DeleteRepository",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSecurityGroupRules",
        "ec2:DescribeNetworkInterfaces",
        "ec2:CreateSecurityGroup",
        "ec2:CreateTags",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:DeleteSecurityGroup",
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:TagResource",
        "logs:DescribeLogGroups",
        "logs:FilterLogEvents",
        # A deploy's env secrets, groundcrew/<app>/<VAR>: write and describe only. Reading a
        # value (GetSecretValue) stays forbidden; ECS injects it with the task execution role.
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:TagResource",
        "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret",
    }
)

# Operations the base identity may call: assume a customer role and check who it is.
BASE_OPERATIONS: frozenset[str] = frozenset({"sts:AssumeRole", "sts:GetCallerIdentity"})

# Never allowed, whatever the allowlist says (they return secret values or customer data).
# Operations an operate session (the groundcrew-operate role, EnableOperate=true) may call:
# approved emergency fixes only. The IAM policy limits the writes to the services and secrets
# listed in the stack; this allowlist keeps code from calling anything else (no create,
# delete, IAM, or secret values).
AWS_OPERATE_OPERATIONS: frozenset[str] = frozenset(
    {
        "sts:GetCallerIdentity",
        "ecs:DescribeServices",
        "ecs:UpdateService",
        "ecs:ListTaskDefinitions",
        "ecs:DescribeTaskDefinition",
        "secretsmanager:DescribeSecret",
        "secretsmanager:UpdateSecretVersionStage",
        "lambda:GetAlias",
        "lambda:ListVersionsByFunction",
        "lambda:UpdateAlias",
        "ec2:DescribeInstanceStatus",
        "ec2:RebootInstances",
    }
)

FORBIDDEN_OPERATIONS: frozenset[str] = frozenset(
    {
        "secretsmanager:GetSecretValue",
        "secretsmanager:BatchGetSecretValue",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath",
        "ssm:GetParameterHistory",
        "kms:Decrypt",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "lambda:GetFunction",  # returns a download URL for the function's code
        "logs:GetLogEvents",  # fine in itself, but Medic uses FilterLogEvents with limits
    }
)

READ_PREFIXES = ("Describe", "List", "Get", "Search", "Filter")

# Global services: one endpoint, no region restriction.
GLOBAL_SERVICES = frozenset({"iam", "sts"})

SESSION_NAME = "groundcrew-discovery"
DEPLOY_SESSION_NAME = "groundcrew-deploy"
OPERATE_SESSION_NAME = "groundcrew-operate"
SESSION_SECONDS = 3600
# Refresh a cached session this long before it expires.
REFRESH_MARGIN_SECONDS = 300

ROLE_ARN = re.compile(r"^arn:aws:iam::(\d{12}):role/(?:[\w+=,.@-]+/)*([\w+=,.@-]{1,64})$")
# The platform identity may only assume roles with this prefix (infra/aws-connect.yaml).
ROLE_PREFIX = "groundcrew-"
# botocore config without profiles or shared files: AWS_PROFILE and ~/.aws are ignored.
_NO_PROFILES = {
    "profile": (None, None, None, None),
    "config_file": (None, None, os.devnull, None),
    "credentials_file": (None, None, os.devnull, None),
}

# Adaptive retries back off on throttling (botocore's ThrottlingException handling).
CLIENT_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 8},
    connect_timeout=5,
    read_timeout=20,
    user_agent_extra="groundcrew-discovery",
)


class AwsError(RuntimeError):
    """An AWS call or setup failed. The message never contains credentials."""


class ReadOnlyViolationError(AwsError):
    """Code tried to call an operation outside the read-only allowlist."""


class RegionNotAllowedError(AwsError):
    """A regional client was asked for outside the connection's regions."""


# Commercial and GovCloud region names: us-east-2, eu-central-1, ap-southeast-4, us-gov-west-1.
AWS_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d{1,2}$")
MAX_CONNECTION_REGIONS = 10


def connection_regions(config: dict[str, Any] | None, settings: Settings) -> list[str]:
    """The regions Groundcrew reads for one AWS connection: the list its owner chose
    (`config["regions"]`), else DISCOVERY_REGIONS (the platform default, as before)."""
    chosen = [
        r for r in (config or {}).get("regions") or [] if isinstance(r, str) and AWS_REGION.match(r)
    ]
    return chosen[:MAX_CONNECTION_REGIONS] or list(settings.discovery_regions)


def arn_region(arn: str | None) -> str | None:
    """The region field of an ARN (arn:aws:ecs:eu-west-1:...), or None."""
    parts = (arn or "").split(":")
    return parts[3] if len(parts) > 4 and parts[0] == "arn" and AWS_REGION.match(parts[3]) else None


def is_read_operation(name: str) -> bool:
    _, _, op = name.partition(":")
    return name not in FORBIDDEN_OPERATIONS and op.startswith(READ_PREFIXES)


def new_external_id() -> str:
    """A fresh ExternalId for one workspace's AWS connection. Not a secret, but unguessable,
    so a different Groundcrew customer cannot get our role assumed for them (confused deputy)."""
    return "gc-" + secrets.token_urlsafe(24)


def parse_role_arn(role_arn: str) -> str:
    """The account ID of a Groundcrew role ARN, or AwsError saying what is wrong with it."""
    match = ROLE_ARN.match(role_arn.strip())
    if not match:
        raise AwsError(
            "That is not an IAM role ARN. Copy RoleArn from the stack's Outputs tab; it looks "
            "like arn:aws:iam::123456789012:role/groundcrew-read-only."
        )
    if not match.group(2).startswith(ROLE_PREFIX):
        raise AwsError(
            f"The role name must start with {ROLE_PREFIX}, so Groundcrew is allowed to assume "
            "it. Use the RoleArn output of the groundcrew-read-only stack."
        )
    return match.group(1)


def platform_session(settings: Settings, region: str) -> boto3.Session:
    """Groundcrew's own identity: the two GROUNDCREW_AWS_* keys, else the default chain
    (environment, container or instance role). Never a profile or ~/.aws."""
    core = botocore.session.Session(session_vars=_NO_PROFILES)
    key_id, secret = (
        settings.groundcrew_aws_access_key_id,
        settings.groundcrew_aws_secret_access_key,
    )
    if bool(key_id) != bool(secret):
        raise AwsError(
            "Set both GROUNDCREW_AWS_ACCESS_KEY_ID and GROUNDCREW_AWS_SECRET_ACCESS_KEY, "
            "or neither."
        )
    if key_id and secret:
        return boto3.Session(
            botocore_session=core,
            aws_access_key_id=key_id.get_secret_value(),
            aws_secret_access_key=secret.get_secret_value(),
            region_name=region,
        )
    return boto3.Session(botocore_session=core, region_name=region)


def _operation_guard(allowed: frozenset[str]) -> Any:
    def guard(model: Any, **_kwargs: Any) -> None:
        name = f"{model.service_model.service_name}:{model.name}"
        if name in FORBIDDEN_OPERATIONS or name not in allowed:
            raise ReadOnlyViolationError(f"Groundcrew does not call {name} (read-only allowlist).")

    return guard


def guard_session(session: boto3.Session, allowed: frozenset[str]) -> boto3.Session:
    """Install the allowlist hook on a session. Clients made from it inherit the hook."""
    # before-parameter-build runs for every call, before any handler can return a response
    # (a stub, a cache), so nothing skips the check.
    session.events.register_first("before-parameter-build", _operation_guard(allowed))
    return session


def error_text(exc: BaseException) -> str:
    """A short, redacted reason for an AWS failure."""
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return redact(f"{err.get('Code', 'Error')}: {err.get('Message', '')}".strip())
    if isinstance(exc, BotoCoreError | AwsError):
        return redact(str(exc))
    return redact(f"{type(exc).__name__}: {exc}")


# The codes AWS uses when a role's policy does not allow a call.
DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException"}
)


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        return str(code) if code else None
    return None


@dataclass(frozen=True)
class CallerIdentity:
    account: str
    arn: str


@dataclass
class _Cached:
    session: boto3.Session
    expires_at: float


class AwsSessions:
    """Base identity plus cached, short-lived customer sessions. In memory only."""

    def __init__(self, settings: Settings | None = None, *, base: boto3.Session | None = None):
        self.settings = settings or get_settings()
        self._base = base
        self._cache: dict[tuple[str, str, frozenset[str], str], _Cached] = {}
        self._lock = threading.Lock()

    @property
    def home_region(self) -> str:
        return self.settings.discovery_regions[0]

    def base(self) -> boto3.Session:
        if self._base is None:
            self._base = guard_session(
                platform_session(self.settings, self.home_region), BASE_OPERATIONS
            )
        return self._base

    def _sts(self, session: boto3.Session) -> Any:
        # Regional STS endpoint in an allowed region (the demo account denies other regions).
        return session.client("sts", region_name=self.home_region, config=CLIENT_CONFIG)

    def principal_arn(self) -> str:
        """The principal customers must trust: GROUNDCREW_PLATFORM_PRINCIPAL_ARN, else whoever
        the platform credentials are (an assumed-role ARN becomes its role ARN)."""
        if self.settings.groundcrew_platform_principal_arn:
            return self.settings.groundcrew_platform_principal_arn
        try:
            arn = str(self._sts(self.base()).get_caller_identity()["Arn"])
        except (ClientError, BotoCoreError) as exc:
            raise AwsError(
                "Groundcrew has no AWS platform identity. Set GROUNDCREW_AWS_ACCESS_KEY_ID, "
                "GROUNDCREW_AWS_SECRET_ACCESS_KEY, and GROUNDCREW_PLATFORM_PRINCIPAL_ARN "
                f"(docs/self-hosting.md). {error_text(exc)}"
            ) from None
        assumed = re.match(r"^arn:aws:sts::(\d{12}):assumed-role/([^/]+)/", arn)
        if assumed:
            return f"arn:aws:iam::{assumed.group(1)}:role/{assumed.group(2)}"
        return arn

    def _assume(
        self, role_arn: str, external_id: str | None, session_name: str = SESSION_NAME
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name,
            "DurationSeconds": SESSION_SECONDS,
        }
        if external_id is not None:
            kwargs["ExternalId"] = external_id
        creds: dict[str, Any] = self._sts(self.base()).assume_role(**kwargs)["Credentials"]
        return creds

    def assume(
        self,
        role_arn: str,
        external_id: str,
        *,
        operations: frozenset[str] = AWS_READ_OPERATIONS,
        region: str | None = None,
        session_name: str = SESSION_NAME,
    ) -> boto3.Session:
        """A customer session guarded by `operations` (read-only unless the deploy allowlist is
        passed), from the cache when it has over 5 minutes left."""
        if not external_id:
            raise AwsError("An ExternalId is required to assume a customer role.")
        region = region or self.home_region
        key = (role_arn, external_id, operations, region)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at - REFRESH_MARGIN_SECONDS > time.time():
                return cached.session
        try:
            creds = self._assume(role_arn, external_id, session_name)
        except (ClientError, BotoCoreError) as exc:
            raise AwsError(f"Could not assume {role_arn}: {error_text(exc)}") from None
        session = guard_session(
            boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=region,
            ),
            operations,
        )
        expiration = creds.get("Expiration")
        expires_at = (
            expiration.timestamp()
            if isinstance(expiration, datetime)
            else time.time() + SESSION_SECONDS
        )
        with self._lock:
            self._cache[key] = _Cached(session, expires_at)
        return session

    def forget(self, role_arn: str, external_id: str) -> None:
        with self._lock:
            for key in [k for k in self._cache if k[:2] == (role_arn, external_id)]:
                del self._cache[key]

    def verify(self, role_arn: str, external_id: str) -> CallerIdentity:
        """Prove the role works only with our ExternalId and belongs to the ARN's account.

        1. AssumeRole without the ExternalId must be refused (the trust policy has the
           sts:ExternalId condition). If it succeeds, the role is refused.
        2. AssumeRole with it, then GetCallerIdentity from the new session.
        Every failure becomes one sentence a non-expert can act on.
        """
        account = parse_role_arn(role_arn)
        try:
            self._assume(role_arn, None)
        except ClientError as exc:
            if error_code(exc) not in ("AccessDenied", "AccessDeniedException"):
                raise AwsError(f"Could not check {role_arn}: {error_text(exc)}") from None
        except BotoCoreError as exc:
            raise AwsError(self._no_platform_identity(exc)) from None
        else:
            raise AwsError(
                "This role can be assumed without the ExternalId, which is unsafe. Delete it and "
                "create it with the Connect AWS link, which requires the ExternalId."
            )
        self.forget(role_arn, external_id)
        try:
            session = self.assume(role_arn, external_id)
        except AwsError as exc:
            if "AccessDenied" not in str(exc):
                raise
            raise AwsError(
                f"Groundcrew could not assume {role_arn}. Usually the stack was created with a "
                f"different ExternalId; this workspace's is {external_id}. Delete the "
                "groundcrew-read-only stack, then use the Connect AWS link again so the "
                "ExternalId is filled in for you. If the stack is new, wait a minute and retry."
            ) from None
        try:
            ident = self._sts(session).get_caller_identity()
        except (ClientError, BotoCoreError) as exc:
            raise AwsError(f"GetCallerIdentity failed: {error_text(exc)}") from None
        if ident["Account"] != account:
            raise AwsError(f"The role's session is in account {ident['Account']}, not {account}.")
        return CallerIdentity(account=str(ident["Account"]), arn=str(ident["Arn"]))

    @staticmethod
    def _no_platform_identity(exc: BaseException) -> str:
        return (
            "Groundcrew's AWS platform identity is not set up: set "
            "GROUNDCREW_AWS_ACCESS_KEY_ID and GROUNDCREW_AWS_SECRET_ACCESS_KEY "
            f"(docs/self-hosting.md). {error_text(exc)}"
        )


class AwsReader:
    """Read-only clients for one customer session, limited to the connection's regions
    (`connection_regions`; DISCOVERY_REGIONS when none are given)."""

    def __init__(
        self,
        session: boto3.Session,
        settings: Settings | None = None,
        regions: list[str] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._regions = list(regions) if regions else list(self.settings.discovery_regions)
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    @property
    def regions(self) -> list[str]:
        return list(self._regions)

    def client(self, service: str, region: str | None = None) -> Any:
        if service in GLOBAL_SERVICES:
            region = self.regions[0]
        elif region is None or region not in self._regions:
            raise RegionNotAllowedError(
                f"Region {region!r} is not one of this connection's regions "
                f"({', '.join(self.regions)})."
            )
        key = (service, region)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = self.session.client(
                    service, region_name=region, config=CLIENT_CONFIG
                )
            return self._clients[key]

    def call(self, service: str, region: str | None, operation: str, **params: Any) -> Any:
        client = self.client(service, region)
        return getattr(client, xform_name(operation))(**params)

    def paginate(
        self,
        service: str,
        region: str | None,
        operation: str,
        key: str,
        *,
        limit: int = 1000,
        **params: Any,
    ) -> list[Any]:
        """All items under `key` across pages, at most `limit`."""
        client = self.client(service, region)
        items: list[Any] = []
        method = xform_name(operation)
        if client.can_paginate(method):
            for page in client.get_paginator(method).paginate(**params):
                items.extend(page.get(key, []))
                if len(items) >= limit:
                    break
        else:
            items.extend(getattr(client, method)(**params).get(key, []))
        return items[:limit]


_sessions: AwsSessions | None = None


def get_sessions() -> AwsSessions:
    """Process-wide session cache (API and worker each have their own)."""
    global _sessions
    if _sessions is None:
        _sessions = AwsSessions()
    return _sessions
