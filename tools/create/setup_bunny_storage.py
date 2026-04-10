#!/usr/bin/env python3
"""
Bunny Storage + Bunny CDN Website Setup Script

This script sets up a CDN for static content using Bunny Storage and Bunny CDN.
Bunny Storage has built-in static website hosting support, including automatic
index.html serving for directory requests.

ADVANTAGES OVER HETZNER S3:
---------------------------
- Automatic index.html serving for / requests
- Built-in 404 handling with custom error pages
- Edge replication for faster global access
- Simpler setup (single provider)

MODES:
------
1. Subdomain Mode (default):
   - Setup CDN for subdomain.example.com
   - Use: python setup_bunny_storage.py --domain example.com --subdomain www

2. Apex Mode (production):
   - Setup CDN for example.com with optional www redirect
   - Use: python setup_bunny_storage.py --domain example.com --apex --www-redirect

Environment Variables:
    BUNNY_API_KEY: Required - Bunny.net API key
    CLOUDFLARE_API_TOKEN: Required for Cloudflare DNS
    HETZNER_DNS_API_TOKEN: Required for Hetzner DNS
    DESEC_API_TOKEN: Required for deSEC DNS

Usage Examples:
    # Subdomain: Basic static site
    python setup_bunny_storage.py --domain example.com --subdomain www

    # Apex: Production website with www redirect
    python setup_bunny_storage.py --domain example.com --apex --www-redirect --spa-mode

    # Different storage region
    python setup_bunny_storage.py --domain example.com --subdomain www --region UK
"""

import os
import json
import time
import logging
import argparse
import requests
from abc import ABC, abstractmethod
from typing import Optional, Dict, List

# Optional imports for DNS providers
Cloudflare = None
try:
    from cloudflare import Cloudflare
except ImportError:
    pass

HetznerDnsZone = None
HetznerDnsRecord = None
try:
    from hetzner_dns_api import DnsZone as HetznerDnsZone, DnsRecord as HetznerDnsRecord
except ImportError:
    pass

from dotenv import load_dotenv
load_dotenv()


# =============================================================================
# Bunny Storage Configuration
# =============================================================================

BUNNY_API_BASE = "https://api.bunny.net"

BUNNY_STORAGE_REGIONS = {
    'DE': {'name': 'Frankfurt, Germany', 'hostname': 'storage.bunnycdn.com'},
    'UK': {'name': 'London, UK', 'hostname': 'uk.storage.bunnycdn.com'},
    'NY': {'name': 'New York, US', 'hostname': 'ny.storage.bunnycdn.com'},
    'LA': {'name': 'Los Angeles, US', 'hostname': 'la.storage.bunnycdn.com'},
    'SG': {'name': 'Singapore', 'hostname': 'sg.storage.bunnycdn.com'},
    'SE': {'name': 'Stockholm, Sweden', 'hostname': 'se.storage.bunnycdn.com'},
    'BR': {'name': 'Sao Paulo, Brazil', 'hostname': 'br.storage.bunnycdn.com'},
    'JH': {'name': 'Johannesburg, SA', 'hostname': 'jh.storage.bunnycdn.com'},
    'SYD': {'name': 'Sydney, Australia', 'hostname': 'syd.storage.bunnycdn.com'},
}


# =============================================================================
# DNS Provider Abstraction Layer (same as setup_hetzner_bunny.py)
# =============================================================================

class DNSProvider(ABC):
    """Abstract base class for DNS providers."""

    @abstractmethod
    def get_zone_id(self, zone_name: str) -> str:
        pass

    @abstractmethod
    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        pass

    @abstractmethod
    def supports_apex_cname(self) -> bool:
        pass

    @abstractmethod
    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        pass


class CloudflareDNSProvider(DNSProvider):
    """Cloudflare DNS API adapter."""

    def __init__(self):
        if Cloudflare is None:
            raise ImportError("Cloudflare library not found. Install: pip install cloudflare")
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        if not token:
            raise ValueError("CLOUDFLARE_API_TOKEN environment variable not set.")
        self.client = Cloudflare(api_token=token)

    def get_zone_id(self, zone_name: str) -> str:
        zones = self.client.zones.list(name=zone_name)
        if not zones.result:
            raise Exception(f"Zone not found in Cloudflare: {zone_name}")
        return zones.result[0].id

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        full_domain_name = f"{name}.{zone_name}"
        logging.info(f"Creating/updating CNAME: '{full_domain_name}' -> '{target}'...")

        try:
            existing_records = self.client.dns.records.list(zone_id=zone_id, name=full_domain_name)
            dns_record = {'name': name, 'type': 'CNAME', 'content': target, 'proxied': False}

            cname_exists = False
            if existing_records.result:
                for record in existing_records.result:
                    if record.type == 'CNAME':
                        cname_exists = True
                        if record.content != target:
                            self.client.dns.records.update(zone_id=zone_id, dns_record_id=record.id, **dns_record)
                            logging.info("CNAME record updated.")
                        else:
                            logging.info("CNAME record already correct.")
                    elif record.type in ['A', 'AAAA']:
                        self.client.dns.records.delete(zone_id=zone_id, dns_record_id=record.id)

            if not cname_exists:
                self.client.dns.records.create(zone_id=zone_id, **dns_record)
                logging.info("CNAME record created.")
        except Exception as e:
            logging.error(f"Error creating CNAME record: {e}")
            raise

    def supports_apex_cname(self) -> bool:
        return True

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        logging.info("Configuring Cloudflare DNS for apex domain...")

        def create_or_update_record(name: str, content: str):
            full_name = name if name == apex_domain else f"{name}.{apex_domain}"
            existing = self.client.dns.records.list(zone_id=zone_id, name=full_name)
            record_data = {'name': name, 'type': 'CNAME', 'content': content, 'proxied': False}

            if existing.result:
                for rec in existing.result:
                    if rec.type in ['A', 'AAAA']:
                        self.client.dns.records.delete(zone_id=zone_id, dns_record_id=rec.id)
                    elif rec.type == 'CNAME':
                        if rec.content != content:
                            self.client.dns.records.update(zone_id=zone_id, dns_record_id=rec.id, **record_data)
                        return
            self.client.dns.records.create(zone_id=zone_id, **record_data)

        create_or_update_record(apex_domain, cdn_domain)
        logging.info(f"Apex configured: {apex_domain} -> {cdn_domain}")
        create_or_update_record('www', cdn_domain)
        logging.info(f"WWW configured: {www_domain} -> {cdn_domain}")


