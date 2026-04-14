#!/usr/bin/env python3
"""
Scaleway Functions (FaaS) Setup Script

This script sets up serverless functions on Scaleway's Function-as-a-Service platform.
Scaleway Functions is a great alternative to AWS Lambda with simpler pricing and
EU-based infrastructure.

FEATURES:
---------
- Creates Scaleway Functions namespaces
- Deploys functions from local directories or Docker images
- Configures custom domains with SSL
- Sets up environment variables and secrets
- Supports multiple regions (Paris, Amsterdam, Warsaw)

Environment Variables:
    SCW_ACCESS_KEY: Scaleway access key
    SCW_SECRET_KEY: Scaleway secret key
    SCW_DEFAULT_ORGANIZATION_ID: Scaleway organization ID
    SCW_DEFAULT_PROJECT_ID: Scaleway project ID

Usage Examples:
    # Create a new function namespace
    python setup_scaleway_faas.py --domain api.m3rp.ai --name m3rp-backend

    # Deploy with custom domain
    python setup_scaleway_faas.py --domain api.m3rp.ai --name m3rp-backend --setup-domain

    # Specify region
    python setup_scaleway_faas.py --domain api.m3rp.ai --name m3rp-backend --region nl-ams
"""

import os
import json
import time
import logging
import argparse
import requests
from typing import Optional, Dict, List
from abc import ABC, abstractmethod

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass


# =============================================================================
# Scaleway Configuration
# =============================================================================

SCALEWAY_API_BASE = "https://api.scaleway.com"

SCALEWAY_REGIONS = {
    'fr-par': {'name': 'Paris, France', 'functions_api': 'https://api.scaleway.com/functions/v1beta1/regions/fr-par'},
    'nl-ams': {'name': 'Amsterdam, Netherlands', 'functions_api': 'https://api.scaleway.com/functions/v1beta1/regions/nl-ams'},
    'pl-waw': {'name': 'Warsaw, Poland', 'functions_api': 'https://api.scaleway.com/functions/v1beta1/regions/pl-waw'},
}

SCALEWAY_RUNTIMES = {
    'node20': 'node20',
    'node22': 'node22',
    'python310': 'python310',
    'python311': 'python311',
    'python312': 'python312',
    'go121': 'go121',
    'rust165': 'rust165',
}


# =============================================================================
# DNS Provider Abstraction (same as other setup scripts)
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


class CloudflareDNSProvider(DNSProvider):
    """Cloudflare DNS API adapter."""

    def __init__(self):
        try:
            from cloudflare import Cloudflare
        except ImportError:
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
        full_domain_name = f"{name}.{zone_name}" if name != zone_name else zone_name
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

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")
        target_with_dot = target if target.endswith('.') else f"{target}."

        # Delete conflicting records
        try:
            self._api_request("DELETE", f"/domains/{zone_id}/rrsets/{name}/A/")
        except:
            pass
        try:
            self._api_request("DELETE", f"/domains/{zone_id}/rrsets/{name}/AAAA/")
        except:
            pass

        data = {
            "subname": name,
            "type": "CNAME",
            "records": [target_with_dot],
            "ttl": max(ttl, self.MIN_TTL)
        }

        try:
            self._api_request("PATCH", f"/domains/{zone_id}/rrsets/{name}/CNAME/", {"records": [target_with_dot], "ttl": max(ttl, self.MIN_TTL)})
        except:
            self._api_request("POST", f"/domains/{zone_id}/rrsets/", data)

        logging.info("CNAME record created/updated.")


