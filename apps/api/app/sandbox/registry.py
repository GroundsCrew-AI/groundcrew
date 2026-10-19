"""Push targets: the local kind registry (no auth) and ECR (a 12-hour token from the deploy
role). The ECR repository itself is created by the ECS target (M3) or by a Kubernetes deploy
with ECR settings (app.targets.k8s), tagged on create; pushing only needs
`ecr:GetAuthorizationToken` and the layer upload actions on `groundcrew-*`."""

import asyncio
import base64

import boto3
from pydantic import SecretStr

from app.config import Settings
from app.sandbox.base import PushTarget, RegistryLogin


def kind_target(settings: Settings, repository: str, tag: str) -> PushTarget:
    """`GC_KIND_REGISTRY/<repository>:<tag>`: the name pod specs use too (docs/kind.md)."""
    return PushTarget(image=f"{settings.gc_kind_registry}/{repository}:{tag}")


async def ecr_login(session: boto3.Session, region: str | None = None) -> RegistryLogin:
    """Registry credentials from `CredentialsProvider.aws_deploy_session()`, for the ECR
    registry in `region` (default: the session's). The password is only ever passed to
    `docker login --password-stdin` or written into an imagePullSecret."""

    def fetch() -> tuple[str, str]:
        client = session.client("ecr", region_name=region) if region else session.client("ecr")
        data = client.get_authorization_token()["authorizationData"][0]
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
