"""``granny email`` — Mailjet + AWS WorkMail management."""

from __future__ import annotations

import getpass

import click


@click.group()
def email() -> None:
    """Email management (Mailjet, WorkMail)."""


# -- Mailjet subgroup ---------------------------------------------------------


@email.group("mailjet")
def mailjet() -> None:
    """Mailjet sender/DNS and contact management."""


def _short_name(fqdn: str, domain: str) -> str:
    """Return the zone-relative short name for ``fqdn`` within ``domain``.

    ``mailjet._domainkey.mezzanin.eu`` + ``mezzanin.eu`` -> ``mailjet._domainkey``
    ``mezzanin.eu`` + ``mezzanin.eu``                    -> ``@``
    """
    fqdn = (fqdn or "").strip().rstrip(".").lower()
    domain = (domain or "").strip().rstrip(".").lower()
    if not fqdn or fqdn == domain:
        return "@"
    if fqdn.endswith("." + domain):
        return fqdn[: -(len(domain) + 1)]
    return fqdn


def _upsert_spf(provider, zone_id: str, qualifier: str, ttl: int) -> tuple[str, list[str]]:
    """Merge ``include:spf.mailjet.com`` into the apex SPF record (or create it).

    Selects the existing SPF record by content (``v=spf1``) rather than by
    name+type, so a co-resident apex TXT (e.g. a verification token) is never
    clobbered.
    """
    from granny.email.mailjet import merge_spf

    records = provider.list_records(zone_id, name="@", record_type="TXT")
    spf = next((r for r in records if "v=spf1" in (r.value or "")), None)
    merged, warnings = merge_spf(spf.value if spf else None, target_qualifier=qualifier)
    if spf is None:
        provider.create_record(zone_id, "@", "TXT", merged, ttl=ttl)
    elif spf.value != merged:
        provider.update_record(zone_id, spf.id, "@", "TXT", merged, ttl=ttl)
    return merged, warnings


