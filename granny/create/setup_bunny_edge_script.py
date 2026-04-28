#!/usr/bin/env python3
"""
Bunny Edge Scripting Setup Script

Creates and deploys serverless edge scripts on Bunny.net's Edge Scripting
platform. Standalone scripts get an auto-created pull zone; middleware scripts
attach to an existing pull zone.

Bunny Edge Scripts run on Deno/V8 with standard Web APIs (fetch, Request,
Response). TypeScript is supported natively.

FEATURES:
---------
- Creates standalone or middleware edge scripts
- Deploys code from a local file
- Configures environment variables and secrets
- Adds custom hostnames to the linked pull zone
- Publishes releases with optional notes
- Supports manual or automated DNS configuration

Environment Variables:
    BUNNY_API_KEY: Bunny.net API key (from https://panel.bunny.net/account)

Usage Examples:
    # Create a standalone edge script with a linked pull zone
    granny create bunny-edge-script --name my-api --code handler.ts

    # Create and bind a custom domain (manual DNS)
    granny create bunny-edge-script --name my-api --code handler.ts \\
        --hostname api.example.com --domain example.com --dns-provider manual

    # Set environment variables and secrets
    granny create bunny-edge-script --name my-api --code handler.ts \\
        --env NODE_ENV=production --env LOG_LEVEL=info \\
        --secret API_KEY=sk-xxx --secret DB_PASSWORD=hunter2

    # Create a middleware script on an existing pull zone
    granny create bunny-edge-script --name my-middleware --code mw.ts \\
        --type middleware --pull-zone-id 12345
"""

import os
import re
import time
import logging
import argparse
import requests
from typing import Optional
from abc import ABC, abstractmethod

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass


# =============================================================================
# Bunny Edge Scripting Configuration
# =============================================================================

BUNNY_API_BASE = "https://api.bunny.net"
EDGE_SCRIPTING_API = "/compute/script"

SCRIPT_TYPES = {
    'standalone': 0,   # Independent HTTP handler, gets its own pull zone
    'middleware': 1,    # Intercepts req/res on an existing pull zone
}


# =============================================================================
# DNS Provider Abstraction (same pattern as other setup scripts)
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


class ManualDNSProvider(DNSProvider):
    """No-op DNS provider that prints instructions for the operator.

    Use when DNS is managed at a provider without API support (e.g.
    easyname.eu). The script creates the edge script and pull zone as
    usual, but instead of touching DNS it prints the exact record to add.
    """

    def __init__(self):
        self._pending_records: list[str] = []

    def get_zone_id(self, zone_name: str) -> str:
        return "manual"

    def create_cname_record(self, zone_id: str, name: str, target: str,
                           zone_name: str, ttl: int = 300) -> None:
        fqdn = f"{name}.{zone_name}" if name and name != '@' else zone_name
        record = f"CNAME  {fqdn}.  ->  {target.rstrip('.')}.  (TTL {ttl})"
        self._pending_records.append(record)
        logging.warning("=" * 70)
        logging.warning("MANUAL DNS ACTION REQUIRED")
        logging.warning("Add the following record at your DNS provider:")
        logging.warning("  %s", record)
        logging.warning("SSL will auto-issue once the record propagates.")
        logging.warning("=" * 70)


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


class BunnyDNSProvider(DNSProvider):
    """Bunny.net DNS API adapter."""

    API_BASE = "https://api.bunny.net"
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
                logging.info("CNAME already correct.")
            else:
                self._api_request("POST", f"/dnszone/{zone_id}/records/{existing_cname['Id']}",
                                {"Type": self.RECORD_TYPE_CNAME, "Name": name, "Value": target, "Ttl": ttl})
                logging.info("CNAME updated.")
        else:
            self._api_request("PUT", f"/dnszone/{zone_id}/records",
                            {"Type": self.RECORD_TYPE_CNAME, "Name": name, "Value": target, "Ttl": ttl})
            logging.info("CNAME created.")


def get_dns_provider(provider_name: str) -> DNSProvider:
    """Factory function to get the appropriate DNS provider."""
    providers = {
        'manual': ManualDNSProvider,
        'cloudflare': CloudflareDNSProvider,
        'bunny': BunnyDNSProvider,
    }
    if provider_name not in providers:
        raise ValueError(f"Unknown DNS provider: {provider_name}")
    return providers[provider_name]()


