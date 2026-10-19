"""Apps that are not the checkout demo: other deploy workflow names, other log libraries."""

import json

import pytest

from app.telemetry.github import is_deploy_workflow
from app.telemetry.log_levels import is_error_line


@pytest.mark.parametrize(
    ("path", "name", "deploy"),
    [
        (".github/workflows/deploy.yml", "Deploy", True),
        (".github/workflows/release.yml", "Release", True),
        (".github/workflows/cd.yml", "CD", True),
        (".github/workflows/ci-cd.yml", "CI/CD", True),
        (".github/workflows/main.yml", "Ship to production", True),
        (".github/workflows/publish-image.yml", "", True),
        (".github/workflows/deploy-staging.yml", "", True),
        (".github/workflows/ci.yml", "CI", False),
        (".github/workflows/test.yml", "Tests", False),
        (".github/workflows/abcd.yml", "Lint", False),
        (".github/workflows/codeql.yml", "CodeQL", False),
    ],
)
def test_deploy_workflows_are_found_by_whole_words(path: str, name: str, deploy: bool) -> None:
    assert is_deploy_workflow({"path": path, "name": name}) is deploy


@pytest.mark.parametrize(
    ("level", "error"), [(10, False), (30, False), (40, False), (50, True), (60, True)]
)
def test_pino_and_bunyan_numeric_levels(level: int, error: bool) -> None:
    line = json.dumps({"level": level, "time": 1, "msg": "request failed: error talking to db"})
    assert is_error_line(line) is error


def test_a_numeric_level_with_a_5xx_or_a_stack_is_an_error() -> None:
    # pino-http: the status is under res.
    completed = {"level": 30, "res": {"statusCode": 503}, "msg": "request completed"}
    assert is_error_line(json.dumps(completed))
    assert not is_error_line(json.dumps({**completed, "res": {"statusCode": 200}}))
    stack = "Error: boom\n    at f (a.js:1)"
    assert is_error_line(json.dumps({"level": 40, "msg": "x", "stack": stack}))
    assert not is_error_line(json.dumps({"level": True, "msg": "fine"}))
