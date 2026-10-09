"""AWS connection: the read-only allowlist, the runtime guard, regions, ExternalId, and the
CloudFormation template. botocore's Stubber drives the ordered STS flows."""

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from botocore.stub import Stubber

from app.config import Settings
from app.connectors.aws import (
    AWS_READ_OPERATIONS,
    BASE_OPERATIONS,
    FORBIDDEN_OPERATIONS,
    AwsError,
    AwsReader,
    AwsSessions,
    ReadOnlyViolationError,
    RegionNotAllowedError,
    guard_session,
    is_read_operation,
    new_external_id,
    parse_role_arn,
    platform_session,
)
from tests.aws_fixture import fixture_session

ROOT = Path(__file__).resolve().parents[3]
ROLE = "arn:aws:iam::881050425883:role/groundcrew-read-only"


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{"discovery_regions": ["us-east-2"], **overrides})


def _session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_access_key="fake",  # noqa: S106
        region_name="us-east-2",
    )


# --- allowlist ---------------------------------------------------------------------------


def test_allowlist_is_reads_only() -> None:
    for name in AWS_READ_OPERATIONS:
        assert is_read_operation(name), f"{name} is not a read operation"
        _, _, op = name.partition(":")
        assert re.match(r"^(Describe|List|Get|Search|Filter)([A-Z]|$)", op), name


def test_allowlist_never_reads_secret_values_or_data() -> None:
    must_never = {
        "secretsmanager:GetSecretValue",
        "secretsmanager:BatchGetSecretValue",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath",
        "kms:Decrypt",
        "s3:GetObject",
        "lambda:GetFunction",
    }
    assert must_never <= FORBIDDEN_OPERATIONS
    assert not must_never & AWS_READ_OPERATIONS
    assert not any(
        op.startswith(("ssm:", "kms:", "s3:", "dynamodb:", "sqs:")) for op in AWS_READ_OPERATIONS
    )
    assert {"sts:AssumeRole", "sts:GetCallerIdentity"} == BASE_OPERATIONS


@pytest.mark.parametrize(
    ("service", "call", "kwargs"),
    [
        ("secretsmanager", "get_secret_value", {"SecretId": "checkout/database-url"}),
        ("ssm", "get_parameter", {"Name": "/x", "WithDecryption": True}),
        ("ecs", "update_service", {"cluster": "c", "service": "s"}),
        ("ecs", "create_cluster", {}),
        ("s3", "get_object", {"Bucket": "b", "Key": "k"}),
        ("kms", "decrypt", {"CiphertextBlob": b"x"}),
    ],
)
def test_guard_refuses_non_read_calls_even_when_stubbed(
    service: str, call: str, kwargs: dict[str, Any]
) -> None:
    client = guard_session(_session(), AWS_READ_OPERATIONS).client(service, region_name="us-east-2")
    with Stubber(client) as stub:
        stub.add_response(call, {})
        with pytest.raises(ReadOnlyViolationError):
            getattr(client, call)(**kwargs)


def test_guard_allows_listed_reads() -> None:
    client = guard_session(_session(), AWS_READ_OPERATIONS).client("ecs", region_name="us-east-2")
    with Stubber(client) as stub:
        stub.add_response("list_clusters", {"clusterArns": ["arn:x"]})
        assert client.list_clusters()["clusterArns"] == ["arn:x"]


def test_discovery_only_calls_allowlisted_operations() -> None:
    calls: list[str] = []
    reader = AwsReader(fixture_session(calls=calls), _settings())
    reader.call("ecs", "us-east-2", "ListClusters")
    with pytest.raises(ReadOnlyViolationError):
        reader.call("secretsmanager", "us-east-2", "GetSecretValue", SecretId="x")
    assert "secretsmanager:GetSecretValue" not in calls


# --- regions -------------------------------------------------------------------------------


def test_regional_clients_only_in_discovery_regions() -> None:
    reader = AwsReader(fixture_session(), _settings())
    reader.client("ecs", "us-east-2")
    for region in ("us-east-1", "eu-west-1", None):
        with pytest.raises(RegionNotAllowedError):
            reader.client("ecs", region)
    # Global services are fine whatever region is asked for (IAM has one global endpoint).
    assert reader.client("iam", "eu-west-1").meta.region_name == "aws-global"
    assert reader.client("sts", None).meta.region_name == "us-east-2"


