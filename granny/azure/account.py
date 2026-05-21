"""Azure account and subscription helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_SUBSCRIPTIONS_API_VERSION = "2022-12-01"
_TENANTS_API_VERSION = "2022-12-01"


@dataclass
class AzureIdentity:
    object_id: str | None
    tenant_id: str | None
    user_principal_name: str | None
    display_name: str | None
    app_id: str | None
    identity_type: str | None
    audience: str | None
    raw_claims: dict[str, Any]


@dataclass
class AzureSubscription:
    subscription_id: str
    display_name: str
    state: str | None
    tenant_id: str | None
    raw: dict[str, Any]


@dataclass
class AzureTenant:
    tenant_id: str
    display_name: str | None
    default_domain: str | None
    raw: dict[str, Any]


def get_signed_in_identity() -> AzureIdentity:
    """Return identity info derived from the current ARM access token."""
    token = _client.get_management_token()
    claims = _client.decode_jwt_payload(token)
    return AzureIdentity(
        object_id=claims.get("oid"),
        tenant_id=claims.get("tid"),
        user_principal_name=claims.get("upn") or claims.get("unique_name"),
        display_name=claims.get("name"),
        app_id=claims.get("appid") or claims.get("azp"),
        identity_type=claims.get("idtyp"),
        audience=claims.get("aud"),
        raw_claims=claims,
    )


def list_subscriptions() -> list[AzureSubscription]:
    """List subscriptions visible to the signed-in identity."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions"
        f"?api-version={_SUBSCRIPTIONS_API_VERSION}"
    )
    return [_parse_subscription(item) for item in _client.paginate(url)]


def list_tenants() -> list[AzureTenant]:
    """List tenants visible to the signed-in identity."""
    url = f"{_client.MANAGEMENT_URL}/tenants?api-version={_TENANTS_API_VERSION}"
    return [_parse_tenant(item) for item in _client.paginate(url)]


def _parse_subscription(item: dict[str, Any]) -> AzureSubscription:
    return AzureSubscription(
        subscription_id=item.get("subscriptionId", ""),
        display_name=item.get("displayName", ""),
        state=item.get("state"),
        tenant_id=item.get("tenantId"),
        raw=item,
    )


def _parse_tenant(item: dict[str, Any]) -> AzureTenant:
    return AzureTenant(
        tenant_id=item.get("tenantId", ""),
        display_name=item.get("displayName"),
        default_domain=item.get("defaultDomain"),
        raw=item,
    )