class HetznerDNSProvider(DNSProvider):
    """Hetzner DNS API adapter."""

    def __init__(self):
        if HetznerDnsZone is None:
            raise ImportError("hetzner-dns-api library not found. Install: pip install hetzner-dns-api")
        token = os.environ.get("HETZNER_DNS_API_TOKEN")
        if not token:
            raise ValueError("HETZNER_DNS_API_TOKEN environment variable not set.")
        self.zone_api = HetznerDnsZone(token)
        self.record_api = HetznerDnsRecord(token)

    def get_zone_id(self, zone_name: str) -> str:
        for zone in self.zone_api.all(name=zone_name):
            if zone.name == zone_name:
                return zone.id
        raise Exception(f"Zone not found in Hetzner DNS: {zone_name}")

    def _get_records_by_name(self, zone_id: str, name: str, record_type: Optional[str] = None) -> list:
        matching = []
        for record in self.record_api.all(zone_id=zone_id):
            if record.name == name:
                if record_type is None or record.type == record_type:
                    matching.append(record)
        return matching

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")
        target = target.rstrip('.')

        existing_cname = self._get_records_by_name(zone_id, name, 'CNAME')
        for record in self._get_records_by_name(zone_id, name, 'A') + self._get_records_by_name(zone_id, name, 'AAAA'):
            self.record_api.delete(record.id)

        if existing_cname:
            record = existing_cname[0]
            if record.value != target:
                self.record_api.update(record_id=record.id, zone_id=zone_id, name=name,
                                      record_type='CNAME', value=target, ttl=ttl)
                logging.info("CNAME record updated.")
            else:
                logging.info("CNAME record already correct.")
        else:
            self.record_api.create(zone_id=zone_id, name=name, record_type='CNAME', value=target, ttl=ttl)
            logging.info("CNAME record created.")

    def supports_apex_cname(self) -> bool:
        return False

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        logging.warning("Hetzner DNS does not support CNAME at apex. Using Bunny anycast IP.")
        bunny_anycast_ip = "185.206.224.1"

        for record in self._get_records_by_name(zone_id, '@', 'A') + self._get_records_by_name(zone_id, '@', 'AAAA'):
            self.record_api.delete(record.id)

        self.record_api.create(zone_id=zone_id, name='@', record_type='A', value=bunny_anycast_ip, ttl=300)
        logging.info(f"Apex A record created: {apex_domain} -> {bunny_anycast_ip}")
        self.create_cname_record(zone_id, 'www', cdn_domain, apex_domain)


class DeSECDNSProvider(DNSProvider):
    """deSEC DNS API adapter."""

    API_BASE = "https://desec.io/api/v1"
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
        url = f"{self.API_BASE}{endpoint}"
        response = getattr(self.session, method.lower())(url, json=data)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 5))
            logging.warning(f"Rate limited. Waiting {retry_after}s...")
            time.sleep(retry_after)
            return self._api_request(method, endpoint, data)

        if response.status_code >= 400:
            raise Exception(f"API error: {response.status_code} - {response.text}")

        return response.json() if response.text and response.status_code != 204 else {}

    def get_zone_id(self, zone_name: str) -> str:
        try:
            self._api_request("GET", f"/domains/{zone_name}/")
            return zone_name
        except:
            raise Exception(f"Zone not found in deSEC: {zone_name}")

    def _create_or_update_rrset(self, domain: str, subname: str, record_type: str,
                                records: list, ttl: int = 3600) -> None:
        ttl = max(ttl, self.MIN_TTL)
        endpoint = f"/domains/{domain}/rrsets/"

        try:
            self._api_request("PATCH", f"{endpoint}{subname}.../{record_type}/", {"records": records, "ttl": ttl})
            logging.info(f"Updated {record_type} record for {subname or '@'}.{domain}")
        except Exception as e:
            if "404" in str(e):
                self._api_request("POST", endpoint, {"subname": subname, "type": record_type, "records": records, "ttl": ttl})
                logging.info(f"Created {record_type} record for {subname or '@'}.{domain}")
            else:
                raise

    def _delete_rrset(self, domain: str, subname: str, record_type: str) -> None:
        try:
            self._api_request("DELETE", f"/domains/{domain}/rrsets/{subname}.../{record_type}/")
        except:
            pass

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")
        target_with_dot = target if target.endswith('.') else f"{target}."
        self._delete_rrset(zone_id, name, 'A')
        self._delete_rrset(zone_id, name, 'AAAA')
        self._create_or_update_rrset(zone_id, name, 'CNAME', [target_with_dot], ttl)

    def supports_apex_cname(self) -> bool:
        return False

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        logging.info("Configuring deSEC DNS for apex domain...")
        bunny_anycast_ip = "185.206.224.1"

        self._delete_rrset(apex_domain, '', 'CNAME')
        self._delete_rrset(apex_domain, '', 'AAAA')
        self._create_or_update_rrset(apex_domain, '', 'A', [bunny_anycast_ip], 300)
        logging.info(f"Apex A record created: {apex_domain} -> {bunny_anycast_ip}")
        self.create_cname_record(apex_domain, 'www', cdn_domain, apex_domain)



