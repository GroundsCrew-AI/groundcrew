"""CredentialsProvider: short-lived access to one connection, for discovery, Medic, targets.

Contract (docs/phase3-interfaces.md):

    class CredentialsProvider(Protocol):
        def kubeconfig(self, connection_id: uuid.UUID) -> Path: ...
        def aws_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
        def aws_deploy_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
        def aws_operate_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
        def azure_credential(self, connection_id: uuid.UUID) -> AzureCredential: ...

All three block (STS call, file write): call them with `asyncio.to_thread` from async code.

`ConnectionCredentialsProvider` serves stored connections. Secrets stay Fernet-encrypted in
`connections.secret_encrypted` and are decrypted only inside these calls:
- aws_session: STS AssumeRole into the customer's read-only role with its ExternalId, from
  Groundcrew's platform identity (app.connectors.aws). The session expires within an hour,
  refuses every non-read operation, and is never stored. No AWS keys are ever stored for a
  customer. Always the read-only role, whatever the connection's tier.
- aws_deploy_session: the groundcrew-deploy role (Tier=deploy only, else CredentialsError), in
  GC_AWS_REGION, guarded by AWS_DEPLOY_OPERATIONS. Proposed contract change for Phase 4.
- aws_operate_session: the groundcrew-operate role (only when the connection has the operate
  capability, else CredentialsError), in the first DISCOVERY_REGIONS region, guarded by
  AWS_OPERATE_OPERATIONS. Phase 5 emergency fixes (app.actions).
- kubeconfig: written to a private temp file (0600 in a 0700 dir), deleted by `close()`.
- azure_credential: a client-secret credential (fixtures only for now, TODO(azure-live)).

`EnvCredentialsProvider` is the temporary version from the contract: it ignores the
connection and reads the environment, so Phase 4/5 code can run before connections exist.
"""

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, Self

import boto3
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.aws import (
    AWS_DEPLOY_OPERATIONS,
    AWS_OPERATE_OPERATIONS,
    DEPLOY_SESSION_NAME,
    OPERATE_SESSION_NAME,
    AwsSessions,
    get_sessions,
)
from app.db.models import Connection
from app.discovery.types import ConnectorError
from app.security.crypto import decrypt


class CredentialsError(ConnectorError):
    """No usable credentials for this connection. Never includes a secret."""