@mailjet.command("setup-dns")
@click.argument("domain")
@click.option("--check", "check_only", is_flag=True, help="Read-only DNS status check.")
@click.option(
    "--dns-provider",
    default=None,
    help="Create the DKIM/SPF/ownership records at this provider "
    "(e.g. inwx, cloudflare, bunny). Omit to only print them.",
)
@click.option(
    "--validate/--no-validate",
    default=None,
    help="Trigger Mailjet's DKIM/SPF check and report status. "
    "Defaults to on when --dns-provider is used.",
)
@click.option("--spf-qualifier", default="~all", show_default=True, help="SPF policy qualifier.")
@click.option("--ttl", default=3600, show_default=True, help="TTL for created records.")
@click.option("--dry-run", is_flag=True, help="Show planned record changes without writing.")
def mailjet_setup_dns(
    domain: str,
    check_only: bool,
    dns_provider: str | None,
    validate: bool | None,
    spf_qualifier: str,
    ttl: int,
    dry_run: bool,
) -> None:
    """Register a Mailjet sender domain and wire up its DNS auth.

    With ``--dns-provider`` granny creates the DKIM, SPF (merged into any
    existing apex SPF), and ownership TXT records and then asks Mailjet to
    re-validate. Without it, the records are printed for manual creation.
    """
    from granny.email.mailjet import MailjetClient

    c = MailjetClient()

    if check_only:
        dns = c.get_dns_settings(domain)
        if not dns:
            click.echo(f"No Mailjet DNS settings for {domain}")
            raise SystemExit(1)
        sender = c.find_sender(domain)
        click.echo(f"Sender:      *@{domain} "
                   f"({'registered' if sender else 'NOT registered'})")
        click.echo(f"DKIM status: {dns.get('DKIMStatus')}")
        click.echo(f"SPF status:  {dns.get('SPFStatus')}")
        return

    sender = c.get_or_create_sender(domain)
    click.echo(f"Sender: *@{domain} (ID={sender.get('ID')}, Status={sender.get('Status')})")

    dns = c.get_dns_settings(domain)
    dkim_name = (dns.get("DKIMRecordName") or f"mailjet._domainkey.{domain}").rstrip(".")
    dkim_value = dns.get("DKIMRecordValue", "(not available)")
    owner_name = (dns.get("OwnerShipTokenRecordName") or domain).rstrip(".")
    owner_value = dns.get("OwnerShipToken")

    if not dns_provider:
        click.echo("")
        click.echo("DNS records to create (pass --dns-provider to apply automatically):")
        click.echo(f"  DKIM:  TXT  {dkim_name}")
        click.echo(f"         {dkim_value}")
        click.echo(f"  SPF:   TXT  {domain}")
        click.echo(f"         {dns.get('SPFRecordValue', 'v=spf1 include:spf.mailjet.com ' + spf_qualifier)}")
        if owner_value:
            click.echo(f"  Owner: TXT  {owner_name}")
            click.echo(f"         {owner_value}")
        return

    from granny.dns.factory import get_provider

    provider = get_provider(dns_provider)
    zone_id = provider.get_zone_id(domain)

    dkim_short = _short_name(dkim_name, domain)
    owner_short = _short_name(owner_name, domain)

    if dry_run:
        click.echo("")
        click.echo(f"[dry-run] would upsert at {dns_provider}:")
        click.echo(f"  TXT {dkim_short}.{domain} = {dkim_value[:40]}...")
        click.echo(f"  TXT @ (SPF, merged with include:spf.mailjet.com {spf_qualifier})")
        if owner_value:
            click.echo(f"  TXT {owner_short}.{domain} = {owner_value}")
        return

    provider.upsert_record(zone_id, dkim_short, "TXT", dkim_value, ttl=ttl)
    click.echo(f"  [ok] DKIM  TXT {dkim_short}.{domain}")

    merged, warnings = _upsert_spf(provider, zone_id, spf_qualifier, ttl)
    for w in warnings:
        click.echo(f"  [spf] {w}")
    click.echo(f"  [ok] SPF   TXT {domain} -> {merged}")

    if owner_value:
        provider.upsert_record(zone_id, owner_short, "TXT", owner_value, ttl=ttl)
        click.echo(f"  [ok] Owner TXT {owner_short}.{domain}")

    # Validate unless explicitly disabled.
    if validate is None:
        validate = True
    if validate:
        dns_id = dns.get("ID")
        if not dns_id:
            click.echo("  [warn] no DNS id from Mailjet; skipping validation")
            return
        result = c.check_dns(str(dns_id))
        click.echo("")
        click.echo(f"Mailjet validation: DKIM={result.get('DKIMStatus')} "
                   f"SPF={result.get('SPFStatus')}")


@mailjet.command("check-contact")
@click.argument("email_address")
def mailjet_check_contact(email_address: str) -> None:
    """Check a contact's status (blocked, active, bounce history)."""
    from granny.email.mailjet_contacts import MailjetContactClient

    c = MailjetContactClient()
    contact = c.get_contact(email_address)
    if not contact:
        click.echo(f"Contact not found: {email_address}")
        raise SystemExit(1)

    excluded = contact.get("IsExcludedFromCampaigns", False)
    status = "BLOCKED" if excluded else "ACTIVE"
    click.echo(f"Email:    {contact.get('Email')}")
    click.echo(f"Status:   {status}")
    click.echo(f"ID:       {contact.get('ID')}")
    click.echo(f"Created:  {contact.get('CreatedAt', 'N/A')}")
    click.echo(f"Activity: {contact.get('LastActivityAt', 'N/A')}")


@mailjet.command("unblock")
@click.argument("email_address")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def mailjet_unblock(email_address: str, yes: bool) -> None:
    """Unblock a contact excluded from campaigns."""
    from granny.email.mailjet_contacts import MailjetContactClient

    c = MailjetContactClient()
    contact = c.get_contact(email_address)
    if not contact:
        click.echo(f"Contact not found: {email_address}")
        raise SystemExit(1)
    if not contact.get("IsExcludedFromCampaigns"):
        click.echo(f"{email_address} is already active.")
        return
    if not yes:
        click.confirm(f"Unblock {email_address}?", abort=True)
    c.unblock_contact(email_address)
    click.echo(f"Unblocked: {email_address}")


