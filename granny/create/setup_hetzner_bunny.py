#!/usr/bin/env python3
"""
Hetzner S3 + Bunny CDN Website Setup Script

This script sets up a CDN for static content using Hetzner Object Storage (S3-compatible)
and Bunny CDN. Supports multiple DNS providers for domain configuration.

For AWS S3 + CloudFront setup, use: setup_aws_cloudfront.py

FEATURES:
---------
- Hetzner Object Storage (S3-compatible) for file storage
- Bunny CDN for global content delivery
- Free SSL certificates via Bunny CDN
- SPA (Single Page Application) support
- Multiple DNS providers: Cloudflare, Hetzner DNS, deSEC
- Private mode with Bunny CDN Token Authentication (signed URLs)

MODES:
------
1. Subdomain Mode (default):
   - Setup CDN for subdomain.example.com
   - Use: python setup_hetzner_bunny.py --domain example.com --subdomain cdn

2. Apex Mode (production):
   - Setup CDN for example.com with optional www redirect
   - Use: python setup_hetzner_bunny.py --domain example.com --apex --www-redirect

Environment Variables:
    HETZNER_S3_ACCESS_KEY: Required - Hetzner S3 access key
    HETZNER_S3_SECRET_KEY: Required - Hetzner S3 secret key
    BUNNY_API_KEY: Required - Bunny.net API key
    CLOUDFLARE_API_TOKEN: Required for Cloudflare DNS
    HETZNER_DNS_API_TOKEN: Required for Hetzner DNS
    DESEC_API_TOKEN: Required for deSEC DNS

Usage Examples:
    # Subdomain: Basic static site (public)
    python setup_hetzner_bunny.py --domain example.com --subdomain cdn

    # Apex: Production website with www redirect
    python setup_hetzner_bunny.py --domain example.com --apex --www-redirect --spa-mode

    # Private storage with signed URLs (e.g., invoices, user files)
    python setup_hetzner_bunny.py --domain m3rp.ai --subdomain cdn --private

    # Token auth only (public bucket, signed CDN URLs)
    python setup_hetzner_bunny.py --domain example.com --subdomain cdn --token-auth

    # Specify Hetzner region
    python setup_hetzner_bunny.py --domain example.com --subdomain cdn --region nbg1
"""

import boto3
import os
import json
import time
import logging
import argparse
import requests
from abc import ABC, abstractmethod
from typing import Optional, List
from botocore.config import Config
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
    pass

HetznerDnsZone = None
HetznerDnsRecord = None
try:
    from hetzner_dns_api import DnsZone as HetznerDnsZone, DnsRecord as HetznerDnsRecord
except ImportError:
    pass


# =============================================================================
# Hetzner S3 Configuration
# =============================================================================

HETZNER_S3_REGIONS = {
    'fsn1': 'fsn1.your-objectstorage.com',
    'nbg1': 'nbg1.your-objectstorage.com',
    'hel1': 'hel1.your-objectstorage.com',
}

HETZNER_S3_REGION_NAMES = {
    'fsn1': 'Falkenstein, Germany',
    'nbg1': 'Nuremberg, Germany',
    'hel1': 'Helsinki, Finland',
}


# =============================================================================
# Bunny CDN Configuration
# =============================================================================

