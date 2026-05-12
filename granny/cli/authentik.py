"""``granny authentik`` — manage the pseekoo Authentik instance via API.

Wraps :mod:`granny.authentik` so the common Authentik ops (per-stage
stoz3n-dash provisioning, group plumbing, secret rotation) work from the
command line without dropping to the admin UI.

Auth resolution: ``AUTHENTIK_API_TOKEN`` (env / .env / vault) or
``AK_TOKEN`` (env / .env). See :meth:`AuthentikClient.from_environment`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from granny.authentik import (
    ADMIN_GROUP_NAME,
    STOZ3N_DASH_STAGES,
    AuthentikClient,
    AuthentikError,
    provision_stoz3n_dash,
)


@click.group()
def authentik() -> None:
    """Manage Authentik (auth.pseekoo.io) via its REST API."""


def _client() -> AuthentikClient:
    try:
        return AuthentikClient.from_environment()
    except AuthentikError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


def _print_provision_report(result: Any) -> None:
    """Pretty-print a ProvisionResult and the paste-ready JSON snippets."""
    click.echo("", err=True)
    click.echo("─" * 72)
    click.echo(f"Stage              : {result.stage}")
    click.echo(f"Authentik URL      : {result.base_url}")
    click.echo(f"Issuer             : {result.issuer}")
    click.echo(f"client_id          : {result.client_id}")
    click.echo(
        f"client_secret      : {result.client_secret or '(not echoed — rotate-secret to see)'}"
    )
    click.echo(f"redirect_uri       : {result.redirect_uri}")
    click.echo(f"admin_group        : {result.admin_group}")
    click.echo(f"scopes             : {result.scopes}")
    flags = [
        f"provider={'NEW' if result.created_provider else 'EXISTS'}",
        f"application={'NEW' if result.created_application else 'EXISTS'}",
        f"binding={'NEW' if result.created_binding else 'EXISTS'}",
        f"group={'NEW' if result.created_group else 'EXISTS'}",
    ]
    click.echo(f"state              : {', '.join(flags)}")
    click.echo("─" * 72)
    click.echo("")
    click.echo(f"# Paste into dotfiles/stoz3n-chat-agent/config.{result.stage}.json")
    click.echo(json.dumps(result.chat_agent_oidc_block(), indent=2))
    click.echo("")
    click.echo(f"# Paste into dotfiles/stoz3n-agent-dash/config.{result.stage}.json")
    click.echo(json.dumps(result.dashboard_oidc_block(), indent=2))
    if not result.client_secret:
        click.echo("", err=True)
        click.echo(
            "WARN: Authentik did not echo client_secret in the response. "
            "Run `granny authentik rotate-secret " + result.application_slug
            + "` to mint a fresh secret you can copy.",
            err=True,
        )


@authentik.command("provision-stoz3n-dash")
@click.argument("stage", type=click.Choice(sorted(STOZ3N_DASH_STAGES.keys())))
@click.option(
    "--rotate",
    is_flag=True,
    help="Force a fresh client_secret if the provider already exists.",
)
def provision_stoz3n_dash_cmd(stage: str, rotate: bool) -> None:
    """End-to-end per-stage stoz3n-dashboard provisioning (idempotent)."""
    client = _client()
    try:
        result = provision_stoz3n_dash(client, stage=stage, rotate_secret=rotate)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    _print_provision_report(result)


_LIST_ENDPOINTS = {
    "providers": "/api/v3/providers/oauth2/",
    "applications": "/api/v3/core/applications/",
    "groups": "/api/v3/core/groups/",
    "flows": "/api/v3/flows/instances/",
    "certs": "/api/v3/crypto/certificatekeypairs/",
    "scopes": "/api/v3/propertymappings/provider/scope/",
    "users": "/api/v3/core/users/",
}

_LIST_COLUMNS = {
    "providers": ("pk", "name", "client_id"),
    "applications": ("slug", "name", "provider"),
    "groups": ("pk", "name", "users_obj"),
    "flows": ("slug", "name", "designation"),
    "certs": ("pk", "name", "private_key_available"),
    "scopes": ("pk", "name", "scope_name"),
    "users": ("pk", "username", "name"),
}


@authentik.command("list")
@click.argument("kind", type=click.Choice(sorted(_LIST_ENDPOINTS)))
@click.option("--json", "as_json", is_flag=True, help="Emit full JSON instead of a table.")
def list_cmd(kind: str, as_json: bool) -> None:
    """List Authentik objects."""
    client = _client()
    items = client.paginate(_LIST_ENDPOINTS[kind])
    if as_json:
        click.echo(json.dumps(items, indent=2, default=str))
        return
    cols = _LIST_COLUMNS[kind]
    for item in items:
        row_parts = []
        for col in cols:
            if col == "users_obj":
                row_parts.append(f"{len(item.get(col, []))} users")
            else:
                row_parts.append(str(item.get(col, ""))[:60])
        click.echo(" | ".join(row_parts))


@authentik.command("get-provider")
@click.argument("name")
def get_provider_cmd(name: str) -> None:
    """Show one OAuth2 provider by name."""
    client = _client()
    provider = client.find_provider_by_name(name)
    if not provider:
        click.echo(f"Provider {name!r} not found", err=True)
        sys.exit(1)
    click.echo(json.dumps(provider, indent=2, default=str))


@authentik.command("rotate-secret")
@click.argument("name")
def rotate_secret_cmd(name: str) -> None:
    """Rotate a provider's client_secret."""
    client = _client()
    try:
        updated = client.rotate_client_secret(name)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    new_secret = updated.get("client_secret") or "(not echoed — view via admin UI)"
    click.echo(f"Rotated. New client_secret: {new_secret}")


@authentik.command("ensure-group")
@click.argument("name", default=ADMIN_GROUP_NAME)
def ensure_group_cmd(name: str) -> None:
    """Create a group if it doesn't exist (defaults to dash_admins)."""
    client = _client()
    existing = client.find_group_by_name(name)
    if existing:
        click.echo(f"Group {name} already exists (pk={existing['pk']})")
        return
    group = client.ensure_group(name)
    click.echo(f"Created group {name} (pk={group['pk']})")


@authentik.command("add-user-to-group")
@click.argument("user")
@click.argument("group", default=ADMIN_GROUP_NAME)
def add_user_to_group_cmd(user: str, group: str) -> None:
    """Add a user to a group by username (group defaults to dash_admins)."""
    client = _client()
    try:
        client.add_user_to_group(user, group)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Added {user} to {group}")


@authentik.command("remove-user-from-group")
@click.argument("user")
@click.argument("group", default=ADMIN_GROUP_NAME)
def remove_user_from_group_cmd(user: str, group: str) -> None:
    """Remove a user from a group by username."""
    client = _client()
    try:
        client.remove_user_from_group(user, group)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Removed {user} from {group}")


@authentik.command("api")
@click.argument("method")
@click.argument("path")
@click.option(
    "--data",
    default=None,
    help="JSON body, or '-' to read from stdin.",
)
def api_cmd(method: str, path: str, data: str | None) -> None:
    """Generic API call — for anything not covered by the named commands."""
    client = _client()
    body: Any = None
    if data is not None:
        raw = sys.stdin.read() if data == "-" else data
        body = json.loads(raw) if raw.strip() else None
    try:
        result = client.request(method, path, body=body)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    if result is None:
        click.echo("(no content)", err=True)
    else:
        click.echo(json.dumps(result, indent=2, default=str))
