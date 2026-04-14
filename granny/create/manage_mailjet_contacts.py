#!/usr/bin/env python3
"""
Mailjet Contact Management Script

Manage contact status in Mailjet — check, unblock, and list blocked contacts.
Useful when contacts are excluded from campaigns or blocked from receiving emails.

FEATURES:
  - Check contact status (exclusion state, creation date, last activity)
  - Unblock contacts excluded from campaigns
  - List all blocked/excluded contacts
  - Bulk unblock from a file of email addresses
  - Dry-run mode for previewing changes

MAILJET CONTACT STATES:
  IsExcludedFromCampaigns: true  → Contact is blocked from receiving emails
  IsExcludedFromCampaigns: false → Contact can receive emails normally

Environment Variables:
  MAILJET_API_KEY       Mailjet API public key
  MAILJET_SECRET_KEY    Mailjet API private key

Usage:
  # Check a contact's status
  python manage_mailjet_contacts.py --action check --email user@example.com

  # Unblock a single contact
  python manage_mailjet_contacts.py --action unblock --email user@example.com

  # List all blocked contacts
  python manage_mailjet_contacts.py --action list-blocked

  # List all blocked contacts for a specific domain
  python manage_mailjet_contacts.py --action list-blocked --domain example.com

  # Bulk unblock from file (one email per line)
  python manage_mailjet_contacts.py --action bulk-unblock --file blocked_emails.txt

  # Dry run (preview without changes)
  python manage_mailjet_contacts.py --action unblock --email user@example.com --dry-run
"""

import os
import sys
import time
import logging
import argparse
import requests
from typing import Optional, Dict, List

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass


# =============================================================================
# Mailjet Contact Client
# =============================================================================

class MailjetContactClient:
    """Mailjet REST API v3 client for contact management."""

    API_BASE = "https://api.mailjet.com/v3/REST"

    def __init__(self):
        api_key = os.environ.get("MAILJET_API_KEY")
        secret_key = os.environ.get("MAILJET_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError(
                "MAILJET_API_KEY and MAILJET_SECRET_KEY environment variables must be set.\n"
                "Get your keys from: https://app.mailjet.com/account/apikeys"
            )
        self.session = requests.Session()
        self.session.auth = (api_key, secret_key)
        self.session.headers.update({"Content-Type": "application/json"})

    def _api_request(self, method: str, endpoint: str,
                     data: dict = None, params: dict = None) -> dict:
        """Make an API request to Mailjet REST API."""
        url = f"{self.API_BASE}{endpoint}"
        response = getattr(self.session, method.lower())(
            url, json=data, params=params
        )

        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            raise Exception(
                f"Mailjet API error: {response.status_code} - {response.text}"
            )

        return response.json() if response.text else {}

    def get_contact(self, email: str) -> Optional[dict]:
        """Get contact details by email address.

        Returns contact dict with keys like:
          - ID, Email, Name, IsExcludedFromCampaigns,
          - CreatedAt, LastActivityAt, DeliveredCount, etc.
        """
        result = self._api_request("GET", f"/contact/{email}")
        if not result:
            return None
        contacts = result.get("Data", [])
        return contacts[0] if contacts else None

    def unblock_contact(self, email: str) -> dict:
        """Unblock a contact by setting IsExcludedFromCampaigns to false.

        Returns updated contact data.
        """
        result = self._api_request(
            "PUT",
            f"/contact/{email}",
            data={"IsExcludedFromCampaigns": "false"}
        )
        if not result:
            raise Exception(f"Contact not found: {email}")
        contacts = result.get("Data", [])
        return contacts[0] if contacts else {}

    def block_contact(self, email: str) -> dict:
        """Block a contact by setting IsExcludedFromCampaigns to true.

        Returns updated contact data.
        """
        result = self._api_request(
            "PUT",
            f"/contact/{email}",
            data={"IsExcludedFromCampaigns": "true"}
        )
        if not result:
            raise Exception(f"Contact not found: {email}")
        contacts = result.get("Data", [])
        return contacts[0] if contacts else {}

    def list_contacts(self, limit: int = 1000, offset: int = 0,
                      is_excluded: bool = None) -> List[dict]:
        """List contacts with optional exclusion filter.

        Args:
            limit: Max contacts to return (max 1000 per request)
            offset: Pagination offset
            is_excluded: If True, only excluded contacts; False, only active; None, all
        """
        params = {"Limit": min(limit, 1000), "Offset": offset}
        if is_excluded is not None:
            params["IsExcludedFromCampaigns"] = str(is_excluded).lower()

        result = self._api_request("GET", "/contact", params=params)
        if not result:
            return []
        return result.get("Data", [])

    def list_all_excluded_contacts(self, domain: str = None) -> List[dict]:
        """List all contacts excluded from campaigns, with optional domain filter.

        Handles pagination automatically.
        """
        all_contacts = []
        offset = 0
        batch_size = 1000

        while True:
            batch = self.list_contacts(
                limit=batch_size, offset=offset, is_excluded=True
            )
            if not batch:
                break

            if domain:
                batch = [c for c in batch if c.get("Email", "").endswith(f"@{domain}")]

            all_contacts.extend(batch)
            offset += batch_size

            if len(batch) < batch_size:
                break

        return all_contacts

    def get_message_history(self, contact_id: int, limit: int = 10) -> List[dict]:
        """Get message history for a contact."""
        result = self._api_request(
            "GET", "/message",
            params={"Contact": contact_id, "Limit": limit}
        )
        if not result:
            return []
        return result.get("Data", [])

    def get_message_event(self, message_id: int) -> Optional[dict]:
        """Get event details for a specific message (bounce reason, etc.)."""
        result = self._api_request("GET", f"/messagehistory/{message_id}")
        if not result:
            return None
        events = result.get("Data", [])
        return events[0] if events else None

    def delete_contact(self, contact_id: int) -> bool:
        """Delete a contact via the v4 API (clears bounce blocks).

        The v3 API does not support contact deletion. The v4 API at
        /v4/contacts/{id} permanently removes the contact and its
        bounce history, allowing future emails to be delivered.
        """
        url = f"https://api.mailjet.com/v4/contacts/{contact_id}"
        response = self.session.delete(url)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise Exception(
            f"Mailjet v4 API error: {response.status_code} - {response.text}"
        )


