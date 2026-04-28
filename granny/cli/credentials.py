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


@credentials.command("show-key")
@click.argument("name")
@click.option("--masked/--no-masked", default=True, help="Mask the middle of the key (default: masked)")
def show_key(name: str, masked: bool) -> None:
    """Print a credential by name from the OS keystore (Windows Credential Manager / Keychain).

    Requires the [vault] extra: pip install granny-devops[vault]
    """
    try:
        from locke.keystore import get_credential
    except ImportError:
        click.echo(
            "locke not installed — install with: pip install 'granny-devops[vault]'",
            err=True,
        )
        sys.exit(1)

    try:
        value = get_credential(name)
    except Exception as e:
        click.echo(f"Keystore access failed: {e}", err=True)
        sys.exit(1)

    if not value:
        click.echo(f"{name} not found in keystore", err=True)
        sys.exit(1)

    if masked:
        display = f"{value[:4]}…{value[-4:]}" if len(value) > 8 else "****"
        click.echo(f"{display}  (length={len(value)})")
    else:
        click.echo(value)


@credentials.command("test-vault")
def test_vault() -> None:
    """Test connectivity to Vaultwarden via Locke.

    Requires the [vault] extra: pip install granny-devops[vault]
    """
    from granny.credentials.secrets import _LOCKE_AVAILABLE, _get_vault_client

    if not _LOCKE_AVAILABLE:
        click.echo(
            "locke not installed — install with: pip install 'granny-devops[vault]'",
            err=True,
        )
        sys.exit(1)

    client = _get_vault_client()
    if client:
        click.echo("Vault connection: OK")
    else:
        click.echo("Vault connection: FAILED", err=True)
        sys.exit(1)
