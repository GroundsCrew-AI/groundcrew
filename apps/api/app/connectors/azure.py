"""Azure: a service principal with Reader on one resource group, discovery via Resource Graph.

Status: fixtures only. The demo Azure subscription is not ready, so `LiveResourceGraph` is not
implemented (TODO(azure-live)); tests and previews use `FixtureResourceGraph`. The connection
form stores the tenant, client, subscription, and resource group as config and the client
secret encrypted (app.security.crypto). infra/azure-connect.bicep grants Reader on the group.

Resource Graph returns ARM properties only. App settings, connection strings, and Key Vault
secrets are not ARM properties, so no secret value can arrive this way; we still never ask
for them (no `list*` actions such as Microsoft.Web/sites/config/list).
"""

import json
from pathlib import Path
from typing import Any, Protocol

from app.discovery.types import ConnectorError

# The only query Groundcrew sends: everything in the connected resource group.
DISCOVERY_KQL = (
    "Resources"
    " | where resourceGroup =~ '{resource_group}'"
    " | project id, name, type, kind, location, resourceGroup, subscriptionId, tags, sku,"
    " properties"
)

# Azure actions the service principal may use (all reads). Writes and `list*` secret actions
# are excluded on purpose; tests check this list.
AZURE_READ_ACTIONS: frozenset[str] = frozenset(
    {
        "Microsoft.ResourceGraph/resources/read",
        "Microsoft.Resources/subscriptions/resourceGroups/read",
        "Microsoft.Web/sites/read",
        "Microsoft.Web/serverfarms/read",
        "Microsoft.ContainerRegistry/registries/read",
        "Microsoft.DBforPostgreSQL/flexibleServers/read",
        "Microsoft.KeyVault/vaults/read",
        "Microsoft.Insights/metrics/read",
        "Microsoft.Insights/diagnosticSettings/read",
        "Microsoft.OperationalInsights/workspaces/read",
    }
)


class ResourceGraph(Protocol):
    async def query(self, kql: str, subscriptions: list[str]) -> list[dict[str, Any]]: ...


class FixtureResourceGraph:
    """Rows recorded from Resource Graph (or written to match its shape)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> "FixtureResourceGraph":
        return cls(json.loads(path.read_text())["data"])

    async def query(self, kql: str, subscriptions: list[str]) -> list[dict[str, Any]]:
        self.queries.append(kql)
        return [r for r in self.rows if r.get("subscriptionId") in subscriptions]


class LiveResourceGraph:
    """TODO(azure-live): azure-identity ClientSecretCredential (or a federated credential) and
    azure-mgmt-resourcegraph `ResourceGraphClient.resources(QueryRequest(...))`, paging with
    `skip_token`. Not built until the demo subscription exists."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._client_secret = client_secret

    async def query(self, kql: str, subscriptions: list[str]) -> list[dict[str, Any]]:
        raise ConnectorError(
            "Azure discovery is in preview: live Resource Graph queries are not enabled yet."
        )


def discovery_query(resource_group: str) -> str:
    if not resource_group.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ConnectorError("Resource group names use letters, digits, - _ . only.")
    return DISCOVERY_KQL.format(resource_group=resource_group)