# =============================================================================
# CLI
# =============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Manage Mailjet contact status (check, unblock, list blocked)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check a contact's status
  python manage_mailjet_contacts.py --action check --email user@example.com

  # Unblock a blocked contact
  python manage_mailjet_contacts.py --action unblock --email user@example.com

  # List all blocked/excluded contacts
  python manage_mailjet_contacts.py --action list-blocked

  # List blocked contacts for a specific domain
  python manage_mailjet_contacts.py --action list-blocked --domain example.com

  # Bulk unblock from file (one email per line)
  python manage_mailjet_contacts.py --action bulk-unblock --file blocked_emails.txt

  # Preview unblock without making changes
  python manage_mailjet_contacts.py --action unblock --email user@example.com --dry-run

Environment Variables:
  MAILJET_API_KEY       Mailjet public API key
  MAILJET_SECRET_KEY    Mailjet private/secret key

Get API keys at: https://app.mailjet.com/account/apikeys
        """
    )

    parser.add_argument('--action', required=True,
                        choices=['check', 'unblock', 'block', 'clear-bounce',
                                 'list-blocked', 'bulk-unblock'],
                        help='Action to perform')
    parser.add_argument('--email',
                        help='Contact email address')
    parser.add_argument('--domain',
                        help='Filter by domain (for list-blocked)')
    parser.add_argument('--file',
                        help='File with email addresses, one per line (for bulk-unblock)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without making them')

    return parser.parse_args()


# =============================================================================
# Action Handlers
# =============================================================================

def format_contact_info(contact: dict) -> str:
    """Format contact details for display."""
    lines = []
    excluded = contact.get("IsExcludedFromCampaigns", False)
    status = "BLOCKED (excluded)" if excluded else "ACTIVE"
    status_icon = "[BLOCKED]" if excluded else "[OK]"

    lines.append(f"  {status_icon} {contact.get('Email', 'N/A')}")
    lines.append(f"    Status: {status}")
    lines.append(f"    Contact ID: {contact.get('ID', 'N/A')}")
    lines.append(f"    Name: {contact.get('Name', '(not set)')}")
    lines.append(f"    Excluded From Campaigns: {excluded}")
    lines.append(f"    Created: {contact.get('CreatedAt', 'N/A')}")
    lines.append(f"    Last Activity: {contact.get('LastActivityAt', 'N/A')}")
    lines.append(f"    Delivered Count: {contact.get('DeliveredCount', 0)}")

    return "\n".join(lines)


def format_message_summary(messages: List[dict]) -> str:
    """Format message history summary showing bounce/block status."""
    lines = []
    bounced = [m for m in messages if m.get('Status') == 'hardbounced']
    blocked = [m for m in messages if m.get('Status') == 'blocked']
    sent = [m for m in messages if m.get('Status') in ('sent', 'opened', 'clicked')]

    lines.append(f"    Messages: {len(messages)} total")
    if bounced:
        lines.append(f"    Hard Bounced: {len(bounced)} (this causes permanent blocking!)")
    if blocked:
        lines.append(f"    Blocked: {len(blocked)} (due to prior hard bounce)")
    if sent:
        lines.append(f"    Delivered: {len(sent)}")

    return "\n".join(lines)


def action_check(client: MailjetContactClient, args) -> int:
    """Check a contact's status."""
    if not args.email:
        logging.error("--email is required for check action")
        return 1

    logging.info(f"\n--- Mailjet Contact Check: {args.email} ---\n")

    contact = client.get_contact(args.email)
    if not contact:
        logging.info(f"  Contact not found in Mailjet: {args.email}")
        logging.info("  This email has never been added as a Mailjet contact.")
        return 0

    logging.info(format_contact_info(contact))

    # Check message history for bounces/blocks
    contact_id = contact.get("ID")
    if contact_id:
        messages = client.get_message_history(contact_id, limit=20)
        if messages:
            logging.info("")
            logging.info(format_message_summary(messages))

            # Show bounce details
            bounced = [m for m in messages if m.get('Status') == 'hardbounced']
            if bounced:
                event = client.get_message_event(bounced[0]['ID'])
                if event:
                    logging.info(f"    Bounce reason: {event.get('Comment', 'N/A')}")
                    logging.info(f"    Bounce state: {event.get('State', 'N/A')}")

            blocked = [m for m in messages if m.get('Status') == 'blocked']
            if bounced or blocked:
                logging.info(f"\n  This contact is HARD BOUNCE BLOCKED by Mailjet.")
                logging.info(f"  To clear the bounce and allow emails again, run:")
                logging.info(f"  python manage_mailjet_contacts.py --action clear-bounce --email {args.email}")
                return 0

    excluded = contact.get("IsExcludedFromCampaigns", False)
    if excluded:
        logging.info(f"\n  To unblock (campaign exclusion), run:")
        logging.info(f"  python manage_mailjet_contacts.py --action unblock --email {args.email}")

    return 0


