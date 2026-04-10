"""CLI commands for credential management."""

import sys

import click


@click.group()
def credentials() -> None:
    """Manage credentials and vault secrets."""


@credentials.command()
@click.argument("name")
def get(name: str) -> None:
    """Retrieve a secret by env var name (e.g. BUNNY_API_KEY)."""
    from granny.credentials import get_secret

    value = get_secret(name)
    if value:
        click.echo(value)
    else:
        click.echo(f"Secret {name!r} not found", err=True)
        sys.exit(1)


@credentials.command()
@click.option(
    "--keys",
    default=None,
    help="Comma-separated list of env var names (default: all registered)",
)
def status(keys: str | None) -> None:
    """Show resolution status for registered secrets."""
    from granny.credentials.secrets import SECRET_MAP, get_secret

    key_list = keys.split(",") if keys else list(SECRET_MAP.keys())

    for key in sorted(key_list):
        value = get_secret(key)
        indicator = "ok" if value else "MISSING"
        click.echo(f"  {key:<30} {indicator}")


