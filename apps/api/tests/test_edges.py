"""Relationship inference: every rule on small inputs, then the whole demo, coverage, risks,
and Scout's system summary (FakeLLM)."""

from typing import Any

import pytest

from app.agents.scout.summary import summarize_system
from app.config import Settings
from app.discovery.types import (
    DiscoveredEdge,
    DiscoveredResource,
    RepoFacts,
    TerraformResource,
    Workflow,
    WorkflowJob,
)
from app.graph import coverage as coverage_module
from app.graph.coverage import coverage, register_actionable
from app.graph.digest import TOOLING_NOTE, digest, find_risks, risks
from app.graph.edges import Confidence, EdgeKind, InferredEdge, infer_edges, key
from app.llm.budget import MemoryUsageStore, RunBudget
from app.llm.router import ROUTES, Task
from tests.discovery_helpers import demo_inventory, plain_api_url_routes
from tests.fakes import FakeLLM, reply

pytestmark = pytest.mark.anyio

REPO = "acme/shop"


def res(rtype: str, name: str, provider: str = "aws", **config: Any) -> DiscoveredResource:
    tags = config.pop("tags", {})
    repo = config.pop("repo", None)
    return DiscoveredResource(
        provider=provider,
        type=rtype,
        external_id=config.pop("external_id", f"arn:{rtype}:{name}"),
        name=name,
        region="us-east-2" if provider == "aws" else None,
        tags=tags,
        repo=repo,
        config=config,
    )


def svc(name: str = "api", **config: Any) -> DiscoveredResource:
    base: dict[str, Any] = {"network": {"security_groups": ["sg-tasks"]}, "containers": []}
    base.update(config)
    return res("ecs:service", name, **base)


def edges_of(
    resources: list[DiscoveredResource], repos: list[RepoFacts] | None = None
) -> dict[tuple[str, str, str], InferredEdge]:
    names = {key(r): r.name for r in resources}
    return {
        (names[e.source], e.kind.value, names[e.target]): e
        for e in infer_edges(resources, repos or []).edges
    }


def gh_repo() -> DiscoveredResource:
    return res("github:repository", REPO, provider="github", external_id=f"github:repo:{REPO}")


def gh_workflow(path: str = ".github/workflows/deploy.yml") -> DiscoveredResource:
    return res(
        "github:workflow",
        "deploy",
        provider="github",
        external_id=f"github:workflow:{REPO}:{path}",
        repo=REPO,
    )


# --- 1. security groups ---------------------------------------------------------------------


def test_rule1_sg_ingress_on_db_port_means_reads() -> None:
    db = res("rds:db", "orders", endpoint={"port": 5432}, security_groups=["sg-db"])
    sg_db = res(
        "ec2:security-group",
        "db-sg",
        group_id="sg-db",
        ingress=[
            {
                "protocol": "tcp",
                "from_port": 5432,
                "to_port": 5432,
                "cidrs": [],
                "source_groups": ["sg-tasks"],
            }
        ],
    )
    sg_tasks = res("ec2:security-group", "tasks", group_id="sg-tasks", ingress=[])
    edge = edges_of([svc(), db, sg_db, sg_tasks])[("api", "reads", "orders")]
    assert edge.confidence == Confidence.high and edge.rule == "sg_db_ingress"
    assert "allows port 5432 from tasks" in edge.evidence


def test_rule1_wrong_port_or_group_is_no_edge() -> None:
    db = res("rds:db", "orders", endpoint={"port": 5432}, security_groups=["sg-db"])
    sg_db = res(
        "ec2:security-group",
        "db-sg",
        group_id="sg-db",
        ingress=[
            {
                "protocol": "tcp",
                "from_port": 3306,
                "to_port": 3306,
                "cidrs": [],
                "source_groups": ["sg-tasks"],
            },
            {
                "protocol": "tcp",
                "from_port": 5432,
                "to_port": 5432,
                "cidrs": [],
                "source_groups": ["sg-other"],
            },
        ],
    )
    assert not any(k[1] == "reads" for k in edges_of([svc(), db, sg_db]))


# --- 2. secrets ---------------------------------------------------------------------------