class BunnyDNSProvider(DNSProvider):
    """Bunny DNS API adapter.

    Bunny.net provides DNS hosting that integrates seamlessly with their CDN.
    Uses the same API key as Bunny Storage/CDN (BUNNY_API_KEY).
    """

    API_BASE = "https://api.bunny.net"

    # DNS Record Types in Bunny API
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

    def list_dns_zones(self) -> List[dict]:
        """List all DNS zones."""
        result = self._api_request("GET", "/dnszone")
        return result.get('Items', []) if isinstance(result, dict) else result

    def find_zone_by_domain(self, domain: str) -> Optional[dict]:
        """Find a DNS zone by domain name."""
        zones = self.list_dns_zones()
        for zone in zones:
            if zone.get('Domain') == domain:
                return zone
        return None

    def create_dns_zone(self, domain: str) -> dict:
        """Create a new DNS zone."""
        logging.info(f"Creating Bunny DNS zone for: {domain}")
        result = self._api_request("POST", "/dnszone", {"Domain": domain})
        logging.info(f"DNS Zone created. ID: {result.get('Id')}")

        # Print nameservers
        nameservers = result.get('NameServer1', ''), result.get('NameServer2', '')
        if nameservers[0]:
            logging.info("=" * 60)
            logging.info("IMPORTANT: Configure these nameservers at your domain registrar:")
            logging.info(f"  {nameservers[0]}")
            logging.info(f"  {nameservers[1]}")
            logging.info("=" * 60)

        return result

    def get_zone_id(self, zone_name: str) -> str:
        """Get zone ID, creating the zone if it doesn't exist."""
        # Check cache first
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]

        # Try to find existing zone
        zone = self.find_zone_by_domain(zone_name)

        if zone:
            zone_id = str(zone.get('Id'))
            self._zone_cache[zone_name] = zone_id
            logging.info(f"Found existing Bunny DNS zone: {zone_name} (ID: {zone_id})")
            return zone_id

        # Create new zone
        logging.info(f"Zone '{zone_name}' not found. Creating new Bunny DNS zone...")
        zone = self.create_dns_zone(zone_name)
        zone_id = str(zone.get('Id'))
        self._zone_cache[zone_name] = zone_id
        return zone_id

    def get_zone_records(self, zone_id: str) -> List[dict]:
        """Get all records for a zone."""
        result = self._api_request("GET", f"/dnszone/{zone_id}")
        return result.get('Records', [])

    def find_record(self, zone_id: str, name: str, record_type: str) -> Optional[dict]:
        """Find a specific DNS record."""
        records = self.get_zone_records(zone_id)
        type_num = self.RECORD_TYPES.get(record_type.upper())

        for record in records:
            if record.get('Name') == name and record.get('Type') == type_num:
                return record
        return None

    def create_record(self, zone_id: str, name: str, record_type: str, value: str,
                     ttl: int = 300, priority: int = 0) -> dict:
        """Create a DNS record."""
        type_num = self.RECORD_TYPES.get(record_type.upper())
        if type_num is None:
            raise ValueError(f"Unknown record type: {record_type}")

        data = {
            "Type": type_num,
            "Name": name,
            "Value": value,
            "Ttl": ttl,
            "Priority": priority,
        }

        return self._api_request("PUT", f"/dnszone/{zone_id}/records", data)

    def delete_record(self, zone_id: str, record_id: int) -> None:
        """Delete a DNS record."""
        self._api_request("DELETE", f"/dnszone/{zone_id}/records/{record_id}")

    def create_or_update_record(self, zone_id: str, name: str, record_type: str,
                                value: str, ttl: int = 300) -> None:
        """Create or update a DNS record."""
        existing = self.find_record(zone_id, name, record_type)

        if existing:
            if existing.get('Value') == value:
                logging.info(f"{record_type} record '{name}' already correct.")
                return
            # Delete existing record
            self.delete_record(zone_id, existing.get('Id'))
            logging.info(f"Deleted old {record_type} record for '{name}'")

        self.create_record(zone_id, name, record_type, value, ttl)
        logging.info(f"Created {record_type} record: {name} -> {value}")

    def delete_records_by_type(self, zone_id: str, name: str, record_types: List[str]) -> None:
        """Delete records of specified types for a name."""
        records = self.get_zone_records(zone_id)

        for record in records:
            if record.get('Name') == name:
                for rtype in record_types:
                    type_num = self.RECORD_TYPES.get(rtype.upper())
                    if record.get('Type') == type_num:
                        self.delete_record(zone_id, record.get('Id'))
                        logging.info(f"Deleted {rtype} record for '{name}'")

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        """Create a CNAME record, removing conflicting A/AAAA records first."""
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")

        # Remove trailing dot from target if present
        target = target.rstrip('.')

        # Delete conflicting A/AAAA records
        self.delete_records_by_type(zone_id, name, ['A', 'AAAA'])

        # Create/update CNAME
        self.create_or_update_record(zone_id, name, 'CNAME', target, ttl)

    def supports_apex_cname(self) -> bool:
        """Bunny DNS doesn't support CNAME at apex, use A record with anycast IP."""
        return False

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        """Configure DNS for apex domain using Bunny's anycast IP.

        Since Bunny DNS doesn't support CNAME at apex, we use Bunny's anycast IP
        (185.206.224.1) which routes to the nearest Bunny edge server.
        """
        logging.info("Configuring Bunny DNS for apex domain...")
        bunny_anycast_ip = "185.206.224.1"

        # Delete any existing conflicting records at apex
        self.delete_records_by_type(zone_id, '', ['AAAA', 'CNAME'])

        # Create A record at apex pointing to Bunny anycast IP
        self.create_or_update_record(zone_id, '', 'A', bunny_anycast_ip, 300)
        logging.info(f"Apex A record created: {apex_domain} -> {bunny_anycast_ip}")

        # Create www CNAME
        self.create_cname_record(zone_id, 'www', cdn_domain, apex_domain)