# =============================================================================
# Bunny Edge Scripting Client
# =============================================================================

class BunnyEdgeScriptClient:
    """Client for Bunny.net Edge Scripting API.

    API reference: https://docs.bunny.net/docs/edge-scripting-overview
    Endpoints live under /api/v1/edgescripting.
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("BUNNY_API_KEY")
        if not self.api_key:
            raise ValueError("BUNNY_API_KEY environment variable not set.")

        self.session = requests.Session()
        self.session.headers.update({
            "AccessKey": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _api(self, method: str, path: str, data=None) -> dict:
        """Make an API request. path is relative to BUNNY_API_BASE."""
        url = f"{BUNNY_API_BASE}{path}"
        kwargs: dict = {"timeout": 60}
        if data is not None:
            kwargs["json"] = data

        response = getattr(self.session, method.lower())(url, **kwargs)

        if response.status_code >= 400:
            raise Exception(
                f"Bunny API {method} {path} -> {response.status_code}: "
                f"{response.text[:500]}"
            )

        if response.text and response.status_code not in [204]:
            return response.json()
        return {}

    # -- Pull Zone helpers (for custom hostnames + SSL) -----------------------

    def get_pull_zone(self, pull_zone_id: int) -> dict:
        return self._api("GET", f"/pullzone/{pull_zone_id}")

    def add_hostname(self, pull_zone_id: int, hostname: str) -> None:
        """Add a custom hostname to a pull zone."""
        logging.info(f"Adding hostname '{hostname}' to pull zone {pull_zone_id}")
        self._api("POST", f"/pullzone/{pull_zone_id}/addHostname",
                  {"Hostname": hostname})

    def load_free_certificate(self, hostname: str) -> int:
        """Request Let's Encrypt certificate. Returns HTTP status code."""
        url = f"{BUNNY_API_BASE}/pullzone/loadFreeCertificate?hostname={hostname}"
        r = self.session.get(url, timeout=30)
        return r.status_code

    # -- Edge Script CRUD -----------------------------------------------------

    def list_scripts(self, search: str = None) -> list[dict]:
        """List all edge scripts, optionally filtered by name."""
        params = ""
        if search:
            params = f"?search={requests.utils.quote(search)}"
        result = self._api("GET", f"{EDGE_SCRIPTING_API}{params}")
        # API returns paginated list; items are in the root list or under a key
        if isinstance(result, list):
            return result
        return result.get("Items", result.get("items", []))

    def find_script_by_name(self, name: str) -> Optional[dict]:
        """Find an edge script by exact name match."""
        scripts = self.list_scripts(search=name)
        for s in scripts:
            if s.get("Name") == name:
                return s
        return None

    def get_script(self, script_id: int) -> dict:
        return self._api("GET", f"{EDGE_SCRIPTING_API}/{script_id}")

    def create_script(self, name: str, code: str = "",
                      script_type: int = 0,
                      create_linked_pull_zone: bool = True,
                      linked_pull_zone_name: str = None,
                      integration_id: int = None) -> dict:
        """Create a new edge script.

        script_type: 0 = standalone, 1 = middleware
        """
        payload: dict = {
            "Name": name,
            "ScriptType": script_type,
        }

        if code:
            payload["Code"] = code

        if script_type == 0:
            # Standalone: create a linked pull zone
            payload["CreateLinkedPullZone"] = create_linked_pull_zone
            if linked_pull_zone_name:
                payload["LinkedPullZoneName"] = linked_pull_zone_name

        if integration_id is not None:
            payload["IntegrationId"] = integration_id

        logging.info(f"Creating edge script: {name} (type={'standalone' if script_type == 0 else 'middleware'})")
        result = self._api("POST", EDGE_SCRIPTING_API, payload)
        logging.info(f"Edge script created. ID: {result.get('Id')}")
        return result

    def update_script(self, script_id: int, **kwargs) -> dict:
        """Update script metadata (Name, ScriptType)."""
        return self._api("PUT", f"{EDGE_SCRIPTING_API}/{script_id}", kwargs)

    def delete_script(self, script_id: int, delete_linked_pull_zones: bool = False) -> None:
        params = f"?deleteLinkedPullZones={'true' if delete_linked_pull_zones else 'false'}"
        self._api("DELETE", f"{EDGE_SCRIPTING_API}/{script_id}{params}")
        logging.info(f"Edge script {script_id} deleted.")

    # -- Code deployment ------------------------------------------------------

    def get_code(self, script_id: int) -> str:
        result = self._api("GET", f"{EDGE_SCRIPTING_API}/{script_id}/code")
        return result.get("Code", "")

    def deploy_code(self, script_id: int, code: str) -> dict:
        """Push code to the edge script. Creates a new release."""
        logging.info(f"Deploying code to edge script {script_id} ({len(code)} bytes)")
        return self._api("POST", f"{EDGE_SCRIPTING_API}/{script_id}/code",
                         {"Code": code})

    # -- Releases -------------------------------------------------------------

    def list_releases(self, script_id: int) -> list[dict]:
        result = self._api("GET", f"{EDGE_SCRIPTING_API}/{script_id}/releases")
        if isinstance(result, list):
            return result
        return result.get("Items", result.get("items", []))

    def publish(self, script_id: int) -> None:
        """Publish the current code. Creates a release and makes it live."""
        logging.info(f"Publishing edge script {script_id}...")
        # Bunny requires Content-Type even for empty POST bodies
        self._api("POST", f"{EDGE_SCRIPTING_API}/{script_id}/publish", {})

    # -- Environment variables (includes secrets) ----------------------------
    #
    # Bunny Edge Scripting uses a single variables endpoint. There is no
    # separate secrets API — all config is stored as variables and accessed
    # via process.env / Deno.env in the script.

    def list_variables(self, script_id: int) -> list[dict]:
        """List variables. Also available as EdgeScriptVariables on GET script."""
        detail = self.get_script(script_id)
        return detail.get("EdgeScriptVariables", [])

    def upsert_variable(self, script_id: int, name: str, default_value: str,
                        required: bool = True) -> dict:
        """Create or update an environment variable (PUT = upsert)."""
        return self._api("PUT", f"{EDGE_SCRIPTING_API}/{script_id}/variables",
                         {"Name": name, "DefaultValue": default_value,
                          "Required": required})

    def delete_variable(self, script_id: int, var_id: int) -> None:
        self._api("DELETE", f"{EDGE_SCRIPTING_API}/{script_id}/variables/{var_id}")


