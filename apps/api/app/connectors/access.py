"""What Groundcrew can read and change through one connection, in plain words, for Settings
and the Connections page. It follows the roles and settings the connection was verified with;
the limits themselves are enforced by the customer's IAM, RBAC, and app permissions and by
Groundcrew's own allowlists (app.connectors.aws, kubernetes, github)."""

from app.config import Settings
from app.connectors.aws import connection_regions
from app.connectors.credentials import capabilities
from app.connectors.kubernetes import CONFIGMAP_CHANGES_KEY
from app.db.models import Connection
from app.targets.k8s_deploy import deploy_settings

NEVER_AWS = (
    "Never reads secret values, SSM parameters, decrypted data, or S3 objects (the role denies "
    "them)."
)


def access_summary(conn: Connection, settings: Settings) -> list[str]:
    c = conn.config or {}
    can = set(capabilities(conn))
    if conn.kind == "aws":
        regions = ", ".join(connection_regions(c, settings))
        lines = [
            f"Reads resource names, configuration, logs, and metrics in {regions} through the "
            "groundcrew-read-only role, with this workspace's ExternalId and 1-hour sessions.",
            NEVER_AWS,
        ]
        if "deploy" in can:
            lines.append(
                "Deploys after your approval: creates and updates groundcrew-* ECS services, ECR "
                "repositories, log groups, security groups, and groundcrew/* secrets, all tagged "
                "project=groundcrew. Writes those secrets but never reads them back."
            )
        if "operate" in can:
            lines.append(
                "Emergency fixes after your approval: rolls back or restarts the ECS services and "
                "rolls back the secrets your stack's Operate lists allow."
            )
        return lines
    if conn.kind == "github":
        repos = list(c.get("repos") or [])
        shown = ", ".join(repos[:5]) + (f" and {len(repos) - 5} more" if len(repos) > 5 else "")
        return [
            f"Reads {len(repos)} {'repository' if len(repos) == 1 else 'repositories'}"
            + (f" ({shown})" if repos else "")
            + " through the GitHub App, with short-lived tokens: files, workflows, and runs.",
            "Opens pull requests on groundcrew/fix-* branches after your approval. Never pushes "
            "to a default branch and never merges.",
        ]
    if conn.kind == "vercel":
        return [
            "Reads project, deployment, and domain names, and env var names. Values only for "
            "plain *_URL and *_HOST variables; encrypted values are never read.",
            "Changes nothing in Vercel. The token is stored encrypted and never shown again.",
        ]
    if conn.kind == "k8s":
        lines = [
            "Reads workloads, pods, events, and logs in the namespaces its service account may "
            "list. Never reads Secret data. The kubeconfig is stored encrypted.",
        ]
        if c.get(CONFIGMAP_CHANGES_KEY):
            lines.append("Lists ConfigMap metadata (never their data) to spot config changes.")
        if "operate" in can:
            namespaces = ", ".join(c.get("operate_namespaces") or []) or "its allowed namespaces"
            lines.append(
                f"Emergency fixes after your approval: rollout undo or restart in {namespaces}."
            )
        deploy = deploy_settings(conn)
        if deploy is not None:
            lines.append(
                f"Deploys after your approval into gc-* namespaces, with images from "
                f"{deploy.registry}."
            )
        return lines
    if conn.kind == "azure":
        return [
            f"Reads resources in resource group {c.get('resource_group') or '?'} with the Reader "
            "role (preview). Changes nothing."
        ]
    return ["Reads metadata only."]
