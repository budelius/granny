"""Azure RBAC role assignment helpers.

Lets you grant a principal (user / group / service principal) a built-in role
at subscription or resource scope -- e.g. give your own account "Cost
Management Reader" and "Reader" on a subscription you currently can't see.

Assigning a role requires the caller to hold "Owner" or "User Access
Administrator" on the target scope; otherwise ARM returns AuthorizationFailed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2022-04-01"


@dataclass
class RoleAssignment:
    name: str
    scope: str | None
    role_definition_id: str | None
    role_name: str | None
    principal_id: str | None
    principal_type: str | None
    id: str
    raw: dict[str, Any]


@dataclass
class RoleDefinition:
    name: str
    role_name: str | None
    id: str
    description: str | None


def _scope(subscription: str, scope: str | None) -> str:
    """Resolve an explicit scope or default to the whole subscription."""
    if scope:
        return scope.lstrip("/")
    return f"subscriptions/{subscription}"


def resolve_role_definition(subscription: str, role_name: str) -> RoleDefinition:
    """Look up a built-in role definition by its display name.

    Raises KeyError if no role with that exact name exists in the scope.
    """
    scope = f"subscriptions/{subscription}"
    quoted = role_name.replace("'", "''")
    url = (
        f"{_client.MANAGEMENT_URL}/{scope}"
        f"/providers/Microsoft.Authorization/roleDefinitions"
        f"?api-version={_API_VERSION}&$filter=roleName eq '{quoted}'"
    )
    for item in _client.paginate(url):
        props = item.get("properties", {}) or {}
        return RoleDefinition(
            name=item.get("name", ""),
            role_name=props.get("roleName"),
            id=item.get("id", ""),
            description=props.get("description"),
        )
    raise KeyError(f"No Azure role definition named {role_name!r}")


def list_role_assignments(
    subscription: str,
    *,
    principal_id: str | None = None,
    scope: str | None = None,
) -> list[RoleAssignment]:
    """List role assignments at a scope, optionally filtered to one principal."""
    resolved = _scope(subscription, scope)
    url = (
        f"{_client.MANAGEMENT_URL}/{resolved}"
        f"/providers/Microsoft.Authorization/roleAssignments"
        f"?api-version={_API_VERSION}"
    )
    if principal_id:
        url += f"&$filter=principalId eq '{principal_id}'"
    return [_parse_assignment(item) for item in _client.paginate(url)]


def assign_role(
    subscription: str,
    role_name: str,
    principal_id: str,
    *,
    scope: str | None = None,
    principal_type: str = "User",
) -> RoleAssignment:
    """Grant ``principal_id`` the built-in ``role_name`` at ``scope``.

    ``scope`` defaults to the whole subscription. ``principal_type`` is one of
    User / Group / ServicePrincipal and lets ARM skip a directory lookup.
    """
    definition = resolve_role_definition(subscription, role_name)
    resolved = _scope(subscription, scope)
    assignment_name = str(uuid.uuid4())
    url = (
        f"{_client.MANAGEMENT_URL}/{resolved}"
        f"/providers/Microsoft.Authorization/roleAssignments/{assignment_name}"
        f"?api-version={_API_VERSION}"
    )
    body = {
        "properties": {
            "roleDefinitionId": definition.id,
            "principalId": principal_id,
            "principalType": principal_type,
        }
    }
    result = _client.put(url, body)
    return _parse_assignment(result)


def _parse_assignment(item: dict[str, Any]) -> RoleAssignment:
    props = item.get("properties", {}) if isinstance(item, dict) else {}
    role_def_id = props.get("roleDefinitionId")
    return RoleAssignment(
        name=item.get("name", ""),
        scope=props.get("scope"),
        role_definition_id=role_def_id,
        role_name=role_def_id.split("/")[-1] if role_def_id else None,
        principal_id=props.get("principalId"),
        principal_type=props.get("principalType"),
        id=item.get("id", ""),
        raw=item,
    )