class AzureCredential(Protocol):
    """The azure-identity TokenCredential shape: get_token(*scopes) -> AccessToken."""

    def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class ClientSecretAzureCredential:
    """A service principal (tenant, client, secret). Live Azure is not built yet:
    TODO(azure-live) swap for azure.identity.ClientSecretCredential, which has the same
    get_token; the secret stays out of repr and logs."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._secret = SecretStr(client_secret)

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        raise CredentialsError("Azure is in preview: live Azure access is not built yet.")

    def __repr__(self) -> str:
        return f"ClientSecretAzureCredential(tenant_id={self.tenant_id!r}, client_id=***)"


class CredentialsProvider(Protocol):
    def kubeconfig(self, connection_id: uuid.UUID) -> Path: ...
    def aws_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
    def aws_deploy_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
    def aws_operate_session(self, connection_id: uuid.UUID) -> boto3.Session: ...
    def azure_credential(self, connection_id: uuid.UUID) -> AzureCredential: ...


def capabilities(conn: Connection) -> list[str]:
    """What an AWS connection may be used for: observe always, deploy with Tier=deploy and a
    verified deploy role, operate with EnableOperate=true and a verified operate role."""
    config = conn.config or {}
    out = ["observe"]
    if config.get("tier") == "deploy" and config.get("deploy_role_arn"):
        out.append("deploy")
    if config.get("operate_role_arn"):
        out.append("operate")
    return out


class _TempFiles:
    def __init__(self) -> None:
        self._dir: Path | None = None
        self.files: dict[uuid.UUID, Path] = {}

    def write(self, connection_id: uuid.UUID, text: str) -> Path:
        if connection_id in self.files and self.files[connection_id].exists():
            return self.files[connection_id]
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix="groundcrew-kube-"))  # 0700
        path = self._dir / f"{connection_id}.kubeconfig"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.files[connection_id] = path
        return path

    def close(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir, self.files = None, {}


class ConnectionCredentialsProvider:
    """Credentials for the given (already loaded) connections. Use as a context manager, or
    call close(), so kubeconfig files are deleted."""

    def __init__(
        self, connections: Iterable[Connection], *, sessions: AwsSessions | None = None
    ) -> None:
        self.connections = {c.id: c for c in connections}
        self._sessions = sessions
        self._temp = _TempFiles()

    @classmethod
    async def for_workspace(cls, session: AsyncSession, workspace_id: uuid.UUID) -> Self:
        rows = await session.scalars(
            select(Connection).where(Connection.workspace_id == workspace_id)
        )
        return cls(list(rows))

    def _get(self, connection_id: uuid.UUID, kind: str) -> Connection:
        conn = self.connections.get(connection_id)
        if conn is None:
            raise CredentialsError(f"Connection {connection_id} is not in this workspace.")
        if conn.kind != kind:
            raise CredentialsError(f"Connection {conn.name} is {conn.kind}, not {kind}.")
        return conn

    def _secret(self, conn: Connection) -> str:
        if not conn.secret_encrypted:
            raise CredentialsError(f"Connection {conn.name} has no stored credential.")
        return decrypt(conn.secret_encrypted)

    def aws_session(self, connection_id: uuid.UUID) -> boto3.Session:
        conn = self._get(connection_id, "aws")
        role_arn, external_id = conn.config.get("role_arn"), conn.config.get("external_id")
        if not role_arn or not external_id:
            raise CredentialsError(
                f"{conn.name} is not finished: paste the role ARN on the Connections page."
            )
        return (self._sessions or get_sessions()).assume(str(role_arn), str(external_id))

    def aws_deploy_session(self, connection_id: uuid.UUID) -> boto3.Session:
        conn = self._get(connection_id, "aws")
        tier = conn.config.get("tier") or "observe"
        if tier != "deploy":
            raise CredentialsError(
                f"{conn.name} is connected with Tier={tier}, which cannot deploy. Update its "
                "groundcrew-read-only stack with Tier=deploy, then turn on deploys for it on "
                "the Connections page (docs/phase4-infra.md)."
            )
        role_arn, external_id = conn.config.get("deploy_role_arn"), conn.config.get("external_id")
        if not role_arn or not external_id:
            raise CredentialsError(f"{conn.name} has no verified deploy role yet.")
        sessions = self._sessions or get_sessions()
        return sessions.assume(
            str(role_arn),
            str(external_id),
            operations=AWS_DEPLOY_OPERATIONS,
            region=sessions.settings.gc_aws_region,
            session_name=DEPLOY_SESSION_NAME,
        )

    def aws_operate_session(self, connection_id: uuid.UUID) -> boto3.Session:
        conn = self._get(connection_id, "aws")
        if "operate" not in capabilities(conn):
            raise CredentialsError(
                f"{conn.name} cannot run emergency fixes: update its groundcrew-read-only stack "
                "with EnableOperate=true and the services and secrets Groundcrew may act on, "
                "then turn on emergency fixes on the Connections page (docs/phase5-interfaces.md)."
            )
        sessions = self._sessions or get_sessions()
        return sessions.assume(
            str(conn.config["operate_role_arn"]),
            str(conn.config["external_id"]),
            operations=AWS_OPERATE_OPERATIONS,
            region=sessions.settings.discovery_regions[0],
            session_name=OPERATE_SESSION_NAME,
        )

    def kubeconfig(self, connection_id: uuid.UUID) -> Path:
        conn = self._get(connection_id, "k8s")
        return self._temp.write(connection_id, self._secret(conn))

    def azure_credential(self, connection_id: uuid.UUID) -> AzureCredential:
        conn = self._get(connection_id, "azure")
        tenant, client = conn.config.get("tenant_id"), conn.config.get("client_id")
        if not tenant or not client:
            raise CredentialsError(f"{conn.name} has no tenant or client id.")
        return ClientSecretAzureCredential(str(tenant), str(client), self._secret(conn))

    def close(self) -> None:
        self._temp.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EnvCredentialsProvider:
    """Temporary provider (contract): the same credentials for every connection_id, from env.

    - kubeconfig: KUBECONFIG
    - aws_session: GROUNDCREW_DEV_AWS_ROLE_ARN + GROUNDCREW_DEV_AWS_EXTERNAL_ID, assumed from
      the platform identity like a real connection (never a profile or ~/.aws)
    - aws_deploy_session: GROUNDCREW_DEV_AWS_DEPLOY_ROLE_ARN + GROUNDCREW_DEV_AWS_EXTERNAL_ID
    - aws_operate_session: GROUNDCREW_DEV_AWS_OPERATE_ROLE_ARN + GROUNDCREW_DEV_AWS_EXTERNAL_ID
    - azure_credential: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
    """

    def __init__(
        self, env: dict[str, str] | None = None, *, sessions: AwsSessions | None = None
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self._sessions = sessions

    def _need(self, *names: str) -> list[str]:
        missing = [n for n in names if not self.env.get(n)]
        if missing:
            raise CredentialsError(f"Set {', '.join(missing)} to use EnvCredentialsProvider.")
        return [self.env[n] for n in names]

    def kubeconfig(self, connection_id: uuid.UUID) -> Path:
        (path,) = self._need("KUBECONFIG")
        return Path(path)

    def aws_session(self, connection_id: uuid.UUID) -> boto3.Session:
        role_arn, external_id = self._need(
            "GROUNDCREW_DEV_AWS_ROLE_ARN", "GROUNDCREW_DEV_AWS_EXTERNAL_ID"
        )
        return (self._sessions or get_sessions()).assume(role_arn, external_id)

    def aws_deploy_session(self, connection_id: uuid.UUID) -> boto3.Session:
        role_arn, external_id = self._need(
            "GROUNDCREW_DEV_AWS_DEPLOY_ROLE_ARN", "GROUNDCREW_DEV_AWS_EXTERNAL_ID"
        )
        sessions = self._sessions or get_sessions()
        return sessions.assume(
            role_arn,
            external_id,
            operations=AWS_DEPLOY_OPERATIONS,
            region=sessions.settings.gc_aws_region,
            session_name=DEPLOY_SESSION_NAME,
        )

    def aws_operate_session(self, connection_id: uuid.UUID) -> boto3.Session:
        role_arn, external_id = self._need(
            "GROUNDCREW_DEV_AWS_OPERATE_ROLE_ARN", "GROUNDCREW_DEV_AWS_EXTERNAL_ID"
        )
        sessions = self._sessions or get_sessions()
        return sessions.assume(
            role_arn,
            external_id,
            operations=AWS_OPERATE_OPERATIONS,
            region=sessions.settings.discovery_regions[0],
            session_name=OPERATE_SESSION_NAME,
        )

    def azure_credential(self, connection_id: uuid.UUID) -> AzureCredential:
        tenant, client, secret = self._need(
            "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"
        )
        return ClientSecretAzureCredential(tenant, client, secret)