class ClouDNSDNSProvider(DNSProvider):
    """ClouDNS DNS API adapter.

    ClouDNS is a European DNS provider with affordable DDoS protection.

    Environment Variables:
        CLOUDNS_AUTH_ID: ClouDNS API auth-id
        CLOUDNS_AUTH_PASSWORD: ClouDNS API auth-password
    """
    API_BASE = "https://api.cloudns.net"
    MIN_TTL = 60

    def __init__(self):
        self.auth_id = os.environ.get("CLOUDNS_AUTH_ID")
        self.auth_password = os.environ.get("CLOUDNS_AUTH_PASSWORD")
        self.sub_auth_id = os.environ.get("CLOUDNS_SUB_AUTH_ID")
        self.sub_auth_user = os.environ.get("CLOUDNS_SUB_AUTH_USER")

        if not self.auth_password:
            raise ValueError("CLOUDNS_AUTH_PASSWORD environment variable not set.")

        if not any([self.auth_id, self.sub_auth_id, self.sub_auth_user]):
            raise ValueError(
                "ClouDNS authentication required. Set CLOUDNS_AUTH_ID, "
                "CLOUDNS_SUB_AUTH_ID, or CLOUDNS_SUB_AUTH_USER"
            )

        self.session = requests.Session()

    def _get_auth_params(self) -> dict:
        params = {"auth-password": self.auth_password}
        if self.auth_id:
            params["auth-id"] = self.auth_id
        elif self.sub_auth_id:
            params["sub-auth-id"] = self.sub_auth_id
        elif self.sub_auth_user:
            params["sub-auth-user"] = self.sub_auth_user
        return params

    def _api_request(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.API_BASE}{endpoint}"
        request_params = self._get_auth_params()
        if params:
            request_params.update(params)

        response = self.session.post(url, data=request_params, timeout=30)
        result = response.json()

        if isinstance(result, dict) and result.get("status") == "Failed":
            raise Exception(f"ClouDNS API error: {result.get('statusDescription')}")

        return result

    def get_zone_id(self, zone_name: str) -> str:
        result = self._api_request("/dns/list-zones.json", {
            "page": 1,
            "rows-per-page": 100,
            "search": zone_name
        })

        if isinstance(result, dict):
            for domain in result.keys():
                if domain == zone_name:
                    return zone_name

        raise Exception(f"Zone not found in ClouDNS: {zone_name}")

    def _find_record(self, zone_name: str, record_name: str, record_type: str = "CNAME") -> dict:
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

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")

        # Delete existing record if found
        existing = self._find_record(zone_name, name, "CNAME")
        if existing:
            self._api_request("/dns/delete-record.json", {
                "domain-name": zone_name,
                "record-id": existing["id"]
            })

        # Also delete conflicting A/AAAA records
        for rtype in ["A", "AAAA"]:
            existing = self._find_record(zone_name, name, rtype)
            if existing:
                try:
                    self._api_request("/dns/delete-record.json", {
                        "domain-name": zone_name,
                        "record-id": existing["id"]
                    })
                except:
                    pass

        # Create new CNAME
        result = self._api_request("/dns/add-record.json", {
            "domain-name": zone_name,
            "record-type": "CNAME",
            "host": name,
            "record": target,
            "ttl": max(ttl, self.MIN_TTL)
        })

        if result.get("status") == "Success":
            logging.info("CNAME record created/updated.")
        else:
            raise Exception(f"Failed to create CNAME: {result}")