def test_rule2_task_secret_and_secret_for_db_by_name() -> None:
    secret = res(
        "secretsmanager:secret",
        "shop/database-url",
        external_id="arn:aws:secretsmanager:r:1:secret:shop/database-url-Ab12",
    )
    db = res("rds:db", "shop-orders-db")
    api = svc(
        task_family="api",
        task_revision=3,
        containers=[
            {
                "name": "api",
                "secrets": [{"name": "DATABASE_URL", "value_from": secret.external_id + ":url::"}],
            }
        ],
    )
    edges = edges_of([api, secret, db])
    uses = edges[("api", "uses", "shop/database-url")]
    assert uses.confidence == Confidence.high and "value not read" in uses.evidence
    # Without a network rule, using the database's secret means reading it (medium); which
    # database the secret is for is evidence on that edge, not an edge of its own.
    reads = edges[("api", "reads", "shop-orders-db")]
    assert reads.confidence == Confidence.medium and reads.rule == "secret_db"
    assert reads.details["secret_name"] == "shop/database-url"  # noqa: S105 - a name
    assert "names only" in reads.evidence
    assert not any(k[0] == "shop/database-url" for k in edges)


def test_rule2_secret_for_db_from_terraform_is_high() -> None:
    secret = res("secretsmanager:secret", "app/creds")  # name says nothing about a database
    db = res("rds:db", "main")
    other_db = res("rds:db", "reporting")
    facts = RepoFacts(
        repo=REPO,
        terraform=[
            TerraformResource(
                address="aws_secretsmanager_secret.creds",
                type="aws_secretsmanager_secret",
                path="s.tf",
                attributes={"name": "app/creds"},
            ),
            TerraformResource(
                address="aws_db_instance.main",
                type="aws_db_instance",
                path="db.tf",
                attributes={"identifier": "main"},
            ),
            TerraformResource(
                address="aws_secretsmanager_secret_version.creds",
                type="aws_secretsmanager_secret_version",
                path="s.tf",
                references=["aws_secretsmanager_secret.creds", "aws_db_instance.main"],
            ),
        ],
    )
    api = svc(containers=[{"name": "api", "secrets": [{"name": "DB", "value_from": "app/creds"}]}])
    edges = edges_of([api, secret, db, other_db, gh_repo()], [facts])
    reads = edges[("api", "reads", "main")]
    assert "Terraform aws_secretsmanager_secret_version.creds" in reads.details["secret_db"]
    assert ("api", "reads", "reporting") not in edges


# --- 3. images ---------------------------------------------------------------------------


def test_rule3_image_to_ecr_and_commit() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    ecr = res("ecr:repository", "shop-api", uri="1.dkr.ecr.us-east-2.amazonaws.com/shop-api")
    api = svc(
        containers=[
            {
                "name": "api",
                "image_registry": "1.dkr.ecr.us-east-2.amazonaws.com",
                "image_repository": "shop-api",
                "image_tag": sha,
            }
        ]
    )
    facts = RepoFacts(
        repo=REPO,
        html_url=f"https://github.com/{REPO}",
        commits=[{"sha": sha, "message": "Fix checkout", "at": "2026-10-08"}],
    )
    edges = edges_of([api, ecr, gh_repo()], [facts])
    assert edges[("api", "pulls_from", "shop-api")].confidence == Confidence.high
    built = edges[("api", "defined_in", REPO)]
    assert built.rule == "image_commit"
    assert built.details["commit_url"] == f"https://github.com/{REPO}/commit/{sha}"
    assert "Fix checkout" in built.evidence


def test_rule3_non_sha_tag_links_no_commit() -> None:
    api = svc(
        containers=[
            {"name": "api", "image_registry": "r", "image_repository": "x", "image_tag": "latest"}
        ]
    )
    assert not any(k[1] == "defined_in" for k in edges_of([api, gh_repo()], [RepoFacts(repo=REPO)]))


# --- 4. logs ---------------------------------------------------------------------------


def test_rule4_awslogs_group() -> None:
    group = res("logs:log-group", "/ecs/api")
    api = svc(
        containers=[
            {
                "name": "api",
                "log": {
                    "driver": "awslogs",
                    "group": "/ecs/api",
                    "region": "us-east-2",
                    "stream_prefix": "api",
                },
            }
        ]
    )
    fn = res("lambda:function", "resize", log_group="/aws/lambda/resize")
    fn_group = res("logs:log-group", "/aws/lambda/resize")
    edges = edges_of([api, group, fn, fn_group])
    assert edges[("api", "logs_to", "/ecs/api")].confidence == Confidence.high
    assert ("resize", "logs_to", "/aws/lambda/resize") in edges


