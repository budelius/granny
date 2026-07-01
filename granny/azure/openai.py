"""Azure Cognitive Services (incl. Azure OpenAI) account and deployment listing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2023-05-01"
# Deployment create/update needs a newer API version than the read path so the
# GlobalStandard / DataZoneStandard deployment SKUs are accepted.
_DEPLOY_API_VERSION = "2024-10-01"


@dataclass
class CognitiveAccount:
    subscription: str
    resource_group: str | None
    name: str
    kind: str | None
    sku: str | None
    location: str | None
    endpoint: str | None
    id: str
    raw: dict[str, Any]


@dataclass
class CognitiveDeployment:
    subscription: str
    resource_group: str | None
    account_name: str
    name: str
    model_name: str | None
    model_version: str | None
    sku: str | None
    sku_capacity: int | None
    provisioning_state: str | None
    id: str
    raw: dict[str, Any]


def list_accounts(subscription: str) -> list[CognitiveAccount]:
    """List Cognitive Services accounts in the subscription."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/providers/Microsoft.CognitiveServices/accounts"
        f"?api-version={_API_VERSION}"
    )
    return [
        _parse_account(subscription=subscription, item=item)
        for item in _client.paginate(url)
    ]


def list_account_deployments(
    subscription: str,
    resource_group: str,
    account_name: str,
) -> list[CognitiveDeployment]:
    """List model deployments under one Cognitive Services account."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}/deployments"
        f"?api-version={_API_VERSION}"
    )
    return [
        _parse_deployment(
            subscription=subscription,
            resource_group=resource_group,
            account_name=account_name,
            item=item,
        )
        for item in _client.paginate(url)
    ]


def list_all_deployments(
    subscription: str,
    kind_filter: str | None = None,
) -> list[CognitiveDeployment]:
    """List every model deployment across every Cognitive Services account."""
    deployments: list[CognitiveDeployment] = []
    for account in list_accounts(subscription):
        if kind_filter and (account.kind or "").lower() != kind_filter.lower():
            continue
        if not account.resource_group:
            continue
        deployments.extend(
            list_account_deployments(
                subscription=subscription,
                resource_group=account.resource_group,
                account_name=account.name,
            )
        )
    return deployments


def get_account(
    subscription: str,
    resource_group: str,
    account_name: str,
) -> CognitiveAccount:
    """Fetch a single Cognitive Services account (incl. its endpoint)."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        f"?api-version={_API_VERSION}"
    )
    return _parse_account(subscription=subscription, item=_client.get(url))


def get_account_keys(
    subscription: str,
    resource_group: str,
    account_name: str,
) -> dict[str, str]:
    """Return the account's access keys (``{"key1": ..., "key2": ...}``)."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        f"/listKeys?api-version={_API_VERSION}"
    )
    keys = _client.post_json(url)
    return {k: v for k, v in keys.items() if k in ("key1", "key2")}


def create_deployment(
    *,
    subscription: str,
    resource_group: str,
    account_name: str,
    deployment_name: str,
    model_name: str,
    model_version: str | None = None,
    model_format: str = "OpenAI",
    sku_name: str = "GlobalStandard",
    sku_capacity: int = 1,
) -> CognitiveDeployment:
    """Create (or update) an Azure OpenAI model deployment via ARM PUT.

    ``deployment_name`` is the name callers request at inference time;
    ``model_name`` (+ optional ``model_version``) is the underlying model, e.g.
    ``gpt-4o`` / ``2024-08-06``. ``sku_capacity`` is in thousands of TPM for the
    standard SKUs. Idempotent: PUT on an existing deployment updates it.
    """
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        f"/deployments/{deployment_name}"
        f"?api-version={_DEPLOY_API_VERSION}"
    )
    model: dict[str, Any] = {"format": model_format, "name": model_name}
    if model_version:
        model["version"] = model_version
    body = {
        "sku": {"name": sku_name, "capacity": sku_capacity},
        "properties": {"model": model},
    }
    item = _client.put(url, body)
    return _parse_deployment(
        subscription=subscription,
        resource_group=resource_group,
        account_name=account_name,
        item=item,
    )


def _parse_account(subscription: str, item: dict[str, Any]) -> CognitiveAccount:
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    sku = item.get("sku", {}) if isinstance(item, dict) else {}
    resource_id = item.get("id", "")
    return CognitiveAccount(
        subscription=subscription,
        resource_group=_resource_group_from_id(resource_id),
        name=item.get("name", ""),
        kind=item.get("kind"),
        sku=sku.get("name") if isinstance(sku, dict) else None,
        location=item.get("location"),
        endpoint=properties.get("endpoint"),
        id=resource_id,
        raw=item,
    )


def _parse_deployment(
    subscription: str,
    resource_group: str,
    account_name: str,
    item: dict[str, Any],
) -> CognitiveDeployment:
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    model = properties.get("model", {}) if isinstance(properties, dict) else {}
    sku = item.get("sku", {}) if isinstance(item, dict) else {}
    return CognitiveDeployment(
        subscription=subscription,
        resource_group=resource_group,
        account_name=account_name,
        name=item.get("name", ""),
        model_name=model.get("name") if isinstance(model, dict) else None,
        model_version=model.get("version") if isinstance(model, dict) else None,
        sku=sku.get("name") if isinstance(sku, dict) else None,
        sku_capacity=sku.get("capacity") if isinstance(sku, dict) else None,
        provisioning_state=properties.get("provisioningState"),
        id=item.get("id", ""),
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