class BunnyDNSProvider(DNSProvider):
    """Bunny.net DNS API adapter.

    Bunny.net provides DNS hosting alongside their CDN.
    Uses the Bunny.net REST API for DNS record management.

    Environment Variables:
        BUNNY_API_KEY: Bunny.net API key (from https://panel.bunny.net/account)
    """
    API_BASE = "https://api.bunny.net"
    # Bunny DNS record types: 0=A, 1=AAAA, 2=CNAME, 3=TXT, 4=MX, 5=Redirect, 7=SRV, 8=CAA
    RECORD_TYPE_A = 0
    RECORD_TYPE_AAAA = 1
    RECORD_TYPE_CNAME = 2

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
        url = f"{self.API_BASE}{endpoint}"
        kwargs = {"timeout": 30}
        if data is not None:
            kwargs["json"] = data

        response = getattr(self.session, method.lower())(url, **kwargs)

        if response.status_code >= 400:
            raise Exception(f"Bunny API error: {response.status_code} - {response.text}")

        if response.text and response.status_code not in [204]:
            return response.json()
        return {}

    def get_zone_id(self, zone_name: str) -> str:
        result = self._api_request("GET", "/dnszone")
        items = result.get("Items", [])
        for zone in items:
            if zone.get("Domain") == zone_name:
                return str(zone["Id"])
        raise Exception(f"Zone not found in Bunny DNS: {zone_name}")

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        logging.info(f"Creating/updating CNAME: '{name}.{zone_name}' -> '{target}'...")

        # Get zone data with all records
        zone_data = self._api_request("GET", f"/dnszone/{zone_id}")
        records = zone_data.get("Records", [])

        # Remove conflicting A/AAAA records
        for record in records:
            if record.get("Name") == name and record.get("Type") in [self.RECORD_TYPE_A, self.RECORD_TYPE_AAAA]:
                logging.info(f"Removing conflicting record (type={record['Type']}, id={record['Id']})...")
                self._api_request("DELETE", f"/dnszone/{zone_id}/records/{record['Id']}")

        # Check existing CNAME
        existing_cname = None
        for record in records:
            if record.get("Name") == name and record.get("Type") == self.RECORD_TYPE_CNAME:
                existing_cname = record
                break

        if existing_cname:
            if existing_cname.get("Value") == target:
                logging.info("CNAME record already correct.")
                return
            # Update existing CNAME
            self._api_request("POST", f"/dnszone/{zone_id}/records/{existing_cname['Id']}", {
                "Type": self.RECORD_TYPE_CNAME,
                "Name": name,
                "Value": target,
                "Ttl": ttl
            })
            logging.info("CNAME record updated.")
        else:
            # Create new CNAME
            self._api_request("PUT", f"/dnszone/{zone_id}/records", {
                "Type": self.RECORD_TYPE_CNAME,
                "Name": name,
                "Value": target,
                "Ttl": ttl
            })
            logging.info("CNAME record created.")


def get_dns_provider(provider_name: str) -> DNSProvider:
    """Factory function to get the appropriate DNS provider."""
    providers = {
        'cloudflare': CloudflareDNSProvider,
        'desec': DeSECDNSProvider,
        'cloudns': ClouDNSDNSProvider,
        'bunny': BunnyDNSProvider,
    }
    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}")
    return providers[provider_name]()


# =============================================================================
# Scaleway Functions Client
# =============================================================================

