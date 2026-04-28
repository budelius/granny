import boto3
import os
import json
import time
import logging
import argparse
import requests
import socket
from abc import ABC, abstractmethod
from typing import Optional

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass

# Optional imports for DNS providers
Cloudflare = None
try:
    from cloudflare import Cloudflare
except ImportError:
    pass  # Will be checked when cloudflare provider is selected

HetznerDnsZone = None
HetznerDnsRecord = None
try:
    from hetzner_dns_api import DnsZone as HetznerDnsZone, DnsRecord as HetznerDnsRecord
except ImportError:
    pass  # Will be checked when hetzner provider is selected

"""
Unified S3 Website CDN Setup Script

This script sets up a CDN for static content using AWS S3, CloudFront, and Cloudflare.
Supports both subdomain deployments (cdn.example.com) and production apex deployments (example.com).

MODES:
------
1. Subdomain Mode (default):
   - Setup CDN for subdomain.example.com
   - Use: python setup_s3_website.py --domain example.com --subdomain cdn-staging

2. Apex Mode (production):
   - Setup CDN for example.com with optional www → apex redirect
   - Uses CloudFront Function for reliable www redirect (not Cloudflare rules)
   - Use: python setup_s3_website.py --domain example.com --apex --www-redirect --spa-mode

SSL Configuration:
- When using --cloudflare-ssl (default): Cloudflare handles SSL termination
- When using --acm-ssl: AWS ACM certificate is created
- For apex mode: Certificate covers both apex (example.com) AND wildcard (*.example.com)

SPA (Single Page Application) Support:
- Use --spa-mode to configure CloudFront for client-side routing (React, Vue, Angular, etc.)
- 404 errors will be redirected to /index.html (or custom path) with HTTP 200 status

WWW Redirect (apex mode only):
- Use --www-redirect with --apex to redirect www.example.com → example.com
- Implemented via CloudFront Function (reliable, works regardless of Cloudflare plan)
- 301 permanent redirect preserving path and query string

Usage Examples:
    # Subdomain: Basic static site
    python setup_s3_website.py --domain example.com --subdomain cdn-staging

    # Subdomain: SPA application
    python setup_s3_website.py --domain example.com --subdomain app-dev --spa-mode

    # Apex: Production website with www redirect
    python setup_s3_website.py --domain example.com --apex --www-redirect --spa-mode

    # Apex: Production without www redirect (both domains serve content)
    python setup_s3_website.py --domain example.com --apex

Environment Variables:
    CLOUDFLARE_API_TOKEN: Required for Cloudflare DNS provider
    HETZNER_DNS_API_TOKEN: Required for Hetzner DNS provider
    DESEC_API_TOKEN: Required for deSEC DNS provider
    AWS_PROFILE: Optional - AWS profile to use
"""


# =============================================================================
# DNS Provider Abstraction Layer
# =============================================================================

class DNSProvider(ABC):
    """Abstract base class for DNS providers."""

    @abstractmethod
    def get_zone_id(self, zone_name: str) -> str:
        """Get zone ID by domain name."""
        pass

    @abstractmethod
    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        """Create or update a CNAME record."""
        pass

    @abstractmethod
    def create_validation_records(self, zone_id: str, zone_name: str,
                                  validation_input) -> list[dict]:
        """Create DNS records for ACM certificate validation.

        Args:
            zone_id: The zone ID
            zone_name: The zone/domain name
            validation_input: Either a single validation CNAME dict or list of validation options

        Returns:
            List of created DNS records for cleanup
        """
        pass

    @abstractmethod
    def cleanup_validation_records(self, zone_id: str,
                                   records: list[dict]) -> None:
        """Remove DNS validation records after certificate issuance."""
        pass

    @abstractmethod
    def supports_apex_cname(self) -> bool:
        """Returns True if provider supports CNAME-like records at apex."""
        pass

    @abstractmethod
    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cloudfront_domain: str) -> None:
        """Create DNS records for apex domain setup."""
        pass

    def supports_zone_creation(self) -> bool:
        """Returns True if provider supports creating new zones via API.

        Override in subclasses that support zone creation.
        """
        return False

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone.

        Args:
            zone_name: The domain name for the zone (e.g., 'example.com')

        Returns:
            The zone ID for the newly created zone

        Raises:
            NotImplementedError: If provider doesn't support zone creation
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support zone creation via API. "
            f"Please create the zone manually in the provider's dashboard."
        )

    def get_or_create_zone(self, zone_name: str, auto_create: bool = False) -> str:
        """Get zone ID, optionally creating the zone if it doesn't exist.

        Args:
            zone_name: The domain name for the zone
            auto_create: If True and zone doesn't exist, attempt to create it

        Returns:
            The zone ID

        Raises:
            Exception: If zone doesn't exist and auto_create is False or unsupported
        """
        try:
            return self.get_zone_id(zone_name)
        except Exception as e:
            if not auto_create:
                raise
            if not self.supports_zone_creation():
                raise Exception(
                    f"Zone '{zone_name}' not found and {self.__class__.__name__} "
                    f"does not support automatic zone creation. "
                    f"Please create the zone manually first."
                ) from e
            logging.info(f"Zone '{zone_name}' not found. Creating new zone...")
            return self.create_zone(zone_name)


class CloudflareDNSProvider(DNSProvider):
    """Cloudflare DNS API adapter."""

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
        """Get the Cloudflare zone ID for the domain."""
        zones = self.client.zones.list(name=zone_name)
        if not zones.result:
            raise Exception(f"Zone not found in Cloudflare: {zone_name}")
        return zones.result[0].id

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        """Creates or updates a CNAME record in Cloudflare pointing to CloudFront."""
        full_domain_name = f"{name}.{zone_name}"
        logging.info(f"Creating/updating CNAME record for '{full_domain_name}' -> '{target}'...")

        try:
            # Search for existing records
            existing_records = self.client.dns.records.list(zone_id=zone_id, name=full_domain_name)

            if not existing_records.result:
                existing_records = self.client.dns.records.list(zone_id=zone_id, name=name)

            dns_record = {
                'name': name,
                'type': 'CNAME',
                'content': target,
                'proxied': False  # Don't proxy - CloudFront handles SSL
            }

            cname_record_exists = False
            records_to_delete = []

            if existing_records.result:
                for record in existing_records.result:
                    if record.type == 'CNAME':
                        cname_record_exists = True
                        if record.content != target or record.proxied:
                            logging.info("Updating existing CNAME record")
                            self.client.dns.records.update(
                                zone_id=zone_id, dns_record_id=record.id, **dns_record
                            )
                            logging.info("CNAME record updated successfully.")
                        else:
                            logging.info("CNAME record already correct.")
                    elif record.type in ['A', 'AAAA']:
                        records_to_delete.append((record.id, record.type))

            # Delete conflicting A/AAAA records
            for record_id, record_type in records_to_delete:
                logging.info(f"Deleting conflicting {record_type} record")
                self.client.dns.records.delete(zone_id=zone_id, dns_record_id=record_id)

            # Create new CNAME if needed
            if not cname_record_exists:
                logging.info(f"Creating new CNAME record for '{name}'")
                self.client.dns.records.create(zone_id=zone_id, **dns_record)
                logging.info("CNAME record created successfully.")

        except Exception as e:
            logging.error(f"Error creating/updating Cloudflare CNAME record: {e}")
            raise

    def create_validation_records(self, zone_id: str, zone_name: str,
                                  validation_input) -> list[dict]:
        """Creates CNAME record(s) in Cloudflare for ACM validation."""
        logging.info("Setting up DNS validation record(s) in Cloudflare...")

        # Normalize input to list
        if isinstance(validation_input, list):
            validation_records = [opt['ResourceRecord'] for opt in validation_input if 'ResourceRecord' in opt]
        else:
            validation_records = [validation_input]

        created_records = []
        for validation_cname in validation_records:
            dns_record = {
                'name': validation_cname['Name'],
                'type': validation_cname['Type'],
                'content': validation_cname['Value'].rstrip('.'),
                'proxied': False,
                'ttl': 60
            }

            try:
                self.client.dns.records.create(zone_id=zone_id, **dns_record)
                logging.info(f"DNS validation record created for {validation_cname['Name']}")
                created_records.append(dns_record)
            except Exception as e:
                if "already exists" in str(e):
                    logging.warning(f"DNS record already exists for {validation_cname['Name']}")
                else:
                    raise

        return created_records

    def cleanup_validation_records(self, zone_id: str, records: list[dict]) -> None:
        """Removes the DNS validation records from Cloudflare."""
        if not records:
            return

        for dns_record in records:
            logging.info(f"Cleaning up DNS validation record: {dns_record['name']}")
            try:
                found = self.client.dns.records.list(
                    zone_id=zone_id, name=dns_record['name'], type=dns_record['type']
                )
                if found.result:
                    self.client.dns.records.delete(zone_id=zone_id, dns_record_id=found.result[0].id)
                    logging.info("DNS validation record cleaned up.")
            except Exception as e:
                logging.error(f"Error cleaning up DNS record: {e}")

    def supports_apex_cname(self) -> bool:
        return True  # Cloudflare supports CNAME flattening

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cloudfront_domain: str) -> None:
        """Create Cloudflare DNS records for apex domain with CNAME flattening."""
        logging.info("Configuring Cloudflare DNS for apex domain...")

        def delete_conflicting_records(name: str):
            full_name = name if '.' in name and name != apex_domain else f"{name}.{apex_domain}" if name != apex_domain else name
            try:
                existing = self.client.dns.records.list(zone_id=zone_id, name=full_name)
                if existing.result:
                    for rec in existing.result:
                        if rec.type in ['A', 'AAAA']:
                            logging.info(f"Deleting conflicting {rec.type} record: {full_name}")
                            self.client.dns.records.delete(zone_id=zone_id, dns_record_id=rec.id)
            except Exception as e:
                logging.warning(f"Error deleting records for {full_name}: {e}")

        def create_or_update_record(name: str, content: str, proxied: bool = False):
            full_name = name if name == apex_domain else f"{name}.{apex_domain}" if '.' not in name else name

            delete_conflicting_records(name)

            existing = self.client.dns.records.list(zone_id=zone_id, name=full_name)
            record_data = {'name': name, 'type': 'CNAME', 'content': content, 'proxied': proxied}

            if existing.result:
                for rec in existing.result:
                    if rec.type == 'CNAME':
                        if rec.content != content or rec.proxied != proxied:
                            logging.info(f"Updating CNAME: {full_name} → {content}")
                            self.client.dns.records.update(zone_id=zone_id, dns_record_id=rec.id, **record_data)
                        return

            logging.info(f"Creating CNAME: {full_name} → {content}")
            self.client.dns.records.create(zone_id=zone_id, **record_data)

        # Apex domain (CNAME flattening)
        create_or_update_record(apex_domain, cloudfront_domain, proxied=False)
        logging.info(f"Apex configured: {apex_domain} → {cloudfront_domain}")

        # WWW domain
        create_or_update_record('www', cloudfront_domain, proxied=False)
        logging.info(f"WWW configured: {www_domain} → {cloudfront_domain}")


