#!/usr/bin/env python3
"""
Mailjet DNS Authentication Setup Script

Automates SPF, DKIM, and DMARC DNS record setup for Mailjet email sending.
Connects to the Mailjet API to retrieve required DNS values, then creates/updates
TXT records via Cloudflare or Bunny DNS.

FEATURES:
  - Registers domain sender (*@domain) in Mailjet
  - Retrieves DKIM public key and SPF include from Mailjet API
  - Smart SPF merge: inserts include:spf.mailjet.com into existing SPF records
  - Upgrades weak SPF qualifiers (?all -> ~all)
  - Creates DKIM TXT record (mailjet._domainkey)
  - Optional DMARC record creation with configurable policy
  - Optional domain ownership verification TXT record
  - Dry-run mode for previewing changes
  - Idempotent: safe to run multiple times
  - Markdown setup report generation

SUPPORTED DNS PROVIDERS:
  - Cloudflare (via official SDK)
  - Bunny DNS (via REST API)

Environment Variables:
  MAILJET_API_KEY       Mailjet API public key
  MAILJET_SECRET_KEY    Mailjet API private key
  CLOUDFLARE_API_TOKEN  Cloudflare API token (for Cloudflare DNS)
  BUNNY_API_KEY         Bunny.net API key (for Bunny DNS)

Usage:
  # Preview changes (dry run)
  python setup_mailjet_dns.py --domain example.com --dry-run

  # Full setup with Cloudflare DNS
  python setup_mailjet_dns.py --domain example.com --dmarc --dmarc-rua admin@example.com

  # Setup with Bunny DNS
  python setup_mailjet_dns.py --domain example.com --dns-provider bunny --dmarc

  # Include ownership verification record
  python setup_mailjet_dns.py --domain example.com --ownership-record --dmarc

  # Check current DNS authentication status
  python setup_mailjet_dns.py --domain example.com --check
"""

import os
import time
import logging
import argparse
import requests
from typing import Optional, List, Tuple

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass

# Optional: Cloudflare SDK
try:
    from cloudflare import Cloudflare
except ImportError:
    Cloudflare = None


# =============================================================================
# Mailjet API Client
# =============================================================================

