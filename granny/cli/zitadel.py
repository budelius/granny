"""``granny zitadel`` — manage a self-hosted Zitadel OIDC server via its API.

Mirrors :mod:`granny.cli.authentik`. Authenticates a machine user via the
JWT-profile grant (service-account key) resolved through granny's normal
env/vault pipeline. See :class:`granny.zitadel.ZitadelClient`.
"""

from __future__ import annotations

import json
import sys

import click

from granny.zitadel import ZitadelClient, ZitadelError


def _client() -> ZitadelClient:
    try:
        return ZitadelClient.from_environment()
    except ZitadelError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


@click.group()
def zitadel() -> None:
    """Manage Zitadel (projects, OIDC apps, MFA policy) via its REST API."""


@zitadel.command("create-project")
@click.argument("name")
@click.option("--dry-run", is_flag=True, help="Show what would be created, change nothing.")
def create_project_cmd(name: str, dry_run: bool) -> None:
    """Ensure a project named NAME exists; print its id (idempotent)."""
    if dry_run:
        click.echo(f"[DRY RUN] Would ensure project: {name}")
        return
    client = _client()
    project_id = client.ensure_project(name)
    click.echo(json.dumps({"project": name, "id": project_id}, indent=2))


@zitadel.command("create-oidc-app")
@click.argument("project")
@click.argument("name")
@click.option(
    "--redirect-uri", "redirect_uris", multiple=True, required=True,
    help="Allowed OIDC redirect URI (repeatable).",
)
@click.option(
    "--post-logout-uri", "post_logout_uris", multiple=True,
    help="Post-logout redirect URI (repeatable).",
)
@click.option("--public", is_flag=True, help="Public client (PKCE, no secret) instead of confidential.")
@click.option("--dev-mode", is_flag=True, help="Enable dev mode (allow non-https redirect URIs).")
@click.option("--dry-run", is_flag=True, help="Show what would be created, change nothing.")
def create_oidc_app_cmd(
    project: str,
    name: str,
    redirect_uris: tuple[str, ...],
    post_logout_uris: tuple[str, ...],
    public: bool,
    dev_mode: bool,
    dry_run: bool,
) -> None:
    """Create an OIDC web app NAME in PROJECT (auth-code + refresh).

    PROJECT may be a project id or name. Prints clientId/clientSecret — the
    secret is only returned at creation time, so capture it now.
    """
    if dry_run:
        click.echo(
            f"[DRY RUN] Would create {'public' if public else 'confidential'} OIDC app "
            f"{name!r} in project {project!r} with redirects {list(redirect_uris)}"
        )
        return
    client = _client()
    # Resolve project: treat as name first, fall back to using it as an id.
    found = client.find_project(project)
    project_id = found["id"] if found else project
    existing = client.find_oidc_app(project_id, name)
    if existing:
        click.echo(
            json.dumps(
                {"app": name, "appId": existing.get("id"), "note": "already exists (secret not re-shown)"},
                indent=2,
            )
        )
        return
    result = client.create_oidc_app(
        project_id,
        name,
        list(redirect_uris),
        post_logout_uris=list(post_logout_uris),
        confidential=not public,
        dev_mode=dev_mode,
    )
    click.echo(
        json.dumps(
            {
                "app": name,
                "appId": result.get("appId"),
                "clientId": result.get("clientId"),
                "clientSecret": result.get("clientSecret"),
            },
            indent=2,
        )
    )


@zitadel.command("enforce-mfa")
@click.option("--no-passkey", is_flag=True, help="Do not enable passwordless passkey (WebAuthn MFA).")
@click.option("--no-totp", is_flag=True, help="Do not enable authenticator-app TOTP.")
@click.option("--no-security-key", is_flag=True, help="Do not enable security-key (U2F) 2nd factor.")
@click.option("--dry-run", is_flag=True, help="Show what would change, change nothing.")
def enforce_mfa_cmd(no_passkey: bool, no_totp: bool, no_security_key: bool, dry_run: bool) -> None:
    """Force MFA on the instance login policy and enable passkey + TOTP factors."""
    if dry_run:
        click.echo(
            "[DRY RUN] Would set forceMfa=true and enable: "
            f"passkey={not no_passkey}, totp={not no_totp}, security_key={not no_security_key}"
        )
        return
    client = _client()
    result = client.enforce_mfa(
        passkey=not no_passkey, totp=not no_totp, security_key=not no_security_key
    )
    click.echo(json.dumps(result, indent=2))