class ScalewayFunctionsClient:
    """Client for Scaleway Functions API."""

    def __init__(self, access_key: str = None, secret_key: str = None,
                 project_id: str = None, region: str = 'fr-par'):
        self.access_key = access_key or os.environ.get("SCW_ACCESS_KEY")
        self.secret_key = secret_key or os.environ.get("SCW_SECRET_KEY")
        self.project_id = project_id or os.environ.get("SCW_DEFAULT_PROJECT_ID")
        self.region = region

        if not all([self.access_key, self.secret_key, self.project_id]):
            raise ValueError(
                "Missing Scaleway credentials. Set SCW_ACCESS_KEY, SCW_SECRET_KEY, "
                "and SCW_DEFAULT_PROJECT_ID environment variables."
            )

        self.api_base = SCALEWAY_REGIONS[region]['functions_api']
        self.session = requests.Session()
        self.session.headers.update({
            "X-Auth-Token": self.secret_key,
            "Content-Type": "application/json"
        })

    def _api_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """Make an API request to Scaleway Functions API."""
        url = f"{self.api_base}{endpoint}"

        try:
            if method.upper() == "GET":
                response = self.session.get(url, timeout=30)
            elif method.upper() == "POST":
                response = self.session.post(url, json=data, timeout=30)
            elif method.upper() == "PATCH":
                response = self.session.patch(url, json=data, timeout=30)
            elif method.upper() == "DELETE":
                response = self.session.delete(url, timeout=30)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if response.status_code >= 400:
                raise Exception(f"Scaleway API error: {response.status_code} - {response.text}")

            return response.json() if response.text and response.status_code not in [204] else {}

        except requests.RequestException as e:
            raise Exception(f"Network error: {e}")

    # -------------------------------------------------------------------------
    # Namespace Operations
    # -------------------------------------------------------------------------

    def list_namespaces(self) -> List[dict]:
        """List all function namespaces in the project."""
        result = self._api_request("GET", f"/namespaces?project_id={self.project_id}")
        return result.get('namespaces', [])

    def get_namespace(self, namespace_id: str) -> dict:
        """Get a specific namespace by ID."""
        return self._api_request("GET", f"/namespaces/{namespace_id}")

    def find_namespace_by_name(self, name: str) -> Optional[dict]:
        """Find a namespace by name."""
        namespaces = self.list_namespaces()
        for ns in namespaces:
            if ns.get('name') == name:
                return ns
        return None

    def create_namespace(self, name: str, description: str = "",
                        environment_variables: dict = None,
                        secret_environment_variables: List[dict] = None) -> dict:
        """Create a new function namespace."""
        logging.info(f"Creating Scaleway Functions namespace: {name}")

        data = {
            "name": name,
            "project_id": self.project_id,
            "description": description or f"Function namespace for {name}",
        }

        if environment_variables:
            data["environment_variables"] = environment_variables

        if secret_environment_variables:
            data["secret_environment_variables"] = secret_environment_variables

        result = self._api_request("POST", "/namespaces", data)
        logging.info(f"Namespace created. ID: {result.get('id')}")
        return result

    def update_namespace(self, namespace_id: str,
                        environment_variables: dict = None,
                        secret_environment_variables: List[dict] = None) -> dict:
        """Update a namespace's environment variables."""
        data = {}
        if environment_variables is not None:
            data["environment_variables"] = environment_variables
        if secret_environment_variables is not None:
            data["secret_environment_variables"] = secret_environment_variables

        return self._api_request("PATCH", f"/namespaces/{namespace_id}", data)

    def delete_namespace(self, namespace_id: str) -> None:
        """Delete a namespace."""
        self._api_request("DELETE", f"/namespaces/{namespace_id}")

    # -------------------------------------------------------------------------
    # Function Operations
    # -------------------------------------------------------------------------

    def list_functions(self, namespace_id: str) -> List[dict]:
        """List all functions in a namespace."""
        result = self._api_request("GET", f"/functions?namespace_id={namespace_id}")
        return result.get('functions', [])

    def get_function(self, function_id: str) -> dict:
        """Get a specific function by ID."""
        return self._api_request("GET", f"/functions/{function_id}")

    def find_function_by_name(self, namespace_id: str, name: str) -> Optional[dict]:
        """Find a function by name within a namespace."""
        functions = self.list_functions(namespace_id)
        for fn in functions:
            if fn.get('name') == name:
                return fn
        return None

    def create_function(self, namespace_id: str, name: str,
                       runtime: str = 'node20',
                       handler: str = 'handler.handler',
                       min_scale: int = 0,
                       max_scale: int = 5,
                       memory_limit: int = 256,
                       timeout: str = '30s',
                       http_option: str = 'enabled',
                       privacy: str = 'public',
                       description: str = "",
                       environment_variables: dict = None,
                       secret_environment_variables: List[dict] = None) -> dict:
        """Create a new function."""
        logging.info(f"Creating function: {name}")

        data = {
            "name": name,
            "namespace_id": namespace_id,
            "runtime": runtime,
            "handler": handler,
            "min_scale": min_scale,
            "max_scale": max_scale,
            "memory_limit": memory_limit,
            "timeout": timeout,
            "http_option": http_option,
            "privacy": privacy,
            "description": description or f"Function {name}",
        }

        if environment_variables:
            data["environment_variables"] = environment_variables

        if secret_environment_variables:
            data["secret_environment_variables"] = secret_environment_variables

        result = self._api_request("POST", "/functions", data)
        logging.info(f"Function created. ID: {result.get('id')}")
        return result

    def update_function(self, function_id: str, **kwargs) -> dict:
        """Update a function's configuration."""
        return self._api_request("PATCH", f"/functions/{function_id}", kwargs)

    def delete_function(self, function_id: str) -> None:
        """Delete a function."""
        self._api_request("DELETE", f"/functions/{function_id}")

    def get_function_upload_url(self, function_id: str, content_length: int) -> dict:
        """Get a presigned URL for uploading function code."""
        return self._api_request("GET",
            f"/functions/{function_id}/upload-url?content_length={content_length}")

    def deploy_function(self, function_id: str) -> dict:
        """Deploy a function after uploading code."""
        return self._api_request("POST", f"/functions/{function_id}/deploy")

    # -------------------------------------------------------------------------
    # Domain Operations
    # -------------------------------------------------------------------------

    def list_domains(self, function_id: str) -> List[dict]:
        """List custom domains for a function."""
        result = self._api_request("GET", f"/domains?function_id={function_id}")
        return result.get('domains', [])

    def create_domain(self, function_id: str, hostname: str) -> dict:
        """Add a custom domain to a function."""
        logging.info(f"Adding custom domain: {hostname}")

        data = {
            "function_id": function_id,
            "hostname": hostname,
        }

        result = self._api_request("POST", "/domains", data)
        logging.info(f"Domain added. ID: {result.get('id')}")
        return result

    def delete_domain(self, domain_id: str) -> None:
        """Remove a custom domain."""
        self._api_request("DELETE", f"/domains/{domain_id}")

    def get_domain_dns_record(self, domain_id: str) -> dict:
        """Get the DNS record needed for a custom domain."""
        domain = self._api_request("GET", f"/domains/{domain_id}")
        return {
            "hostname": domain.get("hostname"),
            "url": domain.get("url"),
            "status": domain.get("status"),
        }

    # -------------------------------------------------------------------------
    # Trigger Operations
    # -------------------------------------------------------------------------

    def list_triggers(self, function_id: str) -> List[dict]:
        """List triggers for a function."""
        result = self._api_request("GET", f"/triggers?function_id={function_id}")
        return result.get('triggers', [])

    def create_cron_trigger(self, function_id: str, name: str,
                           schedule: str, args: dict = None) -> dict:
        """Create a cron trigger for a function."""
        data = {
            "name": name,
            "function_id": function_id,
            "schedule": schedule,
        }
        if args:
            data["args"] = json.dumps(args)

        return self._api_request("POST", "/triggers", data)


