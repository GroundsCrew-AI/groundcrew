"""Azure discovery from Resource Graph rows (fixtures until TODO(azure-live))."""

from typing import Any

from app.connectors.azure import ResourceGraph, discovery_query
from app.discovery.types import ConnectorResult, DiscoveredResource, Progress, no_progress

PROVIDER = "azure"

# ARM property paths worth keeping per type (all non-secret). Others keep no properties.
_KEEP: dict[str, tuple[str, ...]] = {
    "microsoft.web/sites": (
        "defaultHostName",
        "httpsOnly",
        "state",
        "outboundIpAddresses",
        "siteConfig.linuxFxVersion",
        "serverFarmId",
    ),
    "microsoft.containerregistry/registries": ("loginServer", "adminUserEnabled"),
    "microsoft.dbforpostgresql/flexibleservers": (
        "fullyQualifiedDomainName",
        "version",
        "state",
        "network.publicNetworkAccess",
    ),
    "microsoft.keyvault/vaults": ("vaultUri", "enableRbacAuthorization"),
    "microsoft.web/serverfarms": ("status", "numberOfWorkers"),
}


def _get(props: dict[str, Any], path: str) -> Any:
    value: Any = props
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def normalize_row(row: dict[str, Any]) -> DiscoveredResource:
    rtype = str(row["type"]).lower()
    props = row.get("properties") or {}
    config: dict[str, Any] = {
        path: _get(props, path) for path in _KEEP.get(rtype, ()) if _get(props, path) is not None
    }
    if row.get("kind"):
        config["kind"] = row["kind"]
    if isinstance(row.get("sku"), dict):
        config["sku"] = row["sku"].get("name")
    image = str(config.get("siteConfig.linuxFxVersion") or "")
    if image.upper().startswith("DOCKER|"):
        config["image"] = image.split("|", 1)[1]
    return DiscoveredResource(
        provider=PROVIDER,
        type=rtype,
        external_id=str(row["id"]).lower(),
        name=str(row["name"]),
        account=row.get("subscriptionId"),
        region=row.get("location"),
        tags={str(k): str(v) for k, v in (row.get("tags") or {}).items()},
        config={"resource_group": row.get("resourceGroup"), **config},
    )


async def discover_azure(
    graph: ResourceGraph,
    subscription_id: str,
    resource_group: str,
    *,
    progress: Progress = no_progress,
    max_resources: int = 2000,
) -> ConnectorResult:
    rows = await graph.query(discovery_query(resource_group), [subscription_id])
    resources = [normalize_row(r) for r in rows]
    truncated = len(resources) > max_resources
    await progress(f"Azure {resource_group}: {len(resources)} resources (Resource Graph)")
    return ConnectorResult(resources=resources[:max_resources], truncated=truncated)