def action_unblock(client: MailjetContactClient, args) -> int:
    """Unblock a single contact."""
    if not args.email:
        logging.error("--email is required for unblock action")
        return 1

    logging.info(f"\n--- Mailjet Contact Unblock: {args.email} ---\n")

    # Check current status first
    contact = client.get_contact(args.email)
    if not contact:
        logging.error(f"  Contact not found in Mailjet: {args.email}")
        return 1

    excluded = contact.get("IsExcludedFromCampaigns", False)
    if not excluded:
        logging.info(f"  Contact is already active (not blocked): {args.email}")
        return 0

    logging.info(f"  Current status: BLOCKED (IsExcludedFromCampaigns=true)")

    if args.dry_run:
        logging.info(f"  [DRY RUN] Would unblock: {args.email}")
        return 0

    # Unblock
    updated = client.unblock_contact(args.email)
    new_excluded = updated.get("IsExcludedFromCampaigns", True)

    if not new_excluded:
        logging.info(f"  [OK] Successfully unblocked: {args.email}")
        logging.info(f"  Contact can now receive Mailjet emails.")
    else:
        logging.error(f"  [FAIL] Contact still blocked after update: {args.email}")
        return 1

    return 0


def action_block(client: MailjetContactClient, args) -> int:
    """Block a single contact (exclude from campaigns)."""
    if not args.email:
        logging.error("--email is required for block action")
        return 1

    logging.info(f"\n--- Mailjet Contact Block: {args.email} ---\n")

    contact = client.get_contact(args.email)
    if not contact:
        logging.error(f"  Contact not found in Mailjet: {args.email}")
        return 1

    excluded = contact.get("IsExcludedFromCampaigns", False)
    if excluded:
        logging.info(f"  Contact is already blocked: {args.email}")
        return 0

    if args.dry_run:
        logging.info(f"  [DRY RUN] Would block: {args.email}")
        return 0

    updated = client.block_contact(args.email)
    new_excluded = updated.get("IsExcludedFromCampaigns", False)

    if new_excluded:
        logging.info(f"  [OK] Successfully blocked: {args.email}")
    else:
        logging.error(f"  [FAIL] Contact still active after update: {args.email}")
        return 1

    return 0


def action_list_blocked(client: MailjetContactClient, args) -> int:
    """List all blocked/excluded contacts."""
    domain_msg = f" for {args.domain}" if args.domain else ""
    logging.info(f"\n--- Blocked Mailjet Contacts{domain_msg} ---\n")

    contacts = client.list_all_excluded_contacts(domain=args.domain)

    if not contacts:
        logging.info("  No blocked contacts found.")
        return 0

    logging.info(f"  Found {len(contacts)} blocked contact(s):\n")
    logging.info(f"  {'Email':<40} {'Contact ID':<12} {'Created':<22} {'Delivered'}")
    logging.info(f"  {'-'*40} {'-'*12} {'-'*22} {'-'*10}")

    for contact in contacts:
        logging.info(
            f"  {contact.get('Email', 'N/A'):<40} "
            f"{contact.get('ID', 'N/A'):<12} "
            f"{str(contact.get('CreatedAt', 'N/A')):<22} "
            f"{contact.get('DeliveredCount', 0)}"
        )

    return 0


