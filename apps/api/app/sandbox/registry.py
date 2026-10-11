"""Push targets: the local kind registry (no auth) and ECR (a 12-hour token from the deploy
role). The ECR repository itself is created by the ECS target (M3), tagged on create; pushing
only needs `ecr:GetAuthorizationToken` and the layer upload actions on `groundcrew-*`."""

import asyncio
import base64

import boto3
from pydantic import SecretStr

from app.config import Settings
from app.sandbox.base import PushTarget, RegistryLogin


def kind_target(settings: Settings, repository: str, tag: str) -> PushTarget:
    """`GC_KIND_REGISTRY/<repository>:<tag>`: the name pod specs use too (docs/kind.md)."""
    return PushTarget(image=f"{settings.gc_kind_registry}/{repository}:{tag}")


async def ecr_login(session: boto3.Session) -> RegistryLogin:
    """Registry credentials from `CredentialsProvider.aws_deploy_session()`. The password is
    only ever passed to `docker login --password-stdin`."""

    def fetch() -> tuple[str, str]:
        data = session.client("ecr").get_authorization_token()["authorizationData"][0]
        return str(data["authorizationToken"]), str(data["proxyEndpoint"])

    token, endpoint = await asyncio.to_thread(fetch)
    username, password = base64.b64decode(token).decode().split(":", 1)
    return RegistryLogin(
        registry=endpoint.removeprefix("https://"),
        username=username,
        password=SecretStr(password),
    )


async def ecr_target(session: boto3.Session, repository: str, tag: str) -> PushTarget:
    login = await ecr_login(session)
    return PushTarget(image=f"{login.registry}/{repository}:{tag}", login=login)
