"""Connections: AWS (role + ExternalId), GitHub App repos, Vercel token, and Azure / Kubernetes
in preview. Every route needs the signed-in user and workspace membership (404 otherwise).

Secrets (Vercel token, Azure client secret, kubeconfig) are encrypted with SECRETS_KEY before
they are stored and are never returned or logged. AWS stores no credentials at all: the role
ARN, the ExternalId, and the account ID.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import deletion, quotas
from app.accounts import NotFoundError, member_workspace, require_owner
from app.audit import record
from app.config import Settings, get_settings
from app.connectors import aws_operate, github_claims
from app.connectors import kubernetes as kube
from app.connectors.aws import (
    AWS_OPERATE_OPERATIONS,
    OPERATE_SESSION_NAME,
    AwsError,
    connection_regions,
    get_sessions,
    new_external_id,
    parse_role_arn,
)
from app.connectors.deploy_infra import deploy_role_arn, operate_role_arn
from app.connectors.github import GitHubApp, GitHubError, check_repo_name, get_github_app
from app.connectors.kubernetes import kubeconfig_summary
from app.connectors.vercel import VercelClient, VercelError
from app.db.models import (
    Agent,
    Connection,
    Resource,
    RunStatus,
    User,
    Workspace,
    WorkspaceRole,
)
from app.discovery.engine import start_discovery
from app.discovery.types import ConnectorError
from app.remote_agents.signing import new_join_token
from app.routes.deps import ClientIp, CurrentUser, SessionDep, SettingsDep
from app.routes.discovery_schemas import (
    AgentConnectIn,
    AgentJoinOut,
    AgentRegistryIn,
    AwsCompleteIn,
    AwsOperateIn,
    AwsOperateValuesOut,
    AwsRegionsIn,
    AwsStartIn,
    AwsStartOut,
    AwsTemplateUrlOut,
    AwsTierIn,
    AzureConnectIn,
    ConnectionDeleted,
    ConnectionOut,
    GitHubAuthorizeOut,
    GitHubClaimIn,
    GitHubClaimOut,
    GitHubInstallationOut,
    GitHubInstallationsOut,
    GitHubLinkIn,
    GitHubRepoOut,
    K8sConfigMapChangesIn,
    K8sDeploySettingsIn,
    K8sOperateIn,
    KubernetesConnectIn,
    StackParameter,
    VercelConnectIn,
)
from app.routes.schemas import RunCreated
from app.security.crypto import SecretsKeyError, encrypt
from app.targets.k8s_deploy import CONFIG_KEY as DEPLOY_SETTINGS_KEY
from app.targets.k8s_deploy import (
    DeploySettingsError,
    K8sDeploySettings,
    build_settings,
    check_ecr_connection,
    deploy_settings,
)

router = APIRouter(prefix="/connections", tags=["connections"])

STACK_NAME = "groundcrew-read-only"
ROLE_NAME = "groundcrew-read-only"
TEMPLATE = Path(__file__).resolve().parents[1] / "connectors" / "templates" / "aws-connect.yaml"


def _not_found(what: str = "Connection") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


async def _check_member(session: AsyncSession, user: User, workspace_id: uuid.UUID) -> None:
    try:
        await member_workspace(session, user, workspace_id)
    except NotFoundError:
        raise _not_found("Workspace") from None


async def _check_owner(session: AsyncSession, user: User, workspace_id: uuid.UUID) -> None:
    """Connections are workspace infrastructure: only owners add, change, or remove them."""
    try:
        await require_owner(session, user, workspace_id)
    except NotFoundError:
        raise _not_found("Workspace") from None


async def member_connection(
    session: AsyncSession,
    user: User,
    connection_id: uuid.UUID,
    *,
    for_update: bool = False,
    owner: bool = False,
    minimum: WorkspaceRole = WorkspaceRole.viewer,
) -> Connection:
    conn = await session.get(Connection, connection_id, with_for_update=for_update)
    if conn is None:
        raise _not_found()
    try:
        if owner:
            await require_owner(session, user, conn.workspace_id)
        else:
            await member_workspace(session, user, conn.workspace_id, minimum)
    except NotFoundError:
        raise _not_found() from None
    return conn


async def _audit(
    session: AsyncSession, user: User, conn: Connection, action: str, ip: str | None
) -> None:
    """connection.<action> in the audit log, with the connection's kind and name."""
    await session.flush()  # a new connection gets its id
    record(
        session,
        workspace_id=conn.workspace_id,
        actor=user,
        action=f"connection.{action}",
        target_type="connection",
        target_id=conn.id,
        detail={"kind": conn.kind, "name": conn.name},
        ip=ip,
    )


async def _counts(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Resource.connection_id, func.count(Resource.id))
        .where(Resource.connection_id.in_(ids), Resource.stale.is_(False))
        .group_by(Resource.connection_id)
    )
    return {cid: int(n) for cid, n in rows if cid is not None}


async def _agents(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Agent]:
    if not ids:
        return {}
    found = await session.scalars(select(Agent).where(Agent.connection_id.in_(ids)))
    return {a.connection_id: a for a in found}


async def _out(session: AsyncSession, conn: Connection) -> ConnectionOut:
    counts = await _counts(session, [conn.id])
    agents = await _agents(session, [conn.id])
    return ConnectionOut.build(conn, counts.get(conn.id, 0), agents.get(conn.id))


def _encrypt(secret: str) -> str:
    try:
        return encrypt(secret)
    except SecretsKeyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