# =============================================================================
# Setup Report
# =============================================================================

class SetupReport:
    """Track and report edge script setup status."""

    def __init__(self, name: str, domain: str = None):
        self.timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        self.name = name
        self.domain = domain
        self.components = {
            'script': {'status': 'pending', 'details': {}},
            'code': {'status': 'pending', 'details': {}},
            'variables': {'status': 'pending', 'details': {}},
            'secrets': {'status': 'pending', 'details': {}},
            'pull_zone': {'status': 'pending', 'details': {}},
            'hostname': {'status': 'pending', 'details': {}},
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
            f"# Bunny Edge Script Setup Report: {self.name}",
            "",
            f"**Generated:** {self.timestamp}",
            "**Script:** setup_bunny_edge_script.py",
            "**Infrastructure:** Bunny.net Edge Scripting",
            "",
            "---",
            "",
            "## Configuration Summary",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Name | `{self.name}` |",
        ]

        if self.domain:
            lines.append(f"| Custom Domain | `{self.domain}` |")

        lines.extend(["", "## Component Status", ""])

        status_icons = {
            'created': '[NEW]', 'exists': '[OK]', 'configured': '[OK]',
            'deployed': '[OK]', 'published': '[OK]',
            'skipped': '[SKIP]', 'failed': '[FAIL]', 'pending': '[ ]',
        }

        component_names = {
            'script': 'Edge Script',
            'code': 'Code Deployment',
            'variables': 'Environment Variables',
            'secrets': 'Secrets',
            'pull_zone': 'Linked Pull Zone',
            'hostname': 'Custom Hostname',
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
            "# Re-deploy code",
            f"granny create bunny-edge-script --name {self.name} --code handler.ts",
            "```",
            "",
            "---",
            "",
            "*Generated by setup_bunny_edge_script.py*",
        ])

        return '\n'.join(lines)

    def save_report(self, output_dir: str = '.') -> str:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        filename = f"bunny-edge-script-{self.name}-{timestamp}.md"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.generate_markdown())

        logging.info(f"Setup report saved to: {filepath}")
        return filepath

    def write_deploy_env(self, env_file: str = '.deploy.env') -> bool:
        """Write edge script configuration to .deploy.env file."""
        script_id = self.components['script']['details'].get('script_id')
        pull_zone_id = self.components['pull_zone']['details'].get('pull_zone_id')

        if not script_id:
            logging.warning("No script ID to write to deploy env")
            return False

        existing_content = ""
        existing_vars: dict[str, bool] = {}
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                existing_content = f.read()
                for line in existing_content.split('\n'):
                    if '=' in line and not line.strip().startswith('#'):
                        key = line.split('=')[0].strip()
                        existing_vars[key] = True

        new_vars = {
            'BUNNY_EDGE_SCRIPT_ID': str(script_id),
        }
        if pull_zone_id:
            new_vars['BUNNY_EDGE_PULL_ZONE_ID'] = str(pull_zone_id)

        lines_to_add = []
        for key, value in new_vars.items():
            if key in existing_vars:
                pattern = rf'^{key}=.*$'
                existing_content = re.sub(pattern, f'{key}={value}',
                                          existing_content, flags=re.MULTILINE)
                logging.info(f"Updated {key} in {env_file}")
            else:
                lines_to_add.append(f"{key}={value}")

        with open(env_file, 'w') as f:
            if existing_content.strip():
                f.write(existing_content)
                if not existing_content.endswith('\n'):
                    f.write('\n')

            if lines_to_add:
                if existing_content.strip():
                    f.write('\n# Bunny Edge Scripting (auto-generated)\n')
                for line in lines_to_add:
                    f.write(f"{line}\n")
                    key = line.split('=')[0]
                    logging.info(f"Added {key} to {env_file}")

        logging.info(f"[OK] Deploy env written to: {env_file}")
        return True