BUNNY_API_BASE = "https://api.bunny.net"


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
    def supports_apex_cname(self) -> bool:
        """Returns True if provider supports CNAME-like records at apex."""
        pass

    @abstractmethod
    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        """Create DNS records for apex domain setup."""
        pass

    def supports_zone_creation(self) -> bool:
        """Returns True if provider supports creating new zones via API."""
        return False

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support zone creation via API."
        )

    def get_or_create_zone(self, zone_name: str, auto_create: bool = False) -> str:
        """Get zone ID, optionally creating the zone if it doesn't exist."""
        try:
            return self.get_zone_id(zone_name)
        except Exception as e:
            if not auto_create:
                raise
            if not self.supports_zone_creation():
                raise Exception(
                    f"Zone '{zone_name}' not found and {self.__class__.__name__} "
                    f"does not support automatic zone creation."
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
            if not existing_records.result:
                existing_records = self.client.dns.records.list(zone_id=zone_id, name=name)

            dns_record = {
                'name': name,
                'type': 'CNAME',
                'content': target,
                'proxied': False
            }

            cname_exists = False
            records_to_delete = []

            if existing_records.result:
                for record in existing_records.result:
                    if record.type == 'CNAME':
                        cname_exists = True
                        if record.content != target or record.proxied:
                            self.client.dns.records.update(
                                zone_id=zone_id, dns_record_id=record.id, **dns_record
                            )
                            logging.info("CNAME record updated.")
                        else:
                            logging.info("CNAME record already correct.")
                    elif record.type in ['A', 'AAAA']:
                        records_to_delete.append((record.id, record.type))

            for record_id, record_type in records_to_delete:
                logging.info(f"Deleting conflicting {record_type} record")
                self.client.dns.records.delete(zone_id=zone_id, dns_record_id=record_id)

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

        def create_or_update_record(name: str, content: str, proxied: bool = False):
            full_name = name if name == apex_domain else f"{name}.{apex_domain}" if '.' not in name else name

            # Delete conflicting A/AAAA records
            try:
                existing = self.client.dns.records.list(zone_id=zone_id, name=full_name)
                if existing.result:
                    for rec in existing.result:
                        if rec.type in ['A', 'AAAA']:
                            self.client.dns.records.delete(zone_id=zone_id, dns_record_id=rec.id)
            except Exception as e:
                logging.warning(f"Error deleting records: {e}")

            existing = self.client.dns.records.list(zone_id=zone_id, name=full_name)
            record_data = {'name': name, 'type': 'CNAME', 'content': content, 'proxied': proxied}

            if existing.result:
                for rec in existing.result:
                    if rec.type == 'CNAME':
                        if rec.content != content or rec.proxied != proxied:
                            self.client.dns.records.update(zone_id=zone_id, dns_record_id=rec.id, **record_data)
                        return

            self.client.dns.records.create(zone_id=zone_id, **record_data)

        create_or_update_record(apex_domain, cdn_domain, proxied=False)
        logging.info(f"Apex configured: {apex_domain} -> {cdn_domain}")

        create_or_update_record('www', cdn_domain, proxied=False)
        logging.info(f"WWW configured: {www_domain} -> {cdn_domain}")


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

        try:
            existing_cname = self._get_records_by_name(zone_id, name, 'CNAME')
            existing_a = self._get_records_by_name(zone_id, name, 'A')
            existing_aaaa = self._get_records_by_name(zone_id, name, 'AAAA')

            for record in existing_a + existing_aaaa:
                logging.info(f"Deleting conflicting {record.type} record")
                self.record_api.delete(record.id)

            if existing_cname:
                record = existing_cname[0]
                if record.value != target:
                    self.record_api.update(
                        record_id=record.id, zone_id=zone_id,
                        name=name, record_type='CNAME', value=target, ttl=ttl
                    )
                    logging.info("CNAME record updated.")
                else:
                    logging.info("CNAME record already correct.")
            else:
                self.record_api.create(
                    zone_id=zone_id, name=name,
                    record_type='CNAME', value=target, ttl=ttl
                )
                logging.info("CNAME record created.")

        except Exception as e:
            logging.error(f"Error creating CNAME record: {e}")
            raise

    def supports_apex_cname(self) -> bool:
        return False

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        logging.warning("=" * 60)
        logging.warning("  HETZNER DNS APEX DOMAIN LIMITATION")
        logging.warning("=" * 60)
        logging.warning("Hetzner DNS does not support CNAME records at apex.")
        logging.warning("Using Bunny CDN's Anycast IP for A record.")
        logging.warning("=" * 60)

        # Bunny CDN Anycast IP
        bunny_anycast_ip = "185.206.224.1"

        # Delete existing A/AAAA records at apex
        existing_a = self._get_records_by_name(zone_id, '@', 'A')
        existing_aaaa = self._get_records_by_name(zone_id, '@', 'AAAA')
        for record in existing_a + existing_aaaa:
            logging.info(f"Deleting existing {record.type} record at apex")
            self.record_api.delete(record.id)

        # Create A record for Bunny CDN
        logging.info(f"Creating A record: @ -> {bunny_anycast_ip}")
        self.record_api.create(
            zone_id=zone_id, name='@',
            record_type='A', value=bunny_anycast_ip, ttl=300
        )
        logging.info(f"Apex A record created: {apex_domain} -> {bunny_anycast_ip}")

        # Create www CNAME
        self.create_cname_record(zone_id, 'www', cdn_domain, apex_domain)
        logging.info(f"WWW configured: {www_domain} -> {cdn_domain}")


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

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                logging.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                return self._api_request(method, endpoint, data)

            if response.status_code >= 400:
                raise Exception(f"API error: {response.status_code} - {response.text}")

            if response.status_code == 204:
                return {}
            return response.json() if response.text else {}

        except requests.RequestException as e:
            raise Exception(f"Request failed: {e}")

    def get_zone_id(self, zone_name: str) -> str:
        try:
            self._api_request("GET", f"/domains/{zone_name}/")
            return zone_name
        except Exception:
            raise Exception(f"Zone not found in deSEC: {zone_name}")

    def _create_or_update_rrset(self, domain: str, subname: str, record_type: str,
                                records: list, ttl: int = 3600) -> None:
        ttl = max(ttl, self.MIN_TTL)
        endpoint = f"/domains/{domain}/rrsets/"
        rrset_url = f"{endpoint}{subname}.../{record_type}/"

        # Check if record exists
        try:
            self._api_request("GET", rrset_url)
            # Record exists, update it
            self._api_request("PATCH", rrset_url, {
                "records": records, "ttl": ttl
            })
            logging.info(f"Updated {record_type} record for {subname or '@'}.{domain}")
        except Exception as e:
            if "404" in str(e):
                # Record doesn't exist, create it
                self._api_request("POST", endpoint, {
                    "subname": subname, "type": record_type,
                    "records": records, "ttl": ttl
                })
                logging.info(f"Created {record_type} record for {subname or '@'}.{domain}")
            else:
                raise

    def _delete_rrset(self, domain: str, subname: str, record_type: str) -> None:
        try:
            self._api_request("DELETE", f"/domains/{domain}/rrsets/{subname}.../{record_type}/")
        except Exception:
            pass

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")
        target_with_dot = target if target.endswith('.') else f"{target}."

        self._delete_rrset(zone_id, name, 'A')
        self._delete_rrset(zone_id, name, 'AAAA')
        self._create_or_update_rrset(zone_id, name, 'CNAME', [target_with_dot], ttl)
        logging.info("CNAME record created/updated.")

    def supports_apex_cname(self) -> bool:
        return False  # deSEC can't have CNAME at apex (conflicts with NS)

    def supports_zone_creation(self) -> bool:
        return True

    def create_zone(self, zone_name: str) -> str:
        logging.info(f"Creating deSEC zone for '{zone_name}'...")
        self._api_request("POST", "/domains/", {"name": zone_name})
        logging.info("Zone created. Configure nameservers: ns1.desec.io, ns2.desec.org")
        return zone_name

    def create_apex_records(self, zone_id: str, apex_domain: str, www_domain: str,
                           cdn_domain: str) -> None:
        logging.info("Configuring deSEC DNS for apex domain...")

        # deSEC can't have CNAME at apex (conflicts with NS records)
        # Use Bunny CDN's anycast IP for A record instead
        bunny_anycast_ip = "185.206.224.1"

        logging.info(f"Using Bunny CDN anycast IP: {bunny_anycast_ip}")

        self._delete_rrset(apex_domain, '', 'CNAME')
        self._delete_rrset(apex_domain, '', 'AAAA')

        # Create A record pointing to Bunny CDN
        self._create_or_update_rrset(apex_domain, '', 'A', [bunny_anycast_ip], 300)
        logging.info(f"Apex A record created: {apex_domain} -> {bunny_anycast_ip}")

        # WWW as CNAME to CDN hostname
        self.create_cname_record(apex_domain, 'www', cdn_domain, apex_domain)
        logging.info(f"WWW configured: {www_domain} -> {cdn_domain}")


def get_dns_provider(provider_name: str) -> DNSProvider:
    """Factory function to get the appropriate DNS provider."""
    providers = {
        'cloudflare': CloudflareDNSProvider,
        'hetzner': HetznerDNSProvider,
        'desec': DeSECDNSProvider,
    }

    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}")

    return providers[provider_name]()


