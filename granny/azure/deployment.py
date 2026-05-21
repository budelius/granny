"""Azure Resource Manager deployment listing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2024-03-01"


@dataclass
class AzureDeployment:
    subscription: str
    resource_group: str | None
    name: str
    provisioning_state: str | None
    timestamp: str | None
    mode: str | None
    template_hash: str | None
    correlation_id: str | None
    id: str
    raw: dict[str, Any]


def list_deployments(
    subscription: str,
    resource_group: str | None = None,
) -> list[AzureDeployment]:
    """List ARM deployments at subscription or resource-group scope."""
    if resource_group:
        url = (
            f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
            f"/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Resources/deployments?api-version={_API_VERSION}"
        )
    else:
        url = (
            f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
            f"/providers/Microsoft.Resources/deployments?api-version={_API_VERSION}"
        )
    return [
        _parse_deployment(subscription=subscription, item=item)
        for item in _client.paginate(url)
    ]


def list_deployments_all_groups(subscription: str) -> list[AzureDeployment]:
    """List ARM deployments across every resource group in a subscription."""
    deployments: list[AzureDeployment] = []
    for group in _list_resource_groups(subscription):
        deployments.extend(list_deployments(subscription, resource_group=group))
    deployments.extend(list_deployments(subscription))
    return deployments


def _list_resource_groups(subscription: str) -> list[str]:
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourcegroups?api-version=2022-09-01"
    )
    return [
        item.get("name", "")
        for item in _client.paginate(url)
        if item.get("name")
    ]


def _parse_deployment(subscription: str, item: dict[str, Any]) -> AzureDeployment:
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    resource_id = item.get("id", "")
    return AzureDeployment(
        subscription=subscription,
        resource_group=_resource_group_from_id(resource_id),
        name=item.get("name", ""),
        provisioning_state=properties.get("provisioningState"),
        timestamp=properties.get("timestamp"),
        mode=properties.get("mode"),
        template_hash=properties.get("templateHash"),
        correlation_id=properties.get("correlationId"),
        id=resource_id,
        raw=item,
    )


def _resource_group_from_id(resource_id: str) -> str | None:
    parts = resource_id.lower().split("/")
    try:
        idx = parts.index("resourcegroups")
    except ValueError:
        return None
    if idx + 1 < len(parts):
        return resource_id.split("/")[idx + 1]
    return None