# =============================================================================
# Setup Report
# =============================================================================

class SetupReport:
    """Track and report FaaS setup status."""

    def __init__(self, name: str, region: str, domain: str = None):
        self.timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        self.name = name
        self.region = region
        self.region_name = SCALEWAY_REGIONS.get(region, {}).get('name', region)
        self.domain = domain
        self.components = {
            'namespace': {'status': 'pending', 'details': {}},
            'function': {'status': 'pending', 'details': {}},
            'domain': {'status': 'pending', 'details': {}},
            'dns': {'status': 'pending', 'details': {}},
            'ssl': {'status': 'pending', 'details': {}},
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
            f"# Scaleway FaaS Setup Report: {self.name}",
            "",
            f"**Generated:** {self.timestamp}",
            f"**Script:** setup_scaleway_faas.py",
            f"**Infrastructure:** Scaleway Functions",
            "",
            "---",
            "",
            "## Configuration Summary",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Name | `{self.name}` |",
            f"| Region | {self.region} ({self.region_name}) |",
        ]

        if self.domain:
            lines.append(f"| Custom Domain | `{self.domain}` |")

        lines.extend([
            "",
            "## Component Status",
            "",
        ])

        status_icons = {
            'created': '[NEW]', 'exists': '[OK]', 'configured': '[OK]',
            'skipped': '[SKIP]', 'failed': '[FAIL]', 'pending': '[ ]',
        }

        component_names = {
            'namespace': 'Functions Namespace',
            'function': 'Serverless Function',
            'domain': 'Custom Domain',
            'dns': 'DNS Configuration',
            'ssl': 'SSL Certificate',
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
            lines.append("")

        lines.extend([
            "## Deployment Commands",
            "",
            "```bash",
            "# Deploy function code",
            f"scw function deploy --namespace-id <NAMESPACE_ID> --name {self.name}",
            "",
            "# Or using serverless framework with scaleway plugin",
            "serverless deploy --stage production",
            "```",
            "",
            "---",
            "",
            "*Generated by setup_scaleway_faas.py*",
        ])

        return '\n'.join(lines)

    def save_report(self, output_dir: str = '.') -> str:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        filename = f"scaleway-faas-setup-{self.name}-{timestamp}.md"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.generate_markdown())

        logging.info(f"Setup report saved to: {filepath}")
        return filepath

    def write_deploy_env(self, env_file: str = '.deploy.env') -> bool:
        """Write Scaleway configuration to .deploy.env file.

        Appends or updates SCW_FUNCTION_NAMESPACE_ID, SCW_FUNCTION_ID, and SCW_REGION.
        """
        namespace_id = self.components['namespace']['details'].get('namespace_id')
        function_id = self.components['function']['details'].get('function_id')

        if not namespace_id:
            logging.warning("No namespace ID to write to deploy env")
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
            new_vars.append("# Scaleway Functions Configuration")
            new_vars.append(f"# Generated by setup_scaleway_faas.py at {self.timestamp}")
            new_vars.append("")

        # Add/update variables
        scw_vars = {
            'SCW_FUNCTION_NAMESPACE_ID': namespace_id,
            'SCW_REGION': self.region,
        }

        if function_id:
            scw_vars['SCW_FUNCTION_ID'] = function_id

        lines_to_add = []
        for key, value in scw_vars.items():
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
                    f.write('\n# Scaleway Functions (auto-generated)\n')
                for line in lines_to_add:
                    f.write(f"{line}\n")
                    key = line.split('=')[0]
                    logging.info(f"Added {key} to {env_file}")

        logging.info(f"[OK] Deploy env written to: {env_file}")
        return True


