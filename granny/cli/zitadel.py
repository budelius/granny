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
@click.option(
    "--dry-run", is_flag=True, help="Show what would be created, change nothing."
)
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
    "--redirect-uri",
    "redirect_uris",
    multiple=True,
    required=True,
    help="Allowed OIDC redirect URI (repeatable).",
)
@click.option(
    "--post-logout-uri",
    "post_logout_uris",
    multiple=True,
    help="Post-logout redirect URI (repeatable).",
)
@click.option(
    "--public",
    is_flag=True,
    help="Public client (PKCE, no secret) instead of confidential.",
)
@click.option(
    "--dev-mode", is_flag=True, help="Enable dev mode (allow non-https redirect URIs)."
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be created, change nothing."
)
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
                {
                    "app": name,
                    "appId": existing.get("id"),
                    "note": "already exists (secret not re-shown)",
                },
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
@click.option(
    "--no-passkey",
    is_flag=True,
    help="Do not enable passwordless passkey (WebAuthn MFA).",
)
@click.option("--no-totp", is_flag=True, help="Do not enable authenticator-app TOTP.")
@click.option(
    "--no-security-key",
    is_flag=True,
    help="Do not enable security-key (U2F) 2nd factor.",
)
@click.option("--dry-run", is_flag=True, help="Show what would change, change nothing.")
def enforce_mfa_cmd(
    no_passkey: bool, no_totp: bool, no_security_key: bool, dry_run: bool
) -> None:
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


# ── resolution helpers ───────────────────────────────────────────────────


def _resolve_project(client: ZitadelClient, project: str) -> str:
    """Treat PROJECT as a name first, fall back to using it as an id."""
    found = client.find_project(project)
    return found["id"] if found else project


def _resolve_user(client: ZitadelClient, user: str) -> str:
    """Treat USER as a numeric user id, else resolve by userName."""
    if user.isdigit():
        return user
    found = client.find_user(user)
    if not found:
        click.echo(f"user {user!r} not found", err=True)
        sys.exit(1)
    return found.get("id") or found.get("userId")


# ── users ────────────────────────────────────────────────────────────────


@zitadel.command("create-user")
@click.argument("username")
@click.option("--email", default=None, help="Email address.")
@click.option("--first-name", default="", help="Given name.")
@click.option("--last-name", default="", help="Family name.")
@click.option(
    "--display-name", default=None, help="Display name (defaults to first+last)."
)
@click.option(
    "--password", default=None, help="Initial password (omit to leave unset)."
)
@click.option(
    "--email-verified", is_flag=True, help="Mark the email as already verified."
)
@click.option(
    "--password-change-required",
    is_flag=True,
    help="Force a password change on first login.",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be created, change nothing."
)
def create_user_cmd(
    username: str,
    email: str | None,
    first_name: str,
    last_name: str,
    display_name: str | None,
    password: str | None,
    email_verified: bool,
    password_change_required: bool,
    dry_run: bool,
) -> None:
    """Create a human user USERNAME (idempotent)."""
    if dry_run:
        click.echo(f"[DRY RUN] Would create user {username!r} (email={email})")
        return
    client = _client()
    out = client.create_user(
        username,
        email=email,
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        password=password,
        email_verified=email_verified,
        password_change_required=password_change_required,
    )
    click.echo(
        json.dumps(
            {"username": username, "userId": out.get("userId") or out.get("id")},
            indent=2,
        )
    )


@zitadel.command("set-user-password")
@click.argument("user")
@click.option("--password", required=True, help="New password.")
@click.option(
    "--change-required", is_flag=True, help="Force a password change on next login."
)
@click.option("--dry-run", is_flag=True, help="Change nothing.")
def set_user_password_cmd(
    user: str, password: str, change_required: bool, dry_run: bool
) -> None:
    """Admin-set USER's password (USER = userName or id). No recovery email is sent.

    Note: Zitadel has no Authentik-style one-shot recovery link — set the
    password directly here, or have the user use the self-service reset flow.
    """
    if dry_run:
        click.echo(f"[DRY RUN] Would set password for user {user!r}")
        return
    client = _client()
    client.set_user_password(
        _resolve_user(client, user), password, change_required=change_required
    )
    click.echo(json.dumps({"user": user, "passwordSet": True}, indent=2))


@zitadel.command("delete-user")
@click.argument("user")
@click.option("--dry-run", is_flag=True, help="Change nothing.")
def delete_user_cmd(user: str, dry_run: bool) -> None:
    """Delete USER (userName or id)."""
    if dry_run:
        click.echo(f"[DRY RUN] Would delete user {user!r}")
        return
    client = _client()
    client.delete_user(_resolve_user(client, user))
    click.echo(json.dumps({"user": user, "deleted": True}, indent=2))