@mailjet.command("list-blocked")
@click.option("--domain", default=None, help="Filter by domain.")
def mailjet_list_blocked(domain: str | None) -> None:
    """List contacts excluded from campaigns."""
    from granny.email.mailjet_contacts import MailjetContactClient

    c = MailjetContactClient()
    contacts = c.list_all_excluded(domain=domain)
    if not contacts:
        click.echo("No blocked contacts found.")
        return
    click.echo(f"Blocked contacts ({len(contacts)}):")
    for ct in contacts:
        click.echo(f"  {ct.get('Email', '?'):<40} ID={ct.get('ID')}")


# -- WorkMail subgroup --------------------------------------------------------


@email.group("workmail")
def workmail() -> None:
    """AWS WorkMail user management."""


@workmail.command("list-orgs")
@click.option("--region", default=None, help="AWS region (default: us-east-1).")
def workmail_list_orgs(region: str | None) -> None:
    """List WorkMail organizations."""
    from granny.email.workmail import WorkMailManager

    wm = WorkMailManager(region=region)
    orgs = wm.list_organizations()
    if not orgs:
        click.echo("No organizations found.")
        return
    for org in orgs:
        click.echo(
            f"  {org.get('OrganizationId', '?'):<40} "
            f"{org.get('DefaultMailDomain', org.get('Alias', '?')):<30} "
            f"{org.get('State', '?')}"
        )


@workmail.command("list-users")
@click.argument("domain")
@click.option("--region", default=None)
def workmail_list_users(domain: str, region: str | None) -> None:
    """List users in a WorkMail organization."""
    from granny.email.workmail import WorkMailManager

    wm = WorkMailManager(region=region)
    org_id = wm.get_org_id(domain)
    users = wm.list_users(org_id)
    for u in users:
        if u.get("UserRole") == "SYSTEM_USER":
            continue
        click.echo(
            f"  {u.get('Email', '?'):<40} "
            f"{u.get('DisplayName', ''):<20} "
            f"{u.get('State', '?')}"
        )


@workmail.command("create-user")
@click.argument("domain")
@click.option("--email", "user_email", required=True)
@click.option("--display-name", required=True)
@click.option("--first-name", default=None)
@click.option("--last-name", default=None)
@click.option("--region", default=None)
def workmail_create_user(
    domain: str,
    user_email: str,
    display_name: str,
    first_name: str | None,
    last_name: str | None,
    region: str | None,
) -> None:
    """Create a WorkMail user (password prompted interactively)."""
    from granny.email.workmail import WorkMailManager

    password = getpass.getpass("Password: ")
    password2 = getpass.getpass("Confirm:  ")
    if password != password2:
        raise click.ClickException("Passwords do not match.")

    wm = WorkMailManager(region=region)
    org_id = wm.get_org_id(domain)
    user_id = wm.create_and_register_user(
        org_id,
        user_email,
        display_name,
        password,
        first_name=first_name,
        last_name=last_name,
    )
    click.echo(f"User created: {user_email} (ID={user_id})")


@workmail.command("reset-password")
@click.argument("domain")
@click.option("--email", "user_email", required=True)
@click.option("--region", default=None)
def workmail_reset_password(
    domain: str, user_email: str, region: str | None
) -> None:
    """Reset a WorkMail user's password."""
    from granny.email.workmail import WorkMailManager

    password = getpass.getpass("New password: ")
    password2 = getpass.getpass("Confirm:      ")
    if password != password2:
        raise click.ClickException("Passwords do not match.")

    wm = WorkMailManager(region=region)
    org_id = wm.get_org_id(domain)
    user = wm.find_user_by_email(org_id, user_email)
    if not user:
        raise click.ClickException(f"User not found: {user_email}")
    wm.reset_password(org_id, user["Id"], password)
    click.echo(f"Password reset for {user_email}.")