class MailjetClient:
    """Mailjet REST API v3 client for sender and DNS management."""

    API_BASE = "https://api.mailjet.com/v3/REST"

    def __init__(self):
        api_key = os.environ.get("MAILJET_API_KEY")
        secret_key = os.environ.get("MAILJET_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError(
                "MAILJET_API_KEY and MAILJET_SECRET_KEY environment variables must be set."
            )
        self.auth = (api_key, secret_key)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Content-Type": "application/json"})

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to Mailjet REST API."""
        url = f"{self.API_BASE}{endpoint}"
        response = getattr(self.session, method.lower())(url, json=data)

        if response.status_code >= 400:
            raise Exception(
                f"Mailjet API error: {response.status_code} - {response.text}"
            )

        return response.json() if response.text else {}

    def find_sender(self, domain: str) -> Optional[dict]:
        """Find an existing sender for the domain (catch-all *@domain)."""
        result = self._api_request("GET", "/sender")
        catch_all = f"*@{domain}"
        for sender in result.get("Data", []):
            if sender.get("Email", "").lower() == catch_all.lower():
                return sender
        return None

    def create_sender(self, domain: str) -> dict:
        """Create a catch-all sender *@domain for the given domain."""
        catch_all = f"*@{domain}"
        logging.info(f"Creating Mailjet sender: {catch_all}")
        result = self._api_request("POST", "/sender", {"Email": catch_all})
        sender = result.get("Data", [{}])[0]
        logging.info(f"Sender created. ID: {sender.get('ID')}, DNSID: {sender.get('DNSID')}")
        return sender

    def get_or_create_sender(self, domain: str) -> dict:
        """Get existing sender or create a new one."""
        sender = self.find_sender(domain)
        if sender:
            logging.info(
                f"Found existing sender: *@{domain} "
                f"(ID: {sender.get('ID')}, Status: {sender.get('Status')})"
            )
            return sender
        return self.create_sender(domain)

    def get_dns_settings(self, domain: str) -> dict:
        """Retrieve DNS authentication settings for a domain.

        Returns dict with keys:
          DKIMRecordName, DKIMRecordValue, DKIMStatus,
          SPFRecordValue, SPFStatus,
          OwnerShipToken, OwnerShipTokenRecordName,
          ID (dns_id)
        """
        result = self._api_request("GET", f"/dns/{domain}")
        dns_data = result.get("Data", [{}])[0]
        logging.info(f"Retrieved DNS settings for {domain}")
        logging.info(f"  DKIM status: {dns_data.get('DKIMStatus')}")
        logging.info(f"  SPF status: {dns_data.get('SPFStatus')}")
        return dns_data

    def check_dns(self, dns_id: str) -> dict:
        """Trigger Mailjet DNS verification check."""
        logging.info(f"Triggering Mailjet DNS verification (ID: {dns_id})...")
        result = self._api_request("POST", f"/dns/{dns_id}/check", {})
        dns_data = result.get("Data", [{}])[0]
        logging.info(f"  DKIM status: {dns_data.get('DKIMStatus')}")
        logging.info(f"  SPF status: {dns_data.get('SPFStatus')}")
        return dns_data


# =============================================================================
# DNS Providers (TXT record focused)
# =============================================================================

class CloudflareTXTProvider:
    """Cloudflare DNS provider for TXT record management."""

    def __init__(self):
        if Cloudflare is None:
            raise ImportError(
                "Cloudflare library not found. Install it: pip install cloudflare"
            )
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        if not token:
            raise ValueError("CLOUDFLARE_API_TOKEN environment variable not set.")
        self.client = Cloudflare(api_token=token)

    def get_zone_id(self, zone_name: str) -> str:
        """Get Cloudflare zone ID for the domain."""
        zones = self.client.zones.list(name=zone_name)
        if not zones.result:
            raise Exception(f"Zone not found in Cloudflare: {zone_name}")
        zone_id = zones.result[0].id
        logging.info(f"Found Cloudflare zone: {zone_name} (ID: {zone_id})")
        return zone_id

    def get_txt_records(self, zone_id: str, name: str) -> List[dict]:
        """Get all TXT records for a given name."""
        records = self.client.dns.records.list(zone_id=zone_id, name=name, type="TXT")
        return [
            {
                "id": r.id,
                "name": r.name,
                "content": r.content,
            }
            for r in (records.result or [])
        ]

    def create_or_update_txt(self, zone_id: str, name: str, value: str,
                             zone_name: str) -> str:
        """Create or update a TXT record. Returns action taken."""
        full_name = name if name == zone_name or '.' in name else f"{name}.{zone_name}"

        existing = self.get_txt_records(zone_id, full_name)

        # For SPF records, find the existing SPF specifically
        is_spf = value.startswith("v=spf1")
        if is_spf:
            for record in existing:
                if record["content"].startswith("v=spf1"):
                    if record["content"] == value:
                        logging.info(f"TXT record '{full_name}' (SPF) already correct.")
                        return "exists"
                    self.client.dns.records.update(
                        zone_id=zone_id,
                        dns_record_id=record["id"],
                        name=name,
                        type="TXT",
                        content=value,
                    )
                    logging.info(f"TXT record '{full_name}' (SPF) updated.")
                    return "updated"
        else:
            # Non-SPF TXT: match by exact content or by name prefix
            for record in existing:
                if record["content"] == value:
                    logging.info(f"TXT record '{full_name}' already correct.")
                    return "exists"
                # For DKIM/ownership, update if same name exists with different value
                if not record["content"].startswith("v=spf1"):
                    # Check if this is the same record type (DKIM key or ownership token)
                    is_dkim_name = "_domainkey" in full_name
                    is_dmarc_name = full_name.startswith("_dmarc")
                    is_ownership = not is_dkim_name and not is_dmarc_name
                    record_is_dkim = "DKIM" in record["content"] or "k=rsa" in record["content"]

                    if (is_dkim_name and record_is_dkim) or is_dmarc_name or is_ownership:
                        self.client.dns.records.update(
                            zone_id=zone_id,
                            dns_record_id=record["id"],
                            name=name,
                            type="TXT",
                            content=value,
                        )
                        logging.info(f"TXT record '{full_name}' updated.")
                        return "updated"

        # Create new record
        self.client.dns.records.create(
            zone_id=zone_id,
            name=name,
            type="TXT",
            content=value,
            proxied=False,
        )
        logging.info(f"TXT record '{full_name}' created.")
        return "created"

    def delete_txt_record(self, zone_id: str, record_id: str) -> None:
        """Delete a TXT record by ID."""
        self.client.dns.records.delete(zone_id=zone_id, dns_record_id=record_id)
        logging.info(f"TXT record deleted (ID: {record_id})")


class BunnyTXTProvider:
    """Bunny DNS provider for TXT record management."""

    API_BASE = "https://api.bunny.net"

    RECORD_TYPES = {
        'A': 0,
        'AAAA': 1,
        'CNAME': 2,
        'TXT': 3,
        'MX': 4,
        'Redirect': 5,
        'Flatten': 6,
        'PullZone': 7,
        'SRV': 8,
        'CAA': 9,
        'PTR': 10,
        'Script': 11,
        'NS': 12,
    }

    def __init__(self):
        api_key = os.environ.get("BUNNY_API_KEY")
        if not api_key:
            raise ValueError("BUNNY_API_KEY environment variable not set.")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": api_key,
            "Content-Type": "application/json"
        })
        self._zone_cache = {}

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to Bunny.net API."""
        url = f"{self.API_BASE}{endpoint}"
        response = getattr(self.session, method.lower())(url, json=data)

        if response.status_code >= 400:
            raise Exception(f"Bunny API error: {response.status_code} - {response.text}")

        return response.json() if response.text and response.status_code not in [204] else {}

    def get_zone_id(self, zone_name: str) -> str:
        """Get zone ID, looking up by domain name."""
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]

        result = self._api_request("GET", "/dnszone")
        zones = result.get('Items', []) if isinstance(result, dict) else result

        for zone in zones:
            if zone.get('Domain') == zone_name:
                zone_id = str(zone.get('Id'))
                self._zone_cache[zone_name] = zone_id
                logging.info(f"Found Bunny DNS zone: {zone_name} (ID: {zone_id})")
                return zone_id

        raise Exception(f"Zone not found in Bunny DNS: {zone_name}")

    def _get_zone_records(self, zone_id: str) -> List[dict]:
        """Get all records for a zone."""
        result = self._api_request("GET", f"/dnszone/{zone_id}")
        return result.get('Records', [])

    def get_txt_records(self, zone_id: str, name: str) -> List[dict]:
        """Get all TXT records for a given name."""
        records = self._get_zone_records(zone_id)
        txt_type = self.RECORD_TYPES['TXT']
        return [
            {
                "id": r.get('Id'),
                "name": r.get('Name'),
                "content": r.get('Value'),
            }
            for r in records
            if r.get('Type') == txt_type and r.get('Name') == name
        ]

    def create_or_update_txt(self, zone_id: str, name: str, value: str,
                             zone_name: str) -> str:
        """Create or update a TXT record. Returns action taken."""
        existing = self.get_txt_records(zone_id, name)

        is_spf = value.startswith("v=spf1")
        if is_spf:
            for record in existing:
                if record["content"].startswith("v=spf1"):
                    if record["content"] == value:
                        logging.info(f"TXT record '{name}.{zone_name}' (SPF) already correct.")
                        return "exists"
                    # Delete old SPF, create new
                    self._api_request("DELETE", f"/dnszone/{zone_id}/records/{record['id']}")
                    logging.info(f"Deleted old SPF record for '{name}.{zone_name}'")
                    break
        else:
            for record in existing:
                if record["content"] == value:
                    logging.info(f"TXT record '{name}.{zone_name}' already correct.")
                    return "exists"
                # Update if same type of record
                if not record["content"].startswith("v=spf1"):
                    self._api_request("DELETE", f"/dnszone/{zone_id}/records/{record['id']}")
                    logging.info(f"Deleted old TXT record for '{name}.{zone_name}'")
                    break

        # Create new record
        data = {
            "Type": self.RECORD_TYPES['TXT'],
            "Name": name,
            "Value": value,
            "Ttl": 300,
        }
        self._api_request("PUT", f"/dnszone/{zone_id}/records", data)
        logging.info(f"TXT record '{name}.{zone_name}' created.")
        return "created"

    def delete_txt_record(self, zone_id: str, record_id: str) -> None:
        """Delete a TXT record by ID."""
        self._api_request("DELETE", f"/dnszone/{zone_id}/records/{record_id}")
        logging.info(f"TXT record deleted (ID: {record_id})")


