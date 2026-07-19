"""``granny dns`` — provider-agnostic DNS record CRUD.

Replaces ``tools/create/manage-dns.sh`` with a native Python CLI that works
against Cloudflare, Bunny, Hetzner, deSEC, ClouDNS, or a manual stub. The
legacy bash script still works; this command is the preferred entry point.
"""

from __future__ import annotations

import json
import logging
import os

import click
import requests

from granny.dns import DNSRecord, get_provider, provider_choices
from granny.dns.base import DNSProvider

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_ENV = "GRANNY_DNS_PROVIDER"
DEFAULT_DOMAIN_ENV = "GRANNY_DNS_DOMAIN"


def _split_fqdn(value: str, domain: str | None) -> tuple[str, str]:
    """Split ``value`` into (short_name, zone_name).

    - ``api.example.com`` + ``None`` → (``api``, ``example.com``)
    - ``example.com`` + ``None``     → (``@``,   ``example.com``)
    - ``api``         + ``example.com`` → (``api``, ``example.com``)
    - ``@``           + ``example.com`` → (``@``,   ``example.com``)

    Raises ``click.UsageError`` if ``value`` is a bare subdomain and
    ``domain`` is not supplied.
    """
    value = value.strip().rstrip(".")
    if not value:
        raise click.UsageError("NAME cannot be empty")

    if domain:
        domain = domain.strip().rstrip(".").lower()

    if "." in value:
        value_lower = value.lower()
        if domain is None:
            return ("@", value_lower)
        if value_lower == domain:
            return ("@", domain)
        if value_lower.endswith(f".{domain}"):
            short = value_lower[: -(len(domain) + 1)]
            return (short, domain)
        # A dotted name that's neither the zone apex nor a FQDN under it.
        # With an explicit --domain this is a multi-label *relative* subname
        # (e.g. ``_smtp._tls`` or ``v1-rsa-20260629._domainkey``), so treat it
        # as relative to the zone rather than rejecting it. The resulting FQDN
        # is echoed back, so a genuine wrong-domain typo stays visible.
        return (value_lower, domain)

    # Bare label — needs a domain.
    if not domain:
        raise click.UsageError(
            "Bare subdomain requires --domain (or set $GRANNY_DNS_DOMAIN)"
        )
    if value == "@":
        return ("@", domain)
    return (value, domain)


def _provider_instance(name: str | None) -> DNSProvider:
    resolved = name or os.environ.get(DEFAULT_PROVIDER_ENV)
    if not resolved:
        raise click.UsageError(
            "--provider is required (or set $GRANNY_DNS_PROVIDER). "
            f"Choices: {', '.join(provider_choices())}"
        )
    try:
        return get_provider(resolved)
    except ValueError as exc:
        raise click.UsageError(str(exc))
    except Exception as exc:
        raise click.ClickException(f"Failed to initialize provider {resolved!r}: {exc}")


def _detect_public_ip() -> str:
    """Best-effort public IP lookup — mirrors manage-dns.sh."""
    for url in (
        "https://api.ipify.org",
        "https://ifconfig.me",
        "https://icanhazip.com",
    ):
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text.strip()
        except requests.RequestException:
            continue
    raise click.ClickException("Could not auto-detect public IP; pass VALUE explicitly.")