class HetznerDNSProvider(DNSProvider):
    """Hetzner DNS API adapter."""

    def __init__(self):
        if HetznerDnsZone is None or HetznerDnsRecord is None:
            raise ImportError(
                "hetzner-dns-api library not found. Install it: pip install hetzner-dns-api"
            )
        token = os.environ.get("HETZNER_DNS_API_TOKEN")
        if not token:
            raise ValueError("HETZNER_DNS_API_TOKEN environment variable not set.")
        self.zone_api = HetznerDnsZone(token)
        self.record_api = HetznerDnsRecord(token)

    def get_zone_id(self, zone_name: str) -> str:
        """Get the Hetzner zone ID for the domain."""
        logging.info(f"Looking up Hetzner zone for '{zone_name}'...")
        for zone in self.zone_api.all(name=zone_name):
            if zone.name == zone_name:
                logging.info(f"Found zone ID: {zone.id}")
                return zone.id
        raise Exception(f"Zone not found in Hetzner DNS: {zone_name}")

    def _get_records_by_name(self, zone_id: str, name: str, record_type: Optional[str] = None) -> list:
        """Get all records matching name and optionally type."""
        matching = []
        for record in self.record_api.all(zone_id=zone_id):
            if record.name == name:
                if record_type is None or record.type == record_type:
                    matching.append(record)
        return matching

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        """Creates or updates a CNAME record in Hetzner DNS."""
        logging.info(f"Creating/updating CNAME record for '{name}.{zone_name}' -> '{target}'...")

        # Remove trailing dot from target if present
        target = target.rstrip('.')

        try:
            # Find existing records
            existing_cname = self._get_records_by_name(zone_id, name, 'CNAME')
            existing_a = self._get_records_by_name(zone_id, name, 'A')
            existing_aaaa = self._get_records_by_name(zone_id, name, 'AAAA')

            # Delete conflicting A/AAAA records
            for record in existing_a + existing_aaaa:
                logging.info(f"Deleting conflicting {record.type} record for '{name}'")
                self.record_api.delete(record.id)

            if existing_cname:
                # Update existing CNAME
                record = existing_cname[0]
                if record.value != target:
                    logging.info(f"Updating existing CNAME record: {record.value} → {target}")
                    self.record_api.update(
                        record_id=record.id,
                        zone_id=zone_id,
                        name=name,
                        record_type='CNAME',
                        value=target,
                        ttl=ttl
                    )
                    logging.info("CNAME record updated successfully.")
                else:
                    logging.info("CNAME record already points to correct target.")
            else:
                # Create new CNAME
                logging.info(f"Creating new CNAME record: {name} → {target}")
                self.record_api.create(
                    zone_id=zone_id,
                    name=name,
                    record_type='CNAME',
                    value=target,
                    ttl=ttl
                )
                logging.info("CNAME record created successfully.")

        except Exception as e:
            logging.error(f"Error creating/updating Hetzner CNAME record: {e}")
            raise

    def create_validation_records(self, zone_id: str, zone_name: str,
                                  validation_input) -> list[dict]:
        """Creates CNAME records in Hetzner for ACM validation."""
        logging.info("Setting up DNS validation record(s) in Hetzner DNS...")

        # Normalize input
        if isinstance(validation_input, list):
            validation_records = [opt['ResourceRecord'] for opt in validation_input if 'ResourceRecord' in opt]
        else:
            validation_records = [validation_input]

        created_records = []
        for validation_cname in validation_records:
            # Extract the subdomain part from the full validation name
            # e.g., _abc123.example.com -> _abc123
            full_name = validation_cname['Name'].rstrip('.')
            if full_name.endswith(f".{zone_name}"):
                name = full_name[:-len(f".{zone_name}")-1]
            else:
                name = full_name

            value = validation_cname['Value'].rstrip('.')

            try:
                # Check if already exists
                existing = self._get_records_by_name(zone_id, name, 'CNAME')
                if existing:
                    logging.info(f"Validation record already exists for {name}")
                else:
                    self.record_api.create(
                        zone_id=zone_id,
                        name=name,
                        record_type='CNAME',
                        value=value,
                        ttl=60
                    )
                    logging.info(f"DNS validation record created for {name}")

                created_records.append({
                    'name': name,
                    'type': 'CNAME',
                    'value': value
                })
            except Exception as e:
                logging.error(f"Error creating validation record: {e}")
                raise

        return created_records

    def cleanup_validation_records(self, zone_id: str, records: list[dict]) -> None:
        """Removes DNS validation records from Hetzner."""
        if not records:
            return

        for dns_record in records:
            logging.info(f"Cleaning up DNS validation record: {dns_record['name']}")
            try:
                existing = self._get_records_by_name(zone_id, dns_record['name'], 'CNAME')
                for record in existing:
                    self.record_api.delete(record.id)
                    logging.info("DNS validation record cleaned up.")
            except Exception as e:
                logging.error(f"Error cleaning up DNS record: {e}")

    def supports_apex_cname(self) -> bool:
        return False  # Hetzner doesn't support CNAME at apex

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cloudfront_domain: str) -> None:
        """Hetzner doesn't support CNAME at apex - create A records with CloudFront IPs."""
        logging.warning("=" * 60)
        logging.warning("  HETZNER DNS APEX DOMAIN LIMITATION")
        logging.warning("=" * 60)
        logging.warning("Hetzner DNS does not support CNAME records at the apex domain.")
        logging.warning("Creating A records pointing to CloudFront IP addresses.")
        logging.warning("")
        logging.warning("  WARNING: CloudFront IPs may change without notice!")
        logging.warning("  Monitor your site and update A records if CDN stops working.")
        logging.warning("  For production apex domains, consider using:")
        logging.warning("    --dns-provider desec  (EU + DNSSEC + HTTPS records)")
        logging.warning("    --dns-provider cloudflare  (CNAME flattening)")
        logging.warning("=" * 60)

        # Resolve CloudFront domain to IP addresses
        try:
            logging.info(f"Resolving CloudFront domain: {cloudfront_domain}")
            ips = socket.getaddrinfo(cloudfront_domain, 443, socket.AF_INET)
            unique_ips = list(set(ip[4][0] for ip in ips))
            logging.info(f"Found CloudFront IPs: {unique_ips}")
        except socket.gaierror as e:
            raise Exception(f"Could not resolve CloudFront domain {cloudfront_domain}: {e}")

        # Delete existing A/AAAA/CNAME records at apex
        existing_a = self._get_records_by_name(zone_id, '@', 'A')
        existing_aaaa = self._get_records_by_name(zone_id, '@', 'AAAA')
        for record in existing_a + existing_aaaa:
            logging.info(f"Deleting existing {record.type} record at apex")
            self.record_api.delete(record.id)

        # Create A records for each CloudFront IP
        for ip in unique_ips:
            logging.info(f"Creating A record: @ → {ip}")
            self.record_api.create(
                zone_id=zone_id,
                name='@',
                record_type='A',
                value=ip,
                ttl=300  # Short TTL for faster updates if needed
            )

        logging.info(f"Apex A records created: {apex_domain} → {unique_ips}")

        # Create www CNAME
        self.create_cname_record(zone_id, 'www', cloudfront_domain, apex_domain)
        logging.info(f"WWW configured: {www_domain} → {cloudfront_domain}")



class DeSECDNSProvider(DNSProvider):
    """deSEC DNS API adapter with HTTPS record support for apex domains.

    deSEC is a German non-profit providing free DNS with always-on DNSSEC.
    For apex domains, it uses HTTPS records (RFC 9460) instead of CNAME flattening.

    API Documentation: https://desec.readthedocs.io/
    """

    API_BASE = "https://desec.io/api/v1"
    # deSEC requires minimum TTL of 3600 seconds (1 hour)
    MIN_TTL = 3600

    def __init__(self):
        token = os.environ.get("DESEC_API_TOKEN")
        if not token:
            raise ValueError("DESEC_API_TOKEN environment variable not set.")
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json"
        })

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to deSEC."""
        url = f"{self.API_BASE}{endpoint}"
        try:
            if method == "GET":
                response = self.session.get(url)
            elif method == "POST":
                response = self.session.post(url, json=data)
            elif method == "PUT":
                response = self.session.put(url, json=data)
            elif method == "PATCH":
                response = self.session.patch(url, json=data)
            elif method == "DELETE":
                response = self.session.delete(url)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # Handle rate limiting
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                logging.warning(f"Rate limited by deSEC API. Waiting {retry_after}s...")
                time.sleep(retry_after)
                return self._api_request(method, endpoint, data)

            if response.status_code >= 400:
                logging.error(f"deSEC API error: {response.status_code} - {response.text}")
                response.raise_for_status()

            if response.status_code == 204:  # No content
                return {}

            return response.json() if response.text else {}

        except requests.RequestException as e:
            logging.error(f"deSEC API request failed: {e}")
            raise

    def get_zone_id(self, zone_name: str) -> str:
        """Get zone info - deSEC uses domain name as identifier."""
        logging.info(f"Looking up deSEC zone for '{zone_name}'...")
        try:
            result = self._api_request("GET", f"/domains/{zone_name}/")
            if result and result.get("name") == zone_name:
                logging.info(f"Found deSEC zone: {zone_name}")
                return zone_name  # deSEC uses domain name as ID
            raise Exception(f"Zone not found: {zone_name}")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                raise Exception(f"Zone not found in deSEC: {zone_name}")
            raise

    def supports_zone_creation(self) -> bool:
        """deSEC supports creating zones via API."""
        return True

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone in deSEC.

        Args:
            zone_name: The domain name for the zone (e.g., 'example.com')

        Returns:
            The zone name (deSEC uses domain name as ID)

        Raises:
            Exception: If zone creation fails
        """
        logging.info(f"Creating new deSEC zone for '{zone_name}'...")
        try:
            result = self._api_request("POST", "/domains/", {"name": zone_name})
            if result and result.get("name") == zone_name:
                logging.info(f"Successfully created deSEC zone: {zone_name}")
                # Log nameservers for user to configure at registrar
                if "keys" in result:
                    logging.info("Zone created with DNSSEC enabled (deSEC default)")
                # Get and display the nameservers
                self._display_nameservers(zone_name)
                return zone_name
            raise Exception(f"Unexpected response when creating zone: {result}")
        except requests.HTTPError as e:
            if e.response.status_code == 400:
                error_detail = e.response.json() if e.response.text else {}
                if "name" in error_detail:
                    # Check for common errors
                    name_errors = error_detail["name"]
                    if any("already exists" in str(err).lower() for err in name_errors):
                        logging.info(f"Zone '{zone_name}' already exists in deSEC")
                        return zone_name
                    raise Exception(f"Failed to create zone: {name_errors}")
            if e.response.status_code == 403:
                raise Exception(
                    f"Permission denied creating zone '{zone_name}'. "
                    f"Check your deSEC API token permissions."
                )
            raise Exception(f"Failed to create deSEC zone: {e}")

    def _display_nameservers(self, zone_name: str) -> None:
        """Display nameservers for a zone."""
        try:
            result = self._api_request("GET", f"/domains/{zone_name}/")
            if result:
                # deSEC nameservers are typically ns1.desec.io and ns2.desec.org
                logging.info("=" * 60)
                logging.info("IMPORTANT: Configure these nameservers at your domain registrar:")
                logging.info("  ns1.desec.io")
                logging.info("  ns2.desec.org")
                logging.info("=" * 60)
        except Exception as e:
            logging.warning(f"Could not retrieve nameserver info: {e}")

    def _get_rrset(self, domain: str, subname: str, record_type: str) -> Optional[dict]:
        """Get a specific RRset."""
        try:
            endpoint = f"/domains/{domain}/rrsets/{subname}/{record_type}/"
            return self._api_request("GET", endpoint)
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def _create_or_update_rrset(self, domain: str, subname: str, record_type: str,
                                records: list, ttl: int = 3600) -> None:
        """Create or update an RRset.

        Note: deSEC requires minimum TTL of 3600 seconds. Any lower value will be
        automatically adjusted to MIN_TTL.
        """
        # Enforce minimum TTL
        if ttl < self.MIN_TTL:
            logging.debug(f"TTL {ttl} below deSEC minimum, using {self.MIN_TTL}")
            ttl = self.MIN_TTL

        existing = self._get_rrset(domain, subname, record_type)

        data = {
            "subname": subname,
            "type": record_type,
            "records": records,
            "ttl": ttl
        }

        if existing:
            # Update existing
            endpoint = f"/domains/{domain}/rrsets/{subname}/{record_type}/"
            self._api_request("PUT", endpoint, data)
            logging.info(f"Updated {record_type} record for {subname or '@'}.{domain}")
        else:
            # Create new
            endpoint = f"/domains/{domain}/rrsets/"
            self._api_request("POST", endpoint, data)
            logging.info(f"Created {record_type} record for {subname or '@'}.{domain}")

    def _delete_rrset(self, domain: str, subname: str, record_type: str) -> None:
        """Delete an RRset."""
        try:
            endpoint = f"/domains/{domain}/rrsets/{subname}/{record_type}/"
            self._api_request("DELETE", endpoint)
            logging.info(f"Deleted {record_type} record for {subname or '@'}.{domain}")
        except requests.HTTPError as e:
            if e.response.status_code != 404:
                raise

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        """Creates or updates a CNAME record in deSEC."""
        logging.info(f"Creating/updating CNAME record for '{name}.{zone_name}' -> '{target}'...")

        # Ensure target ends with dot for deSEC
        if not target.endswith('.'):
            target = f"{target}."

        # Delete conflicting A/AAAA records
        self._delete_rrset(zone_name, name, 'A')
        self._delete_rrset(zone_name, name, 'AAAA')

        # Create/update CNAME
        self._create_or_update_rrset(zone_name, name, 'CNAME', [target], ttl)

    def create_validation_records(self, zone_id: str, zone_name: str,
                                  validation_input) -> list[dict]:
        """Creates CNAME records in deSEC for ACM validation."""
        logging.info("Setting up DNS validation record(s) in deSEC...")

        # Normalize input
        if isinstance(validation_input, list):
            validation_records = [opt['ResourceRecord'] for opt in validation_input if 'ResourceRecord' in opt]
        else:
            validation_records = [validation_input]

        created_records = []
        for validation_cname in validation_records:
            # Extract subdomain from full name
            full_name = validation_cname['Name'].rstrip('.')
            if full_name.endswith(f".{zone_name}"):
                subname = full_name[:-len(f".{zone_name}")-1]
            else:
                subname = full_name

            value = validation_cname['Value']
            if not value.endswith('.'):
                value = f"{value}."

            try:
                self._create_or_update_rrset(zone_name, subname, 'CNAME', [value], 60)
                created_records.append({
                    'name': subname,
                    'type': 'CNAME',
                    'value': value,
                    'domain': zone_name
                })
            except Exception as e:
                logging.error(f"Error creating validation record: {e}")
                raise

        return created_records

    def cleanup_validation_records(self, zone_id: str, records: list[dict]) -> None:
        """Removes DNS validation records from deSEC."""
        if not records:
            return

        for dns_record in records:
            logging.info(f"Cleaning up DNS validation record: {dns_record['name']}")
            try:
                domain = dns_record.get('domain', zone_id)
                self._delete_rrset(domain, dns_record['name'], 'CNAME')
            except Exception as e:
                logging.error(f"Error cleaning up DNS record: {e}")

    def supports_apex_cname(self) -> bool:
        return True  # deSEC supports apex via HTTPS records (RFC 9460)

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cloudfront_domain: str) -> None:
        """Create deSEC DNS records for apex domain using HTTPS records."""
        logging.info("Configuring deSEC DNS for apex domain with HTTPS records...")
        logging.info("Using RFC 9460 HTTPS records for apex domain support.")

        # Ensure CloudFront domain ends with dot
        cf_target = cloudfront_domain if cloudfront_domain.endswith('.') else f"{cloudfront_domain}."

        # Delete any conflicting A/AAAA records at apex
        self._delete_rrset(apex_domain, '', 'A')
        self._delete_rrset(apex_domain, '', 'AAAA')

        # Create HTTPS record at apex (RFC 9460)
        # Format: priority target [params]
        # priority=1, target=cloudfront domain, alpn for HTTP/2 support
        https_value = f'1 {cf_target} alpn="h2,http/1.1"'

        try:
            self._create_or_update_rrset(apex_domain, '', 'HTTPS', [https_value], 300)
            logging.info(f"Apex HTTPS record created: {apex_domain} -> {cloudfront_domain}")
        except Exception as e:
            logging.error(f"Failed to create HTTPS record: {e}")
            logging.warning("Falling back to A records (less reliable)...")
            # Fallback to A records like Hetzner
            self._create_apex_a_records(apex_domain, cloudfront_domain)

        # Create www CNAME
        self.create_cname_record(zone_id, 'www', cloudfront_domain, apex_domain)
        logging.info(f"WWW configured: {www_domain} -> {cloudfront_domain}")

    def _create_apex_a_records(self, apex_domain: str, cloudfront_domain: str) -> None:
        """Fallback: Create A records at apex (if HTTPS record fails)."""
        logging.warning("Creating A records as fallback - CloudFront IPs may change!")

        try:
            ips = socket.getaddrinfo(cloudfront_domain, 443, socket.AF_INET)
            unique_ips = list(set(ip[4][0] for ip in ips))
            logging.info(f"Resolved CloudFront IPs: {unique_ips}")

            self._create_or_update_rrset(apex_domain, '', 'A', unique_ips, 300)
            logging.info(f"Apex A records created: {apex_domain} -> {unique_ips}")
        except Exception as e:
            raise Exception(f"Failed to create apex A records: {e}")