# --- 5. Terraform -----------------------------------------------------------------------


def test_rule5_terraform_defines_real_resources() -> None:
    api = svc("shop-api")
    facts = RepoFacts(
        repo=REPO,
        terraform=[
            TerraformResource(
                address="aws_ecs_service.api",
                type="aws_ecs_service",
                path="infra/ecs.tf",
                line=12,
                attributes={"name": "shop-api"},
            ),
            TerraformResource(
                address="aws_ecs_service.gone",
                type="aws_ecs_service",
                path="infra/ecs.tf",
                attributes={"name": "not-deployed"},
            ),
        ],
    )
    result = infer_edges([api, gh_repo()], [facts])
    (edge,) = [e for e in result.edges if e.kind == EdgeKind.defined_in]
    assert edge.confidence == Confidence.high and "infra/ecs.tf#L12" in edge.evidence
    assert result.iac[key(api)].path == "infra/ecs.tf#L12"


# --- 6. deploys ------------------------------------------------------------------------


def _deploy_facts(**job: Any) -> RepoFacts:
    defaults: dict[str, Any] = {
        "id": "deploy",
        "role_to_assume": ["${{ vars.AWS_ROLE }}"],
        "ecs_deploy": True,
    }
    return RepoFacts(
        repo=REPO,
        workflows=[
            Workflow(
                path=".github/workflows/deploy.yml",
                name="deploy",
                jobs=[WorkflowJob(**{**defaults, **job})],
            )
        ],
    )


def test_rule6_oidc_role_scope_proves_deployed_by() -> None:
    api, worker, other = svc("shop-api"), svc("shop-worker"), svc("unrelated")
    role = res(
        "iam:role",
        "shop-deploy",
        github_repos=[REPO],
        github_oidc_subjects=[f"repo:{REPO}:ref:refs/heads/main"],
        inline_policies={
            "deploy": [
                {
                    "effect": "Allow",
                    "actions": ["ecs:UpdateService"],
                    "resources": [api.external_id, worker.external_id],
                }
            ]
        },
    )
    edges = edges_of([api, worker, other, role, gh_workflow(), gh_repo()], [_deploy_facts()])
    for name in ("shop-api", "shop-worker"):
        edge = edges[(name, "deployed_by", "deploy")]
        assert edge.confidence == Confidence.high
        assert "role shop-deploy, which only repo:acme/shop:ref:refs/heads/main" in edge.evidence
    assert ("unrelated", "deployed_by", "deploy") not in edges
    assert not any(k[2] == "shop-deploy" for k in edges)  # the role is evidence, not a node


def test_rule6_service_name_hint_without_role_is_medium() -> None:
    api = svc("shop-api")
    facts = _deploy_facts(role_to_assume=[], ecs_services=["${{ vars.ECS_API_SERVICE }}"])
    edge = edges_of([api, gh_workflow(), gh_repo()], [facts])[("shop-api", "deployed_by", "deploy")]
    assert edge.confidence == Confidence.medium and "name hint 'api'" in edge.evidence


def test_rule6_vercel_job_deploys_linked_project() -> None:
    project = res("vercel:project", "storefront", provider="vercel", project_id="prj_1", repo=REPO)
    facts = _deploy_facts(role_to_assume=[], ecs_deploy=False, vercel=True)
    edge = edges_of([project, gh_workflow(), gh_repo()], [facts])[
        ("storefront", "deployed_by", "deploy")
    ]
    assert edge.confidence == Confidence.high and "linked to acme/shop" in edge.evidence


def test_rule6_vercel_single_unlinked_project_is_medium() -> None:
    project = res("vercel:project", "storefront", provider="vercel", project_id="prj_1")
    facts = _deploy_facts(role_to_assume=[], ecs_deploy=False, vercel=True)
    edge = edges_of([project, gh_workflow(), gh_repo()], [facts])[
        ("storefront", "deployed_by", "deploy")
    ]
    assert edge.confidence == Confidence.medium


