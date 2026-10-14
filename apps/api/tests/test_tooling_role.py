"""Marking your own tooling: the AWS tag groundcrew:role (or the Kubernetes label groundcrew/role)
set to "tooling" or "app" decides a resource's role before the name rules (app.graph.roles).
Tooling folds into the Map's Tooling group, stays out of the system summary, and is not read
as a neighbor by Medic unless the incident names it.

The case: checkout-demo's checkout-vercel-api-url Lambda (with its role, log group, rule, and
the demo-tooling/vercel-token secret) is demo tooling, tagged groundcrew:role=tooling.
"""

import copy
import uuid
from typing import Any

import pytest

from app.connectors.aws import AwsReader
from app.db.models import Connection, Edge, Resource, Workspace
from app.db.session import SessionFactory
from app.discovery.aws import AwsDiscovery
from app.discovery.types import Confidence, EdgeKind
from app.graph.digest import digest
from app.graph.edges import InferredEdge
from app.graph.roles import linked_to_app, marked_role, role, role_of
from app.graph.summary_check import check_summary
from app.routes.discovery_schemas import ResourceOut
from app.telemetry.collect import load_resources
from tests.aws_fixture import ACCOUNT, FIXTURE, REGION, _role, fixture_session
from tests.discovery_helpers import settings
from tests.test_edges import res

pytestmark = pytest.mark.anyio

TOOLING = {"groundcrew:role": "tooling"}
LAMBDA = "checkout-vercel-api-url"
FN_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{LAMBDA}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{LAMBDA}"


# --- the role ---------------------------------------------------------------------------------


def test_the_tag_or_label_decides_before_the_name_rules() -> None:
    # AWS tag, any case and spacing.
    assert role_of("lambda:function", LAMBDA, TOOLING) == "tooling"
    assert role_of("lambda:function", LAMBDA, {"groundcrew:role": " Tooling "}) == "tooling"
    # Kubernetes label.
    assert role_of("k8s:deployment", "argocd-server", {"groundcrew/role": "tooling"}) == "tooling"
    # The app override: a name the fallback calls tooling stays in the app.
    assert role_of("s3:bucket", "acme-tfstate-uploads", {"groundcrew:role": "app"}) == "app"
    assert role_of("iam:role", "groundcrew-read-only", {"groundcrew/role": "app"}) == "app"
    # Unknown values are ignored: the name rules decide.
    assert marked_role({"groundcrew:role": "infra"}) is None
    assert role_of("s3:bucket", "acme-tfstate", {"groundcrew:role": "infra"}) == "tooling"


def test_without_a_marker_the_name_rules_still_apply() -> None:
    assert role_of("s3:bucket", "groundcrew-checkout-tfstate-881050425883") == "tooling"
    assert role_of("dynamodb:table", "terraform-locks") == "tooling"
    assert role_of("iam:role", "groundcrew-read-only", {}) == "tooling"
    assert role_of("resource-explorer-2:index", "default") == "tooling"
    assert role_of("lambda:function", LAMBDA) == "app"
    assert role_of("ecs:service", "checkout-api", {"project": "groundcrew"}) == "app"


def test_the_map_uses_the_tags() -> None:
    def out(name: str, tags: dict[str, str]) -> str:
        r = Resource(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            provider="aws",
            type="lambda:function",
            external_id=f"arn:{name}",
            name=name,
            tags=tags,
            config={},
            coverage="observed",
            stale=False,
        )
        return ResourceOut.build(r).role

    assert out(LAMBDA, TOOLING) == "tooling"  # folded into the collapsed Tooling group
    assert out(LAMBDA, {}) == "app"


# --- the summary --------------------------------------------------------------------------------


def _tooling_lambda() -> tuple[list[Any], list[InferredEdge]]:
    """checkout-api plus the Lambda, its role, log group, rule and secret, linked only among
    themselves (as discovery and edge inference find them)."""
    api = res("ecs:service", "checkout-api")
    fn = res("lambda:function", LAMBDA, tags=TOOLING)
    fn_role = res("iam:role", LAMBDA, tags=TOOLING)
    group = res("logs:log-group", f"/aws/lambda/{LAMBDA}", tags=TOOLING)
    rule = res("events:rule", "checkout-api-task-running", tags=TOOLING)
    secret = res("secretsmanager:secret", "demo-tooling/vercel-token", tags=TOOLING)

    def edge(a: Any, b: Any, kind: EdgeKind) -> InferredEdge:
        return InferredEdge(
            source=f"{a.provider}|{a.external_id}",
            target=f"{b.provider}|{b.external_id}",
            kind=kind,
            confidence=Confidence.high,
            rule="test",
            evidence="test",
        )

    edges = [
        edge(fn, secret, EdgeKind.uses),
        edge(fn, group, EdgeKind.logs_to),
        edge(rule, fn, EdgeKind.calls),
    ]
    return [api, fn, fn_role, group, rule, secret], edges