class ClouDNSDNSProvider(DNSProvider):
    """ClouDNS API adapter.

    ClouDNS is a European DNS provider with affordable DDoS protection.
    Uses auth-id + auth-password authentication.

    Environment Variables:
        CLOUDNS_AUTH_ID: ClouDNS auth ID
        CLOUDNS_AUTH_PASSWORD: ClouDNS auth password
    """
    API_BASE = "https://api.cloudns.net"
    VALID_TTLS = [60, 300, 900, 1800, 3600, 21600, 43200, 86400, 172800, 259200, 604800, 1209600, 2592000]
    MIN_TTL = 60

    def __init__(self, auth_id: str = None, auth_password: str = None,
                 sub_auth_id: str = None, sub_auth_user: str = None):
        self.auth_id = auth_id or os.environ.get("CLOUDNS_AUTH_ID")
        self.auth_password = auth_password or os.environ.get("CLOUDNS_AUTH_PASSWORD")
        self.sub_auth_id = sub_auth_id or os.environ.get("CLOUDNS_SUB_AUTH_ID")
        self.sub_auth_user = sub_auth_user or os.environ.get("CLOUDNS_SUB_AUTH_USER")

        if not self.auth_password:
            raise ValueError("ClouDNS auth-password is required (CLOUDNS_AUTH_PASSWORD)")
        if not self.auth_id and not self.sub_auth_id and not self.sub_auth_user:
            raise ValueError("ClouDNS requires auth-id (CLOUDNS_AUTH_ID) or sub-auth credentials")

        self._zone_cache = {}

    def _get_auth_params(self) -> dict:
        """Get authentication parameters for API requests."""
        params = {"auth-password": self.auth_password}
        if self.auth_id:
            params["auth-id"] = self.auth_id
        elif self.sub_auth_id:
            params["sub-auth-id"] = self.sub_auth_id
        elif self.sub_auth_user:
            params["sub-auth-user"] = self.sub_auth_user
        return params

    def _api_request(self, endpoint: str, params: dict = None) -> dict:
        """Make an API request to ClouDNS."""
        url = f"{self.API_BASE}{endpoint}"
        all_params = self._get_auth_params()
        if params:
            all_params.update(params)

        response = requests.post(url, data=all_params, timeout=30)

        if response.status_code >= 400:
            raise Exception(f"ClouDNS API error: {response.status_code} - {response.text}")

        result = response.json()
        if isinstance(result, dict) and result.get("status") == "Failed":
            raise Exception(f"ClouDNS API error: {result.get('statusDescription', 'Unknown error')}")

        return result

    def _get_valid_ttl(self, ttl: int) -> int:
        """Get the closest valid TTL for ClouDNS."""
        for valid_ttl in self.VALID_TTLS:
            if ttl <= valid_ttl:
                return valid_ttl
        return self.VALID_TTLS[-1]

    def get_zone_id(self, zone_name: str) -> str:
        """Get the zone name (ClouDNS uses domain name as identifier)."""
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]

        logging.info(f"Looking up ClouDNS zone for '{zone_name}'...")

        result = self._api_request("/dns/list-zones.json", {"page": 1, "rows-per-page": 100})

        if isinstance(result, list):
            zones = result
        elif isinstance(result, dict):
            zones = list(result.values()) if result else []
        else:
            zones = []

        for zone in zones:
            if isinstance(zone, dict) and zone.get("name") == zone_name:
                logging.info(f"Found ClouDNS zone: {zone_name}")
                self._zone_cache[zone_name] = zone_name
                return zone_name

        raise Exception(f"Zone not found in ClouDNS: {zone_name}")

    def _get_records(self, zone_name: str) -> list:
        """Get all records for a zone."""
        result = self._api_request("/dns/records.json", {"domain-name": zone_name})

        if isinstance(result, list):
            return result
        elif isinstance(result, dict):
            return list(result.values()) if result else []
        return []

    def _find_record(self, zone_name: str, name: str, record_type: str) -> Optional[dict]:
        """Find a specific DNS record."""
        records = self._get_records(zone_name)

        for record in records:
            if not isinstance(record, dict):
                continue
            record_host = record.get("host", "")
            if record_host == name and record.get("type") == record_type:
                return record

        return None

    def _delete_record(self, zone_name: str, record_id: str) -> None:
        """Delete a DNS record."""
        self._api_request("/dns/delete-record.json", {
            "domain-name": zone_name,
            "record-id": record_id
        })

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        """Create or update a CNAME record."""
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")

        target = target.rstrip(".")
        valid_ttl = self._get_valid_ttl(ttl)

        # Delete conflicting A/AAAA records
        for rtype in ['A', 'AAAA']:
            existing = self._find_record(zone_name, name, rtype)
            if existing and existing.get("id"):
                logging.info(f"Deleting conflicting {rtype} record...")
                self._delete_record(zone_name, existing["id"])

        # Check for existing CNAME
        existing_cname = self._find_record(zone_name, name, "CNAME")
        if existing_cname:
            if existing_cname.get("record") == target:
                logging.info("CNAME record already correct.")
                return
            # Delete and recreate
            self._delete_record(zone_name, existing_cname["id"])
            logging.info("Deleted old CNAME record.")

        # Create new CNAME
        self._api_request("/dns/add-record.json", {
            "domain-name": zone_name,
            "record-type": "CNAME",
            "host": name,
            "record": target,
            "ttl": valid_ttl
        })
        logging.info("CNAME record created.")

    def supports_apex_cname(self) -> bool:
        """ClouDNS does not support CNAME at apex."""
        return False

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        """Configure DNS for apex domain using Bunny's anycast IP."""
        logging.info("Configuring ClouDNS for apex domain...")
        bunny_anycast_ip = "185.206.224.1"

        # Delete conflicting records at apex
        for rtype in ['AAAA', 'CNAME']:
            existing = self._find_record(apex_domain, "", rtype)
            if existing and existing.get("id"):
                self._delete_record(apex_domain, existing["id"])

        # Create/update A record at apex
        existing_a = self._find_record(apex_domain, "", "A")
        if existing_a:
            if existing_a.get("record") == bunny_anycast_ip:
                logging.info("Apex A record already correct.")
            else:
                self._delete_record(apex_domain, existing_a["id"])
                self._api_request("/dns/add-record.json", {
                    "domain-name": apex_domain,
                    "record-type": "A",
                    "host": "",
                    "record": bunny_anycast_ip,
                    "ttl": 300
                })
                logging.info(f"Apex A record updated: {apex_domain} -> {bunny_anycast_ip}")
        else:
            self._api_request("/dns/add-record.json", {
                "domain-name": apex_domain,
                "record-type": "A",
                "host": "",
                "record": bunny_anycast_ip,
                "ttl": 300
            })
            logging.info(f"Apex A record created: {apex_domain} -> {bunny_anycast_ip}")

        # Create www CNAME
        self.create_cname_record(apex_domain, "www", cdn_domain, apex_domain)


def get_dns_provider(provider_name: str) -> DNSProvider:
    """Factory function to get the appropriate DNS provider."""
    providers = {
        'cloudflare': CloudflareDNSProvider,
        'hetzner': HetznerDNSProvider,
        'desec': DeSECDNSProvider,
        'bunny': BunnyDNSProvider,
        'cloudns': ClouDNSDNSProvider,
    }
    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}")
    return providers[provider_name]()


# =============================================================================
# Bunny Storage Client
# =============================================================================

class BunnyStorageClient:
    """Client for Bunny Storage API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": api_key,
            "Content-Type": "application/json"
        })

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to Bunny.net API."""
        url = f"{BUNNY_API_BASE}{endpoint}"
        response = getattr(self.session, method.lower())(url, json=data)

        if response.status_code >= 400:
            raise Exception(f"Bunny API error: {response.status_code} - {response.text}")

        return response.json() if response.text and response.status_code not in [204] else {}

    def list_storage_zones(self) -> List[dict]:
        """List all storage zones."""
        return self._api_request("GET", "/storagezone")

    def find_storage_zone_by_name(self, name: str) -> Optional[dict]:
        """Find a storage zone by name."""
        for zone in self.list_storage_zones():
            if zone.get('Name') == name:
                return zone
        return None

    def create_storage_zone(self, name: str, region: str = 'DE') -> dict:
        """Create a new storage zone."""
        logging.info(f"Creating Bunny Storage Zone: {name} in {region}")

        data = {
            "Name": name,
            "Region": region,
            "ZoneTier": 0,  # Standard tier
        }

        result = self._api_request("POST", "/storagezone", data)
        logging.info(f"Storage Zone created. ID: {result.get('Id')}")
        return result

    def delete_storage_zone(self, zone_id: int) -> None:
        """Delete a storage zone."""
        self._api_request("DELETE", f"/storagezone/{zone_id}")

    def upload_file(self, storage_hostname: str, storage_password: str,
                   zone_name: str, path: str, content: bytes, content_type: str = None) -> None:
        """Upload a file to the storage zone."""
        url = f"https://{storage_hostname}/{zone_name}/{path}"
        headers = {"AccessKey": storage_password}
        if content_type:
            headers["Content-Type"] = content_type

        response = requests.put(url, data=content, headers=headers)
        if response.status_code not in [200, 201]:
            raise Exception(f"Upload failed: {response.status_code} - {response.text}")