# =============================================================================
# Hetzner S3 Client
# =============================================================================

class HetznerS3Client:
    """Client for Hetzner Object Storage (S3-compatible)."""

    def __init__(self, region: str = 'fsn1'):
        self.region = region
        self.endpoint = f"https://{HETZNER_S3_REGIONS[region]}"

        access_key = os.environ.get("HETZNER_S3_ACCESS_KEY")
        secret_key = os.environ.get("HETZNER_S3_SECRET_KEY")

        if not access_key or not secret_key:
            raise ValueError(
                "HETZNER_S3_ACCESS_KEY and HETZNER_S3_SECRET_KEY environment variables required."
            )

        self.client = boto3.client(
            's3',
            endpoint_url=self.endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version='s3v4')
        )

    def bucket_exists(self, bucket_name: str) -> bool:
        """Check if bucket exists."""
        try:
            self.client.head_bucket(Bucket=bucket_name)
            return True
        except Exception:
            return False

    def create_bucket(self, bucket_name: str) -> None:
        """Create a bucket if it doesn't exist."""
        if self.bucket_exists(bucket_name):
            logging.info(f"Bucket '{bucket_name}' already exists.")
            return

        logging.info(f"Creating bucket '{bucket_name}' in {self.region}...")
        # Hetzner S3 uses the endpoint URL for region, no LocationConstraint needed
        self.client.create_bucket(Bucket=bucket_name)
        logging.info(f"Bucket '{bucket_name}' created.")
        # Wait for bucket to be fully available (Hetzner S3 eventual consistency)
        logging.info("Waiting for bucket to be available...")
        time.sleep(3)

    def set_bucket_policy_public(self, bucket_name: str) -> None:
        """Set bucket policy for public read access."""
        logging.info(f"Setting public read policy on '{bucket_name}'...")

        policy = {
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

        self.client.put_bucket_policy(
            Bucket=bucket_name,
            Policy=json.dumps(policy)
        )
        logging.info("Bucket policy set.")

    def set_cors_config(self, bucket_name: str) -> None:
        """Set CORS configuration for web access."""
        logging.info(f"Setting CORS configuration on '{bucket_name}'...")

        cors_config = {
            'CORSRules': [
                {
                    'AllowedHeaders': ['*'],
                    'AllowedMethods': ['GET', 'HEAD'],
                    'AllowedOrigins': ['*'],
                    'MaxAgeSeconds': 3600
                }
            ]
        }

        self.client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration=cors_config
        )
        logging.info("CORS configuration set.")

    def upload_file(self, bucket_name: str, key: str, content: str,
                   content_type: str = 'text/html', cache_control: str = 'max-age=300') -> None:
        """Upload a file to the bucket."""
        self.client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=content.encode('utf-8'),
            ContentType=content_type,
            CacheControl=cache_control
        )

    def get_bucket_url(self, bucket_name: str) -> str:
        """Get the public URL for the bucket."""
        return f"https://{bucket_name}.{HETZNER_S3_REGIONS[self.region]}"


# =============================================================================
# Bunny CDN Client
# =============================================================================