@router.get("")
async def list_connections(
    user: CurrentUser, session: SessionDep, workspace_id: Annotated[uuid.UUID, Query()]
) -> list[ConnectionOut]:
    await _check_member(session, user, workspace_id)
    conns = list(
        await session.scalars(
            select(Connection)
            .where(Connection.workspace_id == workspace_id)
            .order_by(Connection.created_at)
        )
    )
    counts = await _counts(session, [c.id for c in conns])
    agents = await _agents(session, [c.id for c in conns if c.kind == "agent"])
    return [ConnectionOut.build(c, counts.get(c.id, 0), agents.get(c.id)) for c in conns]


# ------------------------------------------------------------------------------- AWS


def _console_url(settings: Settings, external_id: str, principal: str, tier: str) -> str | None:
    if not settings.aws_connect_template_url:
        return None
    region = settings.discovery_regions[0]
    params = urlencode(
        {
            "templateURL": settings.aws_connect_template_url,
            "stackName": STACK_NAME,
            "param_GroundcrewPrincipalArn": principal,
            "param_ExternalId": external_id,
            "param_RoleName": ROLE_NAME,
            "param_Tier": tier,
        },
        quote_via=quote,
    )
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/quickcreate?{params}"
    )


@router.get("/aws/template-url")
async def aws_template_url(_user: CurrentUser, settings: SettingsDep) -> AwsTemplateUrlOut:
    """Where the connect template lives, for updating an existing stack (Enable deploys)."""
    return AwsTemplateUrlOut(
        url=settings.aws_connect_template_url, template_path="/connections/aws/template"
    )


@router.get("/aws/template", response_class=PlainTextResponse)
async def aws_template(_user: CurrentUser) -> PlainTextResponse:
    """The CloudFormation template customers deploy (infra/aws-connect.yaml)."""
    return PlainTextResponse(TEMPLATE.read_text(), media_type="text/yaml")


