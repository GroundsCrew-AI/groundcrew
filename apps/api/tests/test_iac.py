"""IaC facts: Terraform names and references, workflows, Dockerfiles, compose, env names."""

from pathlib import Path
from typing import Any

from app.discovery.iac import parse_compose, parse_dockerfile, parse_workflow, read_repo
from tests.discovery_helpers import DEMO_REPO


def _tf(address: str) -> dict[str, Any]:
    facts = read_repo(DEMO_REPO, "GroundsCrew-AI/checkout-demo")
    tf = next(t for t in facts.terraform if t.address == address)
    return tf.model_dump()


def test_terraform_names_resolve_from_variables_locals_and_for_each() -> None:
    assert _tf("aws_ecs_service.api[0]")["attributes"] == {"name": "checkout-api"}
    assert _tf('aws_ecr_repository.app["worker"]')["attributes"] == {"name": "checkout-worker"}
    assert _tf('aws_cloudwatch_log_group.app["api"]')["attributes"] == {"name": "/ecs/checkout-api"}
    assert _tf("aws_db_instance.orders")["attributes"]["identifier"] == "checkout-orders-db"
    assert _tf("aws_secretsmanager_secret.database_url")["attributes"] == {
        "name": "checkout/database-url"
    }
    assert _tf("aws_vpc.main")["attributes"] == {"tags.Name": "checkout-vpc"}


def test_terraform_paths_lines_and_references() -> None:
    service = _tf("aws_ecs_service.api[0]")
    assert service["path"] == "infra/aws/ecs.tf" and service["line"] == 94
    assert "aws_security_group.tasks" in service["references"]
    version = _tf("aws_secretsmanager_secret_version.database_url")
    assert {"aws_secretsmanager_secret.database_url", "aws_db_instance.orders"} <= set(
        version["references"]
    )


def test_unresolvable_names_are_left_out(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text(
        'resource "aws_ecs_service" "x" {\n  name = "${data.foo.bar}-svc"\n}\n'
        'resource "aws_s3_bucket" "b" {\n  bucket = "literal-bucket"\n}\n'
    )
    facts = read_repo(tmp_path, "o/r")
    by_address = {t.address: t.attributes for t in facts.terraform}
    assert by_address == {"aws_ecs_service.x": {}, "aws_s3_bucket.b": {"bucket": "literal-bucket"}}


def test_broken_terraform_does_not_stop_the_rest(tmp_path: Path) -> None:
    (tmp_path / "bad.tf").write_text('resource "x" {{{')
    (tmp_path / "Dockerfile").write_text("FROM node:22\nEXPOSE 3000\n")
    facts = read_repo(tmp_path, "o/r")
    assert facts.terraform == [] and facts.dockerfiles[0].exposed_ports == [3000]


def test_deploy_workflow_facts() -> None:
    facts = read_repo(DEMO_REPO, "GroundsCrew-AI/checkout-demo")
    (workflow,) = facts.workflows
    assert workflow.path == ".github/workflows/deploy.yml"
    assert workflow.triggers == ["push", "workflow_dispatch"] and workflow.branches == ["main"]
    deploy = next(j for j in workflow.jobs if j.id == "deploy")
    assert deploy.role_to_assume == ["${{ vars.AWS_DEPLOY_ROLE_ARN }}"]
    assert deploy.ecs_deploy and deploy.ecs_services == [
        "${{ vars.ECS_API_SERVICE }}",
        "${{ vars.ECS_WORKER_SERVICE }}",
    ]
    assert deploy.pushes_images == [
        "${{ vars.ECR_API_REPOSITORY }}",
        "${{ vars.ECR_WORKER_REPOSITORY }}",
    ]
    web = next(j for j in workflow.jobs if j.id == "web")
    assert web.vercel and web.vercel_env_keys == ["API_URL"] and web.needs == ["deploy"]
    assert web.ecs_services == ["${{ vars.ECS_API_SERVICE }}"]


def test_workflow_variants() -> None:
    text = """
name: release
on: [push]
jobs:
  ship:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/amazon-ecs-deploy-task-definition@v2
        with:
          service: shop-api
      - run: npx vercel deploy --prod && vercel env add API_HOST production
        env:
          VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
"""
    workflow = parse_workflow(".github/workflows/release.yml", text)
    assert workflow is not None and workflow.triggers == ["push"]
    job = workflow.jobs[0]
    assert job.ecs_deploy and job.ecs_services == ["shop-api"]
    assert job.vercel and job.vercel_env_keys == ["API_HOST"]
    assert parse_workflow("x.yml", "{{ not yaml") is None


def test_dockerfiles_and_env_names() -> None:
    facts = read_repo(DEMO_REPO, "GroundsCrew-AI/checkout-demo")
    api = next(d for d in facts.dockerfiles if d.path == "api/Dockerfile")
    assert api.base_images == ["python:3.12-slim"] and api.exposed_ports == [8000]
    assert facts.code_env_refs["API_URL"] == ["web/lib/api.ts"]
    assert parse_dockerfile(
        "D", "FROM --platform=linux/amd64 golang:1.23 AS build\nFROM scratch\n"
    ).base_images == [
        "golang:1.23",
        "scratch",
    ]


def test_compose_and_env_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=never-read\n")
    (tmp_path / "app.py").write_text('import os\nos.environ["REDIS_URL"]\nos.getenv("PORT")\n')
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  api:\n    build: .\n    ports: ['8000:8000']\n"
        "    environment:\n      DB_URL: x\n    depends_on: [db]\n"
        "  db:\n    image: postgres:16\n"
    )
    facts = read_repo(tmp_path, "o/r")
    assert [s.name for s in facts.compose] == ["api", "db"]
    assert facts.compose[0].env_names == ["DB_URL"] and facts.compose[0].depends_on == ["db"]
    assert set(facts.code_env_refs) == {"REDIS_URL", "PORT"}
    assert "never-read" not in facts.model_dump_json()
    assert parse_compose("c.yml", "services: [1, 2]") == []


def test_web_job_sets_api_url_from_the_api_task_ip() -> None:
    from app.discovery.iac import read_repo
    from tests.discovery_helpers import DEMO_REPO

    facts = read_repo(DEMO_REPO, "GroundsCrew-AI/checkout-demo")
    deploy = next(w for w in facts.workflows if w.path.endswith("deploy.yml"))
    web = next(j for j in deploy.jobs if j.id == "web")
    assert web.vercel and web.vercel_env_sources == {"API_URL": "task_ip"}
    assert web.ecs_services == ["${{ vars.ECS_API_SERVICE }}"]
    services = {j.id: j.ecs_services for j in deploy.jobs if j.ecs_deploy}
    assert services, "the deploy job updates the ECS services"