# =============================================================================
# Argument Parsing
# =============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='Setup Bunny.net Edge Scripts',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standalone script with linked pull zone
  granny create bunny-edge-script --name my-api --code handler.ts

  # With custom domain (manual DNS)
  granny create bunny-edge-script --name my-api --code handler.ts \\
      --hostname api.example.com --domain example.com --dns-provider manual

  # Middleware on existing pull zone
  granny create bunny-edge-script --name my-mw --code mw.ts \\
      --type middleware --pull-zone-id 12345

  # Set env vars and secrets
  granny create bunny-edge-script --name my-api --code handler.ts \\
      --env NODE_ENV=production --secret API_KEY=sk-xxx

Environment Variables:
  BUNNY_API_KEY: Bunny.net API key (required)
  CLOUDFLARE_API_TOKEN: For Cloudflare DNS (if --dns-provider cloudflare)
        """
    )

    parser.add_argument('--name', required=True,
                        help='Edge script name')
    parser.add_argument('--code',
                        help='Path to the script file to deploy (TypeScript or JavaScript)')
    parser.add_argument('--type', choices=list(SCRIPT_TYPES.keys()),
                        default='standalone',
                        help='Script type (default: standalone)')
    parser.add_argument('--pull-zone-id', type=int,
                        help='Existing pull zone ID (required for middleware, '
                             'optional for standalone to skip auto-creation)')
    parser.add_argument('--pull-zone-name',
                        help='Name for the auto-created pull zone '
                             '(standalone only, default: same as --name)')

    # Custom domain
    parser.add_argument('--hostname',
                        help='Custom hostname to bind (e.g. api.example.com)')
    parser.add_argument('--domain',
                        help='Root domain for DNS zone lookup (e.g. example.com). '
                             'Required with --hostname when using automated DNS.')
    parser.add_argument('--dns-provider',
                        choices=['manual', 'cloudflare', 'bunny'],
                        default='manual',
                        help='DNS provider (default: manual)')

    # Environment
    parser.add_argument('--env', action='append', metavar='KEY=VALUE',
                        help='Environment variable (can be repeated)')
    parser.add_argument('--secret', action='append', metavar='KEY=VALUE',
                        help='Secret (can be repeated, write-only)')

    # Publish
    parser.add_argument('--publish', action='store_true',
                        help='Publish the release after deploying code')
    parser.add_argument('--publish-note', default='',
                        help='Release note (used with --publish)')

    # Output
    parser.add_argument('--report-dir', default='.',
                        help='Directory to save the setup report')
    parser.add_argument('--deploy-env', default='.deploy.env',
                        help='Path to .deploy.env file (default: .deploy.env)')
    parser.add_argument('--no-deploy-env', action='store_true',
                        help='Skip writing to .deploy.env file')

    return parser.parse_args()


def parse_key_value_list(items: list[str] | None) -> dict[str, str]:
    """Parse KEY=VALUE arguments into a dict."""
    if not items:
        return {}
    result = {}
    for item in items:
        if '=' in item:
            key, value = item.split('=', 1)
            result[key.strip()] = value
    return result


# =============================================================================
# Main Setup
# =============================================================================

def main():
    args = parse_arguments()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    report = SetupReport(name=args.name, domain=args.hostname)

    script_type_int = SCRIPT_TYPES[args.type]

    logging.info("=" * 50)
    logging.info("BUNNY EDGE SCRIPTING SETUP")
    logging.info("=" * 50)
    logging.info(f"Name: {args.name}")
    logging.info(f"Type: {args.type}")
    if args.code:
        logging.info(f"Code: {args.code}")
    if args.hostname:
        logging.info(f"Hostname: {args.hostname}")
    logging.info(f"DNS Provider: {args.dns_provider}")
    logging.info("=" * 50)

    try:
        client = BunnyEdgeScriptClient()

        # ---- Step 1: Create or find edge script ----------------------------
        logging.info("\n--- Step 1: Edge Script ---")

        existing = client.find_script_by_name(args.name)
        if existing:
            script = existing
            script_id = script["Id"]
            logging.info(f"Edge script '{args.name}' already exists. ID: {script_id}")
            report.set_component('script', 'exists', script_id=script_id)
        else:
            pull_zone_name = args.pull_zone_name or args.name
            script = client.create_script(
                name=args.name,
                script_type=script_type_int,
                create_linked_pull_zone=(args.type == 'standalone' and args.pull_zone_id is None),
                linked_pull_zone_name=pull_zone_name,
            )
            script_id = script["Id"]
            report.set_component('script', 'created', script_id=script_id)

        # Find the linked pull zone ID and default hostname
        pull_zone_id = args.pull_zone_id
        cdn_hostname = ""
        default_hostname = ""

        # Re-fetch full details (create response already has them, but
        # if we found an existing script we need them too)
        script_detail = client.get_script(script_id)
        default_hostname = (script_detail.get("DefaultHostname") or "").replace("https://", "")
        system_hostname = (script_detail.get("SystemHostname") or "").replace("https://", "")

        if pull_zone_id is None:
            linked_pz = script_detail.get("LinkedPullZones", [])
            if linked_pz:
                pull_zone_id = linked_pz[0].get("Id")

        cdn_hostname = system_hostname or default_hostname

        if pull_zone_id:
            logging.info(f"Linked pull zone: {pull_zone_id}")
            logging.info(f"Default hostname: {default_hostname}")
            logging.info(f"System hostname:  {system_hostname}")
            report.set_component('pull_zone', 'configured',
                                pull_zone_id=pull_zone_id,
                                default_hostname=default_hostname,
                                system_hostname=system_hostname)
            report.set_url('default_url', f"https://{default_hostname}")
        else:
            logging.warning("No linked pull zone found.")
            report.set_component('pull_zone', 'skipped')

        # ---- Step 2: Deploy code -------------------------------------------
        if args.code:
            logging.info("\n--- Step 2: Code Deployment ---")

            code_path = os.path.abspath(args.code)
            if not os.path.isfile(code_path):
                raise FileNotFoundError(f"Code file not found: {code_path}")

            with open(code_path, 'r', encoding='utf-8') as f:
                code = f.read()

            client.deploy_code(script_id, code)
            report.set_component('code', 'deployed',
                                file=args.code, size_bytes=len(code))
            logging.info(f"Code deployed ({len(code)} bytes)")

            # Publish if requested
            if args.publish:
                client.publish(script_id)
                report.set_component('code', 'published', file=args.code)
                logging.info("Release published.")
        else:
            logging.info("\n--- Step 2: Code Deployment (skipped, no --code) ---")
            report.set_component('code', 'skipped')

        # ---- Step 3: Environment variables ---------------------------------
        env_vars = parse_key_value_list(args.env)
        if env_vars:
            logging.info("\n--- Step 3: Environment Variables ---")
            var_names = []
            for key, value in env_vars.items():
                client.upsert_variable(script_id, key, value, required=True)
                logging.info(f"  {key} = {value}")
                var_names.append(key)
            report.set_component('variables', 'configured', variables=var_names)
        else:
            report.set_component('variables', 'skipped')

        # ---- Step 4: Secrets (stored as variables — no separate API) --------
        secret_vars = parse_key_value_list(args.secret)
        if secret_vars:
            logging.info("\n--- Step 4: Secrets (as variables) ---")
            secret_names = []
            for key, value in secret_vars.items():
                client.upsert_variable(script_id, key, value, required=True)
                logging.info(f"  {key} = ****")
                secret_names.append(key)
            report.set_component('secrets', 'configured', secrets=secret_names)
            logging.warning("Note: Bunny Edge Scripting stores secrets as "
                          "plain variables. Values are visible in the dashboard.")
        else:
            report.set_component('secrets', 'skipped')

        # ---- Step 5: Custom hostname ---------------------------------------
        if args.hostname and pull_zone_id:
            logging.info(f"\n--- Step 5: Custom Hostname ({args.hostname}) ---")

            # Check if hostname already exists on the pull zone
            pz = client.get_pull_zone(pull_zone_id)
            existing_hostnames = [h.get("Value") for h in pz.get("Hostnames", [])]

            if args.hostname in existing_hostnames:
                logging.info(f"Hostname '{args.hostname}' already bound.")
                report.set_component('hostname', 'exists', hostname=args.hostname)
            else:
                client.add_hostname(pull_zone_id, args.hostname)
                report.set_component('hostname', 'configured',
                                    hostname=args.hostname)
            report.set_url('custom_url', f"https://{args.hostname}")

            # ---- Step 6: DNS -----------------------------------------------
            logging.info(f"\n--- Step 6: DNS ({args.dns_provider}) ---")

            # The CNAME target is the pull zone's default b-cdn.net hostname
            cname_target = cdn_hostname if cdn_hostname else f"{args.name}.b-cdn.net"

            # Extract subdomain from hostname
            if args.domain:
                subdomain = args.hostname.replace(f".{args.domain}", "")
            else:
                # Guess: hostname = sub.domain.tld -> subdomain = sub
                parts = args.hostname.split('.')
                subdomain = parts[0] if len(parts) > 2 else args.hostname
                args.domain = '.'.join(parts[1:]) if len(parts) > 2 else args.hostname

            dns = get_dns_provider(args.dns_provider)
            zone_id = dns.get_zone_id(args.domain)
            dns.create_cname_record(zone_id, subdomain, cname_target, args.domain)
            report.set_component('dns', 'configured',
                                provider=args.dns_provider,
                                subdomain=subdomain,
                                target=cname_target)

            # ---- Step 7: SSL -----------------------------------------------
            logging.info("\n--- Step 7: SSL Certificate ---")
            if args.dns_provider == 'manual':
                logging.info("SSL will auto-issue once the CNAME record propagates.")
                report.set_component('ssl', 'skipped',
                                    note='Add CNAME first, then request cert')
            else:
                logging.info("Waiting 15 seconds for DNS propagation...")
                time.sleep(15)
                status = client.load_free_certificate(args.hostname)
                if status == 200:
                    logging.info("SSL certificate issued.")
                    report.set_component('ssl', 'configured', hostname=args.hostname)
                else:
                    logging.warning(f"SSL cert request returned {status}. "
                                    "Retry after DNS propagation.")
                    report.set_component('ssl', 'pending',
                                        note=f'HTTP {status} — retry after DNS propagates')
        else:
            if args.hostname and not pull_zone_id:
                logging.warning("Cannot add hostname: no pull zone available.")
            report.set_component('hostname', 'skipped')
            report.set_component('dns', 'skipped')
            report.set_component('ssl', 'skipped')

        # ---- Summary -------------------------------------------------------
        logging.info("")
        logging.info("=" * 50)
        logging.info("EDGE SCRIPT SETUP COMPLETE")
        logging.info("=" * 50)
        logging.info(f"Script Name:  {args.name}")
        logging.info(f"Script ID:    {script_id}")
        if pull_zone_id:
            logging.info(f"Pull Zone:    {pull_zone_id}")
        if default_hostname:
            logging.info(f"Default URL:  https://{default_hostname}")
        if args.hostname:
            logging.info(f"Custom URL:   https://{args.hostname}")
        logging.info("=" * 50)

        # Save report
        report.save_report(args.report_dir)

        # Write .deploy.env
        if not args.no_deploy_env:
            report.write_deploy_env(args.deploy_env)

        return 0

    except Exception as e:
        logging.error(f"Setup failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