def test_tooling_linked_only_to_tooling_is_left_out_of_the_summary() -> None:
    resources, edges = _tooling_lambda()
    names = {i.name for i in digest(resources, edges)}
    assert names == {"checkout-api"}
    # An edge to an app resource keeps it: the app uses it.
    api, fn = resources[0], resources[1]
    keys = {f"{r.provider}|{r.external_id}": role(r) for r in resources}
    pair = (f"{fn.provider}|{fn.external_id}", f"{api.provider}|{api.external_id}")
    assert pair[0] in linked_to_app(keys, [pair])
    used = [
        *edges,
        InferredEdge(
            source=pair[1],
            target=pair[0],
            kind=EdgeKind.calls,
            confidence=Confidence.high,
            rule="test",
            evidence="test",
        ),
    ]
    assert LAMBDA in {i.name for i in digest(resources, used)}
    # The app override puts it back.
    resources[1] = res("lambda:function", LAMBDA, tags={"groundcrew:role": "app"})
    assert LAMBDA in {i.name for i in digest(resources, edges)}


def test_the_summary_check_drops_a_claim_about_tagged_tooling() -> None:
    resources, edges = _tooling_lambda()
    text = (
        f"- checkout-api runs on ECS.\n- checkout-api updates Vercel through the {LAMBDA} Lambda."
    )
    checked = check_summary(text, resources, edges)
    assert len(checked.dropped) == 1, checked.dropped
    assert "is tooling" in next(iter(dict(checked.dropped).values()))


# --- Medic's evidence --------------------------------------------------------------------------


async def test_medic_skips_tooling_neighbors_unless_the_incident_names_them(
    session_factory: SessionFactory, workspace: Workspace
) -> None:
    async with session_factory() as session:
        conn = Connection(workspace_id=workspace.id, kind="aws", name="demo", status="connected")
        session.add(conn)
        await session.flush()

        def r(rtype: str, name: str, tags: dict[str, str] | None = None) -> Resource:
            row = Resource(
                workspace_id=workspace.id,
                connection_id=conn.id,
                provider="aws",
                type=rtype,
                name=name,
                external_id=f"arn:{rtype}:{name}",
                tags=tags or {},
            )
            session.add(row)
            return row

        api = r("ecs:service", "checkout-api")
        logs = r("logs:log-group", "/ecs/checkout-api")
        fn = r("lambda:function", LAMBDA, TOOLING)
        await session.flush()
        for to, kind in ((logs, "logs_to"), (fn, "calls")):
            session.add(
                Edge(
                    workspace_id=workspace.id,
                    from_id=api.id,
                    to_id=to.id,
                    kind=kind,
                    confidence="high",
                    evidence={},
                )
            )
        await session.commit()
        ids = {"api": api.id, "fn": fn.id}
    _, neighbors, _ = await load_resources(session_factory, workspace.id, [ids["api"]])
    assert [n.name for n in neighbors] == ["/ecs/checkout-api"]
    affected, _, _ = await load_resources(session_factory, workspace.id, [ids["fn"]])
    assert [a.name for a in affected] == [LAMBDA]  # named by the incident: still read


# --- discovery reads the tags ---------------------------------------------------------------------


async def test_discovery_reads_lambda_and_iam_role_tags() -> None:
    table = copy.deepcopy(FIXTURE)
    table["lambda:ListFunctions"] = {
        "Functions": [
            {
                "FunctionName": LAMBDA,
                "FunctionArn": FN_ARN,
                "Runtime": "python3.12",
                "Role": ROLE_ARN,
            }
        ]
    }
    table["lambda:ListTags"] = lambda p: {"Tags": TOOLING} if p["Resource"] == FN_ARN else {}
    listed = _role(
        LAMBDA,
        {"Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}}]},
    )
    roles = {r["RoleName"]: r for r in [*table["iam:ListRoles"]["Roles"], listed]}
    table["iam:ListRoles"] = {"Roles": list(roles.values())}
    # GetRole has the tags ListRoles leaves out.
    table["iam:GetRole"] = lambda p: {
        "Role": {
            **roles[p["RoleName"]],
            "Tags": [{"Key": "groundcrew:role", "Value": "tooling"}]
            if p["RoleName"] == LAMBDA
            else [],
        }
    }
    policies = table["iam:ListRolePolicies"]
    table["iam:ListRolePolicies"] = lambda p: (
        {"PolicyNames": []} if p["RoleName"] == LAMBDA else policies(p)
    )
    calls: list[str] = []
    reader = AwsReader(fixture_session(table, calls), settings())
    result = await AwsDiscovery(reader, ACCOUNT, max_resources=2000).run()
    by_name = {(r.type, r.name): r for r in result.resources}
    assert not [n for n in result.notes if "skipped" in n], result.notes
    fn = by_name[("lambda:function", LAMBDA)]
    fn_role = by_name[("iam:role", LAMBDA)]
    assert fn.tags == TOOLING and role(fn) == "tooling"
    assert fn_role.tags.get("groundcrew:role") == "tooling" and role(fn_role) == "tooling"
    assert "lambda:ListTags" in calls and "iam:GetRole" in calls
