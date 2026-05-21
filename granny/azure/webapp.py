"""Azure App Service (Microsoft.Web) site and deployment listing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2023-12-01"


@dataclass
class WebApp:
    subscription: str
    resource_group: str | None
    name: str
    kind: str | None
    location: str | None
    state: str | None
    default_host_name: str | None
    server_farm_id: str | None
    id: str
    raw: dict[str, Any]


@dataclass
class WebAppDeployment:
    subscription: str
    resource_group: str
    app_name: str
    deployment_id: str
    status: int | None
    author: str | None
    message: str | None
    start_time: str | None
    end_time: str | None
    active: bool | None
    raw: dict[str, Any]


def list_web_apps(subscription: str) -> list[WebApp]:
    """List all App Service sites in the subscription."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/providers/Microsoft.Web/sites?api-version={_API_VERSION}"
    )
    return [
        _parse_web_app(subscription=subscription, item=item)
        for item in _client.paginate(url)
    ]


def list_site_deployments(
    subscription: str,
    resource_group: str,
    app_name: str,
) -> list[WebAppDeployment]:
    """List deployment history entries for one App Service site."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Web/sites/{app_name}/deployments"
        f"?api-version={_API_VERSION}"
    )
    return [
        _parse_site_deployment(
            subscription=subscription,
            resource_group=resource_group,
            app_name=app_name,
            item=item,
        )
        for item in _client.paginate(url)
    ]


def _parse_web_app(subscription: str, item: dict[str, Any]) -> WebApp:
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    resource_id = item.get("id", "")
    return WebApp(
        subscription=subscription,
        resource_group=_resource_group_from_id(resource_id),
        name=item.get("name", ""),
        kind=item.get("kind"),
        location=item.get("location"),
        state=properties.get("state"),
        default_host_name=properties.get("defaultHostName"),
        server_farm_id=properties.get("serverFarmId"),
        id=resource_id,
        raw=item,
    )


def _parse_site_deployment(
    subscription: str,
    resource_group: str,
    app_name: str,
    item: dict[str, Any],
) -> WebAppDeployment:
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    return WebAppDeployment(
        subscription=subscription,
        resource_group=resource_group,
        app_name=app_name,
        deployment_id=item.get("name") or properties.get("id", ""),
        status=properties.get("status") if isinstance(properties, dict) else None,
        author=properties.get("author"),
        message=properties.get("message"),
        start_time=properties.get("startTime") or properties.get("start_time"),
        end_time=properties.get("endTime") or properties.get("end_time"),
        active=properties.get("active"),
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