@router.post("/aws/start")
async def aws_start(
    body: AwsStartIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> AwsStartOut:
    """Start (or resume) connecting an AWS account: a fresh ExternalId and the stack link for
    the chosen tier (stored on the connection; complete verifies the deploy role for deploy)."""
    await _check_owner(session, user, body.workspace_id)
    try:
        principal = await asyncio.to_thread(get_sessions().principal_arn)
    except AwsError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    conn = await session.scalar(
        select(Connection).where(
            Connection.workspace_id == body.workspace_id,
            Connection.kind == "aws",
            Connection.status == "pending",
        )
    )
    region = settings.discovery_regions[0]
    created = conn is None
    if conn is None:
        await quotas.check_can_add_connection(session, body.workspace_id, settings)
        conn = Connection(
            workspace_id=body.workspace_id,
            kind="aws",
            name="AWS",
            status="pending",
            config={
                "external_id": new_external_id(),
                "principal_arn": principal,
                "region": region,
                "tier": body.tier,
            },
        )
        session.add(conn)
        await session.flush()
    elif conn.config.get("tier") != body.tier:
        conn.config = {**conn.config, "tier": body.tier}
    external_id = str(conn.config["external_id"])
    if created:
        await _audit(session, user, conn, "created", ip)
    await session.commit()
    params = (
        f"ParameterKey=GroundcrewPrincipalArn,ParameterValue={principal} "
        f"ParameterKey=ExternalId,ParameterValue={external_id} "
        f"ParameterKey=Tier,ParameterValue={body.tier}"
    )
    return AwsStartOut(
        connection_id=conn.id,
        tier=body.tier,
        external_id=external_id,
        principal_arn=principal,
        region=region,
        stack_name=STACK_NAME,
        console_url=_console_url(settings, external_id, principal, body.tier),
        template_path="/connections/aws/template",
        template_download_url=settings.web_origin.rstrip("/") + "/aws-connect.yaml",
        upload_console_url=(
            f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
            "#/stacks/create"
        ),
        parameters=[
            StackParameter(key="GroundcrewPrincipalArn", value=principal),
            StackParameter(key="ExternalId", value=external_id),
            StackParameter(key="Tier", value=body.tier),
        ],
        cli_command=(
            f"aws cloudformation create-stack --region {region} --stack-name {STACK_NAME} "
            f"--template-body file://aws-connect.yaml --capabilities CAPABILITY_NAMED_IAM "
            f"--parameters {params}"
        ),
        role_arn_command=(
            f"aws cloudformation describe-stacks --region {region} --stack-name {STACK_NAME} "
            "--query \"Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue\" --output text"
        ),
    )


@router.post("/aws/complete")
async def aws_complete(
    body: AwsCompleteIn, user: CurrentUser, session: SessionDep
) -> ConnectionOut:
    """Verify the role (refused without the ExternalId, works with it, right account) and
    store its ARN; for Tier=deploy, verify the groundcrew-deploy role the same way. No
    credentials are stored."""
    conn = await member_connection(session, user, body.connection_id, for_update=True, owner=True)
    if conn.kind != "aws":
        raise _unprocessable("This is not an AWS connection.")
    role_arn = body.role_arn.strip()
    try:
        account = parse_role_arn(role_arn)
    except AwsError as exc:
        raise _unprocessable(str(exc)) from None
    duplicate = await session.scalar(
        select(Connection.id).where(
            Connection.workspace_id == conn.workspace_id,
            Connection.kind == "aws",
            Connection.id != conn.id,
            Connection.config["account_id"].astext == account,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"AWS account {account} is already connected."
        )
    external_id = str(conn.config["external_id"])
    tier = str(conn.config.get("tier") or "observe")
    await session.commit()  # no transaction open during the STS calls
    try:
        identity = await asyncio.to_thread(get_sessions().verify, role_arn, external_id)
        deploy_role = (
            await asyncio.to_thread(_verify_deploy_role, identity.account, external_id)
            if tier == "deploy"
            else None
        )
    except AwsError as exc:
        conn = await member_connection(
            session, user, body.connection_id, for_update=True, owner=True
        )
        conn.config = {**conn.config, "last_error": str(exc)}
        await session.commit()
        raise _unprocessable(str(exc)) from None
    conn = await member_connection(session, user, body.connection_id, for_update=True, owner=True)
    conn.config = {
        **conn.config,
        "role_arn": role_arn,
        "account_id": identity.account,
        "session_arn": identity.arn,
        "tier": tier,
        "deploy_role_arn": deploy_role,
        "last_error": None,
    }
    conn.name = f"AWS {identity.account}"
    conn.status = "connected"
    await session.commit()
    return await _out(session, conn)


def _verify_deploy_role(account: str, external_id: str) -> str:
    """The groundcrew-deploy role, verified like the read-only one (ExternalId required)."""
    role_arn = deploy_role_arn(account)
    try:
        get_sessions().verify(role_arn, external_id)
    except AwsError as exc:
        raise AwsError(
            f"The deploy role {role_arn} could not be used: {exc} It exists only when the "
            "groundcrew-read-only stack has Tier=deploy: change the stack's Tier parameter "
            "to deploy (docs/phase4-infra.md), then try again."
        ) from None
    return role_arn


@router.post("/{connection_id}/aws/tier")
async def aws_set_tier(
    connection_id: uuid.UUID,
    body: AwsTierIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Switch a connected AWS account between observe and deploy after its stack was updated.
    deploy: verifies the groundcrew-deploy role first (nothing changes if it cannot be used).
    observe: Groundcrew stops using the deploy role at once; update the stack to Tier=observe
    to delete it."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "aws" or conn.status != "connected":
        raise _unprocessable("Only a connected AWS account has a tier.")
    account, external_id = str(conn.config["account_id"]), str(conn.config["external_id"])
    await session.commit()  # no transaction open during the STS calls
    deploy_role = None
    if body.tier == "deploy":
        try:
            deploy_role = await asyncio.to_thread(_verify_deploy_role, account, external_id)
        except AwsError as exc:
            raise _unprocessable(str(exc)) from None
    else:
        get_sessions().forget(deploy_role_arn(account), external_id)
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    conn.config = {**conn.config, "tier": body.tier, "deploy_role_arn": deploy_role}
    await _audit(session, user, conn, "tier_changed", ip)
    await session.commit()
    return await _out(session, conn)


def _operate_coverage(
    account: str, external_id: str, targets: list[Resource]
) -> tuple[list[str], list[str]]:
    """Which discovered services and functions the operate role can reach (read calls only)."""
    session = get_sessions().assume(
        operate_role_arn(account),
        external_id,
        operations=AWS_OPERATE_OPERATIONS,
        session_name=OPERATE_SESSION_NAME,
    )
    return aws_operate.unreachable(session, targets)


def _verify_operate_role(account: str, external_id: str) -> str:
    """The groundcrew-operate role, verified like the read-only one (ExternalId required)."""
    role_arn = operate_role_arn(account)
    try:
        get_sessions().verify(role_arn, external_id)
    except AwsError as exc:
        raise AwsError(
            f"The operate role {role_arn} could not be used: {exc} It exists only when the "
            "groundcrew-read-only stack has EnableOperate=true: update the stack with it and "
            "the services and secrets Groundcrew may act on, then try again."
        ) from None
    return role_arn


@router.put("/{connection_id}/aws/regions")
async def aws_set_regions(
    connection_id: uuid.UUID,
    body: AwsRegionsIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Choose the regions Groundcrew discovers, watches, and acts in for this AWS account (owner
    only). An empty list goes back to the platform default (DISCOVERY_REGIONS). The read-only
    and operate roles are global, so no stack update is needed; run a discovery afterwards to
    find the resources in a newly added region."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "aws":
        raise _unprocessable("Only an AWS connection has regions.")
    conn.config = {**conn.config, "regions": body.regions}
    await _audit(session, user, conn, "regions_changed", ip)
    await session.commit()
    return await _out(session, conn)


@router.post("/{connection_id}/aws/operate")
async def aws_set_operate(
    connection_id: uuid.UUID,
    body: AwsOperateIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Turn approved emergency fixes on or off for a connected AWS account. On: verifies the
    groundcrew-operate role first (nothing changes if it cannot be used). Off: Groundcrew stops
    using the role at once; update the stack to EnableOperate=false to delete it. Every fix
    still needs a person's approval."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "aws" or conn.status != "connected":
        raise _unprocessable("Only a connected AWS account can run emergency fixes.")
    account, external_id = str(conn.config["account_id"]), str(conn.config["external_id"])
    targets = aws_operate.probe_targets(await _aws_resources(session, conn))
    await session.commit()  # no transaction open during the STS calls
    role = None
    if body.enabled:
        try:
            role = await asyncio.to_thread(_verify_operate_role, account, external_id)
            reachable, refused = (
                await asyncio.to_thread(_operate_coverage, account, external_id, targets)
                if targets
                else ([], [])
            )
        except AwsError as exc:
            raise _unprocessable(str(exc)) from None
        if refused and not reachable:
            # The stack's lists name nothing Groundcrew found: usually the placeholders.
            raise _unprocessable(
                "The emergency-fix role works, but it may not act on any service Groundcrew "
                f"found ({', '.join(refused[:6])}). Update the groundcrew-read-only stack with "
                "the values shown in this dialog (OperateServiceArns, OperatePassRoleArns), "
                "then click Verify again."
            )
    else:
        get_sessions().forget(operate_role_arn(account), external_id)
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    conn.config = {**conn.config, "operate_role_arn": role}
    await _audit(session, user, conn, "operate_changed", ip)
    await session.commit()
    return await _out(session, conn)


async def _aws_resources(session: AsyncSession, conn: Connection) -> list[Resource]:
    return list(
        await session.scalars(
            select(Resource).where(
                Resource.connection_id == conn.id,
                Resource.type.in_(aws_operate.TYPES),
                Resource.stale.is_(False),
            )
        )
    )


@router.get("/{connection_id}/aws/operate-values")
async def aws_operate_values(
    connection_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> AwsOperateValuesOut:
    """The Operate* stack parameters for this account, from what discovery found: one
    wildcard per ECS cluster, the services' roles, Lambda functions, secrets, and instances.
    Groundcrew's own resources are left out."""
    conn = await member_connection(session, user, connection_id)
    if conn.kind != "aws":
        raise _unprocessable("Only an AWS connection has emergency-fix settings.")
    values = aws_operate.suggested_values(await _aws_resources(session, conn))
    return AwsOperateValuesOut(
        services=values.services,
        pass_roles=values.pass_roles,
        functions=values.functions,
        secrets=values.secrets,
        instances=values.instances,
        discovered=bool(conn.last_discovery),
    )


async def _k8s_workload_namespaces(session: AsyncSession, conn: Connection) -> list[str]:
    """The namespaces of the cluster's discovered Deployments and StatefulSets."""
    return sorted(
        {
            str(r.config.get("namespace") or r.region)
            for r in await session.scalars(
                select(Resource).where(
                    Resource.connection_id == conn.id,
                    Resource.type.in_(("k8s:deployment", "k8s:statefulset")),
                    Resource.stale.is_(False),
                )
            )
            if r.config.get("namespace") or r.region
        }
    )


async def _k8s_operate_namespaces(conn: Connection, namespaces: list[str]) -> list[str]:
    """The namespaces (of the cluster's discovered Deployments and StatefulSets) where the
    kubeconfig's identity may patch Deployments and list ReplicaSets, or patch StatefulSets and
    list ControllerRevisions, or [""] when it may everywhere. Each fix's plan checks again for
    its own kind (app.actions.k8s.OPERATE_RIGHTS)."""
    from app.security.crypto import decrypt

    operator = kube.live_operator(decrypt(conn.secret_encrypted or ""))
    try:

        async def allowed(ns: str | None) -> bool:
            for workloads, revisions in (
                ("deployments", "replicasets"),
                ("statefulsets", "controllerrevisions"),
            ):
                if await operator.can_i("patch", "apps", workloads, ns) and await operator.can_i(
                    "list", "apps", revisions, ns
                ):
                    return True
            return False

        if await allowed(None):
            return [""]
        return [ns for ns in namespaces if await allowed(ns)]
    finally:
        close = getattr(operator, "close", None)
        if close is not None:
            close()


@router.post("/{connection_id}/k8s/operate")
async def k8s_set_operate(
    connection_id: uuid.UUID,
    body: K8sOperateIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Turn approved emergency fixes on or off for a connected cluster. On: checks (with
    SelfSubjectAccessReviews) that the kubeconfig's identity may patch Deployments and list
    ReplicaSets (or patch StatefulSets and list ControllerRevisions), cluster-wide or in a
    namespace of a discovered Deployment or StatefulSet (infra/k8s-operate-rbac.yaml grants
    that); nothing changes if it may not. Every fix still needs a person's approval."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "k8s" or conn.status != "connected":
        raise _unprocessable("Only a connected Kubernetes cluster can run emergency fixes.")
    namespaces = await _k8s_workload_namespaces(session, conn)
    await session.commit()  # no transaction open during the cluster calls
    allowed: list[str] = []
    if body.enabled:
        try:
            allowed = await _k8s_operate_namespaces(conn, namespaces)
        except ConnectorError as exc:
            raise _unprocessable(str(exc)) from None
        if not allowed:
            raise _unprocessable(
                "This kubeconfig's identity may not patch Deployments or StatefulSets"
                + (f" in {', '.join(namespaces)}" if namespaces else "")
                + ". Apply infra/k8s-operate-rbac.yaml (or a Role with patch on deployments and "
                "list on replicasets, or patch on statefulsets and list on controllerrevisions, "
                "in your app's namespace), then try again."
            )
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    conn.config = {
        **conn.config,
        "operate": bool(body.enabled),
        "operate_namespaces": allowed if body.enabled else [],
    }
    await _audit(session, user, conn, "operate_changed", ip)
    await session.commit()
    return await _out(session, conn)


async def _k8s_may_list_configmaps(conn: Connection, namespaces: list[str]) -> bool:
    """Whether the kubeconfig's identity may list ConfigMaps, cluster-wide or in a namespace of
    a discovered Deployment or StatefulSet (a SelfSubjectAccessReview each)."""
    from app.security.crypto import decrypt

    operator = kube.live_operator(decrypt(conn.secret_encrypted or ""))
    try:
        for ns in [None, *namespaces]:
            if await operator.can_i("list", "", "configmaps", ns):
                return True
        return False
    finally:
        close = getattr(operator, "close", None)
        if close is not None:
            close()


@router.put("/{connection_id}/k8s/configmap-changes")
async def k8s_set_configmap_changes(
    connection_id: uuid.UUID,
    body: K8sConfigMapChangesIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Opt in to (or out of) ConfigMap changes as incident causes (owner only). Off by default:
    the `list configmaps` right Groundcrew needs for it would also let a client read ConfigMap
    contents, although Groundcrew only ever asks for metadata (names, resourceVersion, change
    times). On: checks the kubeconfig's identity may list ConfigMaps (the optional rule in
    infra/k8s-readonly-rbac.yaml); nothing changes if it may not. Off: incidents note that
    ConfigMap changes are not visible."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "k8s" or conn.status != "connected":
        raise _unprocessable("Only a connected Kubernetes cluster can detect ConfigMap changes.")
    namespaces = await _k8s_workload_namespaces(session, conn)
    await session.commit()  # no transaction open during the cluster calls
    if body.enabled:
        try:
            allowed = await _k8s_may_list_configmaps(conn, namespaces)
        except ConnectorError as exc:
            raise _unprocessable(str(exc)) from None
        if not allowed:
            raise _unprocessable(
                "This kubeconfig's identity may not list ConfigMaps"
                + (f" in {', '.join(namespaces)}" if namespaces else "")
                + ". Apply the optional ConfigMap rule in infra/k8s-readonly-rbac.yaml (or a "
                "Role with list on configmaps in your app's namespace), then try again."
            )
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    conn.config = {**conn.config, kube.CONFIGMAP_CHANGES_KEY: bool(body.enabled)}
    await _audit(session, user, conn, "configmap_changes_changed", ip)
    await session.commit()
    return await _out(session, conn)


def _allow_private(settings: Settings) -> bool:
    """Same rule as check_kubeconfig: private addresses only with K8S_ALLOW_PRIVATE_SERVERS
    (default on in APP_ENV=dev, for the local kind registry)."""
    if settings.k8s_allow_private_servers is not None:
        return settings.k8s_allow_private_servers
    return settings.app_env == "dev"


@router.put("/{connection_id}/k8s/deploy-settings")
async def k8s_set_deploy_settings(
    connection_id: uuid.UUID,
    body: K8sDeploySettingsIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Where this cluster's deploys go: the image registry (push credentials optional, the
    token stored encrypted and never returned), an optional imagePullSecret, the Ingress class,
    app domain, and TLS. A cluster without them deploys like the local kind cluster.
    registry_auth "ecr": an AWS connection of the same workspace with the deploy capability
    logs in to the ECR registry at push time (no stored token)."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "k8s" or conn.status != "connected":
        raise _unprocessable("Only a connected Kubernetes cluster has deploy settings.")
    try:
        aws_connection_id: str | None = None
        if body.registry_auth == "ecr":
            aws = (
                await session.get(Connection, body.aws_connection_id)
                if body.aws_connection_id
                else None
            )
            # The deploy role's ECR rights cover the region its stack was created in.
            regions = (
                [str(aws.config["region"])]
                if aws is not None and aws.config.get("region")
                else connection_regions(aws.config if aws else None, settings)
            )
            aws_connection_id = check_ecr_connection(body.registry, aws, conn.workspace_id, regions)
        new = build_settings(
            registry_auth=body.registry_auth,
            aws_connection_id=aws_connection_id,
            registry=body.registry,
            username=body.username,
            token=body.token,
            previous=deploy_settings(conn),
            pull_secret=body.pull_secret,
            ingress_class=body.ingress_class,
            app_domain=body.app_domain,
            tls=body.tls,
            tls_cluster_issuer=body.tls_cluster_issuer,
            public_url_base=body.public_url_base,
            allow_private=_allow_private(settings),
            is_private=kube.is_private_host,
            encrypt=_encrypt,
        )
    except DeploySettingsError as exc:
        raise _unprocessable(str(exc)) from None
    conn.config = {**conn.config, DEPLOY_SETTINGS_KEY: new.to_config()}
    await _audit(session, user, conn, "deploy_settings_changed", ip)
    await session.commit()
    return await _out(session, conn)


@router.delete("/{connection_id}/k8s/deploy-settings")
async def k8s_clear_deploy_settings(
    connection_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Forget the deploy settings (and the saved registry token): back to the local kind
    defaults."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "k8s":
        raise _unprocessable("Only a Kubernetes cluster has deploy settings.")
    conn.config = {k: v for k, v in conn.config.items() if k != DEPLOY_SETTINGS_KEY}
    await _audit(session, user, conn, "deploy_settings_cleared", ip)
    await session.commit()
    return await _out(session, conn)


# ---------------------------------------------------------------------------- GitHub


async def _github_installations(
    session: AsyncSession, workspace_id: uuid.UUID
) -> tuple[Workspace, dict[int, dict[str, Any]], set[str]]:
    """The workspace, the installations it may use, and the repositories it linked."""
    workspace = await session.get_one(Workspace, workspace_id)
    conns = list(
        await session.scalars(
            select(Connection).where(
                Connection.workspace_id == workspace_id, Connection.kind == "github"
            )
        )
    )
    linked = {str(r) for c in conns for r in c.config.get("repos") or []}
    return workspace, github_claims.claimed(workspace, conns), linked


async def _install_url(app: GitHubApp) -> str | None:
    try:
        slug = (await app.app_get("/app")).get("slug")
    except GitHubError:
        return None
    return f"https://github.com/apps/{slug}/installations/new" if slug else None


@router.get("/github/installations")
async def github_installations(
    user: CurrentUser, session: SessionDep, workspace_id: Annotated[uuid.UUID, Query()]
) -> GitHubInstallationsOut:
    """The installations of our GitHub App this workspace may use, and their repositories,
    marking the linked ones. Other accounts' installations are never listed."""
    await _check_member(session, user, workspace_id)
    app = get_github_app()
    if not app.configured():
        return GitHubInstallationsOut(configured=False, install_url=None, installations=[])
    _, allowed, linked = await _github_installations(session, workspace_id)
    await session.commit()
    install_url = await _install_url(app)
    if not allowed:
        return GitHubInstallationsOut(
            configured=True, install_url=install_url, installations=[], needs_authorization=True
        )
    ids = sorted(allowed)
    results = await asyncio.gather(
        *(app.installation_repos(i) for i in ids), return_exceptions=True
    )
    installations, errors = [], []
    for iid, repos in zip(ids, results, strict=True):
        if isinstance(repos, BaseException):
            errors.append(f"{allowed[iid].get('account') or iid}: {repos}")
            continue
        installations.append(
            GitHubInstallationOut(
                id=iid,
                account=str(allowed[iid].get("account") or iid),
                repos=[
                    GitHubRepoOut(
                        full_name=r["full_name"],
                        private=r["private"],
                        default_branch=r["default_branch"],
                        linked=r["full_name"] in linked,
                    )
                    for r in repos
                ],
            )
        )
    return GitHubInstallationsOut(
        configured=True,
        install_url=install_url,
        installations=installations,
        error="; ".join(errors) or None,
    )


@router.get("/github/authorize")
async def github_authorize(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    workspace_id: Annotated[uuid.UUID, Query()],
) -> GitHubAuthorizeOut:
    """Where an owner proves which installations of the app they can reach (GitHub OAuth)."""
    await _check_owner(session, user, workspace_id)
    app = get_github_app()
    try:
        url = github_claims.authorize_url(
            settings, github_claims.make_state(settings, user.id, workspace_id)
        )
    except GitHubError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    return GitHubAuthorizeOut(
        authorize_url=url, install_url=await _install_url(app) if app.configured() else None
    )


@router.post("/github/claim")
async def github_claim(
    body: GitHubClaimIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> GitHubClaimOut:
    """Finish the GitHub authorization: the installations of the app this person can reach on
    GitHub become usable in the workspace it was started for (owners). No GitHub token is
    kept."""
    try:
        workspace_id = github_claims.read_state(settings, body.state, user.id)
    except GitHubError as exc:
        raise _unprocessable(str(exc)) from None
    await _check_owner(session, user, workspace_id)
    await session.commit()  # no transaction open during the GitHub calls
    try:
        found = await github_claims.user_installations(settings, body.code)
    except GitHubError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    workspace = await session.get_one(Workspace, workspace_id, with_for_update=True)
    github_claims.add_claims(workspace, found, user.id)
    record(
        session,
        workspace_id=workspace_id,
        actor=user,
        action="github.installations_claimed",
        detail={"accounts": sorted(str(i["account"]) for i in found)},
        ip=ip,
    )
    await session.commit()
    return GitHubClaimOut(
        workspace_id=workspace_id,
        installations=[
            GitHubInstallationOut(id=i["id"], account=i["account"], repos=[]) for i in found
        ],
    )


@router.post("/github")
async def github_link(
    body: GitHubLinkIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
    settings: SettingsDep,
) -> ConnectionOut:
    """Link repositories of one installation to the workspace (replaces that connection's
    repo list). Only repositories the installation can see are accepted."""
    await _check_owner(session, user, body.workspace_id)
    try:
        wanted = sorted({check_repo_name(r) for r in body.repos})
    except GitHubError as exc:
        raise _unprocessable(str(exc)) from None
    app = get_github_app()
    _, allowed, _ = await _github_installations(session, body.workspace_id)
    await session.commit()
    if body.installation_id not in allowed:
        # Not one this workspace proved access to: say nothing about whether it exists.
        raise _unprocessable("That GitHub App installation was not found.")
    try:
        visible = {r["full_name"] for r in await app.installation_repos(body.installation_id)}
    except GitHubError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    missing = [r for r in wanted if r not in visible]
    if missing:
        raise _unprocessable(f"The installation cannot see: {', '.join(missing)}.")
    owner = str(allowed[body.installation_id].get("account") or body.installation_id)
    conn = await session.scalar(
        select(Connection).where(
            Connection.workspace_id == body.workspace_id,
            Connection.kind == "github",
            Connection.config["installation_id"].astext == str(body.installation_id),
        )
    )
    if conn is None:
        await quotas.check_can_add_connection(session, body.workspace_id, settings)
        conn = Connection(workspace_id=body.workspace_id, kind="github", name=f"GitHub {owner}")
        session.add(conn)
    conn.config = {"installation_id": body.installation_id, "account": owner, "repos": wanted}
    conn.status = "connected"
    await _audit(session, user, conn, "created", ip)
    await session.commit()
    return await _out(session, conn)


# ----------------------------------------------------------------------------- Agent


async def _join(session: AsyncSession, agent: Agent, settings: Settings) -> tuple[str, datetime]:
    token, hashed = new_join_token()
    agent.join_token_hash = hashed
    agent.join_expires_at = datetime.now(UTC) + timedelta(minutes=settings.agent_join_token_minutes)
    await session.flush()
    return token, agent.join_expires_at


@router.post("/agent")
async def agent_connect(
    body: AgentConnectIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> AgentJoinOut:
    """Add a Groundcrew agent: a connection that waits for the agent to enroll with the one-time
    join token returned here (shown once; only its hash is kept)."""
    await _check_owner(session, user, body.workspace_id)
    await quotas.check_can_add_connection(session, body.workspace_id, settings)
    conn = Connection(
        workspace_id=body.workspace_id, kind="agent", name=body.name, status="pending", config={}
    )
    session.add(conn)
    await session.flush()
    agent = Agent(workspace_id=body.workspace_id, connection_id=conn.id)
    session.add(agent)
    token, expires = await _join(session, agent, settings)
    await _audit(session, user, conn, "created", ip)
    await session.commit()
    return AgentJoinOut(
        connection=await _out(session, conn),
        token=token,
        expires_at=expires,
        api_url=settings.agent_api_url,
    )


@router.post("/{connection_id}/agent/token")
async def agent_new_token(
    connection_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> AgentJoinOut:
    """A new join token (the old one stops working): to finish a setup whose token expired, or
    to reinstall the agent, whose new key then replaces the old one."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    agent = await session.scalar(select(Agent).where(Agent.connection_id == conn.id))
    if conn.kind != "agent" or agent is None:
        raise _unprocessable("Only an agent connection has join tokens.")
    token, expires = await _join(session, agent, settings)
    await _audit(session, user, conn, "agent_token_created", ip)
    await session.commit()
    return AgentJoinOut(
        connection=await _out(session, conn),
        token=token,
        expires_at=expires,
        api_url=settings.agent_api_url,
    )


@router.put("/{connection_id}/agent/registry")
async def agent_set_registry(
    connection_id: uuid.UUID,
    body: AgentRegistryIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Deploys to this agent keep their images in this registry instead of the Groundcrew
    registry: a username and token (stored encrypted, never returned; the agent gets them only
    while it runs a deploy), or Amazon ECR through an AWS connection with the deploy
    capability (a fresh login per deploy)."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "agent":
        raise _unprocessable("Only an agent connection has this registry setting.")
    try:
        aws_connection_id: str | None = None
        if body.registry_auth == "ecr":
            aws = (
                await session.get(Connection, body.aws_connection_id)
                if body.aws_connection_id
                else None
            )
            regions = (
                [str(aws.config["region"])]
                if aws is not None and aws.config.get("region")
                else connection_regions(aws.config if aws else None, settings)
            )
            aws_connection_id = check_ecr_connection(body.registry, aws, conn.workspace_id, regions)
        new = build_settings(
            registry_auth=body.registry_auth,
            aws_connection_id=aws_connection_id,
            registry=body.registry,
            username=body.username,
            token=body.token,
            previous=K8sDeploySettings.from_config(conn.config),
            pull_secret=False,
            ingress_class="nginx",
            app_domain=None,
            tls=False,
            tls_cluster_issuer=None,
            public_url_base=None,
            allow_private=_allow_private(settings),
            is_private=kube.is_private_host,
            encrypt=_encrypt,
        )
    except DeploySettingsError as exc:
        raise _unprocessable(str(exc)) from None
    conn.config = {**conn.config, DEPLOY_SETTINGS_KEY: new.to_config()}
    await _audit(session, user, conn, "agent_registry_changed", ip)
    await session.commit()
    return await _out(session, conn)


@router.delete("/{connection_id}/agent/registry")
async def agent_clear_registry(
    connection_id: uuid.UUID, user: CurrentUser, session: SessionDep, ip: ClientIp
) -> ConnectionOut:
    """Back to the Groundcrew registry (the saved token is deleted)."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    if conn.kind != "agent":
        raise _unprocessable("Only an agent connection has this registry setting.")
    conn.config = {k: v for k, v in conn.config.items() if k != DEPLOY_SETTINGS_KEY}
    await _audit(session, user, conn, "agent_registry_cleared", ip)
    await session.commit()
    return await _out(session, conn)


# ---------------------------------------------------------------------------- Vercel


@router.post("/vercel")
async def vercel_connect(
    body: VercelConnectIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> ConnectionOut:
    """Check the token (team and projects are readable), then store it encrypted."""
    await _check_owner(session, user, body.workspace_id)
    await quotas.check_can_add_connection(session, body.workspace_id, settings)
    token = body.token.strip()
    team_id = (body.team_id or "").strip() or None
    await session.commit()
    client = VercelClient(token, team_id, settings)
    try:
        who = await client.whoami()
        projects = await client.projects(limit=100)
    except VercelError as exc:
        raise _unprocessable(_vercel_problem(exc, team_id)) from None
    conn = Connection(
        workspace_id=body.workspace_id,
        kind="vercel",
        name=f"Vercel {who.get('name') or who.get('slug')}",
        status="connected",
        config={
            "team_id": team_id,
            "team_slug": who.get("slug"),
            "scope": who.get("kind"),
            "projects_seen": len(projects),
        },
        secret_encrypted=_encrypt(token),
    )
    session.add(conn)
    await _audit(session, user, conn, "created", ip)
    await session.commit()
    return await _out(session, conn)


def _vercel_problem(exc: VercelError, team_id: str | None) -> str:
    """What to do about a token Vercel refused, in one sentence."""
    if exc.status == 401:
        return (
            "Vercel did not accept this token. Create a new one at vercel.com/account/tokens "
            "and paste it again."
        )
    if exc.status in (403, 404) and team_id:
        return (
            f"This token cannot read team {team_id}. Check the Team ID (Vercel, Settings, "
            "General; it starts with team_) and create the token with that team as its scope."
        )
    if exc.status == 403:
        return (
            "This token cannot read your projects. Create a token with your team as its scope "
            "and add the Team ID."
        )
    return f"Could not reach Vercel with this token: {exc}"


# ------------------------------------------------------------------- Azure / Kubernetes


@router.post("/azure")
async def azure_connect(
    body: AzureConnectIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
    settings: SettingsDep,
) -> ConnectionOut:
    """Preview: stores the service principal (secret encrypted). Live discovery is not built
    yet (TODO(azure-live)); the connection shows as Preview."""
    await _check_owner(session, user, body.workspace_id)
    await quotas.check_can_add_connection(session, body.workspace_id, settings)
    conn = Connection(
        workspace_id=body.workspace_id,
        kind="azure",
        name=f"Azure {body.resource_group}",
        status="pending",
        config={
            "tenant_id": body.tenant_id,
            "client_id": body.client_id,
            "subscription_id": body.subscription_id,
            "resource_group": body.resource_group,
        },
        secret_encrypted=_encrypt(body.client_secret),
    )
    session.add(conn)
    await _audit(session, user, conn, "created", ip)
    await session.commit()
    return await _out(session, conn)


@router.post("/k8s")
async def k8s_connect(
    body: KubernetesConnectIn,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
    settings: SettingsDep,
) -> ConnectionOut:
    """Verifies the kubeconfig by listing namespaces with it, then stores it encrypted and
    keeps the cluster name, server, and namespace count as config. Nothing is stored when the
    cluster cannot be read."""
    await _check_owner(session, user, body.workspace_id)
    await quotas.check_can_add_connection(session, body.workspace_id, settings)
    try:
        kube.check_kubeconfig(body.kubeconfig)
        summary = kubeconfig_summary(body.kubeconfig)
        reader = kube.live_reader(body.kubeconfig)
        try:
            namespaces = await reader.verify()
        finally:
            reader.close()
    except ConnectorError as exc:
        raise _unprocessable(str(exc)) from None
    conn = Connection(
        workspace_id=body.workspace_id,
        kind="k8s",
        name=body.name or f"Kubernetes {summary['cluster']}",
        status="connected",
        config={
            **summary,
            "namespaces": len(namespaces),
            "verified_at": datetime.now(UTC).isoformat(),
        },
        secret_encrypted=_encrypt(body.kubeconfig),
    )
    session.add(conn)
    await _audit(session, user, conn, "created", ip)
    await session.commit()
    return await _out(session, conn)


# --------------------------------------------------------------------- refresh, remove


@router.post("/{connection_id}/discover", status_code=status.HTTP_202_ACCEPTED)
async def discover_connection(
    connection_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> RunCreated:
    conn = await member_connection(session, user, connection_id, minimum=WorkspaceRole.approver)
    await quotas.check_can_start_run(session, conn.workspace_id, settings)
    if conn.status != "connected":
        raise HTTPException(status.HTTP_409_CONFLICT, "Finish connecting before running discovery.")
    run = await start_discovery(session, conn.workspace_id, connection=conn)
    await session.commit()
    return RunCreated(id=run.id, status=RunStatus.queued)


def cleanup_steps(conn: Connection) -> list[str]:
    """What the customer removes on their side to end Groundcrew's access."""
    c = conn.config or {}
    region = c.get("region") or get_settings().discovery_regions[0]
    if conn.kind == "aws":
        return [
            f"Delete the CloudFormation stack {STACK_NAME} in {region} (account "
            f"{c.get('account_id') or 'you connected'}): CloudFormation console, select the "
            "stack, Delete. This removes the groundcrew-read-only role and the Resource Explorer "
            "index and view it created.",
            f"Or run: aws cloudformation delete-stack --region {region} --stack-name {STACK_NAME}",
        ]
    if conn.kind == "github":
        return [
            f"To stop access completely, remove the repositories from the Groundcrew GitHub App "
            f"(GitHub, {c.get('account') or 'your organization'} settings, GitHub Apps, "
            "Configure), or uninstall it.",
        ]
    if conn.kind == "vercel":
        return ["Revoke the token you pasted at https://vercel.com/account/tokens."]
    if conn.kind == "azure":
        return [
            "Remove the Reader role assignment on the resource group, then delete the service "
            "principal's client secret (Microsoft Entra ID, App registrations).",
        ]
    return [
        "Delete the groundcrew-reader service account and its ClusterRoleBinding: "
        "kubectl delete -f infra/k8s-readonly-rbac.yaml",
    ]


@router.delete("/{connection_id}")
async def delete_connection(
    connection_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    ip: ClientIp,
) -> ConnectionDeleted:
    """Remove the connection, its stored secret, every resource it discovered (with their
    edges), and the telemetry read for them (app.deletion). Access on the customer's side is
    removed by them: see `cleanup`."""
    conn = await member_connection(session, user, connection_id, for_update=True, owner=True)
    steps = cleanup_steps(conn)
    removed = await deletion.delete_connection(session, conn)
    await _audit(session, user, conn, "deleted", ip)
    await session.commit()
    return ConnectionDeleted(id=connection_id, removed_resources=removed, cleanup=steps)