def _format_table(records: list[DNSRecord]) -> str:
    if not records:
        return "(no records)"
    rows = [
        (rec.type, rec.fqdn or rec.name or "@", rec.value, str(rec.ttl))
        for rec in records
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    return "\n".join(
        "  ".join(row[i].ljust(widths[i]) for i in range(4)) for row in rows
    )


def _format_record(record: DNSRecord) -> str:
    payload = {
        "name": record.fqdn or record.name,
        "type": record.type,
        "value": record.value,
        "ttl": record.ttl,
        "id": record.id,
    }
    return json.dumps(payload, indent=2)


provider_option = click.option(
    "--provider",
    "-p",
    default=None,
    help=(
        "DNS provider. Choices: "
        + ", ".join(provider_choices())
        + f". Default from ${DEFAULT_PROVIDER_ENV}."
    ),
)
domain_option = click.option(
    "--domain",
    "-d",
    default=lambda: os.environ.get(DEFAULT_DOMAIN_ENV),
    help=f"Zone/domain to operate on. Default from ${DEFAULT_DOMAIN_ENV}.",
)


@click.group()
def dns() -> None:
    """Provider-agnostic DNS record management."""


@dns.command("create-zone")
@click.argument("zone")
@provider_option
def create_zone_cmd(zone: str, provider: str | None) -> None:
    """Create zone ZONE at the provider (idempotent).

    Prints the assigned nameservers — point the registrar's NS records at
    them to activate the zone. Cloudflare requires a token with the
    account-level ``Zone: Create`` permission.
    """
    zone = zone.strip().rstrip(".").lower()
    if not zone or "." not in zone:
        raise click.UsageError(f"ZONE must be a registrable domain, got {zone!r}")
    dns_provider = _provider_instance(provider)
    try:
        meta = dns_provider.create_zone(zone)
    except NotImplementedError as exc:
        raise click.ClickException(str(exc))
    click.echo(json.dumps(meta, indent=2))


@dns.command("list")
@provider_option
@domain_option
@click.option("--type", "record_type", default=None, help="Filter by record type.")
def list_cmd(provider: str | None, domain: str | None, record_type: str | None) -> None:
    """List records in a zone."""
    if not domain:
        raise click.UsageError("--domain is required")
    dns_provider = _provider_instance(provider)
    zone_id = dns_provider.get_zone_id(domain)
    records = dns_provider.list_records(zone_id, record_type=record_type)
    click.echo(_format_table(records))


@dns.command("get")
@click.argument("name")
@provider_option
@domain_option
@click.option("--type", "record_type", default="A", show_default=True)
def get_cmd(
    name: str, provider: str | None, domain: str | None, record_type: str
) -> None:
    """Get a record by short name or FQDN."""
    short, zone_name = _split_fqdn(name, domain)
    dns_provider = _provider_instance(provider)
    zone_id = dns_provider.get_zone_id(zone_name)
    record = dns_provider.get_record(zone_id, short, record_type)
    if not record:
        full = zone_name if short == "@" else f"{short}.{zone_name}"
        click.echo(f"No {record_type} record found for {full}")
        raise SystemExit(1)
    click.echo(_format_record(record))


@dns.command("add")
@click.argument("name")
@click.argument("value", required=False)
@provider_option
@domain_option
@click.option("--type", "record_type", default="A", show_default=True)
@click.option("--ttl", default=300, show_default=True, type=int)
@click.option("--proxied/--no-proxied", default=False, help="Cloudflare proxy flag.")
def add_cmd(
    name: str,
    value: str | None,
    provider: str | None,
    domain: str | None,
    record_type: str,
    ttl: int,
    proxied: bool,
) -> None:
    """Create or update a record (upsert).

    If VALUE is omitted and --type is A, auto-detects your public IP (same
    behavior as ``manage-dns.sh add <name>``).
    """
    short, zone_name = _split_fqdn(name, domain)
    if value is None:
        if record_type.upper() != "A":
            raise click.UsageError("VALUE required unless --type A")
        value = _detect_public_ip()
        click.echo(f"Auto-detected public IP: {value}")
    dns_provider = _provider_instance(provider)
    zone_id = dns_provider.get_zone_id(zone_name)
    record = dns_provider.upsert_record(
        zone_id, short, record_type, value, ttl=ttl, proxied=proxied
    )
    full = zone_name if short == "@" else f"{short}.{zone_name}"
    click.echo(f"OK {record_type} {full} -> {value}")
    click.echo(_format_record(record))


@dns.command("upsert")
@click.argument("name")
@click.argument("value")
@provider_option
@domain_option
@click.option("--type", "record_type", default="A", show_default=True)
@click.option("--ttl", default=300, show_default=True, type=int)
@click.option("--proxied/--no-proxied", default=False)
def upsert_cmd(
    name: str,
    value: str,
    provider: str | None,
    domain: str | None,
    record_type: str,
    ttl: int,
    proxied: bool,
) -> None:
    """Explicit upsert (same as ``add`` but requires VALUE)."""
    short, zone_name = _split_fqdn(name, domain)
    dns_provider = _provider_instance(provider)
    zone_id = dns_provider.get_zone_id(zone_name)
    record = dns_provider.upsert_record(
        zone_id, short, record_type, value, ttl=ttl, proxied=proxied
    )
    click.echo(_format_record(record))


@dns.command("delete")
@click.argument("name")
@provider_option
@domain_option
@click.option("--type", "record_type", default="A", show_default=True)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def delete_cmd(
    name: str,
    provider: str | None,
    domain: str | None,
    record_type: str,
    yes: bool,
) -> None:
    """Delete a record."""
    short, zone_name = _split_fqdn(name, domain)
    dns_provider = _provider_instance(provider)
    zone_id = dns_provider.get_zone_id(zone_name)
    record = dns_provider.get_record(zone_id, short, record_type)
    full = zone_name if short == "@" else f"{short}.{zone_name}"
    if not record:
        click.echo(f"No {record_type} record found for {full}")
        raise SystemExit(1)
    if not yes:
        click.confirm(f"Delete {record_type} {full} -> {record.value}?", abort=True)
    dns_provider.delete_record(zone_id, record.id)
    click.echo(f"Deleted {record_type} {full}")


@dns.command("nameservers")
@click.argument("domain")
@provider_option
def nameservers_cmd(domain: str, provider: str | None) -> None:
    """Print the authoritative nameservers the provider has for DOMAIN.

    Useful before flipping registrar delegation with ``granny create
    bunny-storage --registrar easyname``.
    """
    dns_provider = _provider_instance(provider)
    try:
        ns_list = dns_provider.get_nameservers(domain)
    except NotImplementedError as exc:
        raise click.ClickException(str(exc))
    if not ns_list:
        click.echo(f"No nameservers found for {domain}")
        raise SystemExit(1)
    for ns in ns_list:
        click.echo(ns)
