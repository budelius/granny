"""CDN cache management commands."""

import click
import requests

from granny.credentials.secrets import (
    get_bunny_api_key,
    list_bunny_customers,
)


@click.group()
def cdn() -> None:
    """Bunny CDN management commands."""


@cdn.command()
@click.argument("pull_zone_id", type=int)
@click.option(
    "--customer",
    "-c",
    default=None,
    help=(
        "Customer profile to use (resolves vault path "
        "'granny/infra/bunny-api-key-<customer>' or env var "
        "'BUNNY_API_KEY_<CUSTOMER>'). Omit for the default key."
    ),
)
def purge(pull_zone_id: int, customer: str | None) -> None:
    """Purge the Bunny CDN cache for a pull zone."""
    api_key = get_bunny_api_key(customer)
    if not api_key:
        label = f"BUNNY_API_KEY_{customer.upper()}" if customer else "BUNNY_API_KEY"
        raise click.ClickException(
            f"{label} not found. Set it via environment or vault."
        )

    suffix = f" (customer={customer})" if customer else ""
    click.echo(f"Purging cache for pull zone {pull_zone_id}{suffix}...")
    resp = requests.post(
        f"https://api.bunny.net/pullzone/{pull_zone_id}/purgeCache",
        headers={"AccessKey": api_key, "Content-Type": "application/json"},
        timeout=30,
    )

    if resp.status_code in (200, 204):
        click.echo("Cache purged successfully.")
    else:
        raise click.ClickException(
            f"Purge failed (HTTP {resp.status_code}): {resp.text}"
        )


@cdn.command("list-customers")
def list_customers() -> None:
    """List known Bunny customer profiles.

    Sourced from the GRANNY_BUNNY_CUSTOMERS env var and any
    BUNNY_API_KEY_<NAME> env vars currently set.
    """
    customers = list_bunny_customers()
    if not customers:
        click.echo(
            "No customer profiles registered. Add names to "
            "GRANNY_BUNNY_CUSTOMERS or export BUNNY_API_KEY_<NAME>."
        )
        return

    click.echo("Known Bunny customer profiles:")
    for name in customers:
        key = get_bunny_api_key(name)
        indicator = "ok" if key else "MISSING"
        click.echo(f"  {name:<30} {indicator}")
