"""``granny cdn`` — Bunny CDN pull zone management.

Expanded from cache-purge-only to full pull zone CRUD using the shared
``granny.cdn.BunnyCDNClient``.
"""

from __future__ import annotations

import json

import click

from granny.credentials.secrets import (
    get_bunny_api_key,
    list_bunny_customers,
)


def _client(customer: str | None = None):
    from granny.cdn.bunny import BunnyCDNClient

    return BunnyCDNClient(customer=customer)


customer_option = click.option(
    "--customer",
    "-c",
    default=None,
    help="Bunny customer profile (resolves BUNNY_API_KEY_<CUSTOMER>).",
)


@click.group()
def cdn() -> None:
    """Bunny CDN management commands."""


@cdn.command()
@click.argument("pull_zone_id", type=int)
@customer_option
def purge(pull_zone_id: int, customer: str | None) -> None:
    """Purge the Bunny CDN cache for a pull zone."""
    c = _client(customer)
    c.purge_cache(pull_zone_id)
    click.echo(f"Cache purged for pull zone {pull_zone_id}.")


@cdn.command("list-customers")
def list_customers() -> None:
    """List known Bunny customer profiles."""
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


@cdn.command("list-zones")
@customer_option
def list_zones(customer: str | None) -> None:
    """List all Bunny CDN pull zones."""
    c = _client(customer)
    zones = c.list_pull_zones()
    if not zones:
        click.echo("No pull zones found.")
        return
    for z in zones:
        default_host = c.get_default_hostname(z)
        click.echo(f"  {z['Id']:<10} {z['Name']:<30} {default_host}")


@cdn.command("get-zone")
@click.argument("pull_zone_id", type=int)
@customer_option
def get_zone(pull_zone_id: int, customer: str | None) -> None:
    """Get details of a pull zone."""
    c = _client(customer)
    zone = c.get_pull_zone(pull_zone_id)
    click.echo(json.dumps(zone, indent=2))


@cdn.command("add-hostname")
@click.argument("pull_zone_id", type=int)
@click.argument("hostname")
@customer_option
def add_hostname(pull_zone_id: int, hostname: str, customer: str | None) -> None:
    """Add a custom hostname to a pull zone."""
    c = _client(customer)
    result = c.add_hostname(pull_zone_id, hostname)
    if result.get("added"):
        click.echo(f"Hostname {hostname} added to pull zone {pull_zone_id}.")
    else:
        click.echo(f"Hostname {hostname} already registered.")


@cdn.command("ssl")
@click.argument("hostname")
@click.option("--dns01", is_flag=True, help="Use DNS-01 validation (for Bunny DNS zones).")
@customer_option
def ssl(hostname: str, dns01: bool, customer: str | None) -> None:
    """Request a free SSL certificate for a hostname."""
    c = _client(customer)
    ok = c.request_ssl_certificate(hostname, use_dns01=dns01)
    if ok:
        click.echo(f"SSL certificate requested for {hostname}.")
    else:
        raise click.ClickException(f"SSL request failed for {hostname}.")


@cdn.command("list-edge-rules")
@click.argument("pull_zone_id", type=int)
@customer_option
def list_edge_rules(pull_zone_id: int, customer: str | None) -> None:
    """List edge rules for a pull zone."""
    c = _client(customer)
    rules = c.get_edge_rules(pull_zone_id)
    if not rules:
        click.echo("No edge rules configured.")
        return
    for r in rules:
        status = "enabled" if r.get("Enabled") else "disabled"
        desc = r.get("Description", "(no description)")
        guid = r.get("Guid", "?")
        action = r.get("ActionType", "?")
        target = r.get("ActionParameter1", "")
        triggers = []
        for t in r.get("Triggers", []):
            triggers.extend(t.get("PatternMatches", []))
        click.echo(
            f"  {guid}  action={action}  {status}  "
            f"patterns={triggers}  target={target}  {desc}"
        )