# --- 7. Vercel env URLs ----------------------------------------------------------------


def test_rule7_plain_url_matching_task_ip_is_high() -> None:
    api = svc("shop-api", running_tasks=[{"public_ip": "3.3.3.3"}])
    project = res(
        "vercel:project",
        "web",
        provider="vercel",
        env_values={"API_URL": "http://3.3.3.3:8000"},
        env=[{"key": "API_URL", "type": "plain"}],
    )
    edge = edges_of([api, project])[("web", "calls", "shop-api")]
    assert (
        edge.confidence == Confidence.high
        and "public IP of a running shop-api task" in edge.evidence
    )


def test_rule7_plain_url_matching_rds_endpoint() -> None:
    db = res("rds:db", "orders", endpoint={"address": "orders.abc.rds.amazonaws.com", "port": 5432})
    project = res(
        "vercel:project",
        "web",
        provider="vercel",
        env_values={"DB_HOST": "orders.abc.rds.amazonaws.com"},
    )
    assert ("web", "calls", "orders") in edges_of([db, project])


def test_rule7_name_and_shared_repo_is_medium() -> None:
    api = svc("shop-api", containers=[{"name": "api", "ports": [{"port": 8000}]}])
    worker = svc("shop-worker", containers=[{"name": "w", "ports": []}])
    project = res(
        "vercel:project",
        "web",
        provider="vercel",
        repo=REPO,
        env=[{"key": "API_URL", "type": "encrypted"}],
    )
    role = res(
        "iam:role",
        "deploy",
        github_repos=[REPO],
        inline_policies={
            "p": [
                {
                    "effect": "Allow",
                    "actions": ["ecs:UpdateService"],
                    "resources": [api.external_id, worker.external_id],
                }
            ]
        },
    )
    facts = _deploy_facts()
    facts.code_env_refs = {"API_URL": ["web/lib/api.ts"]}
    edges = edges_of([api, worker, project, role, gh_workflow(), gh_repo()], [facts])
    edge = edges[("web", "calls", "shop-api")]
    assert edge.confidence == Confidence.medium
    assert "value not read" in edge.evidence and "web/lib/api.ts reads API_URL" in edge.evidence
    assert ("web", "calls", "shop-worker") not in edges  # no port, and the name does not match


def test_rule7_no_repo_no_guess() -> None:
    api = svc("shop-api", containers=[{"name": "api", "ports": [{"port": 8000}]}])
    project = res(
        "vercel:project", "web", provider="vercel", env=[{"key": "API_URL", "type": "encrypted"}]
    )
    assert not any(k[1] == "calls" for k in edges_of([api, project]))


# --- structural and declared ------------------------------------------------------------


def test_vercel_project_is_deployed_by_its_latest_deployment() -> None:
    project = res("vercel:project", "web", provider="vercel", project_id="prj_1")
    deployment = res("vercel:deployment", "dpl_9", provider="vercel", project_id="prj_1")
    edge = edges_of([project, deployment])[("web", "deployed_by", "dpl_9")]
    assert edge.rule == "vercel_deployment" and edge.confidence == Confidence.high


def test_declared_edges_from_a_connector() -> None:
    target = res("rds:db", "orders")
    declared = DiscoveredEdge(
        from_external_id="arn:svc",
        to_external_id=target.external_id,
        kind=EdgeKind.reads,
        evidence="The app's config names this database.",
    )
    source = res(
        "ecs:service",
        "api",
        external_id="arn:svc",
        declared_edges=[
            declared.model_dump(mode="json"),
            {**declared.model_dump(mode="json"), "to_external_id": "missing"},
        ],
    )
    edges = edges_of([source, target])
    assert edges[("api", "reads", "orders")].evidence == "The app's config names this database."
    assert len(edges) == 1


def test_no_low_value_kinds_and_tags_make_no_edges() -> None:
    a = res("s3:bucket", "a", tags={"component": "uploads"})
    b = res("sqs:queue", "b", tags={"component": "uploads"})
    assert not edges_of([a, b])
    assert {k.value for k in EdgeKind} == {
        "calls",
        "reads",
        "deployed_by",
        "defined_in",
        "uses",
        "pulls_from",
        "logs_to",
    }


# --- the whole demo ------------------------------------------------------------------


