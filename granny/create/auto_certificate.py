#!/usr/bin/env python3
"""
SSL Certificate Automation Tool

Automates SSL certificate creation, DNS validation, and configuration updates.

Certificate Providers:
- AWS ACM: Traditional AWS Certificate Manager with DNS validation
- Bunny CDN: Free SSL certificates for Bunny CDN pull zones

DNS Providers (for ACM validation):
- Cloudflare, Hetzner, deSEC

Supports both specific domain and wildcard certificate creation (ACM only).
"""

import argparse
import os
import sys
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple

import boto3
import requests
from botocore.exceptions import ClientError, WaiterError

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass


# =============================================================================
# DNS Provider Abstraction
# =============================================================================

class DNSProvider(ABC):
    """Abstract base class for DNS providers."""

    @abstractmethod
    def get_zone_id(self, zone_name: str) -> str:
        """Get zone ID by domain name."""
        pass

    @abstractmethod
    def create_validation_record(self, zone_id: str, zone_name: str,
                                  cname_name: str, cname_value: str) -> dict:
        """Create DNS CNAME record for certificate validation.

        Args:
            zone_id: The zone ID
            zone_name: The zone/domain name
            cname_name: Full CNAME record name
            cname_value: CNAME record value

        Returns:
            Dict with record details for cleanup
        """
        pass

    @abstractmethod
    def cleanup_validation_record(self, zone_id: str, record_info: dict) -> None:
        """Remove DNS validation record after certificate issuance."""
        pass

    def supports_zone_creation(self) -> bool:
        """Returns True if provider supports creating new zones via API."""
        return False

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support zone creation via API. "
            f"Please create the zone manually in the provider's dashboard."
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
                    f"does not support automatic zone creation. "
                    f"Please create the zone manually first."
                ) from e
            print(f"Zone '{zone_name}' not found. Creating new zone...")
            return self.create_zone(zone_name)