def action_clear_bounce(client: MailjetContactClient, args) -> int:
    """Clear a hard bounce block by deleting the contact.

    When Mailjet receives a hard bounce (e.g., "user unknown"), it permanently
    blocks all future emails to that address. The only way to clear this is to
    delete the contact entirely via the v4 API. The next email sent to this
    address will create a fresh contact without bounce history.
    """
    if not args.email:
        logging.error("--email is required for clear-bounce action")
        return 1

    logging.info(f"\n--- Mailjet Clear Bounce: {args.email} ---\n")

    contact = client.get_contact(args.email)
    if not contact:
        logging.info(f"  Contact not found in Mailjet: {args.email}")
        logging.info("  No bounce to clear — emails should deliver normally.")
        return 0

    contact_id = contact.get("ID")
    logging.info(format_contact_info(contact))

    # Check message history for bounce evidence
    messages = client.get_message_history(contact_id, limit=20)
    bounced = [m for m in messages if m.get('Status') == 'hardbounced']
    blocked = [m for m in messages if m.get('Status') == 'blocked']

    if messages:
        logging.info("")
        logging.info(format_message_summary(messages))

    if bounced:
        event = client.get_message_event(bounced[0]['ID'])
        if event:
            logging.info(f"    Bounce reason: {event.get('Comment', 'N/A')}")

    if not bounced and not blocked:
        logging.info(f"\n  No bounce or block history found for: {args.email}")
        logging.info("  Use --action unblock if the contact is campaign-excluded instead.")
        return 0

    if args.dry_run:
        logging.info(f"\n  [DRY RUN] Would delete contact {contact_id} to clear bounce block")
        return 0

    # Delete the contact to clear bounce state
    logging.info(f"\n  Deleting contact {contact_id} to clear bounce block...")
    deleted = client.delete_contact(contact_id)

    if deleted:
        logging.info(f"  [OK] Contact deleted. Bounce block cleared for: {args.email}")
        logging.info(f"  Next email to this address will create a fresh contact.")
    else:
        logging.error(f"  [FAIL] Could not delete contact: {args.email}")
        return 1

    return 0


def action_bulk_unblock(client: MailjetContactClient, args) -> int:
    """Bulk unblock contacts from a file."""
    if not args.file:
        logging.error("--file is required for bulk-unblock action")
        return 1

    if not os.path.exists(args.file):
        logging.error(f"File not found: {args.file}")
        return 1

    with open(args.file, 'r') as f:
        emails = [line.strip() for line in f if line.strip() and '@' in line]

    if not emails:
        logging.error(f"No valid email addresses found in: {args.file}")
        return 1

    logging.info(f"\n--- Bulk Unblock: {len(emails)} contact(s) ---\n")

    success = 0
    skipped = 0
    failed = 0
    not_found = 0

    for email in emails:
        contact = client.get_contact(email)
        if not contact:
            logging.info(f"  [SKIP] Not found: {email}")
            not_found += 1
            continue

        excluded = contact.get("IsExcludedFromCampaigns", False)
        if not excluded:
            logging.info(f"  [SKIP] Already active: {email}")
            skipped += 1
            continue

        if args.dry_run:
            logging.info(f"  [DRY] Would unblock: {email}")
            success += 1
            continue

        try:
            updated = client.unblock_contact(email)
            if not updated.get("IsExcludedFromCampaigns", True):
                logging.info(f"  [OK] Unblocked: {email}")
                success += 1
            else:
                logging.error(f"  [FAIL] Still blocked: {email}")
                failed += 1
        except Exception as e:
            logging.error(f"  [FAIL] Error unblocking {email}: {e}")
            failed += 1

    logging.info(f"\n--- Summary ---")
    logging.info(f"  Unblocked: {success}")
    logging.info(f"  Skipped (already active): {skipped}")
    logging.info(f"  Not found: {not_found}")
    logging.info(f"  Failed: {failed}")

    return 0 if failed == 0 else 1


# =============================================================================
# Main
# =============================================================================

def main():
    args = parse_arguments()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    try:
        client = MailjetContactClient()
    except ValueError as e:
        logging.error(str(e))
        return 1

    action_map = {
        'check': lambda: action_check(client, args),
        'unblock': lambda: action_unblock(client, args),
        'block': lambda: action_block(client, args),
        'clear-bounce': lambda: action_clear_bounce(client, args),
        'list-blocked': lambda: action_list_blocked(client, args),
        'bulk-unblock': lambda: action_bulk_unblock(client, args),
    }

    handler = action_map.get(args.action)
    if handler:
        return handler()
    else:
        logging.error(f"Unknown action: {args.action}")
        return 1


if __name__ == "__main__":
    exit(main())