def test_discovery_regions_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOVERY_REGIONS", "us-east-2, eu-west-1")
    assert Settings(_env_file=None).discovery_regions == ["us-east-2", "eu-west-1"]
    monkeypatch.delenv("DISCOVERY_REGIONS")
    assert Settings(_env_file=None).discovery_regions == ["us-east-2"]


# --- ExternalId and verify ------------------------------------------------------------------


def _creds() -> dict[str, Any]:
    return {
        "Credentials": {
            "AccessKeyId": "ASIAFAKEFAKEFAKEFAKE",
            "SecretAccessKey": "fake",
            "SessionToken": "fake",
            "Expiration": datetime.now(UTC) + timedelta(hours=1),
        }
    }


class _StubbedSessions(AwsSessions):
    """AwsSessions whose STS clients are stubbed in order."""

    def __init__(
        self,
        base_stub: list[tuple[str, Any, dict[str, Any] | None]],
        identity: dict[str, Any] | None = None,
    ):
        super().__init__(_settings(), base=guard_session(_session(), BASE_OPERATIONS))
        self.base_stub = base_stub
        self.identity = identity
        self.clients: list[Any] = []
        self._base_client: Any = None

    def _sts(self, session: boto3.Session) -> Any:
        if session is self._base and self._base_client is not None:
            return self._base_client
        client = session.client("sts", region_name="us-east-2")
        stub = Stubber(client)
        if session is self._base:
            for method, response, expected in self.base_stub:
                if isinstance(response, str):
                    stub.add_client_error(method, service_error_code=response, http_status_code=403)
                else:
                    stub.add_response(method, response, expected)
        elif self.identity is not None:
            stub.add_response("get_caller_identity", self.identity)
        stub.activate()
        self.clients.append(stub)
        if session is self._base:
            self._base_client = client
        return client


def test_external_id_is_unique_and_long() -> None:
    ids = {new_external_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) >= 32 and i.startswith("gc-") for i in ids)


def test_assume_requires_external_id() -> None:
    sessions = _StubbedSessions([])
    with pytest.raises(AwsError, match="ExternalId is required"):
        sessions.assume(ROLE, "")


def test_verify_sends_external_id_and_checks_account() -> None:
    expected_with = {
        "RoleArn": ROLE,
        "RoleSessionName": "groundcrew-discovery",
        "DurationSeconds": 3600,
        "ExternalId": "gc-abc",
    }
    sessions = _StubbedSessions(
        [
            ("assume_role", "AccessDenied", None),  # without ExternalId: refused, as it must be
            ("assume_role", _creds(), expected_with),
        ],
        identity={
            "Account": "881050425883",
            "Arn": "arn:aws:sts::881050425883:assumed-role/groundcrew-read-only/x",
            "UserId": "x",
        },
    )
    identity = sessions.verify(ROLE, "gc-abc")
    assert identity.account == "881050425883"
    for stub in sessions.clients:
        stub.assert_no_pending_responses()


def test_verify_refuses_role_that_works_without_external_id() -> None:
    sessions = _StubbedSessions([("assume_role", _creds(), None)])
    with pytest.raises(AwsError, match="without the ExternalId"):
        sessions.verify(ROLE, "gc-abc")


def test_verify_explains_a_wrong_external_id() -> None:
    sessions = _StubbedSessions(
        [("assume_role", "AccessDenied", None), ("assume_role", "AccessDenied", None)]
    )
    with pytest.raises(AwsError) as exc:
        sessions.verify(ROLE, "gc-abc")
    message = str(exc.value)
    assert "different ExternalId; this workspace's is gc-abc" in message
    assert "Connect AWS link again" in message


def test_verify_refuses_account_mismatch() -> None:
    sessions = _StubbedSessions(
        [("assume_role", "AccessDenied", None), ("assume_role", _creds(), None)],
        identity={
            "Account": "111122223333",
            "Arn": "arn:aws:sts::111122223333:assumed-role/x/y",
            "UserId": "x",
        },
    )
    with pytest.raises(AwsError, match="not 881050425883"):
        sessions.verify(ROLE, "gc-abc")


def test_customer_session_is_guarded_and_cached() -> None:
    sessions = _StubbedSessions([("assume_role", _creds(), None)])
    session = sessions.assume(ROLE, "gc-abc")
    assert sessions.assume(ROLE, "gc-abc") is session  # cached in memory, no second STS call
    with pytest.raises(ReadOnlyViolationError):
        session.client("secretsmanager", region_name="us-east-2").get_secret_value(SecretId="x")


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::881050425883:user/x",
        "not-an-arn",
        "arn:aws:iam::12:role/x",
        "arn:aws:iam::881050425883:role/",
        "arn:aws:iam::881050425883:role/GroundcrewReadOnly",  # not groundcrew-*
        "arn:aws:iam::881050425883:role/admin",
    ],
)
def test_parse_role_arn_rejects(arn: str) -> None:
    with pytest.raises(AwsError):
        parse_role_arn(arn)


