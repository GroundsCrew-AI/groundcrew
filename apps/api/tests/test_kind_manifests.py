"""The kind cluster and the groundcrew-deployer RBAC / admission policy, checked offline.
The live proof is `make kind-check` (tests/test_kind_live.py, opt-in)."""

import re
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]
KIND = ROOT / "infra" / "kind"
SA_USER = "system:serviceaccount:groundcrew-system:groundcrew-deployer"


def _docs(name: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((KIND / name).read_text()) if d]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def _granted(rules: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(res, verb) for r in rules for res in r["resources"] for verb in r["verbs"]}


def test_node_image_is_pinned_and_new_enough() -> None:
    cluster = yaml.safe_load((KIND / "cluster.yaml").read_text())
    image = cluster["nodes"][0]["image"]
    m = re.fullmatch(r"kindest/node:v1\.(\d+)\.\d+@sha256:[0-9a-f]{64}", image)
    assert m, image
    assert int(m.group(1)) >= 30  # ValidatingAdmissionPolicy is GA from 1.30
    assert cluster["name"] == "groundcrew"
    ports = {p["hostPort"]: p["listenAddress"] for p in cluster["nodes"][0]["extraPortMappings"]}
    assert ports == {80: "127.0.0.1", 443: "127.0.0.1"}


def test_namespace_admin_role_is_limited() -> None:
    role = _one(_docs("rbac.yaml"), "ClusterRole", "gc-namespace-admin")
    granted = _granted(role["rules"])
    resources = {res for res, _ in granted}
    # No RBAC, no tokens, no ServiceAccounts, no exec: nothing to escalate with.
    assert not resources & {
        "roles",
        "rolebindings",
        "serviceaccounts",
        "serviceaccounts/token",
        "pods/exec",
        "pods/attach",
        "namespaces",
        "*",
    }
    for res in ("deployments", "services", "ingresses", "replicasets", "secrets"):
        assert (res, "create") in granted or (res, "delete") in granted, res
    assert ("pods/log", "get") in granted


def test_cluster_role_binds_only_gc_namespace_admin() -> None:
    docs = _docs("rbac.yaml")
    role = _one(docs, "ClusterRole", "groundcrew-deployer")
    binds = [r for r in role["rules"] if "bind" in r["verbs"]]
    assert binds == [
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["clusterroles"],
            "verbs": ["bind"],
            "resourceNames": ["gc-namespace-admin"],
        }
    ]
    granted = _granted(role["rules"])
    assert not any(
        res in ("secrets", "*") or verb in ("*", "escalate", "impersonate") for res, verb in granted
    )
    # No update or delete on RoleBindings, so it cannot change a binding once made.
    assert {v for res, v in granted if res == "rolebindings"} == {"get", "list", "create"}


def test_cluster_wide_bindings_are_view_and_the_deployer_role() -> None:
    docs = _docs("rbac.yaml")
    bindings = [d for d in docs if d["kind"] == "ClusterRoleBinding"]
    assert sorted(b["roleRef"]["name"] for b in bindings) == ["groundcrew-deployer", "view"]
    for b in bindings:
        assert b["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "groundcrew-deployer",
                "namespace": "groundcrew-system",
            }
        ]
    assert not any(
        d["kind"] in ("Role", "RoleBinding") for d in docs
    )  # nothing in groundcrew-system
    sa = _one(docs, "ServiceAccount", "groundcrew-deployer")
    assert sa["metadata"]["namespace"] == "groundcrew-system"


def test_admission_policy_covers_every_write_by_the_deployer() -> None:
    docs = _docs("admission-policy.yaml")
    policy = _one(docs, "ValidatingAdmissionPolicy", "groundcrew-deployer-scope")
    spec = policy["spec"]
    assert spec["failurePolicy"] == "Fail"
    (rule,) = spec["matchConstraints"]["resourceRules"]
    assert rule["apiGroups"] == ["*"] and rule["resources"] == ["*"]
    assert set(rule["operations"]) >= {"CREATE", "UPDATE", "DELETE"}
    (cond,) = spec["matchConditions"]
    assert SA_USER in cond["expression"]
    text = " ".join(v["expression"] for v in spec["validations"])
    assert "request.namespace.startsWith('gc-')" in text
    assert "request.name.startsWith('gc-')" in text
    assert "object.roleRef.name == 'gc-namespace-admin'" in text
    assert "object.metadata.labels['project'] == 'groundcrew'" in text
    binding = _one(docs, "ValidatingAdmissionPolicyBinding", "groundcrew-deployer-scope")
    assert binding["spec"] == {
        "policyName": "groundcrew-deployer-scope",
        "validationActions": ["Deny"],
    }


def test_settings_and_scripts_agree_with_the_manifests() -> None:
    s = Settings(_env_file=None)
    assert s.gc_namespace_prefix == "gc-"
    lib = (ROOT / "scripts" / "kind" / "lib.sh").read_text()
    assert "NAMESPACE=groundcrew-system" in lib and "SA=groundcrew-deployer" in lib
    port = re.search(r'REGISTRY_PORT="\$\{GC_KIND_REGISTRY_PORT:-(\d+)\}"', lib)
    assert port and s.gc_kind_registry == f"localhost:{port.group(1)}"
    # Every download is checksummed.
    for name in ("KIND", "KUBECTL", "INGRESS", "METRICS"):
        assert re.search(rf"^{name}_SHA256=[0-9a-f]{{64}}$", lib, re.MULTILINE), name
