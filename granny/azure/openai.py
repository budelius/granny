"""Azure Cognitive Services (incl. Azure OpenAI) account and deployment listing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2023-05-01"


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