@cdn.command("spa-cache-headers")
@click.argument("pull_zone_id", type=int)
@click.option(
    "--asset-path",
    default="*/assets/*",
    show_default=True,
    help="URL pattern for content-hashed assets, the only files safe to cache.",
)
@click.option(
    "--asset-max-age",
    type=int,
    default=2592000,
    show_default=True,
    help="Browser cache lifetime, in seconds, for those assets.",
)
@click.option("--purge/--no-purge", default=True, help="Purge the zone afterwards.")
@customer_option
def spa_cache_headers(
    pull_zone_id: int,
    asset_path: str,
    asset_max_age: int,
    purge: bool,
    customer: str | None,
) -> None:
    """Stop browsers pinning a single-page app to a stale build.

    Bunny applies one max-age to every response, index.html included -- so a
    returning browser keeps asking for the previous build's chunk filenames and
    runs stale code, no matter how often the CDN is purged. This defaults the
    zone to no browser caching and gives the long cache back to the
    content-hashed assets alone.
    """
    c = _client(customer)
    c.configure_spa_cache_headers(
        pull_zone_id, asset_path=asset_path, asset_max_age=asset_max_age
    )
    if purge:
        c.purge_cache(pull_zone_id)
    click.echo(
        f"Pull zone {pull_zone_id}: browser cache off by default, "
        f"{asset_max_age}s for {asset_path}."
    )


@cdn.command("add-redirect")
@click.argument("pull_zone_id", type=int)
@click.argument("from_pattern")
@click.argument("to_url")
@click.option("--description", "-d", default=None, help="Rule description.")
@customer_option
def add_redirect(
    pull_zone_id: int,
    from_pattern: str,
    to_url: str,
    description: str | None,
    customer: str | None,
) -> None:
    """Add a URL redirect edge rule to a pull zone.

    FROM_PATTERN is a Bunny wildcard pattern (e.g. '*/10-Must-Haves/*').
    TO_URL is the full redirect target (e.g. 'https://www.example.com/new-path/').
    """
    desc = description or f"Redirect {from_pattern} -> {to_url}"
    rule = {
        "ActionType": 2,
        "ActionParameter1": to_url,
        "Triggers": [
            {
                "Type": 0,
                "PatternMatchingType": 0,
                "PatternMatches": [from_pattern],
            }
        ],
        "TriggerMatchingType": 0,
        "Description": desc,
        "Enabled": True,
    }
    c = _client(customer)
    c.add_or_update_edge_rule(pull_zone_id, rule)
    click.echo(f"Redirect rule added: {from_pattern} -> {to_url}")


@cdn.command("delete-edge-rule")
@click.argument("pull_zone_id", type=int)
@click.argument("rule_guid")
@customer_option
def delete_edge_rule(pull_zone_id: int, rule_guid: str, customer: str | None) -> None:
    """Delete an edge rule by its GUID."""
    c = _client(customer)
    c.delete_edge_rule(pull_zone_id, rule_guid)
    click.echo(f"Edge rule {rule_guid} deleted.")


@cdn.command("create-zone")
@click.argument("name")
@click.option("--origin-url", default=None, help="HTTP origin URL (S3-backed zones).")
@click.option("--storage-zone-id", default=None, type=int, help="Bunny storage zone ID.")
@customer_option
def create_zone(
    name: str,
    origin_url: str | None,
    storage_zone_id: int | None,
    customer: str | None,
) -> None:
    """Create a new Bunny CDN pull zone."""
    if not origin_url and not storage_zone_id:
        raise click.UsageError("Either --origin-url or --storage-zone-id is required.")
    c = _client(customer)
    zone = c.create_pull_zone(
        name, origin_url=origin_url, storage_zone_id=storage_zone_id
    )
    click.echo(f"Pull zone created: ID={zone['Id']} Name={zone['Name']}")
    click.echo(f"Default hostname: {c.get_default_hostname(zone)}")
