"""OAuth configuration commands."""

from __future__ import annotations

from pathlib import Path

import click


@click.group()
def oauth() -> None:
    """Configure OAuth client settings for local apps."""


@oauth.command("configure-productivity-local")
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=Path(".deploy.env"),
    show_default=True,
    help="Env file to update in the current project.",
)
@click.option("--google-client-id", default=None, help="Google OAuth client ID.")
@click.option("--google-client-secret", default=None, help="Google OAuth client secret (optional; not echoed).")
@click.option("--azure-client-id", default=None, help="Microsoft Entra application/client ID.")
@click.option("--azure-client-secret", default=None, help="Microsoft Entra client secret (optional; not echoed).")
@click.option("--dry-run", is_flag=True, help="Print planned keys without writing the env file.")
def configure_productivity_local(
    env_file: Path,
    google_client_id: str | None,
    google_client_secret: str | None,
    azure_client_id: str | None,
    azure_client_secret: str | None,
    dry_run: bool,
) -> None:
    """Write real Google/Microsoft OAuth IDs into a local dashboard env file.

    Provider application creation is intentionally not automated here: Google
    Cloud OAuth consent screens and Microsoft Entra app registrations require
    tenant-admin policy choices. Use this after real provider apps exist.
    """
    updates = {
        "GOOGLE_CLIENT_ID": google_client_id,
        "GOOGLE_CLIENT_SECRET": google_client_secret,
        "AZURE_CLIENT_ID": azure_client_id,
        "AZURE_CLIENT_SECRET": azure_client_secret,
    }
    updates = {key: value for key, value in updates.items() if value}
    if not updates:
        raise click.UsageError("Provide at least one client id or secret value.")

    existing = _read_env(env_file)
    merged = {**existing, **updates}

    click.echo(f"Env file: {env_file}")
    for key in sorted(updates):
        display = _mask(updates[key]) if key.endswith("SECRET") else updates[key]
        click.echo(f"  set {key}={display}")

    if dry_run:
        click.echo("Dry run; no file written.")
        return

    _write_env(env_file, merged)
    click.echo("Updated local OAuth configuration.")


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={_quote_env(value)}" for key, value in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _quote_env(value: str) -> str:
    if not value or any(ch.isspace() for ch in value) or any(ch in value for ch in '"\'#$'):
        return f'"{value.replace(chr(34), chr(92) + chr(34))}"'
    return value


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"
