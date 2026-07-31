"""OAuth provisioning commands."""

from __future__ import annotations

from pathlib import Path

import click

from granny.oauth import (
    ProductivityOAuthCredentials,
    load_google_client_json,
    provision_productivity_oauth,
    update_env_file,
)


@click.group()
def oauth() -> None:
    """Provision OAuth clients for applications."""


@oauth.command("provision-productivity")
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Dashboard dotenv file to update.",
)
@click.option(
    "--obachan-env",
    default=None,
    help="Obachan environment whose OpenBao application secret is updated.",
)
@click.option(
    "--obachan-app",
    default="dashboard",
    show_default=True,
    help="Application name used in the Obachan secret path.",
)
@click.option(
    "--google-client-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="OAuth client JSON downloaded from Google Cloud.",
)
@click.option("--google-client-id", default=None, help="Google OAuth client ID.")
@click.option(
    "--google-client-secret",
    default=None,
    help="Google OAuth client secret (not echoed).",
)
@click.option("--azure-client-id", default=None, help="Microsoft Entra client ID.")
@click.option(
    "--azure-client-secret",
    default=None,
    help="Microsoft Entra client secret (not echoed).",
)
@click.option(
    "--dry-run", is_flag=True, help="Validate and show destinations without writing."
)
def provision_productivity(
    env_file: Path | None,
    obachan_env: str | None,
    obachan_app: str,
    google_client_json: Path | None,
    google_client_id: str | None,
    google_client_secret: str | None,
    azure_client_id: str | None,
    azure_client_secret: str | None,
    dry_run: bool,
) -> None:
    """Add Google and Microsoft mail/calendar OAuth clients.

    Set OBACHAN_BAO_ADDR and OBACHAN_BAO_TOKEN when --obachan-env is used.
    Google Cloud OAuth client downloads can be passed with --google-client-json.
    """
    if google_client_json:
        if google_client_id or google_client_secret:
            raise click.UsageError(
                "Use --google-client-json or explicit Google values, not both."
            )
        try:
            google_client_id, google_client_secret = load_google_client_json(
                google_client_json
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    credentials = ProductivityOAuthCredentials(
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        azure_client_id=azure_client_id,
        azure_client_secret=azure_client_secret,
    )
    try:
        result = provision_productivity_oauth(
            credentials,
            env_file=env_file,
            obachan_env=obachan_env,
            obachan_app=obachan_app,
            dry_run=dry_run,
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    for key in result.keys:
        click.echo(f"would set {key}" if dry_run else f"set {key}")
    if result.env_file:
        action = "Would update" if dry_run else "Updated"
        click.echo(f"{action} {result.env_file}")
    if result.obachan_path:
        action = "Would update" if dry_run else "Updated"
        click.echo(f"{action} OpenBao path {result.obachan_path}")
    if dry_run:
        click.echo("Dry run; no destinations written.")


@oauth.command("configure-productivity-local")
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=Path(".deploy.env"),
    show_default=True,
)
@click.option("--google-client-id", default=None)
@click.option("--google-client-secret", default=None)
@click.option("--azure-client-id", default=None)
@click.option("--azure-client-secret", default=None)
@click.option("--dry-run", is_flag=True, help="Validate without writing the env file.")
def configure_productivity_local(
    env_file: Path,
    google_client_id: str | None,
    google_client_secret: str | None,
    azure_client_id: str | None,
    azure_client_secret: str | None,
    dry_run: bool,
) -> None:
    """Configure a local dashboard (compatibility alias)."""
    credentials = ProductivityOAuthCredentials(
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        azure_client_id=azure_client_id,
        azure_client_secret=azure_client_secret,
    )
    values = credentials.as_env()
    if not values:
        raise click.UsageError("Provide at least one client id or secret value.")
    if not dry_run:
        update_env_file(env_file, values)
    click.echo(f"Env file: {env_file}")
    for key in sorted(values):
        value = values[key]
        display = _mask(value) if key.endswith("SECRET") else value
        click.echo(f"  set {key}={display}")
    if dry_run:
        click.echo("Dry run; no file written.")
    else:
        click.echo("Updated local OAuth configuration.")


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"