def get_dns_provider(provider_name: str):
    """Factory function to get the appropriate DNS provider."""
    providers = {
        'cloudflare': CloudflareTXTProvider,
        'bunny': BunnyTXTProvider,
    }
    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}. Choose from: {list(providers.keys())}")
    return providers[provider_name]()


# =============================================================================
# SPF Record Merge Logic
# =============================================================================

def parse_spf(spf_record: str) -> Tuple[List[str], str]:
    """Parse an SPF record into mechanisms and qualifier.

    Returns:
        (mechanisms, qualifier) where mechanisms is a list of strings
        like ['include:_spf.google.com', 'ip4:1.2.3.4'] and qualifier
        is the all-mechanism string like '~all' or '-all'.
    """
    parts = spf_record.strip().split()
    mechanisms = []
    qualifier = "~all"  # default

    for part in parts:
        if part == "v=spf1":
            continue
        if part.endswith("all"):
            qualifier = part
        else:
            mechanisms.append(part)

    return mechanisms, qualifier


def merge_spf(existing_spf: Optional[str], target_qualifier: str = "~all") -> Tuple[str, List[str]]:
    """Merge Mailjet SPF include into existing SPF record.

    Returns:
        (merged_spf_record, list_of_warnings)
    """
    mailjet_include = "include:spf.mailjet.com"
    warnings = []

    if not existing_spf:
        record = f"v=spf1 {mailjet_include} {target_qualifier}"
        return record, ["No existing SPF record found. Created new one."]

    mechanisms, current_qualifier = parse_spf(existing_spf)

    # Check if Mailjet include already present
    if mailjet_include in mechanisms:
        # Still check qualifier
        if current_qualifier == "?all" and target_qualifier != "?all":
            warnings.append(
                f"Upgrading SPF qualifier from '{current_qualifier}' to '{target_qualifier}' "
                f"(?all means neutral/no policy, {target_qualifier} is recommended)"
            )
            current_qualifier = target_qualifier
        elif current_qualifier != target_qualifier:
            warnings.append(
                f"SPF qualifier is '{current_qualifier}', target is '{target_qualifier}'. "
                f"Keeping existing qualifier."
            )
            # Keep current qualifier unless upgrading from ?all
        record = f"v=spf1 {' '.join(mechanisms)} {current_qualifier}"
        return record, warnings if warnings else ["Mailjet SPF include already present."]

    # Insert Mailjet include
    mechanisms.append(mailjet_include)
    warnings.append(f"Added '{mailjet_include}' to SPF record.")

    # Check and optionally upgrade qualifier
    if current_qualifier == "?all" and target_qualifier != "?all":
        warnings.append(
            f"Upgrading SPF qualifier from '?all' to '{target_qualifier}' "
            f"(?all means neutral/no policy, {target_qualifier} provides softfail protection)"
        )
        current_qualifier = target_qualifier

    record = f"v=spf1 {' '.join(mechanisms)} {current_qualifier}"
    return record, warnings