def get_dns_provider(provider_name: str) -> DNSProvider:
    """Factory function to get the appropriate DNS provider."""
    providers = {
        'cloudflare': CloudflareDNSProvider,
        'hetzner': HetznerDNSProvider,
        'desec': DeSECDNSProvider,
    }

    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}. Available: {list(providers.keys())}")

    return providers[provider_name]()


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Setup CDN with S3, CloudFront, and Cloudflare',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic create with Cloudflare SSL (default)
  python setup_s3_website.py --domain lularge.com --subdomain cdn-staging

  # Apex domain setup (no subdomain)
  python setup_s3_website.py --domain example.com --apex

  # Apex with www redirect
  python setup_s3_website.py --domain example.com --apex --www-redirect

  # Setup with ACM SSL instead of Cloudflare
  python setup_s3_website.py --domain lularge.com --subdomain cdn-staging --acm-ssl

  # Use specific AWS profile
  python setup_s3_website.py --domain lularge.com --subdomain cdn-staging --aws-profile production

  # Force recreate CloudFront distribution
  python setup_s3_website.py --domain lularge.com --subdomain cdn-staging --force

  # Specify different region
  python setup_s3_website.py --domain lularge.com --subdomain cdn-staging --region eu-west-1

  # Setup for Single Page Application (React, Vue, Angular, etc.)
  python setup_s3_website.py --domain lularge.com --subdomain app-dev --spa-mode

  # Custom SPA error handling
  python setup_s3_website.py --domain lularge.com --subdomain app --spa-mode --spa-error-path /app.html --spa-error-code 200

Environment Variables:
  CLOUDFLARE_API_TOKEN: Required - Your Cloudflare API token
  AWS_PROFILE: Optional - AWS profile to use (can be overridden by --aws-profile)
        """
    )

    parser.add_argument('--domain', required=True,
                        help='The root domain (e.g., lularge.com)')
    parser.add_argument('--subdomain', default=None,
                        help='The subdomain for the CDN (e.g., cdn-staging). Omit for apex domain.')
    parser.add_argument('--apex', action='store_true',
                        help='Setup for apex domain (no subdomain, e.g., example.com)')
    parser.add_argument('--www-redirect', action='store_true',
                        help='Create www redirect to apex domain (only valid with --apex)')
    parser.add_argument('--region', default='us-east-1',
                        help='AWS region for S3 bucket (default: us-east-1)')
    parser.add_argument('--aws-profile', default=None,
                        help='AWS profile to use for credentials (default: uses default profile or AWS_PROFILE env var)')

    ssl_group = parser.add_mutually_exclusive_group()
    ssl_group.add_argument('--cloudflare-ssl', action='store_true', default=True,
                          help='Use Cloudflare for SSL (default)')
    ssl_group.add_argument('--acm-ssl', action='store_true',
                          help='Use AWS ACM for SSL instead of Cloudflare')

    parser.add_argument('--test-timeout', type=int, default=60,
                        help='Timeout for CDN tests in seconds (default: 60)')
    parser.add_argument('--skip-tests', action='store_true',
                        help='Skip the CDN functionality tests')
    parser.add_argument('--force', action='store_true',
                        help='Force recreate CloudFront distribution even if it exists')
    
    # SPA (Single Page Application) support
    parser.add_argument('--spa-mode', action='store_true',
                        help='Configure CloudFront for Single Page Application routing (redirect 404s to index.html)')
    parser.add_argument('--spa-error-path', default='/index.html',
                        help='Path to serve for 404 errors in SPA mode (default: /index.html)')
    parser.add_argument('--spa-error-code', type=int, default=200,
                        help='HTTP response code for SPA 404 redirects (default: 200)')

    # DNS Provider selection
    parser.add_argument('--dns-provider', choices=['cloudflare', 'hetzner', 'desec'],
                        default='cloudflare',
                        help='DNS provider to use (default: cloudflare). '
                             'Requires corresponding API token env var.')
    parser.add_argument('--create-zone', action='store_true',
                        help='Create DNS zone if it does not exist (only supported by deSEC). '
                             'Note: You must configure nameservers at your registrar after creation.')

    return parser.parse_args()

# Parse command line arguments
args = parse_arguments()

# --- Configuration from command line ---
DOMAIN_NAME = args.domain
SUBDOMAIN = args.subdomain
APEX_MODE = args.apex
WWW_REDIRECT = args.www_redirect
DNS_PROVIDER_NAME = args.dns_provider

# Determine bucket name based on mode
if APEX_MODE:
    BUCKET_NAME = DOMAIN_NAME  # Apex domain as bucket name (e.g., example.com)
    WWW_DOMAIN = f"www.{DOMAIN_NAME}"
elif SUBDOMAIN:
    BUCKET_NAME = f"{SUBDOMAIN}.{DOMAIN_NAME}"
    WWW_DOMAIN = None
else:
    print("Error: Either --subdomain or --apex must be specified")
    exit(1)

CLOUDFLARE_ZONE_NAME = DOMAIN_NAME
AWS_REGION = args.region
AWS_PROFILE = args.aws_profile
USE_CLOUDFLARE_SSL = not args.acm_ssl  # If --acm-ssl is specified, don't use Cloudflare SSL
TEST_TIMEOUT = args.test_timeout
SKIP_TESTS = args.skip_tests
FORCE_RECREATE = args.force
SPA_MODE = args.spa_mode
SPA_ERROR_PATH = args.spa_error_path
SPA_ERROR_CODE = args.spa_error_code
CREATE_ZONE = args.create_zone

# --- Setup logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Log the configuration
logging.info("=== CDN Setup Configuration ===")
logging.info(f"Domain: {DOMAIN_NAME}")
if APEX_MODE:
    logging.info("Mode: Apex domain (production)")
    logging.info(f"Apex Domain: {BUCKET_NAME}")
    logging.info(f"WWW Domain: {WWW_DOMAIN}")
    logging.info(f"WWW Redirect: {WWW_REDIRECT}")
else:
    logging.info("Mode: Subdomain")
    logging.info(f"Subdomain: {SUBDOMAIN}")
    logging.info(f"Full domain: {BUCKET_NAME}")
logging.info(f"AWS Region: {AWS_REGION}")
logging.info(f"AWS Profile: {AWS_PROFILE if AWS_PROFILE else 'default'}")
logging.info(f"SSL Method: {'Cloudflare' if USE_CLOUDFLARE_SSL else 'AWS ACM'}")
logging.info(f"Skip Tests: {SKIP_TESTS}")
logging.info(f"Force Recreate: {FORCE_RECREATE}")
logging.info(f"SPA Mode: {SPA_MODE}")
if SPA_MODE:
    logging.info(f"  SPA Error Path: {SPA_ERROR_PATH}")
    logging.info(f"  SPA Response Code: {SPA_ERROR_CODE}")
logging.info(f"DNS Provider: {DNS_PROVIDER_NAME}")
logging.info(f"Create Zone: {CREATE_ZONE}")
logging.info("===============================")

# --- Initialize AWS clients ---
def create_aws_clients(aws_profile=None, region=None):
    """Create AWS clients with optional profile support.

    Note: ACM client is ALWAYS created in us-east-1 because CloudFront
    requires certificates to be in us-east-1 regardless of the S3 bucket region.
    """
    if aws_profile:
        logging.info(f"Creating AWS clients using profile: {aws_profile}")
        session = boto3.Session(profile_name=aws_profile)
    else:
        logging.info("Creating AWS clients using default credentials")
        session = boto3.Session()

    # S3 client uses the specified region for bucket operations
    s3_region = region or 'us-east-1'

    return {
        's3': session.client('s3', region_name=s3_region),
        # ACM MUST be in us-east-1 for CloudFront - this is an AWS requirement
        'acm': session.client('acm', region_name='us-east-1'),
        'cloudfront': session.client('cloudfront'),
        'sts': session.client('sts')
    }

# Create AWS clients with the specified profile
aws_clients = create_aws_clients(AWS_PROFILE, AWS_REGION)
s3_client = aws_clients['s3']
acm_client = aws_clients['acm']
cloudfront_client = aws_clients['cloudfront']
sts_client = aws_clients['sts']

def get_cloudflare_client():
    """Gets Cloudflare API token from environment variable and returns a client."""
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        logging.error("CLOUDFLARE_API_TOKEN environment variable not set.")
        raise ValueError("CLOUDFLARE_API_TOKEN not found in environment variables.")
    return Cloudflare(api_token=token)

def create_s3_bucket(bucket_name):
    """Creates an S3 bucket if it doesn't exist."""
    try:
        logging.info(f"Checking if bucket '{bucket_name}' exists...")
        s3_client.head_bucket(Bucket=bucket_name)
        logging.info(f"Bucket '{bucket_name}' already exists.")
    except s3_client.exceptions.ClientError as e:
        if e.response['Error']['Code'] == '404':
            logging.info(f"Bucket '{bucket_name}' does not exist. Creating...")
            if AWS_REGION == 'us-east-1':
                # For us-east-1, don't specify LocationConstraint
                s3_client.create_bucket(Bucket=bucket_name)
            else:
                # For other regions, specify the LocationConstraint
                s3_client.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={'LocationConstraint': AWS_REGION}
                )
            logging.info(f"Bucket '{bucket_name}' created successfully.")
        else:
            logging.error(f"Error checking bucket: {e}")
            raise

