"""Azure quota inspection and request helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

_API_VERSION = "2023-02-01"
_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
_MANAGEMENT_URL = "https://management.azure.com"


@dataclass
class AzureQuotaLimit:
    subscription: str
    location: str
    resource_name: str
    display_name: str
    limit: int | float | None
    unit: str | None
    raw: dict[str, Any]


@dataclass
class AzureQuotaRequest:
    subscription: str
    location: str
    resource_name: str
    requested_limit: int
    status_code: int
    provisioning_state: str | None
    operation_url: str | None
    raw: dict[str, Any]


def list_compute_quotas(
    subscription: str,
    location: str,
    filter_text: str | None = None,
) -> list[AzureQuotaLimit]:
    """List Microsoft.Compute quota limits in an Azure region."""
    data = _get(
        _quota_collection_url(subscription=subscription, location=location),
    )
    rows = [
        _parse_quota_limit(subscription=subscription, location=location, item=item)
        for item in data.get("value", [])
    ]
    if filter_text:
        needle = filter_text.lower()
        rows = [
            row
            for row in rows
            if needle in row.resource_name.lower() or needle in row.display_name.lower()
        ]
    return rows


def get_compute_quota(
    subscription: str,
    location: str,
    resource_name: str,
) -> AzureQuotaLimit:
    """Get one Microsoft.Compute quota limit in an Azure region."""
    data = _get(
        _quota_resource_url(
            subscription=subscription,
            location=location,
            resource_name=resource_name,
        ),
    )
    return _parse_quota_limit(subscription=subscription, location=location, item=data)


def request_compute_quota(
    subscription: str,
    location: str,
    resource_name: str,
    limit: int,
) -> AzureQuotaRequest:
    """Request/update one Microsoft.Compute quota limit in an Azure region."""
    url = _quota_resource_url(
        subscription=subscription,
        location=location,
        resource_name=resource_name,
    )
    body = {
        "properties": {
            "limit": {
                "limitObjectType": "LimitValue",
                "value": limit,
            },
            "name": {
                "value": resource_name,
            },
            "resourceType": "dedicated",
        }
    }
    response = requests.patch(url, headers=_headers(), json=body, timeout=60)
    if response.status_code not in {200, 201, 202}:
        _raise_response_error("PATCH", url, response)

    try:
        data = response.json()
    except ValueError:
        data = {}

    properties = data.get("properties", {}) if isinstance(data, dict) else {}
    return AzureQuotaRequest(
        subscription=subscription,
        location=location,
        resource_name=resource_name,
        requested_limit=limit,
        status_code=response.status_code,
        provisioning_state=properties.get("provisioningState"),
        operation_url=(
            response.headers.get("Azure-AsyncOperation")
            or response.headers.get("Location")
            or response.headers.get("Operation-Location")
        ),
        raw=data if isinstance(data, dict) else {},
    )


def _quota_collection_url(subscription: str, location: str) -> str:
    return (
        f"{_MANAGEMENT_URL}/subscriptions/{subscription}/providers/Microsoft.Compute"
        f"/locations/{location}/providers/Microsoft.Quota/quotas"
        f"?api-version={_API_VERSION}"
    )


def _quota_resource_url(subscription: str, location: str, resource_name: str) -> str:
    return (
        f"{_MANAGEMENT_URL}/subscriptions/{subscription}/providers/Microsoft.Compute"
        f"/locations/{location}/providers/Microsoft.Quota/quotas/{resource_name}"
        f"?api-version={_API_VERSION}"
    )


def _get(url: str) -> dict[str, Any]:
    response = requests.get(url, headers=_headers(), timeout=60)
    if response.status_code != 200:
        _raise_response_error("GET", url, response)
    return response.json()


def _headers() -> dict[str, str]:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as e:
        raise RuntimeError(
            "Azure quota support requires the [azure] extra: "
            "pip install 'granny-devops[azure]'"
        ) from e

    credential = DefaultAzureCredential()
    token = credential.get_token(_MANAGEMENT_SCOPE).token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _parse_quota_limit(
    subscription: str,
    location: str,
    item: dict[str, Any],
) -> AzureQuotaLimit:
    properties = item.get("properties", {})
    name = properties.get("name", {})
    limit = properties.get("limit", {})
    resource_name = _resource_name(item, properties, name)
    display_name = name.get("localizedValue") or resource_name
    return AzureQuotaLimit(
        subscription=subscription,
        location=location,
        resource_name=resource_name,
        display_name=display_name,
        limit=limit.get("value"),
        unit=properties.get("unit"),
        raw=item,
    )


def _resource_name(
    item: dict[str, Any],
    properties: dict[str, Any],
    name: dict[str, Any],
) -> str:
    for candidate in (
        name.get("value"),
        properties.get("resourceName"),
        item.get("name"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate.split("/")[-1]
    return ""


def _raise_response_error(method: str, url: str, response: requests.Response) -> None:
    snippet = response.text[:1000]
    raise RuntimeError(f"Azure quota {method} {url} failed: {response.status_code} {snippet}")