def build_dmarc_record(policy: str = "none", rua_email: Optional[str] = None) -> str:
    """Build a DMARC TXT record value."""
    parts = [f"v=DMARC1; p={policy}"]
    if rua_email:
        parts.append(f"rua=mailto:{rua_email}")
    parts.append("sp=none")
    return "; ".join(parts)


# =============================================================================
# Setup Report
# =============================================================================

class SetupReport:
    """Track and report Mailjet DNS setup status."""

    def __init__(self, domain: str, dns_provider: str):
        self.timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        self.domain = domain
        self.dns_provider = dns_provider
        self.components = {
            'sender': {'status': 'pending', 'details': {}},
            'spf': {'status': 'pending', 'details': {}},
            'dkim': {'status': 'pending', 'details': {}},
            'dmarc': {'status': 'pending', 'details': {}},
            'ownership': {'status': 'pending', 'details': {}},
            'verification': {'status': 'pending', 'details': {}},
        }
        self.warnings = []

    def set_component(self, name: str, status: str, **details):
        if name in self.components:
            self.components[name]['status'] = status
            self.components[name]['details'].update(details)

    def add_warning(self, message: str):
        self.warnings.append(message)

    def generate_markdown(self) -> str:
        lines = [
            f"# Mailjet DNS Authentication Report: {self.domain}",
            "",
            f"**Generated:** {self.timestamp}",
            "**Script:** setup_mailjet_dns.py",
            f"**DNS Provider:** {self.dns_provider.title()}",
            "",
            "---",
            "",
            "## Configuration Summary",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Domain | `{self.domain}` |",
            f"| DNS Provider | {self.dns_provider.title()} |",
            f"| Sender | `*@{self.domain}` |",
            "",
            "## Component Status",
            "",
        ]

        status_icons = {
            'created': '[NEW]', 'exists': '[OK]', 'configured': '[OK]',
            'updated': '[UPD]', 'skipped': '[SKIP]', 'failed': '[FAIL]',
            'pending': '[ ]', 'dry_run': '[DRY]',
        }

        component_names = {
            'sender': 'Mailjet Sender',
            'spf': 'SPF Record',
            'dkim': 'DKIM Record',
            'dmarc': 'DMARC Record',
            'ownership': 'Ownership Verification',
            'verification': 'Mailjet DNS Check',
        }

        for comp_key, comp_name in component_names.items():
            comp = self.components[comp_key]
            status = comp['status']
            icon = status_icons.get(status, '[ ]')
            lines.append(f"### {icon} {comp_name}")
            lines.append("")
            lines.append(f"**Status:** {status.replace('_', ' ').title()}")

            for key, value in comp['details'].items():
                display_key = key.replace('_', ' ').title()
                if isinstance(value, list):
                    lines.append(f"- **{display_key}:**")
                    for item in value:
                        lines.append(f"  - `{item}`")
                else:
                    lines.append(f"- **{display_key}:** `{value}`")
            lines.append("")

        if self.warnings:
            lines.extend([
                "## Warnings",
                "",
            ])
            for warning in self.warnings:
                lines.append(f"- {warning}")
            lines.append("")

        lines.extend([
            "## DNS Records Summary",
            "",
            "| Record | Name | Value |",
            "|--------|------|-------|",
        ])

        for comp_key in ['spf', 'dkim', 'dmarc', 'ownership']:
            comp = self.components[comp_key]
            if comp['status'] not in ['pending', 'skipped']:
                name = comp['details'].get('record_name', '')
                value = comp['details'].get('record_value', '')
                if len(value) > 60:
                    value = value[:57] + "..."
                lines.append(f"| TXT | `{name}` | `{value}` |")

        lines.extend([
            "",
            "## Next Steps",
            "",
            "1. Wait 5-10 minutes for DNS propagation",
            "2. Re-run with `--skip-verify` removed to confirm Mailjet sees the records",
            "3. Check Mailjet dashboard: https://app.mailjet.com/account/sender",
            f"4. Verify with: `dig +short TXT {self.domain}` and `dig +short TXT mailjet._domainkey.{self.domain}`",
            "",
            "---",
            "",
            "*Generated by setup_mailjet_dns.py*",
        ])

        return '\n'.join(lines)

    def save_report(self, output_dir: str = '.') -> str:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        filename = f"mailjet-dns-setup-{self.domain}-{timestamp}.md"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.generate_markdown())

        logging.info(f"Setup report saved to: {filepath}")
        return filepath