class CloudflareDNSProvider(DNSProvider):
    """Cloudflare DNS API adapter."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def get_zone_id(self, zone_name: str) -> str:
        """Get the Cloudflare zone ID for the domain."""
        url = "https://api.cloudflare.com/client/v4/zones"
        params = {"name": zone_name}

        response = requests.get(url, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()

        result = response.json()
        zones = result.get("result", [])

        if not zones:
            raise Exception(
                f"No Cloudflare zone found for domain '{zone_name}'. "
                f"Please verify the domain is added to your Cloudflare account."
            )

        zone_id = zones[0].get("id")
        zone_name_found = zones[0].get("name")
        print(f"Found Cloudflare zone: {zone_name_found} (ID: {zone_id})")
        return zone_id

    def create_validation_record(self, zone_id: str, zone_name: str,
                                  cname_name: str, cname_value: str) -> dict:
        """Create or update DNS validation record in Cloudflare."""
        print("Adding validation record to Cloudflare...")

        # Extract record name (remove base domain)
        record_name = cname_name.replace(f".{zone_name}", "").rstrip(".")

        # Check if record exists
        list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
        params = {"type": "CNAME", "name": cname_name}

        response = requests.get(list_url, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()

        existing_records = response.json().get("result", [])
        record_id = existing_records[0].get("id") if existing_records else None

        # Prepare record data
        record_data = {
            "type": "CNAME",
            "name": record_name,
            "content": cname_value,
            "ttl": 120,
            "proxied": False,
        }

        if record_id:
            print("Updating existing DNS record...")
            update_url = f"{list_url}/{record_id}"
            response = requests.put(
                update_url, headers=self.headers, json=record_data, timeout=30
            )
        else:
            print("Creating new DNS record...")
            response = requests.post(
                list_url, headers=self.headers, json=record_data, timeout=30
            )

        response.raise_for_status()
        result = response.json()

        if result.get("success"):
            print("DNS record added/updated successfully")
            record_id = result.get("result", {}).get("id", record_id)
            return {"id": record_id, "name": cname_name, "zone_id": zone_id}
        else:
            raise RuntimeError(f"Failed to create/update DNS record: {result}")

    def cleanup_validation_record(self, zone_id: str, record_info: dict) -> None:
        """Remove DNS validation record from Cloudflare."""
        record_id = record_info.get("id")
        if not record_id:
            return

        print(f"Cleaning up validation record: {record_info.get('name')}")
        url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"

        try:
            response = requests.delete(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            print("Validation record cleaned up successfully")
        except Exception as e:
            print(f"Warning: Could not clean up validation record: {e}")


class HetznerDNSProvider(DNSProvider):
    """Hetzner DNS API adapter."""

    API_BASE = "https://dns.hetzner.com/api/v1"

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.headers = {
            "Auth-API-Token": api_token,
            "Content-Type": "application/json",
        }

    def get_zone_id(self, zone_name: str) -> str:
        """Get the Hetzner zone ID for the domain."""
        print(f"Looking up Hetzner zone for '{zone_name}'...")
        url = f"{self.API_BASE}/zones"

        response = requests.get(url, headers=self.headers, timeout=30)
        response.raise_for_status()

        zones = response.json().get("zones", [])
        for zone in zones:
            if zone.get("name") == zone_name:
                zone_id = zone.get("id")
                print(f"Found Hetzner zone: {zone_name} (ID: {zone_id})")
                return zone_id

        raise Exception(f"Zone not found in Hetzner DNS: {zone_name}")

    def create_validation_record(self, zone_id: str, zone_name: str,
                                  cname_name: str, cname_value: str) -> dict:
        """Create DNS validation record in Hetzner."""
        print("Adding validation record to Hetzner DNS...")

        # Extract record name (remove base domain and trailing dot)
        clean_name = cname_name.rstrip(".")
        if clean_name.endswith(f".{zone_name}"):
            record_name = clean_name[:-len(f".{zone_name}")]
        else:
            record_name = clean_name

        # Remove trailing dot from value
        cname_value = cname_value.rstrip(".")

        # Check for existing record
        list_url = f"{self.API_BASE}/records"
        params = {"zone_id": zone_id}
        response = requests.get(list_url, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()

        existing_record = None
        for record in response.json().get("records", []):
            if record.get("name") == record_name and record.get("type") == "CNAME":
                existing_record = record
                break

        record_data = {
            "zone_id": zone_id,
            "type": "CNAME",
            "name": record_name,
            "value": cname_value,
            "ttl": 300,
        }

        if existing_record:
            print("Updating existing DNS record...")
            url = f"{self.API_BASE}/records/{existing_record['id']}"
            response = requests.put(url, headers=self.headers, json=record_data, timeout=30)
        else:
            print("Creating new DNS record...")
            url = f"{self.API_BASE}/records"
            response = requests.post(url, headers=self.headers, json=record_data, timeout=30)

        response.raise_for_status()
        result = response.json().get("record", {})
        print("DNS record added/updated successfully")

        return {
            "id": result.get("id"),
            "name": record_name,
            "zone_id": zone_id
        }

    def cleanup_validation_record(self, zone_id: str, record_info: dict) -> None:
        """Remove DNS validation record from Hetzner."""
        record_id = record_info.get("id")
        if not record_id:
            return

        print(f"Cleaning up validation record: {record_info.get('name')}")
        url = f"{self.API_BASE}/records/{record_id}"

        try:
            response = requests.delete(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            print("Validation record cleaned up successfully")
        except Exception as e:
            print(f"Warning: Could not clean up validation record: {e}")


class DeSECDNSProvider(DNSProvider):
    """deSEC DNS API adapter with zone creation support."""

    API_BASE = "https://desec.io/api/v1"
    MIN_TTL = 3600  # deSEC requires minimum TTL of 3600 seconds

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json"
        })

    def _api_request(self, method: str, endpoint: str, data: dict = None,
                      expected_errors: list = None) -> dict:
        """Make an API request to deSEC.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            data: Optional request body
            expected_errors: List of status codes that are expected (won't log as errors)
        """
        url = f"{self.API_BASE}{endpoint}"
        expected_errors = expected_errors or []

        if method == "GET":
            response = self.session.get(url, timeout=30)
        elif method == "POST":
            response = self.session.post(url, json=data, timeout=30)
        elif method == "PUT":
            response = self.session.put(url, json=data, timeout=30)
        elif method == "DELETE":
            response = self.session.delete(url, timeout=30)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Handle rate limiting
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 5))
            print(f"Rate limited by deSEC API. Waiting {retry_after}s...")
            time.sleep(retry_after)
            return self._api_request(method, endpoint, data, expected_errors)

        if response.status_code >= 400:
            # Only log if not an expected error
            if response.status_code not in expected_errors:
                print(f"deSEC API error: {response.status_code} - {response.text}")
            response.raise_for_status()

        if response.status_code == 204:
            return {}

        return response.json() if response.text else {}

    def get_zone_id(self, zone_name: str) -> str:
        """Get zone info - deSEC uses domain name as identifier."""
        print(f"Looking up deSEC zone for '{zone_name}'...")
        try:
            result = self._api_request("GET", f"/domains/{zone_name}/")
            if result and result.get("name") == zone_name:
                print(f"Found deSEC zone: {zone_name}")
                return zone_name
            raise Exception(f"Zone not found: {zone_name}")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                raise Exception(f"Zone not found in deSEC: {zone_name}")
            raise

    def supports_zone_creation(self) -> bool:
        """deSEC supports creating zones via API."""
        return True

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone in deSEC."""
        print(f"Creating new deSEC zone for '{zone_name}'...")
        try:
            result = self._api_request("POST", "/domains/", {"name": zone_name})
            if result and result.get("name") == zone_name:
                print(f"Successfully created deSEC zone: {zone_name}")
                if "keys" in result:
                    print("Zone created with DNSSEC enabled (deSEC default)")
                print("=" * 60)
                print("IMPORTANT: Configure these nameservers at your domain registrar:")
                print("  ns1.desec.io")
                print("  ns2.desec.org")
                print("=" * 60)
                return zone_name
            raise Exception(f"Unexpected response when creating zone: {result}")
        except requests.HTTPError as e:
            if e.response.status_code == 400:
                error_detail = e.response.json() if e.response.text else {}
                if "name" in error_detail:
                    name_errors = error_detail["name"]
                    if any("already exists" in str(err).lower() for err in name_errors):
                        print(f"Zone '{zone_name}' already exists in deSEC")
                        return zone_name
                    raise Exception(f"Failed to create zone: {name_errors}")
            raise Exception(f"Failed to create deSEC zone: {e}")

    def create_validation_record(self, zone_id: str, zone_name: str,
                                  cname_name: str, cname_value: str) -> dict:
        """Create DNS validation record in deSEC."""
        print("Adding validation record to deSEC...")

        # Extract subname (remove zone name suffix)
        full_name = cname_name.rstrip(".")
        if full_name.endswith(f".{zone_name}"):
            subname = full_name[:-len(f".{zone_name}")]
        else:
            subname = full_name

        # Ensure value ends with dot
        if not cname_value.endswith("."):
            cname_value = f"{cname_value}."

        # Check for existing record (404 is expected if record doesn't exist)
        try:
            endpoint = f"/domains/{zone_name}/rrsets/{subname}/CNAME/"
            existing = self._api_request("GET", endpoint, expected_errors=[404])
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                existing = None
            else:
                raise

        data = {
            "subname": subname,
            "type": "CNAME",
            "records": [cname_value],
            "ttl": self.MIN_TTL
        }

        if existing:
            print("Updating existing DNS record...")
            endpoint = f"/domains/{zone_name}/rrsets/{subname}/CNAME/"
            self._api_request("PUT", endpoint, data)
        else:
            print("Creating new DNS record...")
            endpoint = f"/domains/{zone_name}/rrsets/"
            self._api_request("POST", endpoint, data)

        print("DNS record added/updated successfully")
        return {
            "subname": subname,
            "type": "CNAME",
            "zone_name": zone_name
        }

    def cleanup_validation_record(self, zone_id: str, record_info: dict) -> None:
        """Remove DNS validation record from deSEC."""
        subname = record_info.get("subname")
        zone_name = record_info.get("zone_name", zone_id)

        if not subname:
            return

        print(f"Cleaning up validation record: {subname}")
        try:
            endpoint = f"/domains/{zone_name}/rrsets/{subname}/CNAME/"
            self._api_request("DELETE", endpoint)
            print("Validation record cleaned up successfully")
        except Exception as e:
            print(f"Warning: Could not clean up validation record: {e}")


class BunnyDNSProvider(DNSProvider):
    """Bunny DNS API adapter with zone creation support.

    Bunny DNS is a European DNS provider with:
    - CNAME flattening at apex (supports root domain CNAMEs)
    - Global anycast network
    - Built-in DDoS protection
    - Zone creation via API

    Environment Variable: BUNNY_API_KEY
    """
    API_BASE = "https://api.bunny.net"
    MIN_TTL = 60  # Bunny DNS allows lower TTL than deSEC

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": api_key,
            "Content-Type": "application/json"
        })
        self._zone_cache = {}  # Cache zone_name -> zone_id mapping

    def _api_request(self, method: str, endpoint: str, data: dict = None,
                      expected_errors: list = None) -> dict:
        """Make an API request to Bunny DNS."""
        url = f"{self.API_BASE}{endpoint}"
        expected_errors = expected_errors or []

        try:
            if method == "GET":
                response = self.session.get(url, timeout=30)
            elif method == "POST":
                response = self.session.post(url, json=data, timeout=30)
            elif method == "PUT":
                response = self.session.put(url, json=data, timeout=30)
            elif method == "DELETE":
                response = self.session.delete(url, timeout=30)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if response.status_code >= 400:
                if response.status_code not in expected_errors:
                    print(f"Bunny DNS API error: {response.status_code} - {response.text}")
                response.raise_for_status()

            if response.status_code == 204:
                return {}

            return response.json() if response.text else {}

        except requests.RequestException as e:
            raise Exception(f"Bunny DNS API request failed: {e}")

    def get_zone_id(self, zone_name: str) -> str:
        """Get the Bunny DNS zone ID for the domain."""
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]

        print(f"Looking up Bunny DNS zone for '{zone_name}'...")

        result = self._api_request("GET", "/dnszone")

        if isinstance(result, dict) and "Items" in result:
            zones = result["Items"]
        elif isinstance(result, list):
            zones = result
        else:
            zones = []

        for zone in zones:
            if zone.get("Domain") == zone_name:
                zone_id = str(zone.get("Id"))
                print(f"Found Bunny DNS zone: {zone_name} (ID: {zone_id})")
                self._zone_cache[zone_name] = zone_id
                return zone_id

        raise Exception(f"Zone not found in Bunny DNS: {zone_name}")

    def supports_zone_creation(self) -> bool:
        """Bunny DNS supports creating zones via API."""
        return True

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone in Bunny DNS."""
        print(f"Creating new Bunny DNS zone for '{zone_name}'...")

        try:
            result = self._api_request("POST", "/dnszone", {"Domain": zone_name})

            if result and result.get("Id"):
                zone_id = str(result["Id"])
                print(f"Successfully created Bunny DNS zone: {zone_name} (ID: {zone_id})")
                self._zone_cache[zone_name] = zone_id

                nameservers = result.get("Nameservers", [])
                if nameservers:
                    print("=" * 60)
                    print("IMPORTANT: Configure these nameservers at your domain registrar:")
                    for ns in nameservers:
                        print(f"  {ns}")
                    print("=" * 60)

                return zone_id

            raise Exception(f"Unexpected response when creating zone: {result}")

        except requests.HTTPError as e:
            if e.response.status_code == 400:
                try:
                    return self.get_zone_id(zone_name)
                except Exception:
                    pass
            raise Exception(f"Failed to create Bunny DNS zone: {e}")

    def create_validation_record(self, zone_id: str, zone_name: str,
                                  cname_name: str, cname_value: str) -> dict:
        """Create DNS validation record in Bunny DNS."""
        print("Adding validation record to Bunny DNS...")

        full_name = cname_name.rstrip(".")
        if full_name.endswith(f".{zone_name}"):
            record_name = full_name[:-len(f".{zone_name}")]
        else:
            record_name = full_name

        cname_value = cname_value.rstrip(".")

        try:
            records_result = self._api_request("GET", f"/dnszone/{zone_id}")
            existing_records = records_result.get("Records", [])

            existing_record = None
            for record in existing_records:
                if record.get("Name") == record_name and record.get("Type") == 5:
                    existing_record = record
                    break
        except Exception:
            existing_records = []
            existing_record = None

        record_data = {
            "Type": 5,
            "Name": record_name,
            "Value": cname_value,
            "Ttl": self.MIN_TTL
        }

        if existing_record:
            print("Updating existing DNS record...")
            record_id = existing_record.get("Id")
            self._api_request("POST", f"/dnszone/{zone_id}/records/{record_id}", record_data)
        else:
            print("Creating new DNS record...")
            result = self._api_request("PUT", f"/dnszone/{zone_id}/records", record_data)
            record_id = result.get("Id") if result else None

        print("DNS record added/updated successfully")
        return {
            "id": record_id,
            "name": record_name,
            "zone_id": zone_id,
            "zone_name": zone_name
        }

    def cleanup_validation_record(self, zone_id: str, record_info: dict) -> None:
        """Remove DNS validation record from Bunny DNS."""
        record_id = record_info.get("id")
        record_name = record_info.get("name")

        if not record_id:
            if record_name:
                try:
                    records_result = self._api_request("GET", f"/dnszone/{zone_id}")
                    for record in records_result.get("Records", []):
                        if record.get("Name") == record_name and record.get("Type") == 5:
                            record_id = record.get("Id")
                            break
                except Exception:
                    pass

        if not record_id:
            print(f"Warning: Could not find record to clean up: {record_name}")
            return

        print(f"Cleaning up validation record: {record_name}")
        try:
            self._api_request("DELETE", f"/dnszone/{zone_id}/records/{record_id}")
            print("Validation record cleaned up successfully")
        except Exception as e:
            print(f"Warning: Could not clean up validation record: {e}")


class ClouDNSDNSProvider(DNSProvider):
    """ClouDNS API adapter with zone creation support.

    ClouDNS is a European DNS provider with:
    - Affordable DDoS protection plans
    - Global anycast network
    - Zone creation via API
    - Supports both master and slave zones

    Environment Variables:
        CLOUDNS_AUTH_ID: ClouDNS API auth-id
        CLOUDNS_AUTH_PASSWORD: ClouDNS API auth-password

    Alternative (sub-user):
        CLOUDNS_SUB_AUTH_ID: ClouDNS sub-user auth-id
        CLOUDNS_SUB_AUTH_USER: ClouDNS sub-user username
    """
    API_BASE = "https://api.cloudns.net"
    VALID_TTLS = [60, 300, 900, 1800, 3600, 21600, 43200, 86400, 172800, 259200, 604800, 1209600, 2592000]
    MIN_TTL = 60

    def __init__(self, auth_id: str = None, auth_password: str = None,
                 sub_auth_id: str = None, sub_auth_user: str = None):
        """Initialize ClouDNS client.

        Args:
            auth_id: Main API user ID
            auth_password: API password
            sub_auth_id: Sub-user ID (alternative to auth_id)
            sub_auth_user: Sub-user username (alternative to auth_id)
        """
        self.auth_password = auth_password or os.environ.get("CLOUDNS_AUTH_PASSWORD")

        # Determine auth method
        self.auth_id = auth_id or os.environ.get("CLOUDNS_AUTH_ID")
        self.sub_auth_id = sub_auth_id or os.environ.get("CLOUDNS_SUB_AUTH_ID")
        self.sub_auth_user = sub_auth_user or os.environ.get("CLOUDNS_SUB_AUTH_USER")

        if not self.auth_password:
            raise ValueError("CLOUDNS_AUTH_PASSWORD environment variable not set.")

        if not any([self.auth_id, self.sub_auth_id, self.sub_auth_user]):
            raise ValueError(
                "ClouDNS authentication required. Set one of: "
                "CLOUDNS_AUTH_ID, CLOUDNS_SUB_AUTH_ID, or CLOUDNS_SUB_AUTH_USER"
            )

        self.session = requests.Session()

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
        request_params = self._get_auth_params()
        if params:
            request_params.update(params)

        try:
            response = self.session.post(url, data=request_params, timeout=30)
            response.raise_for_status()

            result = response.json()

            # Check for API-level errors
            if isinstance(result, dict) and result.get("status") == "Failed":
                error_msg = result.get("statusDescription", "Unknown error")
                raise Exception(f"ClouDNS API error: {error_msg}")

            return result

        except requests.RequestException as e:
            raise Exception(f"ClouDNS API request failed: {e}")

    def get_zone_id(self, zone_name: str) -> str:
        """Get zone info - ClouDNS uses domain name as identifier.

        Note: ClouDNS doesn't use numeric zone IDs, just domain names.
        """
        print(f"Looking up ClouDNS zone for '{zone_name}'...")

        # List zones and find our domain
        result = self._api_request("/dns/list-zones.json", {
            "page": 1,
            "rows-per-page": 100,
            "search": zone_name
        })

        # Result is a dict with zone names as keys
        if isinstance(result, dict):
            for domain, zone_info in result.items():
                if domain == zone_name:
                    print(f"Found ClouDNS zone: {zone_name}")
                    return zone_name

        raise Exception(f"Zone not found in ClouDNS: {zone_name}")

    def supports_zone_creation(self) -> bool:
        """ClouDNS supports creating zones via API."""
        return True

    def create_zone(self, zone_name: str) -> str:
        """Create a new DNS zone in ClouDNS."""
        print(f"Creating new ClouDNS zone for '{zone_name}'...")

        try:
            result = self._api_request("/dns/register.json", {
                "domain-name": zone_name,
                "zone-type": "master"
            })

            if result.get("status") == "Success":
                print(f"Successfully created ClouDNS zone: {zone_name}")
                print("=" * 60)
                print("IMPORTANT: Configure these nameservers at your domain registrar:")
                print("  ns1.cloudns.net")
                print("  ns2.cloudns.net")
                print("  ns3.cloudns.net")
                print("  ns4.cloudns.net")
                print("=" * 60)
                return zone_name

            raise Exception(f"Unexpected response when creating zone: {result}")

        except Exception as e:
            # Zone might already exist
            if "already exists" in str(e).lower():
                print(f"Zone '{zone_name}' already exists in ClouDNS")
                return zone_name
            raise Exception(f"Failed to create ClouDNS zone: {e}")

    def _get_nearest_ttl(self, ttl: int) -> int:
        """Get the nearest valid TTL value."""
        for valid_ttl in self.VALID_TTLS:
            if ttl <= valid_ttl:
                return valid_ttl
        return self.VALID_TTLS[-1]

    def _find_record(self, zone_name: str, record_name: str, record_type: str = "CNAME") -> dict:
        """Find an existing DNS record."""
        result = self._api_request("/dns/records.json", {
            "domain-name": zone_name,
            "host": record_name,
            "type": record_type
        })

        if isinstance(result, dict):
            for record_id, record_info in result.items():
                if record_info.get("host") == record_name and record_info.get("type") == record_type:
                    return {"id": record_id, **record_info}

        return None

    def create_validation_record(self, zone_id: str, zone_name: str,
                                  cname_name: str, cname_value: str) -> dict:
        """Create DNS validation record in ClouDNS."""
        print("Adding validation record to ClouDNS...")

        # Extract record name (remove zone name suffix)
        full_name = cname_name.rstrip(".")
        if full_name.endswith(f".{zone_name}"):
            record_name = full_name[:-len(f".{zone_name}")]
        else:
            record_name = full_name

        # Remove trailing dot from value
        cname_value = cname_value.rstrip(".")

        # Check for existing record
        existing = self._find_record(zone_name, record_name, "CNAME")

        if existing:
            print("Updating existing DNS record...")
            # Delete existing record first (ClouDNS doesn't have update, need delete+add)
            self._api_request("/dns/delete-record.json", {
                "domain-name": zone_name,
                "record-id": existing["id"]
            })

        print("Creating new DNS record...")
        result = self._api_request("/dns/add-record.json", {
            "domain-name": zone_name,
            "record-type": "CNAME",
            "host": record_name,
            "record": cname_value,
            "ttl": self.MIN_TTL
        })

        if result.get("status") == "Success":
            record_id = result.get("data", {}).get("id")
            print("DNS record added successfully")
            return {
                "id": record_id,
                "name": record_name,
                "zone_name": zone_name
            }

        raise Exception(f"Failed to create DNS record: {result}")

    def cleanup_validation_record(self, zone_id: str, record_info: dict) -> None:
        """Remove DNS validation record from ClouDNS."""
        record_id = record_info.get("id")
        record_name = record_info.get("name")
        zone_name = record_info.get("zone_name", zone_id)

        if not record_id:
            # Try to find record by name
            if record_name:
                existing = self._find_record(zone_name, record_name, "CNAME")
                if existing:
                    record_id = existing.get("id")

        if not record_id:
            print(f"Warning: Could not find record to clean up: {record_name}")
            return

        print(f"Cleaning up validation record: {record_name}")
        try:
            self._api_request("/dns/delete-record.json", {
                "domain-name": zone_name,
                "record-id": record_id
            })
            print("Validation record cleaned up successfully")
        except Exception as e:
            print(f"Warning: Could not clean up validation record: {e}")




def get_dns_provider(provider_name: str, api_token: str = None) -> DNSProvider:
    """Factory function to create DNS provider instance.

    Args:
        provider_name: Name of the provider ('cloudflare', 'hetzner', 'desec', 'bunny', 'cloudns')
        api_token: Optional API token (will use env var if not provided)

    Returns:
        DNSProvider instance
    """
    providers = {
        "cloudflare": (CloudflareDNSProvider, "CLOUDFLARE_API_TOKEN"),
        "hetzner": (HetznerDNSProvider, "HETZNER_DNS_API_TOKEN"),
        "desec": (DeSECDNSProvider, "DESEC_API_TOKEN"),
        "bunny": (BunnyDNSProvider, "BUNNY_API_KEY"),
        "cloudns": (ClouDNSDNSProvider, "CLOUDNS_AUTH_PASSWORD"),
    }

    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}. Supported: {list(providers.keys())}")

    provider_class, env_var = providers[provider_name]
    token = api_token or os.environ.get(env_var)

    if not token:
        raise ValueError(f"{env_var} environment variable not set for {provider_name} provider.")

    return provider_class(token)


# =============================================================================
# Bunny CDN Certificate Provider
# =============================================================================

class BunnyCDNCertificateProvider:
    """Handles SSL certificate requests for Bunny CDN pull zones."""

    API_BASE = "https://api.bunny.net"

    # DNS providers that support CNAME flattening at apex
    CNAME_FLATTENING_PROVIDERS = ["cloudflare", "bunny"]

    def __init__(self, api_key: str = None):
        """Initialize Bunny CDN client.

        Args:
            api_key: Bunny.net API key (uses BUNNY_API_KEY env var if not provided)
        """
        self.api_key = api_key or os.environ.get("BUNNY_API_KEY")
        if not self.api_key:
            raise ValueError("BUNNY_API_KEY environment variable not set.")

        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": self.api_key,
            "Content-Type": "application/json"
        })

    @staticmethod
    def is_apex_domain(hostname: str) -> bool:
        """Check if hostname is an apex/root domain (e.g., example.com vs www.example.com)."""
        parts = hostname.split('.')
        # Simple check: apex domains typically have 2 parts (example.com)
        # or 3 parts for country codes (example.co.uk)
        if len(parts) == 2:
            return True
        # Check for common country-code TLDs
        if len(parts) == 3 and parts[-2] in ['co', 'com', 'org', 'net', 'ac', 'gov']:
            return True
        return False

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to Bunny CDN."""
        url = f"{self.API_BASE}{endpoint}"

        try:
            if method == "GET":
                response = self.session.get(url, timeout=30)
            elif method == "POST":
                response = self.session.post(url, json=data, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if response.status_code >= 400:
                return {"error": True, "status": response.status_code, "message": response.text}

            if response.status_code == 204:
                return {"success": True}

            return response.json() if response.text else {"success": True}

        except requests.RequestException as e:
            return {"error": True, "message": str(e)}

    def list_pull_zones(self) -> list:
        """List all pull zones."""
        result = self._api_request("GET", "/pullzone")
        if isinstance(result, list):
            return result
        return []

    def find_pull_zone_by_hostname(self, hostname: str) -> Optional[dict]:
        """Find a pull zone that has a specific hostname configured."""
        zones = self.list_pull_zones()
        for zone in zones:
            hostnames = zone.get('Hostnames', [])
            for h in hostnames:
                if h.get('Value') == hostname:
                    return zone
        return None

    def find_pull_zone_by_name(self, name: str) -> Optional[dict]:
        """Find a pull zone by its name."""
        zones = self.list_pull_zones()
        for zone in zones:
            if zone.get('Name') == name:
                return zone
        return None

    def request_certificate(self, pull_zone_id: int, hostname: str,
                           max_retries: int = 3, retry_delay: int = 30,
                           use_dns01: bool = False) -> bool:
        """Request a free SSL certificate for a hostname.

        The API endpoint is /pullzone/loadFreeCertificate (without pull zone ID
        in the path). The pull_zone_id parameter is kept for backwards
        compatibility but is not used in the API call.

        Args:
            pull_zone_id: Bunny CDN pull zone ID (unused, kept for compat)
            hostname: The hostname to get certificate for
            max_retries: Number of retry attempts
            retry_delay: Delay between retries in seconds
            use_dns01: Use DNS01 validation instead of HTTP01. Required when
                       the domain uses Bunny DNS or when HTTP01 validation
                       fails (e.g., due to edge caching).

        Returns:
            True if certificate was requested successfully
        """
        print(f"Requesting SSL certificate for '{hostname}'...")
        if use_dns01:
            print("  Using DNS01 validation (useOnlyHttp01=false)")

        for attempt in range(1, max_retries + 1):
            params = f"hostname={hostname}"
            if use_dns01:
                params += "&useOnlyHttp01=false"
            url = f"/pullzone/loadFreeCertificate?{params}"
            result = self._api_request("GET", url)

            if result.get("error"):
                status = result.get("status", "unknown")
                message = result.get("message", "Unknown error")

                if status == 401:
                    print(f"  Attempt {attempt}/{max_retries}: Authorization denied...")
                elif status == 404:
                    print(f"  Attempt {attempt}/{max_retries}: DNS not propagated yet...")
                elif status == 500:
                    print(f"  Attempt {attempt}/{max_retries}: Server error, retrying...")
                else:
                    print(f"  Attempt {attempt}/{max_retries}: HTTP {status} - {message}")

                if attempt < max_retries:
                    print(f"  Waiting {retry_delay}s before retry...")
                    time.sleep(retry_delay)
                continue

            print(f"  SSL certificate requested successfully for '{hostname}'!")
            return True

        print(f"  Failed to request certificate after {max_retries} attempts.")
        return False

    def check_certificate_status(self, pull_zone_id: int, hostname: str) -> dict:
        """Check SSL certificate status for a hostname.

        Returns:
            Dict with certificate status information
        """
        zones = self.list_pull_zones()
        for zone in zones:
            if zone.get('Id') == pull_zone_id:
                for h in zone.get('Hostnames', []):
                    if h.get('Value') == hostname:
                        return {
                            "hostname": hostname,
                            "has_certificate": h.get('HasCertificate', False),
                            "force_ssl": h.get('ForceSSL', False),
                            "certificate_key": h.get('CertificateKey', ''),
                        }
        return {"hostname": hostname, "has_certificate": False}

    def enable_force_ssl(self, pull_zone_id: int, hostname: str) -> bool:
        """Enable Force SSL for a hostname."""
        print(f"Enabling Force SSL for '{hostname}'...")

        result = self._api_request("POST", f"/pullzone/{pull_zone_id}/setForceSSL", {
            "Hostname": hostname,
            "ForceSSL": True
        })

        if result.get("error"):
            print(f"  Warning: Could not enable Force SSL: {result.get('message')}")
            return False

        print("  Force SSL enabled successfully.")
        return True

    def create_apex_redirect_edge_rule(self, pull_zone_id: int, apex_domain: str,
                                        www_domain: str = None) -> bool:
        """Create an edge rule to redirect apex domain to www.

        This is needed when the DNS provider doesn't support CNAME flattening.
        The apex domain can't get an SSL certificate, so we redirect to www.

        Args:
            pull_zone_id: Bunny CDN pull zone ID
            apex_domain: The apex domain (e.g., example.com)
            www_domain: The www domain (default: www.{apex_domain})

        Returns:
            True if edge rule was created successfully
        """
        if www_domain is None:
            www_domain = f"www.{apex_domain}"

        print(f"Creating edge rule to redirect '{apex_domain}' --> '{www_domain}'...")

        data = {
            "ActionType": 2,  # Redirect
            "ActionParameter1": f"https://{www_domain}/",
            "Triggers": [{
                "Type": 0,  # URL match
                "PatternMatchingType": 0,  # Match any
                "PatternMatches": [apex_domain]
            }],
            "TriggerMatchingType": 0,  # Match all triggers
            "Description": f"Redirect apex ({apex_domain}) to www",
            "Enabled": True
        }

        result = self._api_request("POST", f"/pullzone/{pull_zone_id}/edgerules/addOrUpdate", data)

        if result.get("error"):
            print(f"  Warning: Could not create edge rule: {result.get('message')}")
            return False

        print("  Edge rule created successfully.")
        print(f"  Note: HTTP traffic to {apex_domain} will redirect to https://{www_domain}")
        return True

    def check_apex_redirect_exists(self, pull_zone_id: int, apex_domain: str) -> bool:
        """Check if an apex redirect edge rule already exists."""
        result = self._api_request("GET", f"/pullzone/{pull_zone_id}")
        if result.get("error"):
            return False

        edge_rules = result.get("EdgeRules", [])
        for rule in edge_rules:
            description = rule.get("Description", "").lower()
            if "apex" in description and apex_domain.lower() in description:
                return True
            # Also check triggers
            triggers = rule.get("Triggers", [])
            for trigger in triggers:
                patterns = trigger.get("PatternMatches", [])
                if apex_domain in patterns:
                    return True
        return False


class BunnyCertificateAutomation:
    """Automates SSL certificate creation for Bunny CDN."""

    def __init__(
        self,
        hostname: str,
        pull_zone_name: str = None,
        bunny_api_key: str = None,
        force_ssl: bool = True,
        retry_count: int = 5,
        retry_delay: int = 30,
        dns_provider: str = None,
        setup_apex_redirect: bool = True,
    ):
        """Initialize Bunny certificate automation.

        Args:
            hostname: The hostname to get certificate for (e.g., mist-kibl.at)
            pull_zone_name: Pull zone name (default: hostname with dots replaced by hyphens)
            bunny_api_key: Bunny API key (uses env var if not provided)
            force_ssl: Enable Force SSL after certificate is issued
            retry_count: Number of retry attempts for certificate request
            retry_delay: Delay between retries in seconds
            dns_provider: DNS provider name (for apex domain handling)
            setup_apex_redirect: Auto-create edge rule for apex --> www redirect
        """
        self.hostname = hostname
        self.pull_zone_name = pull_zone_name or hostname.replace('.', '-')
        self.force_ssl = force_ssl
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.dns_provider = dns_provider
        self.setup_apex_redirect = setup_apex_redirect

        self.bunny = BunnyCDNCertificateProvider(bunny_api_key)
        self.pull_zone = None
        self.pull_zone_id = None
        self.is_apex = self.bunny.is_apex_domain(hostname)

    def find_pull_zone(self) -> bool:
        """Find the pull zone for the hostname."""
        print(f"Looking for pull zone '{self.pull_zone_name}'...")

        # First try by name
        self.pull_zone = self.bunny.find_pull_zone_by_name(self.pull_zone_name)

        # Then try by hostname
        if not self.pull_zone:
            self.pull_zone = self.bunny.find_pull_zone_by_hostname(self.hostname)

        if self.pull_zone:
            self.pull_zone_id = self.pull_zone.get('Id')
            print(f"Found pull zone: {self.pull_zone.get('Name')} (ID: {self.pull_zone_id})")
            return True

        print(f"Error: Pull zone not found for '{self.pull_zone_name}' or '{self.hostname}'")
        return False

    def check_hostname_configured(self) -> bool:
        """Check if hostname is configured on the pull zone."""
        if not self.pull_zone:
            return False

        hostnames = self.pull_zone.get('Hostnames', [])
        for h in hostnames:
            if h.get('Value') == self.hostname:
                print(f"Hostname '{self.hostname}' is configured on pull zone.")
                return True

        print(f"Warning: Hostname '{self.hostname}' not configured on pull zone.")
        return False

    def run(self) -> bool:
        """Execute the certificate automation workflow.

        Returns:
            True if successful, False otherwise
        """
        print(f"\n{'='*60}")
        print("Bunny CDN SSL Certificate Automation")
        print(f"{'='*60}")
        print(f"Hostname: {self.hostname}")
        print(f"Pull Zone: {self.pull_zone_name}")
        print(f"Is Apex Domain: {self.is_apex}")
        if self.dns_provider:
            print(f"DNS Provider: {self.dns_provider}")
        print(f"{'='*60}\n")

        # Find pull zone
        if not self.find_pull_zone():
            return False

        # Check hostname is configured
        if not self.check_hostname_configured():
            print("Please add the hostname to the pull zone first.")
            return False

        # Handle apex domain limitations
        if self.is_apex:
            supports_cname_flattening = (
                self.dns_provider and
                self.dns_provider.lower() in BunnyCDNCertificateProvider.CNAME_FLATTENING_PROVIDERS
            )

            if not supports_cname_flattening:
                print("\n[!] APEX DOMAIN LIMITATION DETECTED")
                print(f"   '{self.hostname}' is an apex/root domain.")
                if self.dns_provider:
                    print(f"   DNS provider '{self.dns_provider}' does not support CNAME flattening.")
                else:
                    print("   Most DNS providers don't support CNAME flattening at apex.")
                print("   Bunny CDN cannot validate SSL for apex domains without CNAME flattening.")
                print()

                if self.setup_apex_redirect:
                    www_domain = f"www.{self.hostname}"
                    print(f"   Setting up redirect: {self.hostname} --> {www_domain}")

                    # Check if redirect already exists
                    if not self.bunny.check_apex_redirect_exists(self.pull_zone_id, self.hostname):
                        self.bunny.create_apex_redirect_edge_rule(
                            self.pull_zone_id, self.hostname, www_domain
                        )
                    else:
                        print("   Apex redirect edge rule already exists.")

                    print()
                    print(f"   [OK] HTTP traffic to {self.hostname} will redirect to https://{www_domain}")
                    print(f"   [i] Use '{www_domain}' as your primary domain for SSL.")
                    print()
                    return True  # Apex handled via redirect
                else:
                    print("   To fix this, either:")
                    print("   1. Use Cloudflare as DNS provider (supports CNAME flattening)")
                    print(f"   2. Use www.{self.hostname} as your primary domain")
                    print("   3. Run with --setup-apex-redirect to auto-create redirect rule")
                    print()
                    return False

        # Check current certificate status
        status = self.bunny.check_certificate_status(self.pull_zone_id, self.hostname)
        if status.get('has_certificate'):
            print(f"Certificate already exists for '{self.hostname}'!")
            if self.force_ssl and not status.get('force_ssl'):
                self.bunny.enable_force_ssl(self.pull_zone_id, self.hostname)
            return True

        # Request certificate
        # Use DNS01 validation when Bunny DNS is the provider, as this allows
        # Bunny to validate via its own DNS zone without HTTP challenges.
        use_dns01 = self.dns_provider and self.dns_provider.lower() == "bunny"
        success = self.bunny.request_certificate(
            self.pull_zone_id,
            self.hostname,
            max_retries=self.retry_count,
            retry_delay=self.retry_delay,
            use_dns01=use_dns01,
        )

        if not success:
            print("\nCertificate request failed.")
            print("Make sure DNS is properly configured and propagated.")
            if self.is_apex:
                print(f"\nNote: This is an apex domain. Consider using www.{self.hostname} instead.")
            return False

        # Enable Force SSL if requested
        if self.force_ssl:
            time.sleep(5)  # Wait a bit for certificate to be processed
            self.bunny.enable_force_ssl(self.pull_zone_id, self.hostname)

        # Verify certificate
        print("\nVerifying certificate status...")
        time.sleep(5)
        status = self.bunny.check_certificate_status(self.pull_zone_id, self.hostname)

        if status.get('has_certificate'):
            print(f"\n{'='*60}")
            print("Certificate created successfully!")
            print(f"Hostname: {self.hostname}")
            print(f"Force SSL: {'Enabled' if status.get('force_ssl') else 'Disabled'}")
            print(f"{'='*60}\n")
            return True
        else:
            print("\nCertificate may still be processing. Check Bunny dashboard.")
            return True  # Request was accepted, just may take time

class CertificateAutomation:
    """Handles SSL certificate creation and validation."""

    def __init__(
        self,
        domain_name: str,
        stage: str,
        dns_provider: DNSProvider,
        aws_profile: Optional[str] = None,
        aws_region: str = "us-east-1",
        use_wildcard: bool = False,
        config_file: Optional[str] = None,
        skip_config_update: bool = False,
        create_zone: bool = False,
    ):
        """
        Initialize the certificate automation tool.

        Args:
            domain_name: The domain name for the certificate
            stage: Deployment stage (e.g., staging, production)
            dns_provider: DNS provider instance for validation records
            aws_profile: AWS profile name (optional, uses env vars in CI)
            aws_region: AWS region for ACM (default: us-east-1)
            use_wildcard: Create wildcard certificate
            config_file: Path to config file to update (default: stages.yml)
            skip_config_update: Skip updating config file
            create_zone: Create DNS zone if it doesn't exist (provider must support it)
        """
        self.domain_name = domain_name
        self.stage = stage
        self.dns_provider = dns_provider
        self.aws_region = aws_region
        self.use_wildcard = use_wildcard
        self.config_file = config_file or "stages.yml"
        self.skip_config_update = skip_config_update
        self.create_zone = create_zone
        self.validation_record_info = None  # For cleanup

        # Determine if running in CI environment
        self.is_ci = bool(os.environ.get("CI_JOB_ID"))

        # Setup AWS session
        if self.is_ci:
            print("Running in CI environment, using AWS environment variables")
            self.session = boto3.Session(region_name=aws_region)
        else:
            profile = aws_profile or os.environ.get("AWS_PROFILE", "pseekoo")
            print(f"Using AWS profile: {profile}")
            self.session = boto3.Session(profile_name=profile, region_name=aws_region)

        self.acm_client = self.session.client("acm")

        # Extract base domain
        self.base_domain = self._extract_base_domain(domain_name)

        # Get zone ID (auto-create if requested and supported)
        if create_zone:
            if not dns_provider.supports_zone_creation():
                print("Warning: DNS provider does not support automatic zone creation.")
            self.zone_id = dns_provider.get_or_create_zone(self.base_domain, auto_create=True)
        else:
            self.zone_id = dns_provider.get_zone_id(self.base_domain)

        # Determine certificate domain
        if use_wildcard:
            self.cert_domain = f"*.{self.base_domain}"
            print(f"Using wildcard certificate: {self.cert_domain}")
        else:
            self.cert_domain = domain_name
            print(f"Using specific domain certificate: {self.cert_domain}")

    @staticmethod
    def _extract_base_domain(domain: str) -> str:
        """Extract base domain from a full domain name."""
        parts = domain.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return domain

    def find_existing_certificate(self) -> Optional[str]:
        """Check if certificate already exists for the domain."""
        print(f"Checking for existing certificate for {self.cert_domain}...")
        try:
            response = self.acm_client.list_certificates(
                CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]
            )

            for cert in response.get("CertificateSummaryList", []):
                if cert.get("DomainName") == self.cert_domain:
                    arn = cert.get("CertificateArn")
                    print(f"Found existing certificate: {arn}")
                    return arn

            print("No existing certificate found")
            return None

        except ClientError as e:
            print(f"Error checking for existing certificates: {e}")
            raise

    def request_certificate(self) -> str:
        """Request a new SSL certificate from ACM."""
        print(f"Requesting new certificate for {self.cert_domain}...")
        try:
            response = self.acm_client.request_certificate(
                DomainName=self.cert_domain, ValidationMethod="DNS"
            )

            cert_arn = response["CertificateArn"]
            print(f"Certificate requested successfully: {cert_arn}")
            return cert_arn

        except ClientError as e:
            print(f"Error requesting certificate: {e}")
            raise

    def get_validation_records(
        self, cert_arn: str, max_attempts: int = 12, wait_time: int = 10
    ) -> Tuple[str, str]:
        """
        Get DNS validation records for the certificate.

        Args:
            cert_arn: Certificate ARN
            max_attempts: Maximum number of retry attempts
            wait_time: Wait time between attempts in seconds

        Returns:
            Tuple of (CNAME name, CNAME value)
        """
        print("Fetching validation records...")

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.acm_client.describe_certificate(
                    CertificateArn=cert_arn
                )

                validation_options = response.get("Certificate", {}).get(
                    "DomainValidationOptions", []
                )

                if validation_options:
                    resource_record = validation_options[0].get("ResourceRecord")
                    if resource_record:
                        cname_name = resource_record.get("Name", "").rstrip(".")
                        cname_value = resource_record.get("Value", "").rstrip(".")

                        if cname_name and cname_value:
                            print("Found validation records:")
                            print(f"  CNAME Name: {cname_name}")
                            print(f"  CNAME Value: {cname_value}")
                            return cname_name, cname_value

                print(
                    f"Validation records not available yet. Waiting... (Attempt {attempt}/{max_attempts})"
                )
                time.sleep(wait_time)

            except ClientError as e:
                print(f"Error fetching validation records: {e}")
                if attempt == max_attempts:
                    raise

        raise RuntimeError(
            "Could not fetch validation records after multiple attempts"
        )

    def create_dns_validation_record(self, cname_name: str, cname_value: str) -> dict:
        """
        Create DNS validation record using the configured DNS provider.

        Args:
            cname_name: CNAME record name
            cname_value: CNAME record value

        Returns:
            Record info dict for cleanup
        """
        return self.dns_provider.create_validation_record(
            self.zone_id, self.base_domain, cname_name, cname_value
        )

    def cleanup_dns_validation_record(self) -> None:
        """Clean up DNS validation record if one was created."""
        if self.validation_record_info:
            self.dns_provider.cleanup_validation_record(
                self.zone_id, self.validation_record_info
            )

    def wait_for_validation(
        self, cert_arn: str, timeout: int = 1800
    ) -> bool:
        """
        Wait for certificate validation to complete.

        Args:
            cert_arn: Certificate ARN
            timeout: Maximum wait time in seconds (default: 30 minutes)

        Returns:
            True if validation successful, False otherwise
        """
        print(
            f"Waiting for certificate validation (timeout: {timeout//60} minutes)..."
        )
        try:
            waiter = self.acm_client.get_waiter("certificate_validated")
            waiter.wait(
                CertificateArn=cert_arn,
                WaiterConfig={"Delay": 30, "MaxAttempts": timeout // 30},
            )
            print("Certificate validated successfully!")
            return True

        except WaiterError as e:
            print(f"Certificate validation timed out or failed: {e}")
            return False

    def update_config_file(self, cert_arn: str) -> None:
        """
        Update configuration file with certificate ARN.

        Args:
            cert_arn: Certificate ARN to add to config
        """
        if self.skip_config_update:
            print("Skipping config file update (--skip-config-update flag set)")
            return

        if not os.path.exists(self.config_file):
            print(f"Warning: Config file {self.config_file} not found. Skipping update.")
            return

        print(f"Updating {self.config_file} with certificate ARN...")

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                content = f.read()

            # Replace certificate placeholder
            updated_content = content.replace(
                "certificateArn: 'certificate-placeholder'",
                f"certificateArn: '{cert_arn}'",
            )

            # Create backup
            backup_file = f"{self.config_file}.bak"
            with open(backup_file, "w", encoding="utf-8") as f:
                f.write(content)

            # Write updated content
            with open(self.config_file, "w", encoding="utf-8") as f:
                f.write(updated_content)

            print(f"Updated {self.config_file} successfully (backup: {backup_file})")

        except Exception as e:
            print(f"Error updating config file: {e}")
            raise

    def run(self) -> str:
        """
        Execute the full certificate automation workflow.

        Returns:
            Certificate ARN
        """
        print(f"\n{'='*60}")
        print("SSL Certificate Automation")
        print(f"{'='*60}")
        print(f"Domain: {self.cert_domain}")
        print(f"Original domain: {self.domain_name}")
        print(f"Base domain: {self.base_domain}")
        print(f"Stage: {self.stage}")
        print(f"{'='*60}\n")

        # Check for existing certificate
        cert_arn = self.find_existing_certificate()

        if not cert_arn:
            # Request new certificate
            cert_arn = self.request_certificate()

            # Wait for validation records to be available
            print("Waiting for validation records to be available...")
            time.sleep(10)

        # Get validation records
        cname_name, cname_value = self.get_validation_records(cert_arn)

        # Create/update DNS record using configured provider
        self.validation_record_info = self.create_dns_validation_record(cname_name, cname_value)

        # Wait for validation
        if not self.wait_for_validation(cert_arn):
            print("\nCertificate validation timed out.")
            print("Please check AWS Certificate Manager console for status.")
            sys.exit(1)

        # Update config file
        self.update_config_file(cert_arn)

        print(f"\n{'='*60}")
        print("Certificate creation and validation completed successfully!")
        print(f"Certificate ARN: {cert_arn}")
        print(f"{'='*60}\n")

        # Print deployment instructions
        print("Next steps:")
        if self.is_ci:
            print(f"  npx serverless deploy --stage {self.stage} --verbose")
        else:
            profile = os.environ.get("AWS_PROFILE", "pseekoo")
            print(
                f"  npx serverless deploy --stage {self.stage} --verbose --aws-profile {profile}"
            )

        return cert_arn


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Automate SSL certificate creation and DNS validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Bunny CDN certificate for www subdomain (recommended)
  %(prog)s www.mist-kibl.at --cert-provider bunny

  # Bunny CDN with apex domain + deSEC (auto-creates redirect)
  %(prog)s mist-kibl.at --cert-provider bunny --bunny-dns-provider desec
  # This will auto-create an edge rule: mist-kibl.at --> www.mist-kibl.at

  # Bunny CDN with multiple hostnames
  %(prog)s www.mist-kibl.at api.mist-kibl.at --cert-provider bunny

  # Bunny CDN with custom pull zone name
  %(prog)s www.mist-kibl.at --cert-provider bunny --pull-zone mist-kibl-at

  # Bunny CDN apex with Cloudflare DNS (supports CNAME flattening)
  %(prog)s mist-kibl.at --cert-provider bunny --bunny-dns-provider cloudflare

  # Bunny CDN apex with Bunny DNS (European DNS, supports CNAME flattening)
  %(prog)s mist-kibl.at --cert-provider bunny --bunny-dns-provider bunny

  # AWS ACM with Bunny DNS
  %(prog)s app.example.com production --cert-provider acm --dns-provider bunny

  # AWS ACM with ClouDNS (European, affordable DDoS protection)
  %(prog)s app.example.com production --cert-provider acm --dns-provider cloudns

  # AWS ACM wildcard certificate with Cloudflare DNS
  %(prog)s app-dev.lularge.com staging --cert-provider acm --wildcard

  # AWS ACM with deSEC DNS provider
  %(prog)s app.example.com production --cert-provider acm --dns-provider desec

  # AWS ACM with Hetzner DNS
  %(prog)s app.example.com staging --cert-provider acm --dns-provider hetzner

Apex Domain Notes:
  Apex domains (e.g., example.com without www) require CNAME flattening for
  Bunny CDN SSL certificates. Cloudflare and Bunny DNS support this. For other
  DNS providers (deSEC, Hetzner, etc.), the script will:
  1. Auto-create an edge rule to redirect apex --> www
  2. You should request certificate for www subdomain instead

Environment Variables:
  BUNNY_API_KEY          - Bunny.net API key (for bunny provider)
  AWS_PROFILE            - AWS profile name (default: pseekoo)
  AWS_REGION             - AWS region (default: us-east-1)
  CLOUDFLARE_API_TOKEN   - Cloudflare API token (for cloudflare DNS)
  HETZNER_DNS_API_TOKEN  - Hetzner DNS API token (for hetzner DNS)
  DESEC_API_TOKEN        - deSEC API token (for desec DNS)
  CLOUDNS_AUTH_ID        - ClouDNS API auth-id (for cloudns DNS)
  CLOUDNS_AUTH_PASSWORD  - ClouDNS API auth-password (for cloudns DNS)
        """,
    )

    parser.add_argument("domain", nargs="+", help="Domain name(s) for the certificate")
    parser.add_argument(
        "stage",
        nargs="?",
        default="production",
        help="Deployment stage (default: production, only used for ACM)",
    )

    # Certificate provider selection
    parser.add_argument(
        "--cert-provider",
        choices=["bunny", "acm"],
        default="bunny",
        help="Certificate provider: bunny (Bunny CDN) or acm (AWS ACM). Default: bunny",
    )

    # Bunny CDN options
    parser.add_argument(
        "--pull-zone",
        help="Bunny CDN pull zone name (default: domain with dots replaced by hyphens)",
    )
    parser.add_argument(
        "--no-force-ssl",
        action="store_true",
        help="Don't enable Force SSL after certificate is issued (Bunny only)",
    )
    parser.add_argument(
        "--retry-count",
        type=int,
        default=5,
        help="Number of retry attempts for certificate request (default: 5)",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=30,
        help="Delay between retries in seconds (default: 30)",
    )
    parser.add_argument(
        "--bunny-dns-provider",
        choices=["cloudflare", "hetzner", "desec", "bunny", "cloudns", "other"],
        help="DNS provider used with Bunny CDN (for apex domain handling)",
    )
    parser.add_argument(
        "--no-apex-redirect",
        action="store_true",
        help="Don't auto-create apex to www redirect edge rule for apex domains",
    )

    # AWS ACM options
    parser.add_argument(
        "--aws-profile",
        help="AWS profile name (default: env AWS_PROFILE or 'pseekoo')",
        default=None,
    )
    parser.add_argument(
        "--aws-region",
        help="AWS region for ACM (default: us-east-1)",
        default="us-east-1",
    )
    parser.add_argument(
        "--dns-provider",
        choices=["cloudflare", "hetzner", "desec", "bunny", "cloudns"],
        default="cloudflare",
        help="DNS provider for ACM validation (default: cloudflare)",
    )
    parser.add_argument(
        "--create-zone",
        action="store_true",
        help="Create DNS zone if it does not exist (deSEC only)",
    )
    parser.add_argument(
        "--wildcard",
        action="store_true",
        help="Create wildcard certificate (ACM only)",
    )
    parser.add_argument(
        "--config-file",
        help="Path to config file to update (default: stages.yml)",
        default="stages.yml",
    )
    parser.add_argument(
        "--skip-config-update",
        action="store_true",
        help="Skip updating config file with certificate ARN (ACM only)",
    )
    parser.add_argument(
        "--validation-timeout",
        type=int,
        help="Certificate validation timeout in seconds (default: 1800)",
        default=1800,
    )

    args = parser.parse_args()

    try:
        if args.cert_provider == "bunny":
            # Bunny CDN certificate automation
            print("Using Bunny CDN certificate provider")
            if args.bunny_dns_provider:
                print(f"DNS provider: {args.bunny_dns_provider}")

            success = True
            for hostname in args.domain:
                automation = BunnyCertificateAutomation(
                    hostname=hostname,
                    pull_zone_name=args.pull_zone,
                    force_ssl=not args.no_force_ssl,
                    retry_count=args.retry_count,
                    retry_delay=args.retry_delay,
                    dns_provider=args.bunny_dns_provider,
                    setup_apex_redirect=not args.no_apex_redirect,
                )

                if not automation.run():
                    success = False

            if success:
                print("\nAll certificates processed successfully!")
                sys.exit(0)
            else:
                print("\nSome certificates failed. Check output above.")
                sys.exit(1)

        else:
            # AWS ACM certificate automation
            print("Using AWS ACM certificate provider")
            print(f"DNS provider: {args.dns_provider}")

            dns_provider = get_dns_provider(args.dns_provider)

            # ACM only supports single domain (or wildcard)
            domain = args.domain[0]

            automation = CertificateAutomation(
                domain_name=domain,
                stage=args.stage,
                dns_provider=dns_provider,
                aws_profile=args.aws_profile,
                aws_region=args.aws_region,
                use_wildcard=args.wildcard,
                config_file=args.config_file,
                skip_config_update=args.skip_config_update,
                create_zone=args.create_zone,
            )

            cert_arn = automation.run()
            print(f"\nSuccess! Certificate ARN: {cert_arn}")
            sys.exit(0)

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user")
        sys.exit(130)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()