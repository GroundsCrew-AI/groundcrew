"""The platform host's Terraform and first-boot config keep their security properties: the
instance role only assumes groundcrew-* roles, SSH only from the admin address, IMDSv2, an
encrypted root volume, the one AWS account, and the host firewall matching the security group."""

import re
from pathlib import Path
from typing import Any

import hcl2

PLATFORM = Path(__file__).resolve().parents[3] / "infra" / "platform"


def _plain(value: Any) -> Any:
    """python-hcl2 keeps HCL quotes on keys and strings; drop them."""
    if isinstance(value, dict):
        return {_plain(k): _plain(v) for k, v in value.items() if k != "__is_block__"}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _load(name: str) -> dict[str, Any]:
    parsed: dict[str, Any] = _plain(hcl2.loads((PLATFORM / name).read_text()))
    return parsed


def _blocks(doc: dict[str, Any], kind: str, type_: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for block in doc.get(kind, []):
        out.update(block.get(type_, {}))
    return out


def test_the_role_only_assumes_groundcrew_roles_and_only_ec2_uses_it() -> None:
    iam = _load("iam.tf")
    docs = _blocks(iam, "data", "aws_iam_policy_document")
    (allow,) = docs["assume_customer_roles"]["statement"]
    assert allow["actions"] == ["sts:AssumeRole"]
    assert allow["resources"] == ["arn:aws:iam::*:role/groundcrew-*"]
    assert "effect" not in allow or allow["effect"] == "Allow"
    (trust,) = docs["ec2_trust"]["statement"]
    assert trust["principals"][0]["identifiers"] == ["ec2.amazonaws.com"]
    roles = _blocks(iam, "resource", "aws_iam_role")
    assert roles["platform"]["name"] == "groundcrew-platform"
    # No managed policies, no other inline policy.
    assert set(_blocks(iam, "resource", "aws_iam_role_policy")) == {"assume_customer_roles"}
    assert not _blocks(iam, "resource", "aws_iam_role_policy_attachment")


def test_only_web_ports_are_public_and_ssh_is_the_admin_address() -> None:
    main = _load("main.tf")
    rules = _blocks(main, "resource", "aws_vpc_security_group_ingress_rule")
    public = {r["from_port"] for r in rules.values() if r.get("cidr_ipv4") == "0.0.0.0/0"}
    assert public == {80, 443}
    assert rules["ssh"]["from_port"] == 22 and rules["ssh"]["cidr_ipv4"] == "${var.admin_cidr}"
    assert all(r["from_port"] == r["to_port"] for r in rules.values())
    variables = _load("variables.tf")
    admin = _blocks(variables, "variable", "admin_cidr")
    assert "/32" in str(admin)


def test_the_instance_is_locked_down() -> None:
    instance = _blocks(_load("main.tf"), "resource", "aws_instance")["platform"]
    metadata = instance["metadata_options"][0]
    assert metadata["http_tokens"] == "required"
    assert metadata["http_put_response_hop_limit"] == 2
    root = instance["root_block_device"][0]
    assert root["encrypted"] is True and root["volume_type"] == "gp3"
    assert instance["disable_api_termination"] is True
    assert "key_name" not in instance  # the SSH key comes from cloud-init, not a key pair
    allowed = {
        "aws_instance", "aws_eip", "aws_security_group", "aws_vpc_security_group_ingress_rule",
        "aws_vpc_security_group_egress_rule", "aws_iam_role", "aws_iam_role_policy",
        "aws_iam_instance_profile",
    }  # fmt: skip
    used = {
        type_
        for name in ("main.tf", "iam.tf")
        for block in _load(name).get("resource", [])
        for type_ in block
    }
    assert used <= allowed, used - allowed


def test_one_account_one_region_own_state_and_tags() -> None:
    versions = _load("versions.tf")
    (provider,) = [p["aws"] for p in versions["provider"]]
    assert provider["allowed_account_ids"] == ["881050425883"]
    assert provider["region"] == "us-east-2"
    tags = provider["default_tags"][0]["tags"]
    assert tags["project"] == "groundcrew" and tags["groundcrew:role"] == "platform"
    backend = versions["terraform"][0]["backend"][0]["s3"]
    assert backend["key"] == "groundcrew/platform.tfstate" and backend["encrypt"] is True


def test_first_boot_hardens_ssh_and_the_firewall_matches() -> None:
    text = (PLATFORM / "cloud-init.yaml.tftpl").read_text()
    assert "ssh_pwauth: false" in text and "disable_root: true" in text
    for line in ("PasswordAuthentication no", "PermitRootLogin no", "AllowUsers ubuntu"):
        assert line in text
    ufw = re.findall(r"- ufw (.+)", text)
    assert ufw == [
        "default deny incoming",
        "default allow outgoing",
        "allow 80/tcp",
        "allow 443/tcp",
        "allow from ${admin_cidr} to any port 22 proto tcp",
        "--force enable",
    ]
    assert "unattended-upgrades" in text and "fail2ban" in text


def test_no_secrets_are_kept_in_the_repo() -> None:
    ignored = (PLATFORM / ".gitignore").read_text().split()
    assert {"terraform.tfvars", "*.tfstate", ".terraform/"} <= set(ignored)