def create_test_content(bucket_name):
    """Creates a simple test HTML file in the S3 bucket."""
    logging.info(f"Creating test content in bucket '{bucket_name}'...")

    test_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>CDN Test Page</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .container {{ max-width: 600px; margin: 0 auto; text-align: center; }}
        .success {{ color: #28a745; }}
        .info {{ color: #17a2b8; background: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="success">&#127881; CDN Setup Successful!</h1>
        <p>Your CDN is working correctly.</p>
        <div class="info">
            <h3>Bucket Information</h3>
            <p><strong>Bucket:</strong> {bucket_name}</p>
            <p><strong>Region:</strong> {AWS_REGION}</p>
            <p><strong>Served via:</strong> CloudFront + Cloudflare</p>
            <p><strong>SSL:</strong> Cloudflare</p>
            <p><strong>S3 Website Endpoint:</strong> {get_s3_website_endpoint(bucket_name, AWS_REGION)}</p>
        </div>
        <p><small>Generated by setup_s3_website.py</small></p>
    </div>
</body>
</html>"""

    error_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Page Not Found - CDN</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .container {{ max-width: 600px; margin: 0 auto; text-align: center; }}
        .error {{ color: #dc3545; }}
        .info {{ color: #6c757d; background: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="error">404 - Page Not Found</h1>
        <p>The page you're looking for doesn't exist.</p>
        <div class="info">
            <p>This is a custom error page served from S3 static website hosting.</p>
            <p><a href="/">← Back to Home</a></p>
        </div>
        <p><small>CDN: {bucket_name}</small></p>
    </div>
</body>
</html>"""

    try:
        # Upload the test HTML file
        s3_client.put_object(
            Bucket=bucket_name,
            Key='index-test.html',
            Body=test_html.encode('utf-8'),
            ContentType='text/html',
            CacheControl='max-age=300'
        )
        logging.info("Test content (index-test.html) uploaded successfully.")

        # Upload the error HTML file
        s3_client.put_object(
            Bucket=bucket_name,
            Key='error.html',
            Body=error_html.encode('utf-8'),
            ContentType='text/html',
            CacheControl='max-age=300'
        )
        logging.info("Error page (error.html) uploaded successfully.")

        # Also create a simple test file for verification
        test_text = f"CDN test file - {bucket_name} - Generated at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
        s3_client.put_object(
            Bucket=bucket_name,
            Key='test.txt',
            Body=test_text.encode('utf-8'),
            ContentType='text/plain',
            CacheControl='max-age=300'
        )
        logging.info("Test file (test.txt) uploaded successfully.")

        # If SPA mode is enabled, create a proper index.html file for SPA testing
        if SPA_MODE:
            spa_index_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>SPA Test - {bucket_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        .success {{ color: #28a745; }}
        .info {{ color: #17a2b8; background: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0; }}
        .route {{ padding: 10px; margin: 10px 0; background: #e9ecef; border-radius: 3px; }}
        a {{ color: #007bff; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="success">&#127881; SPA-Enabled CDN Setup Successful!</h1>
        <p>Your CDN is configured for Single Page Application routing.</p>
        <div class="info">
            <h3>Configuration</h3>
            <p><strong>Bucket:</strong> {bucket_name}</p>
            <p><strong>Region:</strong> {AWS_REGION}</p>
            <p><strong>SPA Mode:</strong> Enabled</p>
            <p><strong>Error Path:</strong> {SPA_ERROR_PATH}</p>
            <p><strong>Error Code:</strong> {SPA_ERROR_CODE}</p>
        </div>
        <div class="info">
            <h3>Test SPA Routing</h3>
            <p>The following URLs should all serve this page (thanks to CloudFront custom error responses):</p>
            <div class="route">
                <a href="/">https://{bucket_name}/</a> - Root page
            </div>
            <div class="route">
                <a href="/about">https://{bucket_name}/about</a> - Should serve this page
            </div>
            <div class="route">
                <a href="/products/123">https://{bucket_name}/products/123</a> - Should serve this page
            </div>
            <div class="route">
                <a href="/any/nested/route">https://{bucket_name}/any/nested/route</a> - Should serve this page
            </div>
        </div>
        <p><small>Generated by setup_s3_website.py with --spa-mode</small></p>
    </div>
</body>
</html>"""
            
            s3_client.put_object(
                Bucket=bucket_name,
                Key='index.html',
                Body=spa_index_html.encode('utf-8'),
                ContentType='text/html',
                CacheControl='max-age=300'
            )
            logging.info("SPA index.html uploaded successfully.")

    except Exception as e:
        logging.error(f"Error uploading test content: {e}")
        raise

def find_existing_acm_certificate(domain_name):
    """Find an existing ACM certificate that covers the domain."""
    logging.info(f"Searching for existing ACM certificates for '{domain_name}'...")

    try:
        # List all certificates in the region
        response = acm_client.list_certificates(
            CertificateStatuses=['ISSUED']  # Only look for valid certificates
        )

        for cert_summary in response['CertificateSummaryList']:
            cert_arn = cert_summary['CertificateArn']
            cert_domain = cert_summary['DomainName']

            # Get detailed certificate info to check Subject Alternative Names
            cert_details = acm_client.describe_certificate(CertificateArn=cert_arn)
            cert_info = cert_details['Certificate']

            # Check if this certificate covers our domain
            covered_domains = [cert_info['DomainName']]
            if 'SubjectAlternativeNames' in cert_info:
                covered_domains.extend(cert_info['SubjectAlternativeNames'])

            for covered_domain in covered_domains:
                # Check for exact match or wildcard match
                if (covered_domain == domain_name or
                    (covered_domain.startswith('*.') and
                     domain_name.endswith(covered_domain[1:]))):

                    logging.info(f"Found existing certificate: {cert_arn}")
                    logging.info(f"  Certificate domain: {cert_domain}")
                    logging.info(f"  Covers domains: {covered_domains}")
                    logging.info(f"  Status: {cert_info['Status']}")
                    return cert_arn

        logging.info(f"No existing ACM certificate found for '{domain_name}'")
        return None

    except Exception as e:
        logging.error(f"Error searching for existing certificates: {e}")
        return None

def find_certificate_covering_domains(domains):
    """Find an existing ACM certificate that covers ALL specified domains.

    This is used for apex domain setups where we need a certificate that covers
    both the apex domain (example.com) and subdomains (*.example.com).
    A wildcard cert alone does NOT cover the apex domain.
    """
    logging.info(f"Searching for certificate covering all domains: {domains}")

    try:
        response = acm_client.list_certificates(CertificateStatuses=['ISSUED'])

        for cert_summary in response['CertificateSummaryList']:
            cert_arn = cert_summary['CertificateArn']
            cert_details = acm_client.describe_certificate(CertificateArn=cert_arn)
            cert_info = cert_details['Certificate']

            covered_domains = set([cert_info['DomainName']])
            if 'SubjectAlternativeNames' in cert_info:
                covered_domains.update(cert_info['SubjectAlternativeNames'])

            # Check if ALL required domains are covered
            all_covered = True
            for domain in domains:
                domain_covered = False
                for covered in covered_domains:
                    if covered == domain:
                        domain_covered = True
                        break
                    # Wildcard match (*.example.com covers www.example.com but NOT example.com)
                    if covered.startswith('*.') and domain.endswith(covered[1:]) and domain != covered[2:]:
                        domain_covered = True
                        break
                if not domain_covered:
                    all_covered = False
                    break

            if all_covered:
                logging.info(f"Found certificate covering all domains: {cert_arn}")
                logging.info(f"  Covered domains: {covered_domains}")
                return cert_arn

        logging.info("No existing certificate covers all required domains")
        return None

    except Exception as e:
        logging.error(f"Error searching certificates: {e}")
        return None

def request_acm_certificate(domain_name, san_domains=None):
    """Requests an ACM certificate for the domain and returns its ARN and validation info.

    Args:
        domain_name: Primary domain for the certificate
        san_domains: Optional list of Subject Alternative Names (e.g., for apex + wildcard)

    Returns:
        Tuple of (certificate_arn, validation_options, waiter)
        - validation_options is a list when san_domains is provided, single dict otherwise
    """
    logging.info(f"Requesting ACM certificate for '{domain_name}'...")

    cert_params = {
        'DomainName': domain_name,
        'ValidationMethod': 'DNS'
    }

    if san_domains:
        cert_params['SubjectAlternativeNames'] = san_domains
        logging.info(f"Including SANs: {san_domains}")

    response = acm_client.request_certificate(**cert_params)
    certificate_arn = response['CertificateArn']

    logging.info(f"Waiting for certificate details to become available for {certificate_arn}...")
    time.sleep(10)  # Give some time for the validation options to be available

    cert_description = acm_client.describe_certificate(CertificateArn=certificate_arn)
    validation_options = cert_description['Certificate']['DomainValidationOptions']

    # Wait for all validation records to be available
    while not all('ResourceRecord' in opt for opt in validation_options):
        logging.info("Waiting for DNS validation records to be created by AWS...")
        time.sleep(10)
        cert_description = acm_client.describe_certificate(CertificateArn=certificate_arn)
        validation_options = cert_description['Certificate']['DomainValidationOptions']

    waiter = acm_client.get_waiter('certificate_validated')

    logging.info(f"Certificate requested. ARN: {certificate_arn}")
    for opt in validation_options:
        if 'ResourceRecord' in opt:
            logging.info(f"DNS validation record: {opt['ResourceRecord']['Name']} -> {opt['ResourceRecord']['Value']}")

    # For backward compatibility, return single validation_cname if no SANs
    if not san_domains:
        return certificate_arn, validation_options[0]['ResourceRecord'], waiter
    else:
        return certificate_arn, validation_options, waiter

def setup_dns_validation(cf_client, zone_name, validation_input):
    """Creates CNAME record(s) in Cloudflare for ACM validation.

    Args:
        cf_client: Cloudflare client
        zone_name: Cloudflare zone name
        validation_input: Either a single validation CNAME dict or a list of validation options

    Returns:
        Tuple of (zone_id, created_records) where created_records is a list of dicts
    """
    logging.info("Setting up DNS validation record(s) in Cloudflare...")
    try:
        zones = cf_client.zones.list(name=zone_name)
        if not zones.result:
            logging.error(f"Zone '{zone_name}' not found in Cloudflare.")
            raise Exception(f"Zone not found: {zone_name}")
        zone_id = zones.result[0].id

        # Normalize input to list of validation records
        if isinstance(validation_input, list):
            # List of validation options from SAN certificate
            validation_records = []
            for opt in validation_input:
                if 'ResourceRecord' in opt:
                    validation_records.append(opt['ResourceRecord'])
        else:
            # Single validation CNAME dict
            validation_records = [validation_input]

        created_records = []
        for validation_cname in validation_records:
            dns_record = {
                'name': validation_cname['Name'],
                'type': validation_cname['Type'],
                'content': validation_cname['Value'].rstrip('.'),
                'proxied': False,
                'ttl': 60
            }

            try:
                cf_client.dns.records.create(zone_id=zone_id, **dns_record)
                logging.info(f"DNS validation record created for {validation_cname['Name']}.")
                created_records.append(dns_record)
            except Exception as e:
                if "already exists" in str(e):
                    logging.warning(f"DNS record for {validation_cname['Name']} already exists.")
                else:
                    raise

        return zone_id, created_records
    except Exception as e:
        if "already exists" in str(e):
            logging.warning("DNS record already exists.")
            return None, None
        logging.error(f"Cloudflare API error: {e}")
        raise

def wait_for_cert_validation(waiter, certificate_arn):
    """Waits for the ACM certificate to be validated."""
    logging.info("Waiting for ACM certificate validation... This may take a few minutes.")
    try:
        waiter.wait(
            CertificateArn=certificate_arn,
            WaiterConfig={'Delay': 30, 'MaxAttempts': 20}
        )
        logging.info("Certificate has been validated successfully.")
    except Exception as e:
        logging.error(f"Certificate validation failed: {e}")
        raise

def get_existing_oac(oac_name):
    """Check if an OAC with the given name already exists."""
    try:
        response = cloudfront_client.list_origin_access_controls()
        for oac in response['OriginAccessControlList']['Items']:
            if oac['Name'] == oac_name:
                logging.info(f"OAC '{oac_name}' already exists with ID: {oac['Id']}")
                return oac['Id']
        return None
    except Exception as e:
        logging.error(f"Error checking existing OACs: {e}")
        return None

def create_cloudfront_oac():
    """Creates a CloudFront Origin Access Control."""
    oac_name = f"oac-{BUCKET_NAME}"
    logging.info(f"Checking if OAC '{oac_name}' already exists...")

    existing_oac_id = get_existing_oac(oac_name)
    if existing_oac_id:
        return existing_oac_id

    logging.info("Creating CloudFront Origin Access Control (OAC)...")
    response = cloudfront_client.create_origin_access_control(
        OriginAccessControlConfig={
            'Name': oac_name,
            'Description': f"OAC for {BUCKET_NAME}",
            'SigningProtocol': 'sigv4',
            'SigningBehavior': 'always',
            'OriginAccessControlOriginType': 's3'
        }
    )
    oac_id = response['OriginAccessControl']['Id']
    logging.info(f"OAC created with ID: {oac_id}")
    return oac_id

def get_existing_distribution(bucket_name):
    """Check if a CloudFront distribution for the bucket already exists."""
    try:
        response = cloudfront_client.list_distributions()
        if 'DistributionList' in response and 'Items' in response['DistributionList']:
            for dist in response['DistributionList']['Items']:
                # Check origins to see if any match our bucket
                for origin in dist.get('Origins', {}).get('Items', []):
                    if bucket_name in origin.get('DomainName', ''):
                        logging.info(f"Found existing distribution for '{bucket_name}': {dist['Id']}")
                        return dist
        return None
    except Exception as e:
        logging.error(f"Error checking existing distributions: {e}")
        return None

def find_distribution_using_cname(domain_name):
    """Find any CloudFront distribution that uses the specified domain as an alias."""
    try:
        logging.info(f"Searching for distributions using CNAME '{domain_name}'...")
        response = cloudfront_client.list_distributions()
        if 'DistributionList' in response and 'Items' in response['DistributionList']:
            for dist in response['DistributionList']['Items']:
                # Check aliases
                aliases = dist.get('Aliases', {}).get('Items', [])
                if domain_name in aliases:
                    logging.info(f"Found distribution using CNAME '{domain_name}': {dist['Id']}")
                    logging.info(f"  Distribution status: {dist['Status']}")
                    logging.info(f"  Distribution enabled: {dist['Enabled']}")
                    logging.info(f"  All aliases: {aliases}")
                    return dist
        return None
    except Exception as e:
        logging.error(f"Error searching for distributions using CNAME: {e}")
        return None

def enable_s3_website_hosting(bucket_name):
    """Enable static website hosting on the S3 bucket."""
    logging.info(f"Enabling static website hosting for bucket '{bucket_name}'...")

    try:
        # First, disable block public access settings to allow public bucket policy
        logging.info("Disabling block public access settings...")
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                'BlockPublicAcls': False,
                'IgnorePublicAcls': False,
                'BlockPublicPolicy': False,
                'RestrictPublicBuckets': False
            }
        )
        logging.info("Block public access settings disabled successfully.")

        # Wait a moment for the settings to propagate
        time.sleep(5)

        # Configure the bucket for static website hosting
        website_configuration = {
            'IndexDocument': {
                'Suffix': 'index.html'
            },
            'ErrorDocument': {
                'Key': 'error.html'
            }
        }

        s3_client.put_bucket_website(
            Bucket=bucket_name,
            WebsiteConfiguration=website_configuration
        )
        logging.info("S3 static website hosting enabled successfully.")

        # Set bucket policy to allow public read access for website hosting
        public_read_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicReadGetObject",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket_name}/*"
                }
            ]
        }

        s3_client.put_bucket_policy(
            Bucket=bucket_name,
            Policy=json.dumps(public_read_policy)
        )
        logging.info("S3 bucket policy updated for public read access.")

    except Exception as e:
        logging.error(f"Error enabling S3 website hosting: {e}")
        raise

def get_s3_website_endpoint(bucket_name, region):
    """Get the S3 static website endpoint for the bucket."""
    if region == 'us-east-1':
        return f"{bucket_name}.s3-website-us-east-1.amazonaws.com"
    else:
        return f"{bucket_name}.s3-website.{region}.amazonaws.com"

def update_cloudfront_distribution_origin(distribution_id, bucket_name, oac_id):
    """Update an existing CloudFront distribution with the correct S3 website origin."""
    logging.info(f"Updating CloudFront distribution {distribution_id} with S3 website origin...")

    try:
        # Get the current distribution configuration
        response = cloudfront_client.get_distribution_config(Id=distribution_id)
        distribution_config = response['DistributionConfig']
        etag = response['ETag']

        # Use the S3 website endpoint
        s3_website_domain = get_s3_website_endpoint(bucket_name, AWS_REGION)

        logging.info(f"Updating origin to use S3 website endpoint: {s3_website_domain}")

        # Use the domain name (bucket_name) as the origin ID
        origin_id = bucket_name

        # Update the origin configuration
        for origin in distribution_config.get('Origins', {}).get('Items', []):
            if bucket_name in origin.get('DomainName', ''):
                logging.info(f"Updating origin: {origin.get('DomainName')} -> {s3_website_domain}")
                origin['Id'] = origin_id  # Update the origin ID to use domain name
                origin['DomainName'] = s3_website_domain

                # Remove S3OriginConfig and OAC since we're using website endpoint
                if 'S3OriginConfig' in origin:
                    del origin['S3OriginConfig']
                if 'OriginAccessControlId' in origin:
                    del origin['OriginAccessControlId']

                # Use CustomOriginConfig for S3 website endpoint
                # OriginSslProtocols is required even for http-only
                origin['CustomOriginConfig'] = {
                    'HTTPPort': 80,
                    'HTTPSPort': 443,
                    'OriginProtocolPolicy': 'http-only',
                    'OriginSslProtocols': {
                        'Quantity': 1,
                        'Items': ['TLSv1.2']
                    },
                    'OriginReadTimeout': 30,
                    'OriginKeepaliveTimeout': 5
                }

                logging.info(f"Origin configuration updated: {origin}")
                break

        # Update the default cache behavior to use the new origin ID
        if 'DefaultCacheBehavior' in distribution_config:
            distribution_config['DefaultCacheBehavior']['TargetOriginId'] = origin_id
            logging.info(f"Updated default cache behavior to target origin ID: {origin_id}")
        else:
            logging.warning("DefaultCacheBehavior not found in distribution config, skipping target origin update")

        # Ensure aliases are properly configured (always ensure aliases are configured)
        aliases_items = distribution_config.get('Aliases', {}).get('Items', [])
        if 'Aliases' not in distribution_config or not aliases_items:
            logging.info(f"Adding missing alias: {bucket_name}")
            distribution_config['Aliases'] = {
                'Quantity': 1,
                'Items': [bucket_name]
            }
        elif bucket_name not in aliases_items:
            logging.info(f"Adding alias to existing list: {bucket_name}")
            distribution_config['Aliases']['Items'].append(bucket_name)
            distribution_config['Aliases']['Quantity'] = len(distribution_config['Aliases']['Items'])
        else:
            logging.info(f"Alias {bucket_name} already configured")

        if USE_CLOUDFLARE_SSL:
            logging.info("Using Cloudflare SSL - but aliases are still required for proper routing")

        # Add custom error responses for SPA mode if updating existing distribution
        if SPA_MODE:
            existing_error_responses = distribution_config.get('CustomErrorResponses', {}).get('Items', [])
            spa_error_exists = any(
                err.get('ErrorCode') == 404 and err.get('ResponsePagePath') == SPA_ERROR_PATH 
                for err in existing_error_responses
            )
            
            if not spa_error_exists:
                logging.info("Adding SPA mode error handling to existing distribution")
                distribution_config['CustomErrorResponses'] = {
                    'Quantity': 1,
                    'Items': [{
                        'ErrorCode': 404,
                        'ResponsePagePath': SPA_ERROR_PATH,
                        'ResponseCode': str(SPA_ERROR_CODE),
                        'ErrorCachingMinTTL': 0
                    }]
                }
            else:
                logging.info("SPA error handling already configured for distribution")

        # Update the distribution
        update_response = cloudfront_client.update_distribution(
            Id=distribution_id,
            DistributionConfig=distribution_config,
            IfMatch=etag
        )

        logging.info("CloudFront distribution updated successfully.")
        logging.info(f"Origin ID: {origin_id}")
        logging.info("Note: It may take 5-15 minutes for changes to propagate globally.")
        return update_response['Distribution']

    except Exception as e:
        logging.error(f"Error updating CloudFront distribution: {e}")
        raise

def delete_cloudfront_distribution(distribution_id):
    """Delete an existing CloudFront distribution."""
    logging.info(f"Deleting CloudFront distribution {distribution_id}...")

    try:
        # First, get the distribution configuration
        response = cloudfront_client.get_distribution_config(Id=distribution_id)
        distribution_config = response['DistributionConfig']
        etag = response['ETag']

        # Disable the distribution first
        if distribution_config['Enabled']:
            logging.info("Disabling CloudFront distribution...")
            distribution_config['Enabled'] = False

            cloudfront_client.update_distribution(
                Id=distribution_id,
                DistributionConfig=distribution_config,
                IfMatch=etag
            )

            # Wait for the distribution to be disabled
            logging.info("Waiting for distribution to be disabled...")
            waiter = cloudfront_client.get_waiter('distribution_deployed')
            waiter.wait(Id=distribution_id, WaiterConfig={'Delay': 30, 'MaxAttempts': 20})

            # Get the updated ETag after disabling
            response = cloudfront_client.get_distribution_config(Id=distribution_id)
            etag = response['ETag']

        # Now delete the distribution
        logging.info("Deleting CloudFront distribution...")
        cloudfront_client.delete_distribution(Id=distribution_id, IfMatch=etag)
        logging.info(f"CloudFront distribution {distribution_id} deleted successfully.")

        # Wait a bit for the deletion to propagate and release CNAMEs
        logging.info("Waiting 30 seconds for distribution deletion to propagate...")
        time.sleep(30)

    except Exception as e:
        logging.error(f"Error deleting CloudFront distribution: {e}")
        raise

def update_distribution_spa_mode(distribution_id, error_path='/index.html', response_code=200):
    """Update an existing CloudFront distribution to handle SPA routing."""
    logging.info(f"Updating CloudFront distribution {distribution_id} for SPA mode...")
    
    try:
        # Get the current distribution configuration
        response = cloudfront_client.get_distribution_config(Id=distribution_id)
        distribution_config = response['DistributionConfig']
        etag = response['ETag']
        
        # Check if SPA error handling already exists
        existing_error_responses = distribution_config.get('CustomErrorResponses', {}).get('Items', [])
        spa_error_exists = any(
            err.get('ErrorCode') == 404 and err.get('ResponsePagePath') == error_path 
            for err in existing_error_responses
        )
        
        if spa_error_exists:
            logging.info(f"SPA error handling already configured for distribution {distribution_id}")
            return True
        
        # Add or update custom error responses
        new_error_response = {
            'ErrorCode': 404,
            'ResponsePagePath': error_path,
            'ResponseCode': str(response_code),
            'ErrorCachingMinTTL': 0
        }
        
        if existing_error_responses:
            # Update existing 404 handler or add new one
            found_404 = False
            for err in existing_error_responses:
                if err.get('ErrorCode') == 404:
                    err.update(new_error_response)
                    found_404 = True
                    break
            
            if not found_404:
                existing_error_responses.append(new_error_response)
            
            distribution_config['CustomErrorResponses'] = {
                'Quantity': len(existing_error_responses),
                'Items': existing_error_responses
            }
        else:
            # Create new custom error responses
            distribution_config['CustomErrorResponses'] = {
                'Quantity': 1,
                'Items': [new_error_response]
            }
        
        # Update the distribution
        logging.info(f"Adding SPA mode: 404 errors will return '{error_path}' with status {response_code}")
        cloudfront_client.update_distribution(
            Id=distribution_id,
            DistributionConfig=distribution_config,
            IfMatch=etag
        )
        
        logging.info("Distribution updated successfully for SPA mode.")
        logging.info("Note: It may take 5-15 minutes for changes to propagate globally.")
        return True
        
    except Exception as e:
        logging.error(f"Error updating distribution for SPA mode: {e}")
        raise

def ensure_distribution_aliases(distribution_id, bucket_name):
    """Ensure the CloudFront distribution has the correct aliases configured."""
    logging.info(f"Checking aliases for CloudFront distribution {distribution_id}...")

    try:
        # Get the current distribution configuration
        response = cloudfront_client.get_distribution_config(Id=distribution_id)
        distribution_config = response['DistributionConfig']
        etag = response['ETag']

        # Check if aliases are properly configured
        aliases_missing = False
        current_aliases = distribution_config.get('Aliases', {}).get('Items', [])

        if not current_aliases:
            logging.info(f"No aliases found, adding: {bucket_name}")
            aliases_missing = True
        elif bucket_name not in current_aliases:
            logging.info(f"Alias {bucket_name} missing from current aliases: {current_aliases}")
            aliases_missing = True
        else:
            logging.info(f"Alias {bucket_name} already configured correctly")
            return True

        if aliases_missing:
            # Add or update aliases
            if bucket_name not in current_aliases:
                current_aliases.append(bucket_name)

            distribution_config['Aliases'] = {
                'Quantity': len(current_aliases),
                'Items': current_aliases
            }

            # Update the distribution
            logging.info(f"Updating distribution aliases to: {current_aliases}")
            cloudfront_client.update_distribution(
                Id=distribution_id,
                DistributionConfig=distribution_config,
                IfMatch=etag
            )

            logging.info("Distribution aliases updated successfully.")
            logging.info("Note: It may take 5-15 minutes for changes to propagate globally.")
            return True

    except Exception as e:
        logging.error(f"Error updating distribution aliases: {e}")
        raise

def create_cloudfront_distribution(bucket_name, certificate_arn, oac_id):
    """Creates or updates a CloudFront distribution."""
    logging.info(f"Checking if CloudFront distribution for '{bucket_name}' already exists...")

    # First check for distributions using our bucket
    existing_dist = get_existing_distribution(bucket_name)

    # Also check for any distribution using our domain as CNAME (to handle conflicts)
    cname_conflict_dist = find_distribution_using_cname(bucket_name)

    # If there's a CNAME conflict with a different distribution, handle it
    if cname_conflict_dist and (not existing_dist or cname_conflict_dist['Id'] != existing_dist['Id']):
        logging.warning(f"CNAME conflict detected! Domain '{bucket_name}' is used by distribution {cname_conflict_dist['Id']}")
        if FORCE_RECREATE:
            logging.info("Force recreate enabled - deleting conflicting distribution...")
            delete_cloudfront_distribution(cname_conflict_dist['Id'])
            logging.info("Conflicting distribution deleted, proceeding with creation...")
        else:
            logging.error(f"Cannot proceed: Domain '{bucket_name}' is already used by distribution {cname_conflict_dist['Id']}")
            logging.error("Options:")
            logging.error("  1. Use --force flag to delete the conflicting distribution")
            logging.error("  2. Choose a different subdomain")
            logging.error("  3. Manually delete the conflicting distribution first")
            raise Exception(f"CNAME conflict: {bucket_name} already in use by distribution {cname_conflict_dist['Id']}")

    if existing_dist:
        if FORCE_RECREATE:
            logging.info(f"Force recreate enabled - deleting existing distribution: {existing_dist['Id']}")
            delete_cloudfront_distribution(existing_dist['Id'])
            logging.info("Existing distribution deleted, creating new one...")
        else:
            logging.info(f"Found existing CloudFront distribution: {existing_dist['Id']}")

            # Check if the origin is using the correct endpoint
            current_origin = None
            for origin in existing_dist.get('Origins', {}).get('Items', []):
                if bucket_name in origin.get('DomainName', ''):
                    current_origin = origin
                    break

            origin_needs_update = False
            if current_origin:
                current_domain = current_origin.get('DomainName', '')
                expected_domain = get_s3_website_endpoint(bucket_name, AWS_REGION)

                logging.info(f"Current origin domain: {current_domain}")
                logging.info(f"Expected origin domain: {expected_domain}")

                if current_domain != expected_domain or 'CustomOriginConfig' not in current_origin:
                    origin_needs_update = True
            else:
                logging.warning("Could not find matching origin in existing distribution.")
                origin_needs_update = True

            # Check if aliases are properly configured (always check now, regardless of SSL mode)
            aliases_need_update = False
            current_aliases = existing_dist.get('Aliases', {}).get('Items', [])
            aliases_need_update = bucket_name not in current_aliases
            logging.info(f"Current aliases: {current_aliases}")
            logging.info(f"Aliases need update: {aliases_need_update}")
            if USE_CLOUDFLARE_SSL:
                logging.info("Using Cloudflare SSL - but still ensuring aliases are configured")
            
            # Check if SPA mode needs to be configured
            spa_needs_update = False
            if SPA_MODE:
                existing_error_responses = existing_dist.get('CustomErrorResponses', {}).get('Items', [])
                spa_error_exists = any(
                    err.get('ErrorCode') == 404 and err.get('ResponsePagePath') == SPA_ERROR_PATH 
                    for err in existing_error_responses
                )
                spa_needs_update = not spa_error_exists
                logging.info(f"SPA mode configured: {not spa_needs_update}")

            if origin_needs_update or spa_needs_update:
                logging.info("Origin domain or SPA mode needs updating...")
                return update_cloudfront_distribution_origin(existing_dist['Id'], bucket_name, oac_id)
            elif aliases_need_update:
                logging.info("Origin is correct but aliases need updating...")
                ensure_distribution_aliases(existing_dist['Id'], bucket_name)
                if spa_needs_update:
                    update_distribution_spa_mode(existing_dist['Id'], SPA_ERROR_PATH, SPA_ERROR_CODE)
                return existing_dist
            else:
                logging.info("Origin domain and aliases are correct, using existing distribution.")
                return existing_dist

    logging.info(f"Creating new CloudFront distribution for '{bucket_name}'...")
    caller_reference = f"dist-for-{bucket_name}-{int(time.time())}"

    # Use the S3 website endpoint
    s3_website_domain = get_s3_website_endpoint(bucket_name, AWS_REGION)

    logging.info(f"Using S3 website endpoint: {s3_website_domain}")

    # Create the origin configuration for S3 website endpoint
    # Use the domain name (bucket_name) as the origin ID instead of generic naming
    origin_id = bucket_name

    origin_config = {
        'Id': origin_id,
        'DomainName': s3_website_domain,
        'CustomOriginConfig': {
            'HTTPPort': 80,
            'HTTPSPort': 443,
            'OriginProtocolPolicy': 'http-only',
            'OriginSslProtocols': {
                'Quantity': 1,
                'Items': ['TLSv1.2']
            },
            'OriginReadTimeout': 30,
            'OriginKeepaliveTimeout': 5
        }
    }

    distribution_config = {
        'CallerReference': caller_reference,
        'Comment': f"Distribution for {bucket_name}",
        'Enabled': True,
        'DefaultRootObject': 'index.html',
        'Origins': {
            'Quantity': 1,
            'Items': [origin_config]
        },
        'DefaultCacheBehavior': {
            'TargetOriginId': origin_id,
            'ViewerProtocolPolicy': 'redirect-to-https',
            'AllowedMethods': {
                'Quantity': 2,
                'Items': ['GET', 'HEAD'],
                'CachedMethods': { 'Quantity': 2, 'Items': ['GET', 'HEAD']}
            },
            'Compress': True,
            'ForwardedValues': {
                'QueryString': False,
                'Cookies': {'Forward': 'none'}
            },
            'TrustedSigners': {
                'Enabled': False,
                'Quantity': 0
            },
            'MinTTL': 0,
            'DefaultTTL': 86400,
            'MaxTTL': 31536000,
        }
    }

    # Add custom error responses for SPA mode
    if SPA_MODE:
        distribution_config['CustomErrorResponses'] = {
            'Quantity': 1,
            'Items': [{
                'ErrorCode': 404,
                'ResponsePagePath': SPA_ERROR_PATH,
                'ResponseCode': str(SPA_ERROR_CODE),
                'ErrorCachingMinTTL': 0  # Don't cache error responses
            }]
        }
        logging.info(f"Configured SPA mode: 404 errors will return '{SPA_ERROR_PATH}' with status {SPA_ERROR_CODE}")

    # Add SSL certificate configuration and aliases
    # Always add aliases since CloudFront requires them for custom domains
    # For apex mode with www redirect, include both apex and www domains
    if APEX_MODE and WWW_DOMAIN:
        aliases = [bucket_name, WWW_DOMAIN]
        logging.info(f"Apex mode: Adding aliases for both {bucket_name} and {WWW_DOMAIN}")
    else:
        aliases = [bucket_name]

    distribution_config['Aliases'] = {
        'Quantity': len(aliases),
        'Items': aliases
    }

    if not USE_CLOUDFLARE_SSL and certificate_arn:
        # Use ACM certificate for SSL when explicitly requested
        distribution_config['ViewerCertificate'] = {
            'ACMCertificateArn': certificate_arn,
            'SSLSupportMethod': 'sni-only',
            'MinimumProtocolVersion': 'TLSv1.2_2021'
        }
    else:
        # For Cloudflare SSL mode, try to find existing certificate
        # If no certificate is provided, try to find an existing one
        if not certificate_arn:
            certificate_arn = find_existing_acm_certificate(bucket_name)

        if certificate_arn:
            # Use ACM certificate (either existing or newly created)
            logging.info(f"Using ACM certificate for aliases: {certificate_arn}")
            distribution_config['ViewerCertificate'] = {
                'ACMCertificateArn': certificate_arn,
                'SSLSupportMethod': 'sni-only',
                'MinimumProtocolVersion': 'TLSv1.2_2021'
            }
        else:
            # This should not happen anymore since we create certificates automatically
            logging.error(f"No ACM certificate available for '{bucket_name}' - this should not happen!")
            raise Exception("Certificate creation failed - cannot proceed without valid SSL certificate for aliases")

    try:
        response = cloudfront_client.create_distribution(DistributionConfig=distribution_config)
        distribution = response['Distribution']
        logging.info(f"CloudFront distribution created. ID: {distribution['Id']}")
        logging.info(f"Domain Name: {distribution['DomainName']}")
        logging.info(f"Origin ID: {origin_id}")
        return distribution
    except Exception as e:
        error_str = str(e)
        if "CNAMEAlreadyExists" in error_str:
            logging.error(f"CNAME conflict error: {e}")
            logging.error(f"The domain '{bucket_name}' is still associated with another CloudFront distribution.")
            logging.error("This can happen when:")
            logging.error("  1. A distribution was recently deleted but not fully propagated")
            logging.error("  2. Another distribution is using the same domain")
            logging.error("  3. DNS propagation delays")
            logging.error("\nTroubleshooting steps:")
            logging.error("  1. Wait 5-10 minutes and try again")
            logging.error("  2. Check for other distributions using this domain:")
            logging.error(f"     create cloudfront list-distributions --query 'DistributionList.Items[?Aliases.Items[?contains(@, `{bucket_name}`)]]'")
            logging.error("  3. If found, delete the conflicting distribution manually")

            # Try to find the conflicting distribution and update the CNAME
            logging.info("Attempting to resolve CNAME conflict by updating the CNAME record...")

            # Create the distribution without the alias
            logging.info("Creating distribution without the conflicting alias...")
            distribution_config['Aliases'] = {'Quantity': 0, 'Items': []}

            try:
                response = cloudfront_client.create_distribution(DistributionConfig=distribution_config)
                distribution = response['Distribution']
                distribution_domain = distribution['DomainName']
                logging.info(f"CloudFront distribution created without alias. ID: {distribution['Id']}")
                logging.info(f"Domain Name: {distribution_domain}")

                # Update the Cloudflare CNAME record to point to the new distribution
                logging.info(f"Updating Cloudflare CNAME record to point to the new distribution: {distribution_domain}")
                cf_client = get_cloudflare_client()
                zone_id = get_cloudflare_zone_id(cf_client, CLOUDFLARE_ZONE_NAME)
                create_cloudflare_cname_record(cf_client, zone_id, SUBDOMAIN, distribution_domain)

                logging.info("CNAME record updated successfully to point to the new distribution.")

                # Now add the alias to the distribution since the CNAME conflict should be resolved
                logging.info("Adding alias to the distribution after CNAME update...")
                logging.info("Waiting 30 seconds for DNS changes to propagate before adding alias...")
                time.sleep(30)

                # Add the alias to the distribution
                ensure_distribution_aliases(distribution['Id'], bucket_name)

                # Add SPA mode if enabled
                if SPA_MODE:
                    update_distribution_spa_mode(distribution['Id'], SPA_ERROR_PATH, SPA_ERROR_CODE)

                return distribution
            except Exception as inner_e:
                logging.error(f"Error creating distribution without alias: {inner_e}")
                raise inner_e
        else:
            logging.error(f"Error creating CloudFront distribution: {e}")
        raise

def update_s3_bucket_policy(bucket_name, distribution_arn):
    """Updates S3 bucket policy to grant access to CloudFront."""
    logging.info(f"Updating bucket policy for '{bucket_name}'...")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontServicePrincipal",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:create:s3:::{bucket_name}/*",
                "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}}
            }
        ]
    }
    s3_client.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
    logging.info("Bucket policy updated successfully.")

def get_cloudflare_zone_id(cf_client, zone_name):
    """Get the Cloudflare zone ID for the domain."""
    zones = cf_client.zones.list(name=zone_name)
    if not zones.result:
        logging.error(f"Zone '{zone_name}' not found in Cloudflare.")
        raise Exception(f"Zone not found: {zone_name}")
    return zones.result[0].id

def create_cloudflare_cname_record(cf_client, zone_id, cname_name, cname_content):
    """Creates or updates a CNAME record in Cloudflare pointing to CloudFront."""
    # Build the full domain name for the CNAME record
    full_domain_name = f"{cname_name}.{CLOUDFLARE_ZONE_NAME}"
    logging.info(f"Creating/updating CNAME record for '{full_domain_name}' -> '{cname_content}'...")

    try:
        # Search for any existing records with the same name (A, AAAA, or CNAME)
        # Try both the subdomain and the full domain name
        existing_records = cf_client.dns.records.list(zone_id=zone_id, name=full_domain_name)

        # If not found with FQDN, try with just the subdomain
        if not existing_records.result:
            logging.info(f"No records found for '{full_domain_name}', trying '{cname_name}'...")
            existing_records = cf_client.dns.records.list(zone_id=zone_id, name=cname_name)

        dns_record = {
            'name': cname_name,  # Cloudflare expects just the subdomain when creating/updating
            'type': 'CNAME',
            'content': cname_content,
            'proxied': False  # Don't proxy - CloudFront handles SSL with ACM certificate
        }

        cname_record_exists = False
        records_to_delete = []

        if existing_records.result:
            logging.info(f"Found {len(existing_records.result)} existing record(s) for the domain")
            for record in existing_records.result:
                record_type = record.type
                record_id = record.id
                record_name = record.name

                logging.info(f"  Record name: '{record_name}', type: {record_type}, ID: {record_id}")

                if record_type == 'CNAME':
                    # Found existing CNAME record
                    cname_record_exists = True
                    current_content = record.content
                    current_proxied = record.proxied

                    logging.info(f"  Current CNAME content: '{current_content}'")
                    logging.info(f"  Target CNAME content: '{cname_content}'")
                    logging.info(f"  Current proxied: {current_proxied}")
                    logging.info("  Target proxied: False")

                    # Check if we need to update it
                    if current_content != cname_content or current_proxied:
                        logging.info(f"Updating existing CNAME record '{record_name}' (ID: {record_id})")
                        logging.info(f"  Old target: {current_content} (proxied: {current_proxied})")
                        logging.info(f"  New target: {cname_content} (proxied: False)")
                        cf_client.dns.records.update(zone_id=zone_id, dns_record_id=record_id, **dns_record)
                        logging.info("Cloudflare CNAME record updated successfully.")
                    else:
                        logging.info(f"CNAME record for '{record_name}' is already pointing to the correct target.")
                elif record_type in ['A', 'AAAA']:
                    # Found conflicting A or AAAA record - need to delete it first
                    logging.info(f"Found conflicting {record_type} record for '{record_name}' (ID: {record_id})")
                    logging.info(f"  {record_type} record content: {record.content}")
                    records_to_delete.append((record_id, record_type))
        else:
            logging.info(f"No existing records found for '{full_domain_name}' or '{cname_name}'")

        # Delete conflicting A/AAAA records
        for record_id, record_type in records_to_delete:
            logging.info(f"Deleting conflicting {record_type} record (ID: {record_id})")
            cf_client.dns.records.delete(zone_id=zone_id, dns_record_id=record_id)
            logging.info(f"Conflicting {record_type} record deleted successfully.")

        # Create new CNAME record if none existed or we deleted conflicting records
        if not cname_record_exists or records_to_delete:
            logging.info(f"Creating new CNAME record for '{cname_name}'")
            cf_client.dns.records.create(zone_id=zone_id, **dns_record)
            logging.info("Cloudflare CNAME record created successfully.")

    except Exception as e:
        logging.error(f"Error creating/updating Cloudflare CNAME record: {e}")
        raise

def cleanup_dns_validation(cf_client, zone_id, dns_records_to_delete):
    """Removes the DNS validation record(s) from Cloudflare.

    Args:
        cf_client: Cloudflare client
        zone_id: Cloudflare zone ID
        dns_records_to_delete: Single dict or list of dicts with 'name' and 'type' keys
    """
    if not dns_records_to_delete:
        return

    # Normalize to list
    if isinstance(dns_records_to_delete, dict):
        dns_records_to_delete = [dns_records_to_delete]

    for dns_record in dns_records_to_delete:
        logging.info(f"Cleaning up DNS validation record: {dns_record['name']}")
        try:
            records = cf_client.dns.records.list(zone_id=zone_id, name=dns_record['name'], type=dns_record['type'])
            if records.result:
                record_id = records.result[0].id
                cf_client.dns.records.delete(zone_id=zone_id, dns_record_id=record_id)
                logging.info("DNS validation record cleaned up successfully.")
            else:
                logging.warning("Could not find DNS validation record to cleanup.")
        except Exception as e:
            logging.error(f"Error cleaning up DNS record: {e}")

def create_cloudfront_www_redirect_function(apex_domain, distribution_id):
    """Create and attach a CloudFront Function for www → apex redirect.

    This is more reliable than Cloudflare redirect rules because:
    1. Works regardless of Cloudflare plan/permissions
    2. Executes at CloudFront edge (faster)
    3. No dependency on Cloudflare proxy mode

    Args:
        apex_domain: The apex domain (e.g., example.com)
        distribution_id: CloudFront distribution ID to attach the function to

    Returns:
        bool: True if function was created/attached successfully
    """
    function_name = f"www-redirect-{apex_domain.replace('.', '-')}"
    logging.info(f"Setting up CloudFront Function for www redirect: {function_name}")

    try:
        # CloudFront Function code for www → apex redirect
        function_code = """function handler(event) {
  var request = event.request;
  var host = request.headers.host.value;

  if (host.startsWith('www.')) {
    var newHost = host.substring(4);
    return {
      statusCode: 301,
      statusDescription: 'Moved Permanently',
      headers: {
        'location': { value: 'https://' + newHost + request.uri }
      }
    };
  }

  return request;
}"""

        function_arn = None

        # Check if function already exists
        try:
            existing = cloudfront_client.describe_function(Name=function_name, Stage='LIVE')
            function_arn = existing['FunctionSummary']['FunctionMetadata']['FunctionARN']
            logging.info(f"CloudFront Function already exists: {function_name}")
        except cloudfront_client.exceptions.NoSuchFunctionExists:
            # Create new function
            logging.info(f"Creating CloudFront Function: {function_name}")
            create_response = cloudfront_client.create_function(
                Name=function_name,
                FunctionConfig={
                    'Comment': f'Redirect www to apex for {apex_domain}',
                    'Runtime': 'cloudfront-js-2.0'
                },
                FunctionCode=function_code.encode('utf-8')
            )
            etag = create_response['ETag']

            # Publish the function
            logging.info(f"Publishing CloudFront Function: {function_name}")
            publish_response = cloudfront_client.publish_function(
                Name=function_name,
                IfMatch=etag
            )
            function_arn = publish_response['FunctionSummary']['FunctionMetadata']['FunctionARN']
            logging.info(f"CloudFront Function published: {function_arn}")

        # Attach function to distribution
        if function_arn and distribution_id:
            logging.info(f"Attaching function to distribution {distribution_id}...")

            # Get current distribution config
            dist_response = cloudfront_client.get_distribution_config(Id=distribution_id)
            config = dist_response['DistributionConfig']
            etag = dist_response['ETag']

            # Check if function is already attached
            existing_associations = config['DefaultCacheBehavior'].get('FunctionAssociations', {})
            existing_items = existing_associations.get('Items', [])

            already_attached = any(
                item.get('FunctionARN') == function_arn
                for item in existing_items
            )

            if already_attached:
                logging.info("Function already attached to distribution")
                return True

            # Add function association
            new_item = {
                'FunctionARN': function_arn,
                'EventType': 'viewer-request'
            }

            if existing_items:
                existing_items.append(new_item)
            else:
                existing_items = [new_item]

            config['DefaultCacheBehavior']['FunctionAssociations'] = {
                'Quantity': len(existing_items),
                'Items': existing_items
            }

            # Update distribution
            cloudfront_client.update_distribution(
                Id=distribution_id,
                IfMatch=etag,
                DistributionConfig=config
            )
            logging.info("CloudFront Function attached to distribution")

            # Wait for distribution to deploy
            logging.info("Waiting for distribution to deploy (this may take 3-5 minutes)...")
            waiter = cloudfront_client.get_waiter('distribution_deployed')
            waiter.wait(
                Id=distribution_id,
                WaiterConfig={'Delay': 15, 'MaxAttempts': 40}
            )
            logging.info("Distribution deployed with www redirect function")

        return True

    except Exception as e:
        logging.error(f"Failed to create CloudFront Function: {e}")
        return False

def create_apex_cloudflare_cname_records(cf_client, zone_id, apex_domain, www_domain, cloudfront_domain):
    """Create Cloudflare DNS records for apex domain setup with www redirect.

    This uses CNAME flattening for the apex domain (Cloudflare automatically converts
    CNAME at apex to A records) and a standard CNAME for www.

    Args:
        cf_client: Cloudflare client
        zone_id: Cloudflare zone ID
        apex_domain: The apex domain (e.g., example.com)
        www_domain: The www domain (e.g., www.example.com)
        cloudfront_domain: CloudFront distribution domain
    """
    logging.info("Configuring Cloudflare DNS records for apex domain setup...")

    def delete_conflicting_records(name):
        """Delete ALL existing A, AAAA records for a domain (to allow CNAME)."""
        full_name = name if '.' in name and name != apex_domain else f"{name}.{apex_domain}" if name != apex_domain else name

        try:
            existing = cf_client.dns.records.list(zone_id=zone_id, name=full_name)
            if existing.result:
                for rec in existing.result:
                    if rec.type in ['A', 'AAAA']:
                        logging.info(f"Deleting conflicting {rec.type} record: {full_name} → {rec.content}")
                        cf_client.dns.records.delete(zone_id=zone_id, dns_record_id=rec.id)
        except Exception as e:
            logging.warning(f"Error deleting records for {full_name}: {e}")

    def create_or_update_record(name, content, record_type='CNAME', proxied=False):
        """Create or update a DNS record."""
        full_name = name if name == apex_domain else f"{name}.{apex_domain}" if '.' not in name else name

        try:
            # First, delete ALL conflicting A/AAAA records
            delete_conflicting_records(name)

            # Check for existing CNAME records
            existing = cf_client.dns.records.list(zone_id=zone_id, name=full_name)

            record_data = {
                'name': name,
                'type': record_type,
                'content': content,
                'proxied': proxied
            }

            if existing.result:
                for rec in existing.result:
                    if rec.type == 'CNAME':
                        if rec.content != content or rec.proxied != proxied:
                            logging.info(f"Updating CNAME record: {full_name} → {content} (proxied={proxied})")
                            cf_client.dns.records.update(zone_id=zone_id, dns_record_id=rec.id, **record_data)
                        else:
                            logging.info(f"CNAME record already correct: {full_name} → {content} (proxied={proxied})")
                        return

            logging.info(f"Creating {record_type} record: {full_name} → {content}")
            cf_client.dns.records.create(zone_id=zone_id, **record_data)

        except Exception as e:
            if "already exists" not in str(e):
                raise
            logging.warning(f"Record already exists: {full_name}")

    # Apex domain (CNAME flattening in Cloudflare)
    # Cloudflare automatically flattens CNAME at apex to A records
    create_or_update_record(apex_domain, cloudfront_domain, proxied=False)
    logging.info(f"Apex domain configured: {apex_domain} → {cloudfront_domain} (CNAME flattening)")

    # WWW domain - use proxied=False (no Cloudflare proxy)
    # The redirect is handled by CloudFront Function, not Cloudflare
    create_or_update_record('www', cloudfront_domain, proxied=False)
    logging.info(f"WWW DNS configured: {www_domain} → {cloudfront_domain} (redirect via CloudFront Function)")

def test_cdn_functionality(domain, timeout=60):
    """Test the CDN functionality by fetching pages and validating responses."""
    logging.info("\n=== Testing CDN Functionality ===")

    test_urls = [
        f"https://{domain}/index-test.html",
        f"https://{domain}/test.txt",
        f"https://{domain}/nonexistent-page.html"  # Should return 404 with custom error page
    ]
    
    # Add SPA routing tests if SPA mode is enabled
    if SPA_MODE:
        test_urls.extend([
            f"https://{domain}/",  # Root should work
            f"https://{domain}/about",  # SPA route should serve index.html
            f"https://{domain}/products/123",  # Nested SPA route should serve index.html
            f"https://{domain}/any/nested/route"  # Deep nested route should serve index.html
        ])

    results = {}

    for url in test_urls:
        logging.info(f"Testing URL: {url}")

        try:
            # Make request with timeout
            response = requests.get(url, timeout=timeout, allow_redirects=True)

            results[url] = {
                'status_code': response.status_code,
                'success': True,
                'response_time': response.elapsed.total_seconds(),
                'content_length': len(response.content),
                'headers': dict(response.headers)
            }

            # Validate specific responses
            if url.endswith('index-test.html'):
                # Main test page should return 200 and contain success message
                if response.status_code == 200:
                    if 'CDN Setup Successful' in response.text:
                        logging.info("✅ Test page test PASSED - Found success message")
                        results[url]['validation'] = 'PASSED'
                    else:
                        logging.warning("⚠️  Test page test PARTIAL - Page loaded but missing expected content")
                        results[url]['validation'] = 'PARTIAL'
                else:
                    logging.error(f"❌ Test page test FAILED - Status: {response.status_code}")
                    results[url]['validation'] = 'FAILED'

            elif url.endswith('.txt'):
                # Text file should return 200 and contain expected content
                if response.status_code == 200:
                    if 'CDN test file' in response.text:
                        logging.info("✅ Text file test PASSED - Found expected content")
                        results[url]['validation'] = 'PASSED'
                    else:
                        logging.warning("⚠️  Text file test PARTIAL - File loaded but unexpected content")
                        results[url]['validation'] = 'PARTIAL'
                else:
                    logging.error(f"❌ Text file test FAILED - Status: {response.status_code}")
                    results[url]['validation'] = 'FAILED'

            elif 'nonexistent' in url:
                # 404 page should return 404 and show custom error page (unless SPA mode)
                if SPA_MODE:
                    # In SPA mode, this should return 200 with index.html content
                    if response.status_code == 200:
                        if 'SPA-Enabled CDN Setup Successful' in response.text:
                            logging.info("✅ SPA 404 redirect test PASSED - Returned index.html with 200")
                            results[url]['validation'] = 'PASSED'
                        else:
                            logging.warning("⚠️  SPA 404 redirect test PARTIAL - 200 returned but not index.html")
                            results[url]['validation'] = 'PARTIAL'
                    else:
                        logging.error(f"❌ SPA 404 redirect test FAILED - Status: {response.status_code} (expected 200)")
                        results[url]['validation'] = 'FAILED'
                else:
                    # Normal mode - expect 404 with custom error page
                    if response.status_code == 404:
                        if 'Page Not Found' in response.text:
                            logging.info("✅ 404 error page test PASSED - Custom error page displayed")
                            results[url]['validation'] = 'PASSED'
                        else:
                            logging.warning("⚠️  404 error page test PARTIAL - 404 returned but no custom error page")
                            results[url]['validation'] = 'PARTIAL'
                    else:
                        logging.warning(f"⚠️  404 error page test UNEXPECTED - Status: {response.status_code} (expected 404)")
                        results[url]['validation'] = 'UNEXPECTED'
            
            elif SPA_MODE and any(path in url for path in ['/about', '/products/', '/any/']):
                # SPA routing tests - these should all return 200 with index.html content
                if response.status_code == 200:
                    if 'SPA-Enabled CDN Setup Successful' in response.text:
                        logging.info(f"✅ SPA routing test PASSED - '{url}' served index.html")
                        results[url]['validation'] = 'PASSED'
                    else:
                        logging.warning("⚠️  SPA routing test PARTIAL - 200 returned but not index.html")
                        results[url]['validation'] = 'PARTIAL'
                else:
                    logging.error(f"❌ SPA routing test FAILED - Status: {response.status_code} (expected 200)")
                    results[url]['validation'] = 'FAILED'

            # Log response details
            logging.info(f"  Status: {response.status_code}")
            logging.info(f"  Response time: {response.elapsed.total_seconds():.2f}s")
            logging.info(f"  Content length: {len(response.content)} bytes")

            # Check for CloudFront headers
            if 'X-Amz-Cf-Id' in response.headers:
                logging.info(f"  ✅ CloudFront serving content (X-Amz-Cf-Id: {response.headers['X-Amz-Cf-Id'][:20]}...)")
            else:
                logging.warning("  ⚠️  CloudFront headers not detected")

        except requests.exceptions.Timeout:
            logging.error(f"❌ TIMEOUT - Request to {url} timed out after {timeout}s")
            results[url] = {'success': False, 'error': 'timeout', 'validation': 'FAILED'}

        except requests.exceptions.ConnectionError as e:
            logging.error(f"❌ CONNECTION ERROR - Could not connect to {url}: {e}")
            results[url] = {'success': False, 'error': 'connection_error', 'validation': 'FAILED'}

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ REQUEST ERROR - Error fetching {url}: {e}")
            results[url] = {'success': False, 'error': str(e), 'validation': 'FAILED'}

        # Wait a bit between requests
        time.sleep(2)

    # Summary
    logging.info("\n=== Test Results Summary ===")
    passed = 0
    total = len(test_urls)

    for url, result in results.items():
        if result.get('success', False):
            validation = result.get('validation', 'UNKNOWN')
            if validation == 'PASSED':
                logging.info(f"✅ {url} - {validation}")
                passed += 1
            elif validation == 'PARTIAL':
                logging.warning(f"⚠️  {url} - {validation}")
            else:
                logging.error(f"❌ {url} - {validation}")
        else:
            logging.error(f"❌ {url} - FAILED ({result.get('error', 'unknown error')})")

    success_rate = (passed / total) * 100
    logging.info(f"\nOverall Success Rate: {passed}/{total} ({success_rate:.1f}%)")

    if success_rate >= 80:
        logging.info("🎉 CDN is working well!")
        return True
    elif success_rate >= 50:
        logging.warning("⚠️  CDN is partially working - some issues detected")
        return False
    else:
        logging.error("❌ CDN has significant issues")
        return False

def main():
    """Main function to orchestrate the CDN setup.

    Supports two modes:
    1. Subdomain mode: Setup CDN for subdomain.example.com
    2. Apex mode: Setup CDN for example.com with optional www redirect
    """
    try:
        # Initialize DNS provider based on CLI argument
        logging.info(f"Using DNS provider: {DNS_PROVIDER_NAME}")
        dns_provider = get_dns_provider(DNS_PROVIDER_NAME)

        # Get or create DNS zone
        if CREATE_ZONE:
            if not dns_provider.supports_zone_creation():
                logging.warning(
                    f"DNS provider '{DNS_PROVIDER_NAME}' does not support automatic zone creation. "
                    f"Please create the zone manually first."
                )
            zone_id = dns_provider.get_or_create_zone(CLOUDFLARE_ZONE_NAME, auto_create=True)
        else:
            zone_id = dns_provider.get_zone_id(CLOUDFLARE_ZONE_NAME)

        # Check apex mode compatibility
        if APEX_MODE and not dns_provider.supports_apex_cname():
            logging.warning("Selected DNS provider does not fully support apex domains.")
            logging.warning("Proceeding with fallback approach (A records with CloudFront IPs).")

        # 1. S3 Bucket
        create_s3_bucket(BUCKET_NAME)

        # 2. Enable S3 Website Hosting
        enable_s3_website_hosting(BUCKET_NAME)

        # 3. ACM Certificate handling
        cert_arn = None

        if APEX_MODE:
            # For apex mode, we need a certificate that covers BOTH apex AND wildcard
            # because wildcard (*.example.com) does NOT cover the apex (example.com)
            logging.info("Apex mode: Checking for certificate covering apex + wildcard...")

            # Required domains for apex mode with www redirect
            required_domains = [BUCKET_NAME]  # apex domain
            if WWW_REDIRECT:
                required_domains.append(WWW_DOMAIN)

            cert_arn = find_certificate_covering_domains(required_domains)

            if not cert_arn:
                logging.info("No existing certificate covers required domains.")
                logging.info("Creating new ACM certificate for apex + wildcard...")
                # Request cert with apex as primary and wildcard as SAN
                # This covers: example.com, *.example.com (including www, staging, etc.)
                cert_arn, validation_options, cert_waiter = request_acm_certificate(
                    BUCKET_NAME,
                    san_domains=[BUCKET_NAME, f"*.{CLOUDFLARE_ZONE_NAME}"]
                )

                # DNS Validation for all domains
                validation_dns_records = dns_provider.create_validation_records(zone_id, CLOUDFLARE_ZONE_NAME, validation_options)

                # Wait for Validation
                wait_for_cert_validation(cert_waiter, cert_arn)

                # Cleanup DNS validation records
                dns_provider.cleanup_validation_records(zone_id, validation_dns_records)

            logging.info(f"Using ACM certificate: {cert_arn}")

        elif not USE_CLOUDFLARE_SSL:
            # ACM SSL mode for subdomain
            cert_arn, validation_cname, cert_waiter = request_acm_certificate(BUCKET_NAME)

            # DNS Validation
            validation_dns_record = dns_provider.create_validation_records(zone_id, CLOUDFLARE_ZONE_NAME, validation_cname)

            # Wait for Validation
            wait_for_cert_validation(cert_waiter, cert_arn)

            # Cleanup DNS validation record
            dns_provider.cleanup_validation_records(zone_id, validation_dns_record)
        else:
            # Cloudflare SSL mode for subdomain
            logging.info("Using Cloudflare for SSL, but checking for existing ACM certificates...")
            cert_arn = find_existing_acm_certificate(BUCKET_NAME)
            if cert_arn:
                logging.info(f"Found existing ACM certificate: {cert_arn}")
            else:
                logging.info("No existing ACM certificate found - creating wildcard certificate for aliases...")
                wildcard_domain = f"*.{CLOUDFLARE_ZONE_NAME}"
                logging.info(f"Creating ACM certificate for '{wildcard_domain}'...")
                cert_arn, validation_cname, cert_waiter = request_acm_certificate(wildcard_domain)

                # DNS Validation
                validation_dns_record = dns_provider.create_validation_records(zone_id, CLOUDFLARE_ZONE_NAME, validation_cname)

                # Wait for Validation
                wait_for_cert_validation(cert_waiter, cert_arn)

                # Cleanup DNS validation record
                dns_provider.cleanup_validation_records(zone_id, validation_dns_record)

        # 4. CloudFront Distribution
        distribution = create_cloudfront_distribution(BUCKET_NAME, cert_arn, None)
        distribution_domain = distribution['DomainName']
        distribution_id = distribution['Id']

        # 5. DNS Configuration
        if APEX_MODE:
            # Create DNS records for apex + www
            dns_provider.create_apex_records(
                zone_id,
                BUCKET_NAME, WWW_DOMAIN,
                distribution_domain
            )

            # 6. Setup CloudFront Function for www → apex redirect
            if WWW_REDIRECT:
                logging.info("Setting up www → apex redirect via CloudFront Function...")
                redirect_success = create_cloudfront_www_redirect_function(BUCKET_NAME, distribution_id)
                if not redirect_success:
                    logging.warning("CloudFront Function setup failed - www will serve same content as apex")
        else:
            # Standard subdomain CNAME
            dns_provider.create_cname_record(zone_id, SUBDOMAIN, distribution_domain, CLOUDFLARE_ZONE_NAME)

        # 7. Create test content
        create_test_content(BUCKET_NAME)

        # Summary
        logging.info("\n" + "=" * 50)
        logging.info("CDN SETUP COMPLETE")
        logging.info("=" * 50)
        logging.info(f"S3 Bucket: {BUCKET_NAME}")
        logging.info(f"S3 Website Endpoint: {get_s3_website_endpoint(BUCKET_NAME, AWS_REGION)}")
        logging.info(f"CloudFront Domain: {distribution_domain}")
        logging.info(f"CloudFront Distribution ID: {distribution_id}")

        if APEX_MODE:
            logging.info(f"Apex Domain: https://{BUCKET_NAME}")
            logging.info(f"WWW Domain: https://{WWW_DOMAIN}")
            if WWW_REDIRECT:
                logging.info(f"WWW Redirect: https://{WWW_DOMAIN} → https://{BUCKET_NAME}")
        else:
            if USE_CLOUDFLARE_SSL:
                logging.info(f"Public URL: https://{BUCKET_NAME} (SSL via Cloudflare)")
            else:
                logging.info(f"Public URL: https://{BUCKET_NAME} (SSL via ACM)")

        logging.info(f"SPA Mode: {SPA_MODE}")
        logging.info(f"Certificate: {cert_arn}")
        logging.info("=" * 50)
        logging.info("Test URLs:")
        logging.info(f"  - https://{BUCKET_NAME}/index-test.html")
        logging.info(f"  - https://{BUCKET_NAME}/test.txt")
        if APEX_MODE and WWW_REDIRECT:
            logging.info(f"  - https://{WWW_DOMAIN}/ (should redirect to apex)")
        logging.info("=" * 50)

        # 8. Test CDN functionality (unless skipped)
        if not SKIP_TESTS:
            logging.info("\nWaiting 30 seconds for DNS/CDN propagation before testing...")
            time.sleep(30)

            test_success = test_cdn_functionality(BUCKET_NAME, TEST_TIMEOUT)

            if test_success:
                logging.info("\n🎉 CDN setup completed successfully and is working!")
                return 0
            else:
                logging.warning("\n⚠️  CDN setup completed but tests indicate some issues. Check the logs above.")
                return 1
        else:
            logging.info("\n✅ CDN setup completed successfully (tests skipped)")
            return 0

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    exit(main()) 