# =============================================================================
# Bunny CDN Client
# =============================================================================

class BunnyCDNClient:
    """Client for Bunny CDN API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": api_key,
            "Content-Type": "application/json"
        })

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        url = f"{BUNNY_API_BASE}{endpoint}"
        response = getattr(self.session, method.lower())(url, json=data)

        if response.status_code >= 400:
            raise Exception(f"Bunny API error: {response.status_code} - {response.text}")

        return response.json() if response.text and response.status_code not in [204] else {}

    def list_pull_zones(self) -> List[dict]:
        """List all pull zones."""
        return self._api_request("GET", "/pullzone")

    def find_pull_zone_by_name(self, name: str) -> Optional[dict]:
        """Find a pull zone by name."""
        for zone in self.list_pull_zones():
            if zone.get('Name') == name:
                return zone
        return None

    def find_pull_zone_by_hostname(self, hostname: str) -> Optional[dict]:
        """Find a pull zone by hostname."""
        for zone in self.list_pull_zones():
            for h in zone.get('Hostnames', []):
                if h.get('Value') == hostname:
                    return zone
        return None

    def create_pull_zone_for_storage(self, name: str, storage_zone_id: int) -> dict:
        """Create a pull zone connected to a storage zone."""
        logging.info(f"Creating Bunny CDN Pull Zone: {name}")

        data = {
            "Name": name,
            "OriginType": 2,  # Storage Zone
            "StorageZoneId": storage_zone_id,
            "EnableGeoZoneUS": True,
            "EnableGeoZoneEU": True,
            "EnableGeoZoneASIA": True,
            "EnableGeoZoneSA": True,
            "EnableGeoZoneAF": True,
        }

        result = self._api_request("POST", "/pullzone", data)
        logging.info(f"Pull Zone created. ID: {result.get('Id')}")
        return result

    def add_hostname(self, pull_zone_id: int, hostname: str) -> dict:
        """Add a custom hostname to a pull zone."""
        logging.info(f"Adding hostname '{hostname}' to pull zone {pull_zone_id}")

        try:
            self._api_request("POST", f"/pullzone/{pull_zone_id}/addHostname", {"Hostname": hostname})
            logging.info(f"Hostname '{hostname}' added.")
            return {"added": True}
        except Exception as e:
            if "hostname_already_registered" in str(e):
                logging.info(f"Hostname '{hostname}' already registered.")
                return {"added": False, "exists": True}
            raise

    def request_ssl_certificate(self, pull_zone_id: int, hostname: str,
                                use_dns01: bool = False) -> bool:
        """Request a free SSL certificate for a hostname.

        The API endpoint is /pullzone/loadFreeCertificate (without pull zone ID
        in the path).

        Args:
            pull_zone_id: Unused, kept for backwards compatibility.
            hostname: The hostname to request a certificate for.
            use_dns01: Use DNS01 validation instead of HTTP01. Set to True when
                       the domain uses Bunny DNS.
        """
        logging.info(f"Requesting SSL certificate for '{hostname}'...")

        try:
            params = f"hostname={hostname}"
            if use_dns01:
                params += "&useOnlyHttp01=false"
                logging.info("Using DNS01 validation (useOnlyHttp01=false)")
            url = f"{BUNNY_API_BASE}/pullzone/loadFreeCertificate?{params}"
            response = self.session.get(url)
            if response.status_code in [200, 204]:
                logging.info("SSL certificate requested successfully.")
                return True
            else:
                logging.warning(f"SSL certificate request returned: {response.status_code}")
                return False
        except Exception as e:
            logging.warning(f"SSL certificate request failed: {e}")
            return False

    def configure_error_pages(self, pull_zone_id: int, spa_mode: bool = False) -> None:
        """Configure error page handling (legacy approach, prefer configure_spa_edge_rules)."""
        if not spa_mode:
            return

        logging.info("Configuring SPA error handling...")
        data = {
            "ErrorPageEnableCustomCode": True,
            "ErrorPageCustomCode": '<!DOCTYPE html><html><head><script>sessionStorage.redirect=location.pathname+location.search+location.hash;</script><meta http-equiv="refresh" content="0;url=/"></head></html>',
            "ErrorPageWhitelabel": True
        }
        self._api_request("POST", f"/pullzone/{pull_zone_id}", data)
        logging.info("SPA error handling configured.")

    def get_edge_rules(self, pull_zone_id: int) -> List[dict]:
        """Get edge rules for a pull zone."""
        pull_zone = self._api_request("GET", f"/pullzone/{pull_zone_id}")
        return pull_zone.get('EdgeRules', [])

    def add_or_update_edge_rule(self, pull_zone_id: int, rule: dict) -> dict:
        """Add or update an edge rule on a pull zone."""
        return self._api_request("POST", f"/pullzone/{pull_zone_id}/edgerules/addOrUpdate", rule)

    def configure_spa_routing(self, storage_zone_id: int) -> None:
        """Configure SPA (Single Page Application) routing on a storage zone.

        Sets the storage zone to serve /index.html for all 404 responses and
        rewrites the status code from 404 to 200. This enables client-side
        routing with clean URLs (no hash fragments).

        This is the recommended approach for Bunny Storage-based pull zones,
        as edge rules cannot perform internal URL rewrites on storage origins.
        """
        logging.info("Configuring SPA routing on storage zone...")

        data = {
            "Custom404FilePath": "/index.html",
            "Rewrite404To200": True,
        }

        self._api_request("POST", f"/storagezone/{storage_zone_id}", data)
        logging.info("SPA routing configured: 404 -> /index.html (200)")

    def get_pull_zone_hostname(self, pull_zone: dict) -> str:
        """Get the default hostname for a pull zone."""
        hostnames = pull_zone.get('Hostnames', [])
        for hostname in hostnames:
            if hostname.get('Value', '').endswith('.b-cdn.net'):
                return hostname['Value']
        return f"{pull_zone['Name']}.b-cdn.net"

    def purge_cache(self, pull_zone_id: int) -> None:
        """Purge the CDN cache."""
        logging.info(f"Purging cache for pull zone {pull_zone_id}...")
        self._api_request("POST", f"/pullzone/{pull_zone_id}/purgeCache")


# =============================================================================
# Setup Report
# =============================================================================

class SetupReport:
    """Track and report CDN setup status."""

    def __init__(self, domain: str, mode: str, dns_provider: str, region: str):
        self.timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        self.domain = domain
        self.mode = mode
        self.dns_provider = dns_provider
        self.region = region
        self.region_name = BUNNY_STORAGE_REGIONS.get(region, {}).get('name', region)
        self.components = {
            'storage_zone': {'status': 'pending', 'details': {}},
            'pull_zone': {'status': 'pending', 'details': {}},
            'hostnames': {'status': 'pending', 'details': {}},
            'dns_records': {'status': 'pending', 'details': {}},
            'ssl_certificates': {'status': 'pending', 'details': {}},
            'spa_config': {'status': 'pending', 'details': {}},
            'test_content': {'status': 'pending', 'details': {}},
            'cdn_test': {'status': 'pending', 'details': {}},
        }
        self.urls = {}

    def set_component(self, name: str, status: str, **details):
        if name in self.components:
            self.components[name]['status'] = status
            self.components[name]['details'].update(details)

    def set_url(self, name: str, url: str):
        self.urls[name] = url

    def generate_markdown(self) -> str:
        lines = [
            f"# CDN Setup Report: {self.domain}",
            "",
            f"**Generated:** {self.timestamp}",
            f"**Script:** setup_bunny_storage.py",
            f"**Infrastructure:** Bunny Storage + Bunny CDN",
            "",
            "---",
            "",
            "## Configuration Summary",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Domain | `{self.domain}` |",
            f"| Mode | {self.mode.title()} |",
            f"| DNS Provider | {self.dns_provider.title()} |",
            f"| Storage Region | {self.region} ({self.region_name}) |",
            "",
            "## Component Status",
            "",
        ]

        status_icons = {
            'created': '[NEW]', 'exists': '[OK]', 'configured': '[OK]',
            'requested': '[OK]', 'uploaded': '[OK]', 'passed': '[OK]',
            'skipped': '[SKIP]', 'failed': '[FAIL]', 'pending': '[ ]',
        }

        component_names = {
            'storage_zone': 'Bunny Storage Zone',
            'pull_zone': 'Bunny CDN Pull Zone',
            'hostnames': 'Custom Hostnames',
            'dns_records': 'DNS Records',
            'ssl_certificates': 'SSL Certificates',
            'spa_config': 'SPA Configuration',
            'test_content': 'Test Content',
            'cdn_test': 'CDN Functionality Test',
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

        if self.urls:
            lines.extend([
                "## URLs and Endpoints",
                "",
                "| Name | URL |",
                "|------|-----|",
            ])
            for name, url in self.urls.items():
                lines.append(f"| {name.replace('_', ' ').title()} | {url} |")
            lines.extend([
                "",
                "### Quick Test Links",
                "",
            ])
            if 'public_url' in self.urls:
                base_url = self.urls['public_url']
                lines.append(f"- Homepage: {base_url}/")
                lines.append(f"- Test page: {base_url}/index-test.html")
            lines.append("")

        lines.extend([
            "## Advantages of Bunny Storage",
            "",
            "- **Automatic index.html**: Root URL `/` automatically serves `index.html`",
            "- **Built-in 404 handling**: Custom error pages work correctly",
            "- **Edge replication**: Files replicated to edge locations globally",
            "- **Simple billing**: Single provider for storage and CDN",
            "",
            "---",
            "",
            "*Generated by setup_bunny_storage.py*",
        ])

        return '\n'.join(lines)

    def save_report(self, output_dir: str = '.') -> str:
        os.makedirs(output_dir, exist_ok=True)
        safe_domain = self.domain.replace('.', '-')
        timestamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        filename = f"cdn-setup-{safe_domain}-{timestamp}.md"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.generate_markdown())

        logging.info(f"Setup report saved to: {filepath}")
        return filepath

    def write_deploy_env(self, env_file: str = '.deploy.env') -> bool:
        """Write Bunny CDN configuration to .deploy.env file.

        Appends or updates BUNNY_STORAGE_ZONE, BUNNY_STORAGE_PASSWORD, and BUNNY_PULL_ZONE_ID.
        """
        storage_zone = self.components['storage_zone']['details'].get('storage_zone_name')
        pull_zone_id = self.components['pull_zone']['details'].get('pull_zone_id')

        if not storage_zone:
            logging.warning("No storage zone name to write to deploy env")
            return False

        # Read existing content
        existing_content = ""
        existing_vars = {}
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                existing_content = f.read()
                for line in existing_content.split('\n'):
                    if '=' in line and not line.strip().startswith('#'):
                        key = line.split('=')[0].strip()
                        existing_vars[key] = True

        # Prepare new variables
        new_vars = []

        # Add header comment if file is empty or doesn't exist
        if not existing_content.strip():
            new_vars.append("# Bunny CDN Configuration")
            new_vars.append(f"# Generated by setup_bunny_storage.py at {self.timestamp}")
            new_vars.append("")

        # Get storage password from storage zone details or try to get from component
        storage_password = self.components['storage_zone']['details'].get('storage_password', '')

        # Add/update variables
        bunny_vars = {
            'BUNNY_STORAGE_ZONE': storage_zone,
        }

        if storage_password:
            bunny_vars['BUNNY_STORAGE_PASSWORD'] = storage_password

        if pull_zone_id:
            bunny_vars['BUNNY_PULL_ZONE_ID'] = str(pull_zone_id)

        lines_to_add = []
        for key, value in bunny_vars.items():
            if key in existing_vars:
                # Update existing variable
                import re
                pattern = rf'^{key}=.*$'
                existing_content = re.sub(pattern, f'{key}={value}', existing_content, flags=re.MULTILINE)
                logging.info(f"Updated {key} in {env_file}")
            else:
                lines_to_add.append(f"{key}={value}")

        # Write file
        with open(env_file, 'w') as f:
            # Write updated content
            if existing_content.strip():
                f.write(existing_content)
                if not existing_content.endswith('\n'):
                    f.write('\n')

            # Add new variables
            if lines_to_add:
                if existing_content.strip():
                    f.write('\n# Bunny CDN (auto-generated)\n')
                for line in lines_to_add:
                    f.write(f"{line}\n")
                    key = line.split('=')[0]
                    logging.info(f"Added {key} to {env_file}")

        logging.info(f"[OK] Deploy env written to: {env_file}")
        return True


# =============================================================================
# Setup Functions
# =============================================================================

def create_test_content(storage_client: BunnyStorageClient, storage_hostname: str,
                       storage_password: str, zone_name: str, region: str, spa_mode: bool) -> None:
    """Create test content in the storage zone."""
    logging.info("Creating test content...")

    test_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>CDN Test Page</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .container {{ max-width: 600px; margin: 0 auto; text-align: center; }}
        .success {{ color: #28a745; }}
        .info {{ background: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="success">CDN Setup Successful!</h1>
        <p>Your Bunny Storage + Bunny CDN is working correctly.</p>
        <div class="info">
            <h3>Configuration</h3>
            <p><strong>Storage:</strong> Bunny Storage ({BUNNY_STORAGE_REGIONS.get(region, {}).get('name', region)})</p>
            <p><strong>CDN:</strong> Bunny CDN</p>
            <p><strong>Zone:</strong> {zone_name}</p>
            <p><strong>SPA Mode:</strong> {'Enabled' if spa_mode else 'Disabled'}</p>
        </div>
        <p><small>Generated by setup_bunny_storage.py</small></p>
    </div>
</body>
</html>"""

    error_html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>404 - Page Not Found</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; text-align: center; }
        .error { color: #dc3545; }
    </style>
</head>
<body>
    <h1 class="error">404 - Page Not Found</h1>
    <p>The page you're looking for doesn't exist.</p>
    <p><a href="/">Back to Home</a></p>
</body>
</html>"""

    files = [
        ('index.html', test_html, 'text/html'),
        ('index-test.html', test_html, 'text/html'),
        ('error.html', error_html, 'text/html'),
        ('test.txt', f"CDN test - {zone_name} - {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}", 'text/plain'),
    ]

    for filename, content, content_type in files:
        content_bytes = content.encode('utf-8') if isinstance(content, str) else content
        storage_client.upload_file(storage_hostname, storage_password, zone_name, filename, content_bytes, content_type)
        logging.info(f"{filename} uploaded.")


def test_cdn_functionality(url: str, timeout: int = 60) -> bool:
    """Test if the CDN is working correctly."""
    logging.info(f"Testing CDN at: {url}")
    test_url = f"{url}/test.txt"
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            response = requests.get(test_url, timeout=10)
            if response.status_code == 200:
                logging.info(f"CDN test passed! Status: {response.status_code}")
                return True
            logging.info(f"Got status {response.status_code}, waiting...")
        except requests.RequestException as e:
            logging.info(f"Request failed: {e}, retrying...")
        time.sleep(5)

    logging.warning("CDN test timed out.")
    return False


# =============================================================================
# Main Setup
# =============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Setup CDN with Bunny Storage + Bunny CDN',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup_bunny_storage.py --domain example.com --subdomain www
  python setup_bunny_storage.py --domain example.com --apex --www-redirect
  python setup_bunny_storage.py --domain example.com --subdomain www --spa-mode
  python setup_bunny_storage.py --add-spa-rules 12345  # Configure SPA routing on existing storage zone

Environment Variables:
  BUNNY_API_KEY: Bunny.net API key
  CLOUDFLARE_API_TOKEN: For Cloudflare DNS
  HETZNER_DNS_API_TOKEN: For Hetzner DNS
  DESEC_API_TOKEN: For deSEC DNS
        """
    )

    parser.add_argument('--domain', default=None, help='The root domain (e.g., example.com)')
    parser.add_argument('--subdomain', default=None, help='The subdomain (e.g., www)')
    parser.add_argument('--apex', action='store_true', help='Setup for apex domain')
    parser.add_argument('--www-redirect', action='store_true', help='Add www redirect (with --apex)')
    parser.add_argument('--region', default='DE', choices=list(BUNNY_STORAGE_REGIONS.keys()),
                        help='Bunny Storage region (default: DE)')
    parser.add_argument('--dns-provider', choices=['cloudflare', 'hetzner', 'desec', 'bunny', 'cloudns'],
                        default='cloudflare', help='DNS provider (default: cloudflare)')
    parser.add_argument('--spa-mode', action='store_true', help='Configure for Single Page Application')
    parser.add_argument('--skip-tests', action='store_true', help='Skip CDN functionality tests')
    parser.add_argument('--test-timeout', type=int, default=60, help='Timeout for CDN tests')
    parser.add_argument('--report-dir', default='.', help='Directory to save the setup report')
    parser.add_argument('--deploy-env', default='.deploy.env',
                        help='Path to .deploy.env file (default: .deploy.env)')
    parser.add_argument('--no-deploy-env', action='store_true',
                        help='Skip writing to .deploy.env file')
    parser.add_argument('--add-spa-rules', type=int, metavar='STORAGE_ZONE_ID',
                        help='Configure SPA routing on an existing storage zone and exit')

    return parser.parse_args()


