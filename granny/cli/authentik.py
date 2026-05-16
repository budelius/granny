"""``granny authentik`` — manage an Authentik instance via API.

Auth resolution: ``AUTHENTIK_API_TOKEN`` (env / .env / vault) or
``AK_TOKEN`` (env / .env). See :meth:`AuthentikClient.from_environment`.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from granny.authentik import AuthentikClient, AuthentikError

ADMIN_GROUP_NAME = "dash_admins"


@click.group()
def authentik() -> None:
    """Manage Authentik via its REST API."""


def _client() -> AuthentikClient:
    try:
        return AuthentikClient.from_environment()
    except AuthentikError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


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


@authentik.command("create-user")
@click.argument("username")
@click.option("--name", default=None, help="Display name (defaults to username).")
@click.option("--email", default=None, help="Email address.")
@click.option(
    "--group",
    default=None,
    help="Group to add the new user to after creation (e.g. dash_admins).",
)
@click.option(
    "--password",
    default=None,
    help="Set this password directly. Suppresses --recovery-link.",
)
@click.option(
    "--recovery-link/--no-recovery-link",
    default=True,
    help="Print a one-shot recovery URL the user opens to set their own password (default: yes).",
)
def create_user_cmd(
    username: str,
    name: str | None,
    email: str | None,
    group: str | None,
    password: str | None,
    recovery_link: bool,
) -> None:
    """Create an internal Authentik user (idempotent on username).

    Defaults to printing a one-shot recovery URL so the user sets their
    own password — pass ``--password`` to skip that and set one inline.
    """
    if password is not None:
        recovery_link = False
    client = _client()
    try:
        existed_before = client.find_user_by_username(username) is not None
        user = client.create_user(username, name=name, email=email)
        click.echo(
            f"User {username} {'already exists' if existed_before else 'created'} (pk={user['pk']})"
        )
        if group:
            client.add_user_to_group(username, group)
            click.echo(f"Added {username} to {group}")
        if password is not None:
            client.set_user_password(username, password)
            click.echo(f"Password set for {username}")
        if recovery_link:
            link = client.generate_recovery_link(username)
            click.echo("")
            click.echo("Recovery link (single-use, share over a secure channel):")
            click.echo(f"  {link}")
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)


@authentik.command("recovery-link")
@click.argument("username")
def recovery_link_cmd(username: str) -> None:
    """Mint a one-shot recovery URL the user opens to set a password."""
    client = _client()
    try:
        link = client.generate_recovery_link(username)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    click.echo(link)


@authentik.command("set-password")
@click.argument("username")
@click.option("--password", required=True, help="New password to set.")
def set_password_cmd(username: str, password: str) -> None:
    """Set a user's password directly (admin reset)."""
    client = _client()
    try:
        client.set_user_password(username, password)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Password updated for {username}")


@authentik.command("delete-user")
@click.argument("username")
@click.confirmation_option(prompt="Really delete this Authentik user?")
def delete_user_cmd(username: str) -> None:
    """Delete an Authentik user. No-op if the user doesn't exist."""
    client = _client()
    try:
        client.delete_user(username)
    except AuthentikError as exc:
        click.echo(f"Authentik error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"User {username} deleted (or already absent)")


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