REPO_DEMO = "GroundsCrew-AI/checkout-demo"

EXPECTED = {
    ("checkout-demo", "calls", "checkout-api"),
    ("checkout-api", "reads", "checkout-orders-db"),
    ("checkout-worker", "reads", "checkout-orders-db"),
    ("checkout-api", "uses", "checkout/database-url"),
    ("checkout-worker", "uses", "checkout/database-url"),
    ("checkout-api", "pulls_from", "checkout-api"),
    ("checkout-worker", "pulls_from", "checkout-worker"),
    ("checkout-api", "logs_to", "/ecs/checkout-api"),
    ("checkout-worker", "logs_to", "/ecs/checkout-worker"),
    ("checkout-api", "deployed_by", "deploy (deploy.yml)"),
    ("checkout-worker", "deployed_by", "deploy (deploy.yml)"),
    ("checkout-demo", "deployed_by", "deploy (deploy.yml)"),
    ("checkout-api", "defined_in", "GroundsCrew-AI/checkout-demo"),
}


async def test_demo_graph_has_every_expected_edge_with_evidence() -> None:
    resources, repos = await demo_inventory()
    edges = edges_of(resources, repos)
    assert set(edges) >= EXPECTED
    assert all(e.evidence for e in edges.values())
    # API_URL is encrypted on Vercel (as live), so its value is never read: medium, with the
    # name, the shared repository, and deploy.yml's web job (it writes the api task IP) as evidence.
    calls = edges[("checkout-demo", "calls", "checkout-api")]
    assert calls.confidence == Confidence.medium and calls.rule == "env_url"
    assert "API_URL (encrypted, value not read)" in calls.evidence
    assert "same repository GroundsCrew-AI/checkout-demo" in calls.evidence
    assert (
        "deploy.yml job web writes API_URL to Vercel from the public IP of a running "
        "checkout-api task" in calls.evidence
    )
    assert ("checkout-demo", "calls", "checkout-worker") not in edges
    # The production deployment's commit ties the project to the repository too.
    assert edges[("checkout-demo", "defined_in", REPO_DEMO)].rule in ("deploy_commit", "terraform")
    # The running image's commit is evidence on the defined_in edge, with the Terraform file.
    defined = edges[("checkout-api", "defined_in", "GroundsCrew-AI/checkout-demo")]
    assert defined.rule == "terraform" and defined.details["commit"].startswith("cef79e7")
    for k in EXPECTED - {("checkout-demo", "calls", "checkout-api")}:
        assert edges[k].confidence == Confidence.high, k


async def test_without_the_clone_web_still_calls_api_at_medium() -> None:
    """Live failure mode: the clone failed, so no workflows or Terraform. Commits come from
    the REST API: the image tag and the Vercel deployment are both commits of checkout-demo."""
    resources, repos = await demo_inventory()
    (facts,) = repos
    bare = RepoFacts(repo=facts.repo, html_url=facts.html_url, commits=facts.commits)
    kept = [r for r in resources if r.type != "github:workflow"]
    edges = edges_of(kept, [bare])
    assert edges[("checkout-demo", "defined_in", REPO_DEMO)].rule == "deploy_commit"
    calls = edges[("checkout-demo", "calls", "checkout-api")]
    assert calls.confidence == Confidence.medium
    assert "same repository GroundsCrew-AI/checkout-demo" in calls.evidence


async def test_demo_graph_with_plain_api_url_is_high() -> None:
    resources, repos = await demo_inventory(plain_api_url_routes("http://3.15.10.20:8000"))
    edge = edges_of(resources, repos)[("checkout-demo", "calls", "checkout-api")]
    assert edge.confidence == Confidence.high and "3.15.10.20" in edge.evidence


async def test_demo_iac_links() -> None:
    resources, repos = await demo_inventory()
    result = infer_edges(resources, repos)
    api = next(r for r in resources if r.type == "ecs:service" and r.name == "checkout-api")
    assert result.iac[key(api)].path == "infra/aws/ecs.tf#L94"


# --- coverage, risks, summary --------------------------------------------------------


