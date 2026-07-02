"""Azure resource-group create/list helpers (ARM REST via ``_client``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2021-04-01"


@dataclass
class ResourceGroup:
    subscription: str
    name: str
    location: str | None
    provisioning_state: str | None
    id: str
    raw: dict[str, Any]


def _parse(subscription: str, item: dict[str, Any]) -> ResourceGroup:
    props = item.get("properties", {}) if isinstance(item, dict) else {}
    return ResourceGroup(
        subscription=subscription,
        name=item.get("name", ""),
        location=item.get("location"),
        provisioning_state=props.get("provisioningState") if isinstance(props, dict) else None,
        id=item.get("id", ""),
        raw=item,
    )


def create_resource_group(
    *, subscription: str, name: str, location: str
) -> ResourceGroup:
    """Create (or update) a resource group. Idempotent."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourcegroups/{name}?api-version={_API_VERSION}"
    )
    return _parse(subscription, _client.put(url, {"location": location}))


def list_resource_groups(subscription: str) -> list[ResourceGroup]:
    """List resource groups in the subscription."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourcegroups?api-version={_API_VERSION}"
    )
    return [_parse(subscription, item) for item in _client.paginate(url)]