def main():
    args = parse_arguments()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    # Handle standalone SPA routing command
    if args.add_spa_rules:
        api_key = os.environ.get("BUNNY_API_KEY")
        if not api_key:
            logging.error("BUNNY_API_KEY environment variable not set.")
            return 1
        # --add-spa-rules takes a STORAGE ZONE ID (not pull zone ID)
        cdn_client = BunnyCDNClient(api_key)
        cdn_client.configure_spa_routing(args.add_spa_rules)
        return 0

    if not args.domain:
        logging.error("--domain is required for full setup (or use --add-spa-rules PULL_ZONE_ID)")
        return 1

    if args.apex:
        full_domain = args.domain
        www_domain = f"www.{args.domain}"
        mode = 'apex'
    elif args.subdomain:
        full_domain = f"{args.subdomain}.{args.domain}"
        www_domain = None
        mode = 'subdomain'
    else:
        logging.error("Either --subdomain or --apex must be specified")
        return 1

    # Sanitize zone name
    zone_name = full_domain.replace('.', '-')

    # Initialize report
    report = SetupReport(domain=full_domain, mode=mode, dns_provider=args.dns_provider, region=args.region)

    logging.info("=" * 50)
    logging.info("BUNNY STORAGE + BUNNY CDN SETUP")
    logging.info("=" * 50)
    logging.info(f"Domain: {args.domain}")
    logging.info(f"Mode: {mode.title()}")
    logging.info(f"Full Domain: {full_domain}")
    logging.info(f"Storage Zone: {zone_name}")
    logging.info(f"Region: {args.region} ({BUNNY_STORAGE_REGIONS[args.region]['name']})")
    logging.info(f"DNS Provider: {args.dns_provider}")
    logging.info(f"SPA Mode: {args.spa_mode}")
    logging.info("=" * 50)

    try:
        # Initialize clients
        api_key = os.environ.get("BUNNY_API_KEY")
        if not api_key:
            raise ValueError("BUNNY_API_KEY environment variable not set.")

        storage_client = BunnyStorageClient(api_key)
        cdn_client = BunnyCDNClient(api_key)
        dns_provider = get_dns_provider(args.dns_provider)
        zone_id = dns_provider.get_zone_id(args.domain)

        # 1. Create Storage Zone
        logging.info("\n--- Step 1: Bunny Storage Zone ---")
        existing_storage = storage_client.find_storage_zone_by_name(zone_name)

        if existing_storage:
            logging.info(f"Storage zone '{zone_name}' already exists.")
            storage_zone = existing_storage
            report.set_component('storage_zone', 'exists',
                                storage_zone_id=storage_zone['Id'],
                                storage_zone_name=zone_name,
                                storage_password=storage_zone['Password'])
        else:
            storage_zone = storage_client.create_storage_zone(zone_name, args.region)
            report.set_component('storage_zone', 'created',
                                storage_zone_id=storage_zone['Id'],
                                storage_zone_name=zone_name,
                                storage_password=storage_zone['Password'])

        storage_zone_id = storage_zone['Id']
        storage_hostname = storage_zone.get('StorageHostname', BUNNY_STORAGE_REGIONS[args.region]['hostname'])
        storage_password = storage_zone['Password']

        # 2. Create Pull Zone
        logging.info("\n--- Step 2: Bunny CDN Pull Zone ---")
        existing_pull_zone = cdn_client.find_pull_zone_by_hostname(full_domain)

        if not existing_pull_zone:
            existing_pull_zone = cdn_client.find_pull_zone_by_name(zone_name)

        if existing_pull_zone:
            logging.info(f"Pull zone already exists (ID: {existing_pull_zone['Id']})")
            pull_zone = existing_pull_zone
            report.set_component('pull_zone', 'exists',
                                pull_zone_id=pull_zone['Id'],
                                pull_zone_name=pull_zone['Name'])
        else:
            pull_zone = cdn_client.create_pull_zone_for_storage(zone_name, storage_zone_id)
            report.set_component('pull_zone', 'created',
                                pull_zone_id=pull_zone['Id'],
                                pull_zone_name=zone_name)

        pull_zone_id = pull_zone['Id']
        cdn_hostname = cdn_client.get_pull_zone_hostname(pull_zone)
        logging.info(f"CDN Hostname: {cdn_hostname}")

        report.set_url('cdn_hostname', f"https://{cdn_hostname}")

        # 3. Add Custom Hostnames
        logging.info("\n--- Step 3: Custom Hostnames ---")
        hostname_details = {'configured_hostnames': []}

        existing_hostnames = [h.get('Value', '') for h in pull_zone.get('Hostnames', [])]

        if full_domain not in existing_hostnames:
            cdn_client.add_hostname(pull_zone_id, full_domain)
            hostname_details['configured_hostnames'].append(f"{full_domain} (added)")
        else:
            hostname_details['configured_hostnames'].append(f"{full_domain} (existed)")

        if args.apex and args.www_redirect and www_domain not in existing_hostnames:
            cdn_client.add_hostname(pull_zone_id, www_domain)
            hostname_details['configured_hostnames'].append(f"{www_domain} (added)")

        report.set_component('hostnames', 'configured', **hostname_details)

        # 4. Configure SPA Mode (Storage Zone 404 rewrite)
        if args.spa_mode:
            logging.info("\n--- Step 4: SPA Configuration ---")
            cdn_client.configure_spa_routing(storage_zone_id)
            report.set_component('spa_config', 'configured', mode='storage_404_rewrite',
                                description='Storage zone serves /index.html for all 404s with status 200')
        else:
            report.set_component('spa_config', 'skipped', mode='disabled')

        # 5. Configure DNS
        logging.info("\n--- Step 5: DNS Configuration ---")
        dns_details = {'provider': args.dns_provider, 'records': []}

        if args.apex:
            dns_provider.create_apex_records(zone_id, args.domain, www_domain, cdn_hostname)
            if dns_provider.supports_apex_cname():
                dns_details['records'].append(f"CNAME {args.domain} -> {cdn_hostname}")
            else:
                dns_details['records'].append(f"A {args.domain} -> 185.206.224.1")
            dns_details['records'].append(f"CNAME www.{args.domain} -> {cdn_hostname}")
        else:
            dns_provider.create_cname_record(zone_id, args.subdomain, cdn_hostname, args.domain)
            dns_details['records'].append(f"CNAME {full_domain} -> {cdn_hostname}")

        report.set_component('dns_records', 'configured', **dns_details)

        # 6. Request SSL Certificates
        logging.info("\n--- Step 6: SSL Certificates ---")
        logging.info("Waiting 30 seconds for DNS propagation...")
        time.sleep(30)

        ssl_details = {'certificates': []}
        use_dns01 = args.dns_provider == 'bunny'
        ssl_success = cdn_client.request_ssl_certificate(pull_zone_id, full_domain, use_dns01=use_dns01)
        ssl_details['certificates'].append(f"{full_domain}: {'requested' if ssl_success else 'pending'}")

        if args.apex and args.www_redirect:
            www_ssl = cdn_client.request_ssl_certificate(pull_zone_id, www_domain, use_dns01=use_dns01)
            ssl_details['certificates'].append(f"{www_domain}: {'requested' if www_ssl else 'pending'}")

        report.set_component('ssl_certificates', 'requested', **ssl_details)

        # 7. Upload Test Content
        logging.info("\n--- Step 7: Test Content ---")
        create_test_content(storage_client, storage_hostname, storage_password, zone_name, args.region, args.spa_mode)
        report.set_component('test_content', 'uploaded', files=['index.html', 'index-test.html', 'error.html', 'test.txt'])

        # Set URLs
        report.set_url('public_url', f"https://{full_domain}")
        if args.apex and args.www_redirect:
            report.set_url('www_url', f"https://{www_domain}")

        # Summary
        logging.info("\n" + "=" * 50)
        logging.info("CDN SETUP COMPLETE")
        logging.info("=" * 50)
        logging.info(f"Storage Zone: {zone_name} (ID: {storage_zone_id})")
        logging.info(f"Pull Zone: {pull_zone_id}")
        logging.info(f"CDN Hostname: {cdn_hostname}")
        logging.info(f"Public URL: https://{full_domain}")
        logging.info("=" * 50)

        # 8. Test CDN
        test_success = True
        if not args.skip_tests:
            logging.info("\nWaiting 30 seconds for CDN propagation...")
            time.sleep(30)
            test_success = test_cdn_functionality(f"https://{full_domain}", args.test_timeout)
            report.set_component('cdn_test', 'passed' if test_success else 'failed',
                               test_url=f"https://{full_domain}/test.txt")
        else:
            report.set_component('cdn_test', 'skipped')

        # Save report
        report_path = report.save_report(args.report_dir)

        # Write to .deploy.env
        if not args.no_deploy_env:
            report.write_deploy_env(args.deploy_env)

        if test_success or args.skip_tests:
            logging.info(f"\n[OK] CDN setup completed successfully!")
            logging.info(f"[i] Setup report: {report_path}")
            return 0
        else:
            logging.warning(f"\n[!] CDN setup completed but tests indicate issues.")
            logging.info(f"[i] Setup report: {report_path}")
            return 1

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()

        try:
            report_path = report.save_report(args.report_dir)
            logging.info(f"[i] Partial report saved: {report_path}")
        except:
            pass

        return 1


if __name__ == "__main__":
    exit(main())