async def test_coverage_levels() -> None:
    resources, repos = await demo_inventory()
    edges = infer_edges(resources, repos).edges
    by_name = {(r.type, r.name): r for r in resources}

    def level(rtype: str, name: str) -> str:
        return coverage(rtype, key(by_name[(rtype, name)]), edges)

    assert level("ecs:service", "checkout-api") == "diagnosable"  # linked log group
    assert level("rds:db", "checkout-orders-db") == "diagnosable"  # built-in metrics
    assert level("ecr:repository", "checkout-api") == "observed"
    register_actionable("ecs:service")
    try:
        assert level("ecs:service", "checkout-api") == "actionable"
    finally:
        coverage_module._actionable.discard("ecs:service")


async def test_risks_flag_the_api_without_a_stable_endpoint() -> None:
    resources, repos = await demo_inventory()
    found = risks(resources, infer_edges(resources, repos).edges)
    assert any(
        "checkout-api has no load balancer or stable endpoint" in r
        and "checkout-demo calls it" in r
        for r in found
    )
    assert any("checkout-api allows 8000 from 0.0.0.0/0" in r for r in found)
    assert not any("publicly accessible" in r for r in found)  # the db is private


def test_risks_public_db_and_failed_deploy() -> None:
    db = res("rds:db", "orders", publicly_accessible=True)
    api = svc("api", running_count=0, desired_count=1, deployments=[{"rollout_state": "FAILED"}])
    found = risks([db, api], [])
    assert "Risk: database orders is publicly accessible." in found
    assert "Risk: the latest deployment of api failed." in found
    assert "Risk: api runs 0 of 1 tasks." in found


async def test_digest_is_compact_and_named() -> None:
    resources, repos = await demo_inventory()
    lines = digest(resources, infer_edges(resources, repos).edges)
    api = next(line for line in lines if line.name == "checkout-api" and line.type == "ecs:service")
    assert (
        "reads checkout-orders-db (RDS database)" in api.relations
        and "deployed_by deploy (deploy.yml) (GitHub workflow)" in api.relations
    )
    assert api.notes and "no load balancer" in api.notes
    assert not any(line.type in ("ec2:security-group", "iam:role") for line in lines)


async def test_summary_uses_scouts_summarize_graph_with_risks_in_notes() -> None:
    resources, repos = await demo_inventory()
    edges = infer_edges(resources, repos).edges
    settings = Settings(_env_file=None, model_nano="m/nano", model_super="m/super")
    llm = FakeLLM(reply("- checkout-api on ECS\n- Risk: no stable endpoint"), settings=settings)
    budget = RunBudget(usage=MemoryUsageStore(), settings=settings)
    items = digest(resources, edges, risks=find_risks(resources, edges))
    text = await summarize_system(llm, budget, items)
    assert text.endswith("Risk: no stable endpoint")
    assert llm.models == ["m/nano"] and llm.reasoning_kwargs == [{"enable_thinking": False}]
    prompt = llm.calls[0]["messages"][1]["content"]
    api = next(i for i in items if i.name == "checkout-api" and i.type == "ecs:service")
    assert api.notes and "Risk: checkout-api has no load balancer or stable endpoint" in api.notes
    assert "stable endpoint" in prompt
    assert "postgresql://" not in prompt and "arn:aws" not in prompt
    assert Task.summarize_graph in ROUTES


def test_digest_is_deterministic_and_folds_to_fit() -> None:
    many = [res("lambda:function", f"fn-{i:03}") for i in range(400)]
    api = svc("api")
    db = res("rds:db", "orders")
    edge = InferredEdge(
        source=key(api),
        target=key(db),
        kind=EdgeKind.reads,
        confidence=Confidence.high,
        rule="sg_db_ingress",
        evidence="x",
    )
    items = digest([*many, api, db], [edge], token_cap=1_000)
    assert items == digest(list(reversed([*many, api, db])), [edge], token_cap=1_000)
    folded = next(i for i in items if i.type == "lambda:function")
    assert folded.name == "400 x lambda:function" and folded.notes
    assert any(i.name == "api" and "reads orders (RDS database)" in i.relations for i in items)
    assert sum(len(i.model_dump_json()) for i in items) < 1_000 * 4


# --- summary check ----------------------------------------------------------------------