# ── list ─────────────────────────────────────────────────────────────────


@zitadel.command("list")
@click.argument("kind", type=click.Choice(["users", "projects", "apps", "roles"]))
@click.option(
    "--project", default=None, help="Project (id or name) — required for apps/roles."
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit full JSON instead of a table."
)
def list_cmd(kind: str, project: str | None, as_json: bool) -> None:
    """List Zitadel objects of KIND (users | projects | apps | roles)."""
    client = _client()
    if kind == "users":
        rows = client.list_users()
        cols = ("id", "userName", "state")
    elif kind == "projects":
        rows = client.list_projects()
        cols = ("id", "name", "state")
    else:
        if not project:
            click.echo(f"--project is required for `list {kind}`", err=True)
            sys.exit(1)
        pid = _resolve_project(client, project)
        rows = client.list_apps(pid) if kind == "apps" else client.list_roles(pid)
        cols = (
            ("id", "name", "state")
            if kind == "apps"
            else ("key", "displayName", "group")
        )
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    click.echo("  ".join(cols))
    for row in rows:
        click.echo("  ".join(str(row.get(c, "")) for c in cols))


# ── apps ─────────────────────────────────────────────────────────────────


@zitadel.command("get-app")
@click.argument("project")
@click.argument("app_id")
def get_app_cmd(project: str, app_id: str) -> None:
    """Show one app (PROJECT = id or name, APP_ID = the app id)."""
    client = _client()
    click.echo(
        json.dumps(
            client.get_app(_resolve_project(client, project), app_id),
            indent=2,
            default=str,
        )
    )


@zitadel.command("rotate-secret")
@click.argument("project")
@click.argument("app")
@click.option("--dry-run", is_flag=True, help="Change nothing.")
def rotate_secret_cmd(project: str, app: str, dry_run: bool) -> None:
    """Regenerate an OIDC app's client secret (APP = app name or id). Shown once."""
    if dry_run:
        click.echo(
            f"[DRY RUN] Would rotate client secret for app {app!r} in {project!r}"
        )
        return
    client = _client()
    pid = _resolve_project(client, project)
    found = client.find_oidc_app(pid, app)
    app_id = found["id"] if found else app
    result = client.rotate_app_secret(pid, app_id)
    click.echo(
        json.dumps(
            {"appId": app_id, "clientSecret": result.get("clientSecret")}, indent=2
        )
    )


# ── roles + grants (Zitadel's analog to Authentik groups) ────────────────


@zitadel.command("ensure-role")
@click.argument("project")
@click.argument("role_key")
@click.option("--display-name", default=None, help="Human-readable role name.")
@click.option("--group", default="", help="Optional role group bucket.")
@click.option("--dry-run", is_flag=True, help="Change nothing.")
def ensure_role_cmd(
    project: str, role_key: str, display_name: str | None, group: str, dry_run: bool
) -> None:
    """Ensure a project ROLE_KEY exists (idempotent). Roles are Zitadel's group analog."""
    if dry_run:
        click.echo(f"[DRY RUN] Would ensure role {role_key!r} in project {project!r}")
        return
    client = _client()
    client.ensure_role(
        _resolve_project(client, project),
        role_key,
        display_name=display_name,
        group=group,
    )
    click.echo(json.dumps({"project": project, "role": role_key}, indent=2))


@zitadel.command("grant-user-role")
@click.argument("project")
@click.argument("user")
@click.argument("role_keys", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Change nothing.")
def grant_user_role_cmd(
    project: str, user: str, role_keys: tuple[str, ...], dry_run: bool
) -> None:
    """Grant ROLE_KEYS on PROJECT to USER (userName or id). Idempotent."""
    if dry_run:
        click.echo(
            f"[DRY RUN] Would grant {list(role_keys)} on {project!r} to {user!r}"
        )
        return
    client = _client()
    pid = _resolve_project(client, project)
    result = client.grant_user_role(pid, _resolve_user(client, user), list(role_keys))
    click.echo(json.dumps(result, indent=2, default=str))


@zitadel.command("revoke-user-role")
@click.argument("project")
@click.argument("user")
@click.option("--dry-run", is_flag=True, help="Change nothing.")
def revoke_user_role_cmd(project: str, user: str, dry_run: bool) -> None:
    """Revoke USER's grant on PROJECT (no-op if absent)."""
    if dry_run:
        click.echo(f"[DRY RUN] Would revoke {user!r}'s grant on {project!r}")
        return
    client = _client()
    client.revoke_user_role(
        _resolve_project(client, project), _resolve_user(client, user)
    )
    click.echo(
        json.dumps({"project": project, "user": user, "revoked": True}, indent=2)
    )
