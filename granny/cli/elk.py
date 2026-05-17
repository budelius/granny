"""``granny elk`` — manage Elasticsearch / Kibana users."""

from __future__ import annotations

import getpass
import json
import sys
from typing import Any

import click

from granny.elk.client import (
    ElasticsearchSecurityClient,
    ElasticsearchSecurityError,
    generate_password as generate_elk_password,
)


@click.group()
def elk() -> None:
    """Manage Elasticsearch / Kibana security objects."""


def _client(
    url: str | None,
    admin_username: str | None,
    admin_password: str | None,
    api_key: str | None,
    verify_tls: bool,
) -> ElasticsearchSecurityClient:
    try:
        return ElasticsearchSecurityClient.from_environment(
            base_url=url,
            username=admin_username,
            password=admin_password,
            api_key=api_key,
            verify_tls=verify_tls,
        )
    except ElasticsearchSecurityError as exc:
        raise click.ClickException(str(exc)) from exc


_COMMON_OPTIONS = [
    click.option("--url", default=None, help="Elasticsearch URL (env: ELASTICSEARCH_URL)."),
    click.option(
        "--admin-username",
        default=None,
        help="Admin username (env/vault: ELASTICSEARCH_USERNAME).",
    ),
    click.option(
        "--admin-password",
        default=None,
        help="Admin password (env/vault: ELASTICSEARCH_PASSWORD).",
    ),
    click.option(
        "--api-key",
        default=None,
        help="Elasticsearch API key (env/vault: ELASTICSEARCH_API_KEY).",
    ),
    click.option("--verify-tls/--no-verify-tls", default=True, show_default=True),
]


def common_options(func: Any) -> Any:
    for option in reversed(_COMMON_OPTIONS):
        func = option(func)
    return func


@elk.command("add-user")
@click.argument("username")
@click.option("--email", default=None, help="User email address.")
@click.option("--full-name", default=None, help="User display name.")
@click.option(
    "--role",
    "roles",
    multiple=True,
    required=True,
    help="Elasticsearch role. Repeat for multiple roles, e.g. --role kibana_admin.",
)
@click.option("--password", default=None, help="Initial password. Prompts if omitted.")
@click.option(
    "--generate-password",
    is_flag=True,
    help="Generate and print a random initial password instead of prompting.",
)
@click.option("--disabled", is_flag=True, help="Create/update the user as disabled.")
@click.option(
    "--metadata",
    default=None,
    help="Optional JSON object for Elasticsearch user metadata.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON result.")
@common_options
def add_user_cmd(
    username: str,
    email: str | None,
    full_name: str | None,
    roles: tuple[str, ...],
    password: str | None,
    generate_password: bool,
    disabled: bool,
    metadata: str | None,
    as_json: bool,
    url: str | None,
    admin_username: str | None,
    admin_password: str | None,
    api_key: str | None,
    verify_tls: bool,
) -> None:
    """Create or update an Elasticsearch native user for Kibana/ELK access."""
    if password and generate_password:
        raise click.ClickException("Use either --password or --generate-password, not both")
    generated_password: str | None = None
    if generate_password:
        generated_password = generate_elk_password()
        password = generated_password
    elif password is None:
        password = getpass.getpass(f"Initial password for {username}: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise click.ClickException("Passwords do not match")

    parsed_metadata: dict[str, Any] | None = None
    if metadata:
        try:
            raw_metadata = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Invalid --metadata JSON: {exc}") from exc
        if not isinstance(raw_metadata, dict):
            raise click.ClickException("--metadata must be a JSON object")
        parsed_metadata = raw_metadata

    client = _client(url, admin_username, admin_password, api_key, verify_tls)
    try:
        existed_before = client.get_user(username) is not None
        result = client.put_user(
            username,
            password=password,
            roles=roles,
            full_name=full_name,
            email=email,
            enabled=not disabled,
            metadata=parsed_metadata,
        )
    except ElasticsearchSecurityError as exc:
        raise click.ClickException(str(exc)) from exc

    out = {
        "username": username,
        "created": not existed_before,
        "updated": existed_before,
        "roles": list(roles),
        "enabled": not disabled,
        "result": result,
    }
    if generated_password is not None:
        out["password"] = generated_password
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    click.echo(f"{'Created' if not existed_before else 'Updated'} ELK user {username}")
    click.echo(f"Roles: {', '.join(roles)}")
    if generated_password is not None:
        click.echo(f"Password: {generated_password}")

@elk.command("get-user")
@click.argument("username")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON result.")
@common_options
def get_user_cmd(
    username: str,
    as_json: bool,
    url: str | None,
    admin_username: str | None,
    admin_password: str | None,
    api_key: str | None,
    verify_tls: bool,
) -> None:
    """Show an Elasticsearch native user."""
    client = _client(url, admin_username, admin_password, api_key, verify_tls)
    try:
        user = client.get_user(username)
    except ElasticsearchSecurityError as exc:
        raise click.ClickException(str(exc)) from exc
    if user is None:
        click.echo(f"User {username!r} not found", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(user, indent=2, default=str))
        return
    click.echo(f"{username} | roles={','.join(user.get('roles', []))} | enabled={user.get('enabled')}")