# =============================================================================
# CLI
# =============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Setup Mailjet DNS authentication (SPF, DKIM, DMARC)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview changes without modifying DNS
  python setup_mailjet_dns.py --domain example.com --dry-run

  # Full setup with DMARC (Cloudflare DNS)
  python setup_mailjet_dns.py --domain example.com --dmarc --dmarc-rua admin@example.com

  # Setup with Bunny DNS
  python setup_mailjet_dns.py --domain example.com --dns-provider bunny --dmarc

  # Include ownership verification record
  python setup_mailjet_dns.py --domain example.com --ownership-record --dmarc

  # Strict SPF qualifier
  python setup_mailjet_dns.py --domain example.com --spf-qualifier="-all"

  # Check current DNS authentication status
  python setup_mailjet_dns.py --domain example.com --check

Environment Variables:
  MAILJET_API_KEY       Mailjet API public key (from account settings)
  MAILJET_SECRET_KEY    Mailjet API private key (from account settings)
  CLOUDFLARE_API_TOKEN  Cloudflare API token (for Cloudflare DNS)
  BUNNY_API_KEY         Bunny.net API key (for Bunny DNS)
        """
    )

    parser.add_argument('--domain', required=True,
                        help='Domain to configure (e.g., example.com)')
    parser.add_argument('--dns-provider', choices=['cloudflare', 'bunny'],
                        default='cloudflare',
                        help='DNS provider (default: cloudflare)')
    parser.add_argument('--dmarc', action='store_true',
                        help='Also create a DMARC record')
    parser.add_argument('--dmarc-policy', choices=['none', 'quarantine', 'reject'],
                        default='none',
                        help='DMARC policy (default: none)')
    parser.add_argument('--dmarc-rua',
                        help='DMARC aggregate report email (e.g., admin@example.com)')
    parser.add_argument('--ownership-record', action='store_true',
                        help='Also create the ownership verification TXT record')
    parser.add_argument('--spf-qualifier', choices=['~all', '-all', '?all'],
                        default='~all',
                        help='SPF qualifier for the all mechanism (default: ~all)')
    parser.add_argument('--check', action='store_true',
                        help='Check current DNS authentication status without making changes')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview DNS changes without making them')
    parser.add_argument('--skip-verify', action='store_true',
                        help='Skip Mailjet DNS check after setup')
    parser.add_argument('--report-dir', default='.',
                        help='Directory to save the setup report')

    return parser.parse_args()


# =============================================================================
# Check Mode
# =============================================================================

def run_check(args) -> int:
    """Check current DNS authentication status for a domain.

    Queries both Mailjet API and DNS provider to show current state
    of SPF, DKIM, DMARC, and sender configuration.
    """
    logging.info("=" * 60)
    logging.info("MAILJET DNS AUTHENTICATION CHECK")
    logging.info("=" * 60)
    logging.info(f"Domain: {args.domain}")
    logging.info(f"DNS Provider: {args.dns_provider}")
    logging.info("=" * 60)

    issues = []
    ok_count = 0

    try:
        # --- Mailjet API Status ---
        logging.info("\n--- Mailjet API Status ---")
        mailjet = MailjetClient()

        sender = mailjet.find_sender(args.domain)
        if sender:
            status = sender.get('Status', 'Unknown')
            logging.info(f"  Sender: *@{args.domain} (Status: {status})")
            if status == 'Active':
                ok_count += 1
            else:
                issues.append(f"Sender status is '{status}', expected 'Active'")
        else:
            logging.info(f"  Sender: *@{args.domain} NOT FOUND")
            issues.append("No catch-all sender registered in Mailjet")

        dns_settings = mailjet.get_dns_settings(args.domain)
        dkim_status_mj = dns_settings.get('DKIMStatus', 'Unknown')
        spf_status_mj = dns_settings.get('SPFStatus', 'Unknown')
        logging.info(f"  Mailjet DKIM status: {dkim_status_mj}")
        logging.info(f"  Mailjet SPF status: {spf_status_mj}")

        if dkim_status_mj == 'OK':
            ok_count += 1
        else:
            issues.append(f"Mailjet reports DKIM status: {dkim_status_mj}")

        if spf_status_mj == 'OK':
            ok_count += 1
        else:
            issues.append(f"Mailjet reports SPF status: {spf_status_mj}")

        # --- DNS Records ---
        logging.info("\n--- DNS Records ---")
        dns = get_dns_provider(args.dns_provider)
        zone_id = dns.get_zone_id(args.domain)

        # SPF
        spf_name = "" if args.dns_provider == "bunny" else args.domain
        spf_records = dns.get_txt_records(zone_id, spf_name)
        spf_found = False
        for record in spf_records:
            if record["content"].startswith("v=spf1"):
                spf_found = True
                logging.info(f"  SPF: {record['content']}")
                if "include:spf.mailjet.com" in record["content"]:
                    ok_count += 1
                    logging.info("    -> Mailjet include: PRESENT")
                else:
                    issues.append("SPF record missing 'include:spf.mailjet.com'")
                    logging.info("    -> Mailjet include: MISSING")

                _, qualifier = parse_spf(record["content"])
                if qualifier == "?all":
                    issues.append(f"SPF qualifier is '{qualifier}' (neutral/weak). Recommend '~all' or '-all'")
                    logging.info(f"    -> Qualifier: {qualifier} (WEAK)")
                else:
                    logging.info(f"    -> Qualifier: {qualifier}")
                break

        if not spf_found:
            logging.info("  SPF: NOT FOUND")
            issues.append("No SPF record found")

        # DKIM
        dkim_name_full = dns_settings.get('DKIMRecordName', f'mailjet._domainkey.{args.domain}')
        dkim_name_full = dkim_name_full.rstrip('.')  # Remove trailing dot from FQDN
        dkim_subdomain = dkim_name_full
        if dkim_name_full.endswith(f".{args.domain}"):
            dkim_subdomain = dkim_name_full[:-len(f".{args.domain}")]
        dkim_lookup = dkim_subdomain if args.dns_provider == "bunny" else dkim_name_full
        dkim_records = dns.get_txt_records(zone_id, dkim_lookup)
        dkim_found = False
        for record in dkim_records:
            if "k=rsa" in record["content"] or "DKIM" in record["content"]:
                dkim_found = True
                value_preview = record["content"][:80] + "..." if len(record["content"]) > 80 else record["content"]
                logging.info(f"  DKIM ({dkim_name_full}): {value_preview}")
                ok_count += 1
                break

        if not dkim_found:
            logging.info(f"  DKIM ({dkim_name_full}): NOT FOUND")
            issues.append(f"No DKIM record found at {dkim_name_full}")

        # DMARC
        dmarc_lookup = "_dmarc" if args.dns_provider == "bunny" else f"_dmarc.{args.domain}"
        dmarc_records = dns.get_txt_records(zone_id, dmarc_lookup)
        dmarc_found = False
        for record in dmarc_records:
            if record["content"].startswith("v=DMARC1"):
                dmarc_found = True
                logging.info(f"  DMARC: {record['content']}")
                ok_count += 1
                break

        if not dmarc_found:
            logging.info(f"  DMARC (_dmarc.{args.domain}): NOT FOUND")
            issues.append("No DMARC record found (recommended for deliverability)")

        # --- Summary ---
        logging.info("\n" + "=" * 60)
        logging.info("CHECK SUMMARY")
        logging.info("=" * 60)
        logging.info(f"  Checks passed: {ok_count}")
        logging.info(f"  Issues found:  {len(issues)}")

        if issues:
            logging.info("\n  Issues:")
            for i, issue in enumerate(issues, 1):
                logging.info(f"    {i}. {issue}")

            logging.info("\n  To fix, run:")
            logging.info(f"    python setup_mailjet_dns.py --domain {args.domain} --dmarc")
        else:
            logging.info("\n  All checks passed! Email authentication is properly configured.")

        logging.info("=" * 60)
        return 0 if not issues else 1

    except Exception as e:
        logging.error(f"Check failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


# =============================================================================
# Main Setup
# =============================================================================

def main():
    """Main function to orchestrate Mailjet DNS authentication setup."""
    args = parse_arguments()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # Check mode: read-only status check
    if args.check:
        return run_check(args)

    report = SetupReport(domain=args.domain, dns_provider=args.dns_provider)

    logging.info("=" * 60)
    logging.info("MAILJET DNS AUTHENTICATION SETUP")
    logging.info("=" * 60)
    logging.info(f"Domain: {args.domain}")
    logging.info(f"DNS Provider: {args.dns_provider}")
    logging.info(f"SPF Qualifier: {args.spf_qualifier}")
    logging.info(f"DMARC: {'Yes (policy: ' + args.dmarc_policy + ')' if args.dmarc else 'No'}")
    logging.info(f"Ownership Record: {'Yes' if args.ownership_record else 'No'}")
    logging.info(f"Dry Run: {'Yes' if args.dry_run else 'No'}")
    logging.info("=" * 60)

    try:
        # =================================================================
        # Step 1: Connect to Mailjet and get/create sender
        # =================================================================
        logging.info("\n--- Step 1: Mailjet Sender ---")
        mailjet = MailjetClient()
        sender = mailjet.get_or_create_sender(args.domain)
        report.set_component('sender', 'configured',
                             email=f"*@{args.domain}",
                             sender_id=str(sender.get('ID')),
                             status=sender.get('Status'))

        # =================================================================
        # Step 2: Retrieve DNS settings from Mailjet
        # =================================================================
        logging.info("\n--- Step 2: Retrieve DNS Settings ---")
        dns_settings = mailjet.get_dns_settings(args.domain)

        dkim_name = dns_settings.get('DKIMRecordName', '')
        dkim_value = dns_settings.get('DKIMRecordValue', '')
        spf_value = dns_settings.get('SPFRecordValue', '')
        ownership_token = dns_settings.get('OwnerShipToken', '')
        ownership_record_name = dns_settings.get('OwnerShipTokenRecordName', '')
        dns_id = dns_settings.get('ID')

        # Extract the subdomain part from the DKIM record name
        # e.g. "mailjet._domainkey.example.com" -> "mailjet._domainkey"
        dkim_subdomain = dkim_name
        if dkim_name.endswith(f".{args.domain}"):
            dkim_subdomain = dkim_name[:-len(f".{args.domain}")]

        logging.info(f"  DKIM record: {dkim_name}")
        logging.info(f"  DKIM value: {dkim_value[:60]}..." if len(dkim_value) > 60 else f"  DKIM value: {dkim_value}")
        logging.info(f"  SPF suggestion: {spf_value}")
        if ownership_token:
            logging.info(f"  Ownership token: {ownership_token}")

        # =================================================================
        # Step 3: Initialize DNS provider
        # =================================================================
        logging.info("\n--- Step 3: DNS Provider ---")
        if args.dry_run:
            logging.info(f"[DRY RUN] Would connect to {args.dns_provider} DNS")
            dns = None
            zone_id = "dry-run"
        else:
            dns = get_dns_provider(args.dns_provider)
            zone_id = dns.get_zone_id(args.domain)

        # =================================================================
        # Step 4: SPF Record
        # =================================================================
        logging.info("\n--- Step 4: SPF Record ---")

        if args.dry_run:
            # In dry run, we can't read existing records, show what we'd do
            merged_spf, spf_warnings = merge_spf(None, args.spf_qualifier)
            logging.info(f"[DRY RUN] Would create/update SPF record at '{args.domain}':")
            logging.info(f"  Value: {merged_spf}")
            logging.info("  Note: In live run, existing SPF will be merged if present")
            report.set_component('spf', 'dry_run',
                                 record_name=args.domain,
                                 record_value=merged_spf)
        else:
            # Read existing TXT records at root to find SPF
            existing_txt = dns.get_txt_records(zone_id, "" if args.dns_provider == "bunny" else args.domain)
            existing_spf = None
            for record in existing_txt:
                if record["content"].startswith("v=spf1"):
                    existing_spf = record["content"]
                    logging.info(f"  Found existing SPF: {existing_spf}")
                    break

            if not existing_spf:
                logging.info("  No existing SPF record found.")

            merged_spf, spf_warnings = merge_spf(existing_spf, args.spf_qualifier)
            for warning in spf_warnings:
                logging.info(f"  SPF: {warning}")
                report.add_warning(warning)

            logging.info(f"  Final SPF: {merged_spf}")

            # The name for root TXT in Bunny is "" (empty), in Cloudflare it's the domain
            spf_name = "" if args.dns_provider == "bunny" else args.domain
            action = dns.create_or_update_txt(zone_id, spf_name, merged_spf, args.domain)
            report.set_component('spf', action,
                                 record_name=args.domain,
                                 record_value=merged_spf)

        # =================================================================
        # Step 5: DKIM Record
        # =================================================================
        logging.info("\n--- Step 5: DKIM Record ---")

        if not dkim_value:
            logging.warning("  No DKIM value returned from Mailjet. Skipping DKIM setup.")
            report.set_component('dkim', 'skipped', reason='No DKIM value from Mailjet API')
        elif args.dry_run:
            logging.info("[DRY RUN] Would create DKIM TXT record:")
            logging.info(f"  Name: {dkim_name}")
            logging.info(f"  Value: {dkim_value[:60]}...")
            report.set_component('dkim', 'dry_run',
                                 record_name=dkim_name,
                                 record_value=dkim_value)
        else:
            dkim_record_name = dkim_subdomain if args.dns_provider == "bunny" else dkim_name
            action = dns.create_or_update_txt(zone_id, dkim_record_name, dkim_value, args.domain)
            report.set_component('dkim', action,
                                 record_name=dkim_name,
                                 record_value=dkim_value)

        # =================================================================
        # Step 6: DMARC Record (optional)
        # =================================================================
        logging.info("\n--- Step 6: DMARC Record ---")

        if not args.dmarc:
            logging.info("  DMARC not requested. Skipping. Use --dmarc to enable.")
            report.set_component('dmarc', 'skipped', reason='Not requested')
        elif args.dry_run:
            dmarc_value = build_dmarc_record(args.dmarc_policy, args.dmarc_rua)
            logging.info("[DRY RUN] Would create DMARC TXT record:")
            logging.info(f"  Name: _dmarc.{args.domain}")
            logging.info(f"  Value: {dmarc_value}")
            report.set_component('dmarc', 'dry_run',
                                 record_name=f"_dmarc.{args.domain}",
                                 record_value=dmarc_value)
        else:
            dmarc_value = build_dmarc_record(args.dmarc_policy, args.dmarc_rua)
            dmarc_name = "_dmarc" if args.dns_provider == "bunny" else f"_dmarc.{args.domain}"
            action = dns.create_or_update_txt(zone_id, dmarc_name, dmarc_value, args.domain)
            report.set_component('dmarc', action,
                                 record_name=f"_dmarc.{args.domain}",
                                 record_value=dmarc_value)

        # =================================================================
        # Step 7: Ownership Verification Record (optional)
        # =================================================================
        logging.info("\n--- Step 7: Ownership Verification ---")

        if not args.ownership_record:
            logging.info("  Ownership record not requested. Use --ownership-record to enable.")
            report.set_component('ownership', 'skipped', reason='Not requested')
        elif not ownership_token:
            logging.warning("  No ownership token returned from Mailjet. Skipping.")
            report.set_component('ownership', 'skipped', reason='No token from Mailjet API')
        elif args.dry_run:
            logging.info("[DRY RUN] Would create ownership TXT record:")
            logging.info(f"  Name: {ownership_record_name or args.domain}")
            logging.info(f"  Value: {ownership_token}")
            report.set_component('ownership', 'dry_run',
                                 record_name=ownership_record_name or args.domain,
                                 record_value=ownership_token)
        else:
            own_name = "" if args.dns_provider == "bunny" else (ownership_record_name or args.domain)
            action = dns.create_or_update_txt(zone_id, own_name, ownership_token, args.domain)
            report.set_component('ownership', action,
                                 record_name=ownership_record_name or args.domain,
                                 record_value=ownership_token)

        # =================================================================
        # Step 8: Mailjet DNS Verification
        # =================================================================
        logging.info("\n--- Step 8: Mailjet Verification ---")

        if args.dry_run:
            logging.info("[DRY RUN] Would trigger Mailjet DNS verification check.")
            report.set_component('verification', 'dry_run')
        elif args.skip_verify:
            logging.info("  Verification skipped (--skip-verify).")
            report.set_component('verification', 'skipped', reason='--skip-verify flag')
        elif dns_id:
            logging.info("  Waiting 5 seconds for DNS propagation...")
            time.sleep(5)
            check_result = mailjet.check_dns(dns_id)
            dkim_status = check_result.get('DKIMStatus', 'Unknown')
            spf_status = check_result.get('SPFStatus', 'Unknown')

            if dkim_status == 'OK' and spf_status == 'OK':
                report.set_component('verification', 'configured',
                                     dkim_status=dkim_status,
                                     spf_status=spf_status)
            else:
                report.set_component('verification', 'pending',
                                     dkim_status=dkim_status,
                                     spf_status=spf_status,
                                     note='DNS may need more time to propagate. Re-run later.')
                report.add_warning(
                    f"Verification not yet passing (DKIM: {dkim_status}, SPF: {spf_status}). "
                    "DNS propagation can take up to 10 minutes. Re-run the script to check again."
                )
        else:
            logging.warning("  No DNS ID available. Cannot trigger verification.")
            report.set_component('verification', 'skipped', reason='No DNS ID')

        # =================================================================
        # Summary
        # =================================================================
        logging.info("\n" + "=" * 60)
        logging.info("MAILJET DNS SETUP COMPLETE")
        logging.info("=" * 60)
        logging.info(f"Domain: {args.domain}")
        logging.info(f"Sender: *@{args.domain}")

        for comp_key, comp_name in [('spf', 'SPF'), ('dkim', 'DKIM'),
                                     ('dmarc', 'DMARC'), ('ownership', 'Ownership')]:
            status = report.components[comp_key]['status']
            logging.info(f"{comp_name}: {status}")

        logging.info("=" * 60)

        # Save report
        report_path = report.save_report(args.report_dir)

        # Print next steps
        logging.info("\n[Next Steps]")
        logging.info(f"1. Verify DNS propagation: dig +short TXT {args.domain}")
        logging.info(f"2. Verify DKIM: dig +short TXT mailjet._domainkey.{args.domain}")
        if args.dmarc:
            logging.info(f"3. Verify DMARC: dig +short TXT _dmarc.{args.domain}")
        logging.info("4. Check Mailjet dashboard: https://app.mailjet.com/account/sender")
        logging.info(f"\n[OK] Setup report: {report_path}")

        return 0

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()

        try:
            report_path = report.save_report(args.report_dir)
            logging.info(f"[i] Partial report saved: {report_path}")
        except Exception:
            pass

        return 1


if __name__ == "__main__":
    exit(main())