# -- Stalwart subgroup --------------------------------------------------------


@email.group("stalwart")
def stalwart() -> None:
    """Stalwart mail server admin (domains, mailboxes, DNS, delivery test)."""


@stalwart.command("list-domains")
def stalwart_list_domains() -> None:
    """List configured local domains."""
    from granny.email.stalwart import StalwartClient

    c = StalwartClient.from_environment()
    domains = c.list_domains()
    if not domains:
        click.echo("No domains configured.")
        return
    for d in domains:
        dkim = (d.get("dkimManagement") or {}).get("@type", "?")
        click.echo(
            f"  {d.get('id', '?'):<5} {d.get('name', '?'):<30} "
            f"enabled={d.get('isEnabled')}  dkim={dkim}"
        )


@stalwart.command("add-domain")
@click.argument("name")
def stalwart_add_domain(name: str) -> None:
    """Create a new local domain (Manual cert, Automatic DKIM, Manual DNS)."""
    from granny.email.stalwart import StalwartClient

    c = StalwartClient.from_environment()
    created = c.add_domain(name)
    click.echo(f"Domain {name} ready (id={created.get('id')})")
    click.echo(f"Next: granny email stalwart show-dns {name}")


@stalwart.command("list-mailboxes")
def stalwart_list_mailboxes() -> None:
    """List individual mailboxes."""
    from granny.email.stalwart import StalwartClient

    c = StalwartClient.from_environment()
    mailboxes = c.list_mailboxes()
    if not mailboxes:
        click.echo("No mailboxes configured.")
        return
    for a in mailboxes:
        role = (a.get("roles") or {}).get("@type", "?")
        click.echo(
            f"  {a.get('id', '?'):<5} {a.get('emailAddress', '?'):<40} role={role}"
        )


@stalwart.command("add-mailbox")
@click.argument("email_address")
@click.option("--description", default=None, help="Free-text mailbox description.")
@click.option(
    "--password",
    default=None,
    help="Use this password instead of generating one (NOT recommended).",
)
def stalwart_add_mailbox(
    email_address: str, description: str | None, password: str | None,
) -> None:
    """Create a mailbox under an existing local domain.

    The generated password is shown ONCE on stdout. Save it immediately
    (e.g. into Vaultwarden) — Stalwart hashes it server-side.
    """
    from granny.email.stalwart import StalwartClient

    c = StalwartClient.from_environment()
    record, generated = c.add_mailbox(
        email_address, password=password, description=description,
    )
    click.echo(f"Mailbox created (id={record.get('id')})")
    click.echo("")
    click.echo(f"  Email:    {email_address}")
    click.echo(f"  Password: {generated}")
    click.echo("")
    base = c.base_url.removeprefix("https://").removeprefix("http://")
    click.echo(f"  SMTP submission: {base}:587 (STARTTLS) or :465 (implicit TLS)")
    click.echo(f"  IMAP:            {base}:993")
    click.echo(f"  JMAP / WebDAV:   {c.base_url}/")
    click.echo("")
    click.echo("  This password is shown ONCE — save it now.")


@stalwart.command("show-dns")
@click.argument("name")
def stalwart_show_dns(name: str) -> None:
    """Print Stalwart's generated DNS zone for a domain.

    Output is in BIND zone-file syntax (SPF, MX, DKIM, DMARC, MTA-STS,
    TLS-RPT, SRV autoconfig). Publish at the domain's registrar.
    """
    from granny.email.stalwart import StalwartClient

    c = StalwartClient.from_environment()
    click.echo(c.get_dns_zone(name))


@stalwart.command("delivery-test")
@click.argument("recipient")
def stalwart_delivery_test(recipient: str) -> None:
    """Stream Stalwart's live deliverability report for one recipient.

    Mirrors the admin UI's *Live Delivery Test* page (MX lookup,
    MTA-STS, TLS-RPT, banner, STARTTLS, etc.).
    """
    from granny.email.stalwart import StalwartClient

    c = StalwartClient.from_environment()
    for line in c.stream_delivery_test(recipient):
        click.echo(line)