# =============================================================================
# Main Setup
# =============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Setup Scaleway Functions (FaaS)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup_scaleway_faas.py --name m3rp-backend --domain api.m3rp.ai
  python setup_scaleway_faas.py --name m3rp-backend --region nl-ams
  python setup_scaleway_faas.py --name m3rp-backend --setup-domain --dns-provider cloudflare

Environment Variables:
  SCW_ACCESS_KEY: Scaleway access key
  SCW_SECRET_KEY: Scaleway secret key
  SCW_DEFAULT_PROJECT_ID: Scaleway project ID
  CLOUDFLARE_API_TOKEN: For Cloudflare DNS
  DESEC_API_TOKEN: For deSEC DNS
  BUNNY_API_KEY: For Bunny.net DNS
        """
    )

    parser.add_argument('--name', required=True, help='Function/namespace name')
    parser.add_argument('--domain', help='Custom domain (e.g., api.m3rp.ai)')
    parser.add_argument('--region', default='fr-par', choices=list(SCALEWAY_REGIONS.keys()),
                        help='Scaleway region (default: fr-par)')
    parser.add_argument('--runtime', default='node20', choices=list(SCALEWAY_RUNTIMES.keys()),
                        help='Function runtime (default: node20)')
    parser.add_argument('--handler', default='handler.handler',
                        help='Function handler (default: handler.handler)')
    parser.add_argument('--memory', type=int, default=512,
                        help='Memory limit in MB (default: 512)')
    parser.add_argument('--timeout', default='30s',
                        help='Function timeout (default: 30s)')
    parser.add_argument('--min-scale', type=int, default=0,
                        help='Minimum instances (default: 0)')
    parser.add_argument('--max-scale', type=int, default=5,
                        help='Maximum instances (default: 5)')
    parser.add_argument('--setup-domain', action='store_true',
                        help='Configure custom domain with DNS')
    parser.add_argument('--dns-provider', choices=['cloudflare', 'desec', 'cloudns', 'bunny'],
                        default='cloudflare', help='DNS provider (default: cloudflare)')
    parser.add_argument('--namespace-only', action='store_true',
                        help='Only create namespace, no function')
    parser.add_argument('--env', action='append', metavar='KEY=VALUE',
                        help='Environment variable (can be used multiple times)')
    parser.add_argument('--secret', action='append', metavar='KEY=VALUE',
                        help='Secret environment variable (can be used multiple times)')
    parser.add_argument('--report-dir', default='.', help='Directory to save the setup report')
    parser.add_argument('--deploy-env', default='.deploy.env',
                        help='Path to .deploy.env file (default: .deploy.env)')
    parser.add_argument('--no-deploy-env', action='store_true',
                        help='Skip writing to .deploy.env file')

    return parser.parse_args()


def parse_env_vars(env_list: List[str]) -> dict:
    """Parse KEY=VALUE environment variable arguments."""
    if not env_list:
        return {}

    result = {}
    for item in env_list:
        if '=' in item:
            key, value = item.split('=', 1)
            result[key] = value
    return result


def parse_secret_vars(secret_list: List[str]) -> List[dict]:
    """Parse KEY=VALUE secret arguments into Scaleway format."""
    if not secret_list:
        return []

    result = []
    for item in secret_list:
        if '=' in item:
            key, value = item.split('=', 1)
            result.append({"key": key, "value": value})
    return result


def main():
    args = parse_arguments()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    # Initialize report
    report = SetupReport(name=args.name, region=args.region, domain=args.domain)

    logging.info("=" * 50)
    logging.info("SCALEWAY FUNCTIONS SETUP")
    logging.info("=" * 50)
    logging.info(f"Name: {args.name}")
    logging.info(f"Region: {args.region} ({SCALEWAY_REGIONS[args.region]['name']})")
    if args.domain:
        logging.info(f"Custom Domain: {args.domain}")
    logging.info(f"Runtime: {args.runtime}")
    logging.info(f"Memory: {args.memory}MB")
    logging.info("=" * 50)

    try:
        # Initialize client
        client = ScalewayFunctionsClient(region=args.region)

        # Parse environment variables
        env_vars = parse_env_vars(args.env)
        secret_vars = parse_secret_vars(args.secret)

        # 1. Create or get namespace
        logging.info("\n--- Step 1: Functions Namespace ---")
        existing_ns = client.find_namespace_by_name(args.name)

        if existing_ns:
            logging.info(f"Namespace '{args.name}' already exists.")
            namespace = existing_ns
            report.set_component('namespace', 'exists',
                                namespace_id=namespace['id'],
                                namespace_name=args.name)
        else:
            namespace = client.create_namespace(
                name=args.name,
                environment_variables=env_vars,
                secret_environment_variables=secret_vars if secret_vars else None
            )
            report.set_component('namespace', 'created',
                                namespace_id=namespace['id'],
                                namespace_name=args.name)

        namespace_id = namespace['id']
        logging.info(f"Namespace ID: {namespace_id}")

        # Wait for namespace to be ready
        logging.info("Waiting for namespace to be ready...")
        for _ in range(30):
            ns_status = client.get_namespace(namespace_id)
            if ns_status.get('status') == 'ready':
                break
            time.sleep(2)

        report.set_url('namespace_url', namespace.get('registry_endpoint', ''))

        if args.namespace_only:
            logging.info("Namespace-only mode, skipping function creation.")
            report.set_component('function', 'skipped')
        else:
            # 2. Create function
            logging.info("\n--- Step 2: Serverless Function ---")
            function_name = f"{args.name}-api"
            existing_fn = client.find_function_by_name(namespace_id, function_name)

            if existing_fn:
                logging.info(f"Function '{function_name}' already exists.")
                function = existing_fn
                report.set_component('function', 'exists',
                                    function_id=function['id'],
                                    function_name=function_name)
            else:
                function = client.create_function(
                    namespace_id=namespace_id,
                    name=function_name,
                    runtime=args.runtime,
                    handler=args.handler,
                    min_scale=args.min_scale,
                    max_scale=args.max_scale,
                    memory_limit=args.memory,
                    timeout=args.timeout,
                    environment_variables=env_vars,
                    secret_environment_variables=secret_vars if secret_vars else None
                )
                report.set_component('function', 'created',
                                    function_id=function['id'],
                                    function_name=function_name,
                                    runtime=args.runtime,
                                    memory=f"{args.memory}MB")

            function_id = function['id']
            function_url = function.get('domain_name', '')

            logging.info(f"Function ID: {function_id}")
            logging.info(f"Function URL: https://{function_url}")

            report.set_url('function_url', f"https://{function_url}")

            # 3. Configure custom domain
            if args.domain:
                logging.info("\n--- Step 3: Custom Domain ---")

                existing_domains = client.list_domains(function_id)
                domain_exists = any(d.get('hostname') == args.domain for d in existing_domains)

                if domain_exists:
                    logging.info(f"Domain '{args.domain}' already configured.")
                    report.set_component('domain', 'exists', hostname=args.domain)
                else:
                    domain_result = client.create_domain(function_id, args.domain)
                    report.set_component('domain', 'created',
                                        hostname=args.domain,
                                        domain_id=domain_result.get('id'))

                report.set_url('custom_domain', f"https://{args.domain}")

                # 4. Configure DNS
                if args.setup_domain:
                    logging.info("\n--- Step 4: DNS Configuration ---")

                    # Extract base domain
                    domain_parts = args.domain.split('.')
                    if len(domain_parts) > 2:
                        base_domain = '.'.join(domain_parts[-2:])
                        subdomain = '.'.join(domain_parts[:-2])
                    else:
                        base_domain = args.domain
                        subdomain = '@'

                    dns_provider = get_dns_provider(args.dns_provider)
                    zone_id = dns_provider.get_zone_id(base_domain)

                    # Create CNAME to Scaleway function URL
                    dns_provider.create_cname_record(
                        zone_id, subdomain, function_url, base_domain
                    )

                    report.set_component('dns', 'configured',
                                        provider=args.dns_provider,
                                        record=f"CNAME {args.domain} -> {function_url}")

                    # SSL is automatic with Scaleway
                    report.set_component('ssl', 'configured',
                                        note="Automatic SSL via Scaleway")
                else:
                    report.set_component('dns', 'skipped',
                                        note="Use --setup-domain to configure DNS")
                    report.set_component('ssl', 'skipped')
            else:
                report.set_component('domain', 'skipped')
                report.set_component('dns', 'skipped')
                report.set_component('ssl', 'skipped')

        # Summary
        logging.info("\n" + "=" * 50)
        logging.info("SCALEWAY FAAS SETUP COMPLETE")
        logging.info("=" * 50)
        logging.info(f"Namespace: {args.name} (ID: {namespace_id})")
        if not args.namespace_only:
            logging.info(f"Function URL: https://{function_url}")
            if args.domain:
                logging.info(f"Custom Domain: https://{args.domain}")
        logging.info("=" * 50)

        # Save report
        report_path = report.save_report(args.report_dir)

        # Write to .deploy.env
        if not args.no_deploy_env:
            report.write_deploy_env(args.deploy_env)

        # Print next steps
        logging.info("\n[Next Steps]")
        logging.info("1. Deploy your function code using the Scaleway CLI or API")
        logging.info("2. Or configure serverless-scaleway-functions plugin")
        logging.info(f"\n[OK] Setup report: {report_path}")

        return 0

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