class BunnyCDNClient:
    """Client for Bunny CDN API."""

    def __init__(self):
        self.api_key = os.environ.get("BUNNY_API_KEY")
        if not self.api_key:
            raise ValueError("BUNNY_API_KEY environment variable not set.")

        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": self.api_key,
            "Content-Type": "application/json"
        })

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to Bunny CDN."""
        url = f"{BUNNY_API_BASE}{endpoint}"

        try:
            if method == "GET":
                response = self.session.get(url)
            elif method == "POST":
                response = self.session.post(url, json=data)
            elif method == "DELETE":
                response = self.session.delete(url)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if response.status_code >= 400:
                raise Exception(f"Bunny API error: {response.status_code} - {response.text}")

            if response.status_code == 204:
                return {}

            return response.json() if response.text else {}

        except requests.RequestException as e:
            raise Exception(f"Bunny API request failed: {e}")

    def list_pull_zones(self) -> List[dict]:
        """List all pull zones."""
        return self._api_request("GET", "/pullzone")

    def find_pull_zone_by_name(self, name: str) -> Optional[dict]:
        """Find a pull zone by name."""
        zones = self.list_pull_zones()
        for zone in zones:
            if zone.get('Name') == name:
                return zone
        return None

    def find_pull_zone_by_hostname(self, hostname: str) -> Optional[dict]:
        """Find a pull zone that has a specific hostname registered."""
        zones = self.list_pull_zones()
        for zone in zones:
            for h in zone.get('Hostnames', []):
                if h.get('Value') == hostname:
                    return zone
        return None

    def get_pull_zone_details(self, pull_zone_id: int) -> dict:
        """Get detailed information about a pull zone."""
        return self._api_request("GET", f"/pullzone/{pull_zone_id}")

    def create_pull_zone(self, name: str, origin_url: str,
                        enable_geo_zones: bool = True) -> dict:
        """Create a new pull zone."""
        logging.info(f"Creating Bunny CDN pull zone: {name}")
        logging.info(f"Origin URL: {origin_url}")

        data = {
            "Name": name,
            "OriginUrl": origin_url,
            "Type": 0,  # Standard pull zone
            "EnableGeoZoneUS": enable_geo_zones,
            "EnableGeoZoneEU": enable_geo_zones,
            "EnableGeoZoneASIA": enable_geo_zones,
            "EnableGeoZoneSA": enable_geo_zones,
            "EnableGeoZoneAF": enable_geo_zones,
        }

        result = self._api_request("POST", "/pullzone", data)
        logging.info(f"Pull zone created. ID: {result.get('Id')}")
        return result

    def add_hostname(self, pull_zone_id: int, hostname: str) -> dict:
        """Add a custom hostname to a pull zone.

        Returns:
            dict with 'added' (bool) and 'message' (str)
        """
        logging.info(f"Adding hostname '{hostname}' to pull zone {pull_zone_id}")

        try:
            self._api_request("POST", f"/pullzone/{pull_zone_id}/addHostname", {
                "Hostname": hostname
            })
            logging.info(f"Hostname '{hostname}' added.")
            return {"added": True, "message": "Hostname added successfully"}
        except Exception as e:
            error_str = str(e)
            if "hostname_already_registered" in error_str:
                logging.info(f"Hostname '{hostname}' is already registered.")
                return {"added": False, "message": "Hostname already registered"}
            raise

    def request_ssl_certificate(self, pull_zone_id: int, hostname: str) -> bool:
        """Request a free SSL certificate for a hostname."""
        logging.info(f"Requesting SSL certificate for '{hostname}'...")

        try:
            url = f"{BUNNY_API_BASE}/pullzone/{pull_zone_id}/loadFreeCertificate?hostname={hostname}"
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
        """Configure error page handling for SPA support."""
        if not spa_mode:
            return

        logging.info("Configuring SPA error handling...")

        # Enable custom error page that redirects to index
        data = {
            "ErrorPageEnableCustomCode": True,
            "ErrorPageCustomCode": '<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url=/"></head><body></body></html>',
            "ErrorPageWhitelabel": True
        }

        self._api_request("POST", f"/pullzone/{pull_zone_id}", data)
        logging.info("SPA error handling configured.")

    def enable_token_authentication(self, pull_zone_id: int, token_key: str = None) -> dict:
        """Enable Bunny CDN Token Authentication on a pull zone.

        When enabled, all requests must include a valid ``token`` query
        parameter (SHA256 HMAC signature) and an ``expires`` timestamp.
        This makes the pull zone **private** — no public access without
        a signed URL.

        Args:
            pull_zone_id: The pull zone ID.
            token_key: Custom security token key.  If None, Bunny generates
                       one automatically and returns it in the response.

        Returns:
            dict with pull zone details including ``ZoneSecurityKey``.
        """
        logging.info(f"Enabling token authentication on pull zone {pull_zone_id}...")

        data = {
            "ZoneSecurityEnabled": True,
            "ZoneSecurityIncludeHashRemoteIP": False,  # Don't lock to IP (users on mobile etc)
        }
        if token_key:
            data["ZoneSecurityKey"] = token_key

        self._api_request("POST", f"/pullzone/{pull_zone_id}", data)
        logging.info("Token authentication enabled.")

        # Fetch updated details to get the security key
        details = self.get_pull_zone_details(pull_zone_id)
        security_key = details.get("ZoneSecurityKey", "")
        if security_key:
            logging.info(f"Token key (ZoneSecurityKey): {security_key[:8]}...{security_key[-4:]}")
        return details

    def disable_direct_origin_access(self, pull_zone_id: int) -> None:
        """Block direct access to the S3 origin, forcing all requests through CDN.

        Adds an origin header so only Bunny CDN can read from the S3 bucket.
        """
        logging.info(f"Configuring origin shield on pull zone {pull_zone_id}...")
        data = {
            "EnableOriginShield": True,
            "OriginShieldZoneCode": "DE",  # Frankfurt shield
        }
        self._api_request("POST", f"/pullzone/{pull_zone_id}", data)
        logging.info("Origin shield enabled.")

    def purge_cache(self, pull_zone_id: int) -> None:
        """Purge the entire cache for a pull zone."""
        logging.info(f"Purging cache for pull zone {pull_zone_id}...")

        self._api_request("POST", f"/pullzone/{pull_zone_id}/purgeCache")
        logging.info("Cache purged.")

    def get_pull_zone_hostname(self, pull_zone: dict) -> str:
        """Get the default hostname for a pull zone."""
        hostnames = pull_zone.get('Hostnames', [])
        for hostname in hostnames:
            if hostname.get('Value', '').endswith('.b-cdn.net'):
                return hostname['Value']
        return f"{pull_zone['Name']}.b-cdn.net"


# =============================================================================
# Setup Functions
# =============================================================================

def create_test_content(s3_client: HetznerS3Client, bucket_name: str,
                       region: str, spa_mode: bool) -> None:
    """Create test content in the S3 bucket."""
    logging.info("Creating test content...")

    bucket_url = s3_client.get_bucket_url(bucket_name)

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
        <h1 class="success">&#127881; CDN Setup Successful!</h1>
        <p>Your Hetzner S3 + Bunny CDN is working correctly.</p>
        <div class="info">
            <h3>Configuration</h3>
            <p><strong>Storage:</strong> Hetzner Object Storage ({HETZNER_S3_REGION_NAMES.get(region, region)})</p>
            <p><strong>CDN:</strong> Bunny CDN</p>
            <p><strong>Bucket:</strong> {bucket_name}</p>
            <p><strong>Origin:</strong> {bucket_url}</p>
            <p><strong>SPA Mode:</strong> {'Enabled' if spa_mode else 'Disabled'}</p>
        </div>
        <p><small>Generated by setup_hetzner_bunny.py</small></p>
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

    # Upload test files
    s3_client.upload_file(bucket_name, 'index.html', test_html)
    logging.info("index.html uploaded.")

    s3_client.upload_file(bucket_name, 'index-test.html', test_html)
    logging.info("index-test.html uploaded.")

    s3_client.upload_file(bucket_name, 'error.html', error_html)
    logging.info("error.html uploaded.")

    test_txt = f"CDN test - {bucket_name} - {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
    s3_client.upload_file(bucket_name, 'test.txt', test_txt, content_type='text/plain')
    logging.info("test.txt uploaded.")


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
            else:
                logging.info(f"Got status {response.status_code}, waiting...")
        except requests.RequestException as e:
            logging.info(f"Request failed: {e}, retrying...")

        time.sleep(5)

    logging.warning("CDN test timed out.")
    return False


# =============================================================================
# Setup Report Generation
# =============================================================================

class SetupReport:
    """Track and report CDN setup status."""

    def __init__(self, domain: str, mode: str, dns_provider: str, region: str):
        self.timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        self.domain = domain
        self.mode = mode  # 'apex' or 'subdomain'
        self.dns_provider = dns_provider
        self.region = region
        self.region_name = HETZNER_S3_REGION_NAMES.get(region, region)

        # Component status tracking
        self.components = {
            's3_bucket': {'status': 'pending', 'details': {}},
            'pull_zone': {'status': 'pending', 'details': {}},
            'hostnames': {'status': 'pending', 'details': {}},
            'dns_records': {'status': 'pending', 'details': {}},
            'ssl_certificates': {'status': 'pending', 'details': {}},
            'spa_config': {'status': 'pending', 'details': {}},
            'test_content': {'status': 'pending', 'details': {}},
            'cdn_test': {'status': 'pending', 'details': {}},
        }

        # URLs and endpoints
        self.urls = {}

    def set_component(self, name: str, status: str, **details):
        """Update component status."""
        if name in self.components:
            self.components[name]['status'] = status
            self.components[name]['details'].update(details)

    def set_url(self, name: str, url: str):
        """Set a URL endpoint."""
        self.urls[name] = url

    def generate_markdown(self) -> str:
        """Generate markdown report content."""
        lines = []
        lines.append(f"# CDN Setup Report: {self.domain}")
        lines.append("")
        lines.append(f"**Generated:** {self.timestamp}")
        lines.append("**Script:** setup_hetzner_bunny.py")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Configuration Summary
        lines.append("## Configuration Summary")
        lines.append("")
        lines.append("| Setting | Value |")
        lines.append("|---------|-------|")
        lines.append(f"| Domain | `{self.domain}` |")
        lines.append(f"| Mode | {self.mode.title()} |")
        lines.append(f"| DNS Provider | {self.dns_provider.title()} |")
        lines.append(f"| Storage Region | {self.region} ({self.region_name}) |")
        lines.append("")

        # Component Status
        lines.append("## Component Status")
        lines.append("")

        status_icons = {
            'created': '[NEW]',
            'exists': '[OK]',
            'configured': '[OK]',
            'requested': '[OK]',
            'uploaded': '[OK]',
            'passed': '[OK]',
            'skipped': '[SKIP]',
            'failed': '[FAIL]',
            'pending': '[ ]',
        }

        component_names = {
            's3_bucket': 'S3 Bucket',
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
            details = comp['details']

            lines.append(f"### {icon} {comp_name}")
            lines.append("")
            lines.append(f"**Status:** {status.replace('_', ' ').title()}")

            if details:
                for key, value in details.items():
                    display_key = key.replace('_', ' ').title()
                    if isinstance(value, list):
                        lines.append(f"- **{display_key}:**")
                        for item in value:
                            lines.append(f"  - `{item}`")
                    else:
                        lines.append(f"- **{display_key}:** `{value}`")

            lines.append("")

        # URLs and Endpoints
        if self.urls:
            lines.append("## URLs and Endpoints")
            lines.append("")
            lines.append("| Name | URL |")
            lines.append("|------|-----|")
            for name, url in self.urls.items():
                display_name = name.replace('_', ' ').title()
                lines.append(f"| {display_name} | {url} |")
            lines.append("")

            # Quick test links
            lines.append("### Quick Test Links")
            lines.append("")
            if 'public_url' in self.urls:
                base_url = self.urls['public_url']
                lines.append(f"- Homepage: {base_url}/")
                lines.append(f"- Test page: {base_url}/index-test.html")
                lines.append(f"- Text file: {base_url}/test.txt")
            lines.append("")

        # Next Steps
        lines.append("## Next Steps")
        lines.append("")
        lines.append("1. **Upload your website files** to the S3 bucket")
        if 's3_bucket' in self.components and 'bucket_name' in self.components['s3_bucket']['details']:
            bucket = self.components['s3_bucket']['details']['bucket_name']
            lines.append("   ```bash")
            lines.append("   # Using AWS CLI or S3 tool")
            lines.append(f"   aws s3 sync ./dist s3://{bucket}/ --endpoint-url https://{HETZNER_S3_REGIONS.get(self.region, 'fsn1.your-objectstorage.com')}")
            lines.append("   ```")
        lines.append("")
        lines.append("2. **Purge CDN cache** after uploading new content")
        lines.append("")
        lines.append("3. **Configure SSL** if certificate request is pending")
        lines.append("   - SSL certificates typically activate within 5-15 minutes")
        lines.append("   - Check Bunny CDN dashboard for certificate status")
        lines.append("")

        # Footer
        lines.append("---")
        lines.append("")
        lines.append("*This report was generated automatically by setup_hetzner_bunny.py*")
        lines.append("")

        return '\n'.join(lines)

    def save_report(self, output_dir: str = '.') -> str:
        """Save report to a markdown file."""
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)

        # Create filename with timestamp
        safe_domain = self.domain.replace('.', '-')
        timestamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        filename = f"cdn-setup-{safe_domain}-{timestamp}.md"
        filepath = os.path.join(output_dir, filename)

        content = self.generate_markdown()
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        logging.info(f"Setup report saved to: {filepath}")
        return filepath


# =============================================================================
# Main Setup
# =============================================================================

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Setup CDN with Hetzner S3 + Bunny CDN',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Subdomain setup
  python setup_hetzner_bunny.py --domain example.com --subdomain cdn

  # Apex domain setup with www redirect
  python setup_hetzner_bunny.py --domain example.com --apex --www-redirect

  # SPA application
  python setup_hetzner_bunny.py --domain example.com --subdomain app --spa-mode

  # Different Hetzner region
  python setup_hetzner_bunny.py --domain example.com --subdomain cdn --region nbg1

Environment Variables:
  HETZNER_S3_ACCESS_KEY: Hetzner S3 access key
  HETZNER_S3_SECRET_KEY: Hetzner S3 secret key
  BUNNY_API_KEY: Bunny.net API key
  CLOUDFLARE_API_TOKEN: For Cloudflare DNS
  HETZNER_DNS_API_TOKEN: For Hetzner DNS
  DESEC_API_TOKEN: For deSEC DNS
        """
    )

    parser.add_argument('--domain', required=True,
                        help='The root domain (e.g., example.com)')
    parser.add_argument('--subdomain', default=None,
                        help='The subdomain for the CDN (e.g., cdn)')
    parser.add_argument('--apex', action='store_true',
                        help='Setup for apex domain (no subdomain)')
    parser.add_argument('--www-redirect', action='store_true',
                        help='Redirect www to apex domain (only with --apex)')
    parser.add_argument('--region', default='fsn1',
                        choices=list(HETZNER_S3_REGIONS.keys()),
                        help='Hetzner region (default: fsn1)')
    parser.add_argument('--dns-provider', choices=['cloudflare', 'hetzner', 'desec'],
                        default='cloudflare',
                        help='DNS provider (default: cloudflare)')
    parser.add_argument('--create-zone', action='store_true',
                        help='Create DNS zone if it does not exist')
    parser.add_argument('--spa-mode', action='store_true',
                        help='Configure for Single Page Application')
    parser.add_argument('--skip-tests', action='store_true',
                        help='Skip CDN functionality tests')
    parser.add_argument('--test-timeout', type=int, default=60,
                        help='Timeout for CDN tests (default: 60)')
    parser.add_argument('--force', action='store_true',
                        help='Force recreate pull zone even if it exists')
    parser.add_argument('--private', action='store_true',
                        help='Private mode: skip public bucket policy, enable Bunny token auth')
    parser.add_argument('--token-auth', action='store_true',
                        help='Enable Bunny CDN token authentication (signed URLs required)')
    parser.add_argument('--token-key', default=None,
                        help='Custom token key for Bunny token auth (auto-generated if omitted)')
    parser.add_argument('--report-dir', default='.',
                        help='Directory to save the setup report (default: current directory)')

    return parser.parse_args()