def test_parse_role_arn_accepts_groundcrew_roles_with_paths() -> None:
    assert parse_role_arn(ROLE) == "881050425883"
    assert parse_role_arn("arn:aws:iam::881050425883:role/team/groundcrew-x") == "881050425883"


# --- platform identity --------------------------------------------------------------------


def test_platform_session_uses_only_the_groundcrew_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # A profile in the environment (or ~/.aws) is never used.
    monkeypatch.setenv("AWS_PROFILE", "someone-elses-admin")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/credentials")
    settings = _settings(
        groundcrew_aws_access_key_id="AKIAPLATFORMEXAMPLE1",
        groundcrew_aws_secret_access_key="platform-secret",  # noqa: S106 - fake
    )
    session = platform_session(settings, "us-east-2")
    creds = session.get_credentials()
    assert creds is not None and creds.access_key == "AKIAPLATFORMEXAMPLE1"
    assert session.profile_name == "default" and session.region_name == "us-east-2"
    assert session._session.get_config_variable("credentials_file") == os.devnull


def test_platform_session_without_keys_uses_the_chain_but_no_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "someone-elses-admin")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATASKROLEEXAMPLE1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    creds = platform_session(_settings(), "us-east-2").get_credentials()
    assert creds is not None and creds.access_key == "AKIATASKROLEEXAMPLE1"


def test_platform_session_needs_both_keys() -> None:
    with pytest.raises(AwsError, match="or neither"):
        platform_session(_settings(groundcrew_aws_access_key_id="AKIAONLYONE"), "us-east-2")


def test_principal_is_the_configured_platform_arn() -> None:
    arn = "arn:aws:iam::881050425883:user/groundcrew-platform"
    sessions = AwsSessions(_settings(groundcrew_platform_principal_arn=arn))
    assert sessions.principal_arn() == arn


# --- CloudFormation template -------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)  # type: ignore[arg-type]


_CfnLoader.add_multi_constructor("!", _tag)


def _template() -> dict[str, Any]:
    data: dict[str, Any] = yaml.load((ROOT / "infra" / "aws-connect.yaml").read_text(), _CfnLoader)  # noqa: S506
    return data


def test_template_copy_served_by_api_matches_infra() -> None:
    served = ROOT / "apps" / "api" / "app" / "connectors" / "templates" / "aws-connect.yaml"
    assert served.read_text() == (ROOT / "infra" / "aws-connect.yaml").read_text()


def test_template_trust_needs_external_id_and_one_hour_sessions() -> None:
    template = _template()
    role = template["Resources"]["GroundcrewRole"]["Properties"]
    assert role["MaxSessionDuration"] == 3600
    (statement,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"AWS": "GroundcrewPrincipalArn"}
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": "ExternalId"}}
    assert set(template["Parameters"]) >= {"GroundcrewPrincipalArn", "ExternalId"}
    assert "RoleArn" in template["Outputs"]


def test_template_policy_is_read_only_with_explicit_deny() -> None:
    template = _template()
    (policy,) = template["Resources"]["GroundcrewRole"]["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    allows = [a for s in statements if s["Effect"] == "Allow" for a in s["Action"]]
    for action in allows:
        verb = action.split(":", 1)[1]
        assert verb.startswith(("Describe", "List", "Get", "Search", "Filter")), action
        assert action not in {"secretsmanager:GetSecretValue", "kms:Decrypt", "s3:GetObject"}
    deny = next(s for s in statements if s["Effect"] == "Deny")
    assert {
        "secretsmanager:GetSecretValue",
        "ssm:GetParameter*",
        "kms:Decrypt",
        "s3:GetObject",
    } <= set(deny["Action"])
    assert deny["Resource"] == "*"


def test_template_indexes_with_resource_explorer_and_never_enables_config() -> None:
    resources = _template()["Resources"]
    types = {r["Type"] for r in resources.values()}
    assert resources["ResourceExplorerIndex"]["Properties"]["Type"] == "LOCAL"
    assert "AWS::ResourceExplorer2::DefaultViewAssociation" in types
    assert not any(t.startswith("AWS::Config::") for t in types)