LIVE_SUMMARY = """\
- The checkout-api has no load balancer; clients reach it directly on the task's public IP.
- It is an ECS service in the `checkout` cluster, pulling `checkout-api` from ECR.
- A worker service runs alongside the API in the same cluster, also using the same image and \
reading the same database secret.
- `checkout-api` and `checkout-worker` read `checkout-orders-db` via `checkout/database-url`.
- Both the API and worker store state in S3 buckets (`groundcrew-checkout-tfstate-…` and \
`groundcrew-templates-…`) for Terraform state and static assets.
- The services pull images from ECR repositories checkout-api and checkout-worker."""


async def test_summary_check_drops_the_live_false_claims() -> None:
    from app.graph.summary_check import check_summary

    resources, repos = await demo_inventory()
    resources.append(res("s3:bucket", "groundcrew-templates-881050425883"))
    edges = infer_edges(resources, repos).edges
    checked = check_summary(LIVE_SUMMARY, resources, edges)
    dropped = dict(checked.dropped)
    assert len(checked.dropped) == 2, checked.dropped
    assert any("same image" in b for b in dropped) and any("S3 buckets" in b for b in dropped)
    assert any("different images" in why for why in dropped.values())
    assert any("is tooling" in why for why in dropped.values())
    assert "read `checkout-orders-db`" in checked.text and "pull images from ECR" in checked.text
    assert "S3" not in checked.text and "same image" not in checked.text


async def test_digest_tags_tooling_and_states_each_image() -> None:
    resources, repos = await demo_inventory()
    resources.append(res("s3:bucket", "groundcrew-templates-881050425883"))
    resources.append(res("iam:role", "groundcrew-read-only"))
    edges = infer_edges(resources, repos).edges
    items = digest(resources, edges)
    buckets = [i for i in items if i.type == "s3:bucket"]  # state and template buckets
    assert len(buckets) == 2 and all(b.notes and TOOLING_NOTE in b.notes for b in buckets)
    assert not any(b.relations for b in buckets)
    gc_role = next(i for i in items if i.name == "groundcrew-read-only")
    assert gc_role.notes and TOOLING_NOTE in gc_role.notes
    assert not any(TOOLING_NOTE in (i.notes or "") for i in items if i.type == "ecs:service")
    worker = next(i for i in items if i.name == "checkout-worker" and i.type == "ecs:service")
    assert worker.notes and "own image checkout-worker:cef79e7" in worker.notes
    assert "pulls_from checkout-worker (ECR image repository)" in worker.relations


async def test_summary_check_drops_relationships_with_no_edge() -> None:
    from app.graph.summary_check import check_summary

    resources, repos = await demo_inventory()
    edges = infer_edges(resources, repos).edges
    text = "\n".join(
        [
            "- checkout-api calls checkout-worker over HTTP.",
            "- checkout-api and checkout-worker read checkout-orders-db.",
            "- checkout-demo calls checkout-api through API_URL.",
            "- checkout-api and checkout-worker run in the checkout cluster.",
            "- checkout\u2011worker runs alongside checkout\u2011api and reads the same database.",
        ]
    )
    checked = check_summary(text, resources, edges)
    assert [b for b, _ in checked.dropped] == [
        "checkout-api calls checkout-worker over HTTP.",
        "checkout\u2011worker runs alongside checkout\u2011api and reads the same database.",
    ]
    assert "no edge relates" in checked.dropped[0][1]
    assert "read checkout-orders-db" in checked.text and "through API_URL" in checked.text
    assert "checkout cluster" in checked.text


async def test_summary_check_names_workflows_by_file_and_checks_provisioning() -> None:
    """Live summary (2026-10-09): deploy.yml deploys the services; Terraform provisions them."""
    from app.graph.summary_check import check_summary

    resources, repos = await demo_inventory()
    edges = infer_edges(resources, repos).edges
    text = "\n".join(
        [
            "- The entire infrastructure is provisioned by the GitHub workflow `deploy.yml`.",
            "- `deploy.yml` deploys checkout-api and checkout-worker on every push to main.",
        ]
    )
    checked = check_summary(text, resources, edges)
    assert [b for b, _ in checked.dropped] == [
        "The entire infrastructure is provisioned by the GitHub workflow `deploy.yml`."
    ]
    assert "provisions nothing" in checked.dropped[0][1]
    assert "deploys checkout-api and checkout-worker" in checked.text