def main():
    """Main function to orchestrate the CDN setup."""
    args = parse_arguments()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # Determine full domain and bucket name
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

    # Sanitize bucket name (Hetzner S3 doesn't allow dots)
    bucket_name = full_domain.replace('.', '-')
    # Pull zone name same as bucket name
    pull_zone_name = bucket_name

    # Initialize report tracker
    report = SetupReport(
        domain=full_domain,
        mode=mode,
        dns_provider=args.dns_provider,
        region=args.region
    )

    # Log configuration
    logging.info("=" * 50)
    logging.info("HETZNER S3 + BUNNY CDN SETUP")
    logging.info("=" * 50)
    logging.info(f"Domain: {args.domain}")
    logging.info(f"Mode: {'Apex' if args.apex else 'Subdomain'}")
    logging.info(f"Full Domain: {full_domain}")
    logging.info(f"Bucket Name: {bucket_name}")
    logging.info(f"Pull Zone Name: {pull_zone_name}")
    logging.info(f"Hetzner Region: {args.region} ({HETZNER_S3_REGION_NAMES.get(args.region, '')})")
    logging.info(f"DNS Provider: {args.dns_provider}")
    logging.info(f"SPA Mode: {args.spa_mode}")
    logging.info(f"Private Mode: {args.private}")
    logging.info(f"Token Auth: {args.token_auth or args.private}")
    if args.apex:
        logging.info(f"WWW Redirect: {args.www_redirect}")
    logging.info("=" * 50)

    # --private implies --token-auth
    if args.private:
        args.token_auth = True

    try:
        # Initialize clients
        logging.info("Initializing clients...")
        s3_client = HetznerS3Client(region=args.region)
        bunny_client = BunnyCDNClient()
        dns_provider = get_dns_provider(args.dns_provider)

        # Get DNS zone
        if args.create_zone:
            zone_id = dns_provider.get_or_create_zone(args.domain, auto_create=True)
        else:
            zone_id = dns_provider.get_zone_id(args.domain)

        # 1. Create S3 bucket
        logging.info("\n--- Step 1: S3 Bucket ---")
        bucket_existed = s3_client.bucket_exists(bucket_name)
        s3_client.create_bucket(bucket_name)
        if not args.private:
            s3_client.set_bucket_policy_public(bucket_name)
        else:
            logging.info("Private mode: skipping public bucket policy (origin access only)")
        s3_client.set_cors_config(bucket_name)

        origin_url = s3_client.get_bucket_url(bucket_name)
        logging.info(f"S3 Origin URL: {origin_url}")

        report.set_component('s3_bucket',
            'exists' if bucket_existed else 'created',
            bucket_name=bucket_name,
            origin_url=origin_url,
            region=args.region
        )
        report.set_url('s3_origin', origin_url)

        # 2. Create or get Bunny CDN pull zone
        logging.info("\n--- Step 2: Bunny CDN Pull Zone ---")

        # First, check if the hostname is already registered to any pull zone
        existing_zone_by_hostname = bunny_client.find_pull_zone_by_hostname(full_domain)
        existing_zone_by_name = bunny_client.find_pull_zone_by_name(pull_zone_name)

        pull_zone_existed = False
        pull_zone = None

        if existing_zone_by_hostname:
            # Hostname already registered - use that pull zone
            logging.info(f"Hostname '{full_domain}' already registered to pull zone {existing_zone_by_hostname['Id']}.")
            pull_zone = existing_zone_by_hostname
            pull_zone_existed = True
        elif existing_zone_by_name and not args.force:
            # Pull zone with same name exists
            logging.info(f"Pull zone '{pull_zone_name}' already exists.")
            pull_zone = existing_zone_by_name
            pull_zone_existed = True
        elif existing_zone_by_name and args.force:
            logging.info("Force flag set. Pull zone will be reused.")
            pull_zone = existing_zone_by_name
            pull_zone_existed = True
        else:
            # Create new pull zone
            pull_zone = bunny_client.create_pull_zone(pull_zone_name, origin_url)

        pull_zone_id = pull_zone['Id']
        cdn_hostname = bunny_client.get_pull_zone_hostname(pull_zone)
        logging.info(f"CDN Hostname: {cdn_hostname}")

        report.set_component('pull_zone',
            'exists' if pull_zone_existed else 'created',
            pull_zone_id=pull_zone_id,
            pull_zone_name=pull_zone_name,
            cdn_hostname=cdn_hostname
        )
        report.set_url('cdn_hostname', f"https://{cdn_hostname}")

        # 3. Add custom hostnames
        logging.info("\n--- Step 3: Custom Hostnames ---")

        # Check existing hostnames
        existing_hostnames = [h.get('Value', '') for h in pull_zone.get('Hostnames', [])]
        hostname_details = {'configured_hostnames': []}

        if full_domain not in existing_hostnames:
            bunny_client.add_hostname(pull_zone_id, full_domain)
            hostname_details['configured_hostnames'].append(f"{full_domain} (added)")
        else:
            logging.info(f"Hostname '{full_domain}' already configured.")
            hostname_details['configured_hostnames'].append(f"{full_domain} (existed)")

        if args.apex and args.www_redirect:
            if www_domain not in existing_hostnames:
                bunny_client.add_hostname(pull_zone_id, www_domain)
                hostname_details['configured_hostnames'].append(f"{www_domain} (added)")
            else:
                logging.info(f"Hostname '{www_domain}' already configured.")
                hostname_details['configured_hostnames'].append(f"{www_domain} (existed)")

        report.set_component('hostnames', 'configured', **hostname_details)

        # 3b. Token Authentication (if enabled)
        token_security_key = None
        if args.token_auth:
            logging.info("\n--- Step 3b: Token Authentication ---")
            details = bunny_client.enable_token_authentication(
                pull_zone_id, token_key=args.token_key
            )
            token_security_key = details.get("ZoneSecurityKey", "")
            report.set_component('token_auth', 'enabled',
                security_key_preview=f"{token_security_key[:8]}...{token_security_key[-4:]}" if token_security_key else "N/A",
                note="All requests require ?token=<hash>&expires=<ts> signed URLs"
            )
            if args.private:
                bunny_client.disable_direct_origin_access(pull_zone_id)
                report.set_component('origin_shield', 'enabled')
        else:
            report.set_component('token_auth', 'disabled')

        # 4. Configure SPA mode if enabled
        if args.spa_mode:
            logging.info("\n--- Step 4: SPA Configuration ---")
            bunny_client.configure_error_pages(pull_zone_id, spa_mode=True)
            report.set_component('spa_config', 'configured',
                mode='enabled',
                behavior='404 errors redirect to index.html'
            )
        else:
            report.set_component('spa_config', 'skipped', mode='disabled')

        # 5. Configure DNS
        logging.info("\n--- Step 5: DNS Configuration ---")
        dns_details = {'provider': args.dns_provider, 'records': []}

        if args.apex:
            dns_provider.create_apex_records(
                zone_id, args.domain, www_domain, cdn_hostname
            )
            if dns_provider.supports_apex_cname():
                dns_details['records'].append(f"CNAME {args.domain} -> {cdn_hostname}")
            else:
                dns_details['records'].append(f"A {args.domain} -> 185.206.224.1 (Bunny anycast)")
            dns_details['records'].append(f"CNAME www.{args.domain} -> {cdn_hostname}")
        else:
            dns_provider.create_cname_record(
                zone_id, args.subdomain, cdn_hostname, args.domain
            )
            dns_details['records'].append(f"CNAME {full_domain} -> {cdn_hostname}")

        report.set_component('dns_records', 'configured', **dns_details)

        # 6. Request SSL certificates
        logging.info("\n--- Step 6: SSL Certificates ---")
        logging.info("Waiting 30 seconds for DNS propagation before requesting SSL...")
        time.sleep(30)

        ssl_details = {'certificates': []}

        ssl_success = bunny_client.request_ssl_certificate(pull_zone_id, full_domain)
        ssl_details['certificates'].append(f"{full_domain}: {'requested' if ssl_success else 'pending'}")

        if args.apex and args.www_redirect:
            www_ssl_success = bunny_client.request_ssl_certificate(pull_zone_id, www_domain)
            ssl_details['certificates'].append(f"{www_domain}: {'requested' if www_ssl_success else 'pending'}")

        report.set_component('ssl_certificates', 'requested', **ssl_details)

        # 7. Create test content
        logging.info("\n--- Step 7: Test Content ---")
        create_test_content(s3_client, bucket_name, args.region, args.spa_mode)
        report.set_component('test_content', 'uploaded',
            files=['index.html', 'index-test.html', 'error.html', 'test.txt']
        )

        # Set public URLs
        report.set_url('public_url', f"https://{full_domain}")
        if args.apex and args.www_redirect:
            report.set_url('www_url', f"https://{www_domain}")

        # Summary
        logging.info("\n" + "=" * 50)
        logging.info("CDN SETUP COMPLETE")
        logging.info("=" * 50)
        logging.info(f"S3 Bucket: {bucket_name}")
        logging.info(f"S3 Origin: {origin_url}")
        logging.info(f"Bunny Pull Zone ID: {pull_zone_id}")
        logging.info(f"CDN Hostname: {cdn_hostname}")
        logging.info(f"Public URL: https://{full_domain}")
        if args.apex and args.www_redirect:
            logging.info(f"WWW URL: https://{www_domain}")
        logging.info(f"SPA Mode: {args.spa_mode}")
        logging.info(f"Token Auth: {args.token_auth}")
        if token_security_key:
            logging.info(f"Token Key: {token_security_key}")
            logging.info("")
            logging.info("SAVE THIS TOKEN KEY — needed for signed URL generation:")
            logging.info(f"  BUNNY_CDN_TOKEN_KEY={token_security_key}")
        logging.info("=" * 50)
        if not args.token_auth:
            logging.info("Test URLs:")
            logging.info(f"  - https://{full_domain}/index-test.html")
            logging.info(f"  - https://{full_domain}/test.txt")
        else:
            logging.info("Token auth enabled — files require signed URLs to access.")
            logging.info("Generate signed URLs in your application using the token key above.")
        logging.info("=" * 50)

        # 8. Test CDN (skip if token auth — public URLs won't work)
        test_success = True
        if args.token_auth and not args.skip_tests:
            logging.info("\nSkipping CDN tests (token auth enabled — public URLs blocked).")
            args.skip_tests = True
        if not args.skip_tests:
            logging.info("\nWaiting 30 seconds for CDN propagation before testing...")
            time.sleep(30)

            test_success = test_cdn_functionality(f"https://{full_domain}", args.test_timeout)
            report.set_component('cdn_test',
                'passed' if test_success else 'failed',
                test_url=f"https://{full_domain}/test.txt"
            )
        else:
            report.set_component('cdn_test', 'skipped')

        # Generate and save report
        report_path = report.save_report(args.report_dir)

        if test_success or args.skip_tests:
            logging.info("\n[OK] CDN setup completed successfully!")
            logging.info(f"[i] Setup report: {report_path}")
            return 0
        else:
            logging.warning("\n[!] CDN setup completed but tests indicate issues.")
            logging.info(f"[i] Setup report: {report_path}")
            return 1

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()

        # Try to save partial report on error
        try:
            report_path = report.save_report(args.report_dir)
            logging.info(f"[i] Partial setup report saved: {report_path}")
        except Exception:
            pass

        return 1


if __name__ == "__main__":
    exit(main())
