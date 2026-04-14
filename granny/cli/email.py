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


@mailjet.command("setup-dns")
@click.argument("domain")
@click.option("--check", "check_only", is_flag=True, help="Read-only DNS status check.")
@click.option("--dry-run", is_flag=True)
def mailjet_setup_dns(domain: str, check_only: bool, dry_run: bool) -> None:
    """Register domain sender and retrieve DNS auth settings.

    Prints the DKIM, SPF, and ownership values you need to create as TXT
    records. Use ``granny dns`` to create them.
    """
    from granny.email.mailjet import MailjetClient

    c = MailjetClient()

    if check_only:
        sender = c.find_sender(domain)
        if not sender:
            click.echo(f"No sender registered for *@{domain}")
            raise SystemExit(1)
        dns = c.get_dns_settings(domain)
        click.echo(f"DKIM status: {dns.get('DKIMStatus')}")
        click.echo(f"SPF status:  {dns.get('SPFStatus')}")
        return

    sender = c.get_or_create_sender(domain)
    click.echo(f"Sender: *@{domain} (ID={sender.get('ID')}, Status={sender.get('Status')})")

    dns = c.get_dns_settings(domain)
    click.echo("")
    click.echo("DNS records to create:")
    click.echo(f"  DKIM:  TXT  {dns.get('DKIMRecordName', 'mailjet._domainkey.' + domain)}")
    click.echo(f"         {dns.get('DKIMRecordValue', '(not available)')}")
    click.echo(f"  SPF:   TXT  {domain}")
    click.echo(f"         {dns.get('SPFRecordValue', 'v=spf1 include:spf.mailjet.com ~all')}")
    if dns.get("OwnerShipToken"):
        click.echo(f"  Owner: TXT  {dns.get('OwnerShipTokenRecordName', domain)}")
        click.echo(f"         {dns.get('OwnerShipToken')}")


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
