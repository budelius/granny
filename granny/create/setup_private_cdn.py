#!/usr/bin/env python3
"""Private CDN Setup: Hetzner S3 + Bunny CDN with Token Authentication.

Creates a private file storage setup where all access requires signed URLs:
  1. Hetzner S3 bucket (private, no public access)
  2. Bunny CDN pull zone with Token Authentication
  3. DNS CNAME via Cloudflare
  4. Free SSL certificate

Files are only accessible via time-limited signed URLs generated server-side.
This is ideal for invoices, user uploads, private documents, etc.

Architecture:
    App (JWT auth) → generate signed URL → user browser
    Browser → https://cdn.example.com/path?token=<hash>&expires=<ts>
    Bunny CDN (validates token) → Hetzner S3 (private origin)

Environment Variables (from Vaultwarden or .env):
    HETZNER_S3_ACCESS_KEY   - Hetzner S3 access key
    HETZNER_S3_SECRET_KEY   - Hetzner S3 secret key
    BUNNY_API_KEY           - Bunny.net API key
    CLOUDFLARE_API_TOKEN    - Cloudflare DNS token (optional, for auto DNS)

Usage:
    # Dry-run: show what would be created
    python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn --dry-run

    # Create everything
    python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn

    # Use existing bucket
    python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn --existing-bucket

    # Custom CORS origins
    python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn \\
        --cors-origins "https://app.example.com,http://localhost:3000"

    # Export Vaultwarden CSV for secret import
    python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn \\
        --vault-csv --vault-folder "myproject/infra"

Output:
    - Hetzner S3 bucket (private, CORS configured)
    - Bunny CDN pull zone with token auth + origin shield
    - DNS CNAME record (if Cloudflare token provided)
    - SSL certificate (free, via Bunny)
    - Token key for signed URL generation
    - Optional: Vaultwarden CSV for import
"""

import argparse
import csv
import io
import json
import logging
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Load secrets via granny's resolution chain (no-op if already in env).
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env(["HETZNER_S3_ACCESS_KEY", "HETZNER_S3_SECRET_KEY",
                           "BUNNY_API_KEY", "CLOUDFLARE_API_TOKEN"])
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8.8s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUNNY_API = "https://api.bunny.net"

HETZNER_REGIONS = {
    "fsn1": "fsn1.your-objectstorage.com",
    "nbg1": "nbg1.your-objectstorage.com",
    "hel1": "hel1.your-objectstorage.com",
}

DEFAULT_CORS_ORIGINS = ["http://localhost:3000", "http://localhost:5173"]


# ---------------------------------------------------------------------------
# Bunny CDN helpers
# ---------------------------------------------------------------------------

def bunny_request(method: str, endpoint: str, data: dict = None) -> dict:
    """Make a Bunny CDN API request."""
    import requests
    headers = {
        "AccessKey": os.environ["BUNNY_API_KEY"],
        "Content-Type": "application/json",
    }
    url = f"{BUNNY_API}{endpoint}"
    resp = requests.request(method, url, headers=headers, json=data)
    if resp.status_code >= 400:
        raise Exception(f"Bunny API {method} {endpoint}: {resp.status_code} - {resp.text}")
    return resp.json() if resp.text and resp.status_code != 204 else {}


def find_pull_zone(name: str = None, hostname: str = None) -> dict | None:
    """Find existing pull zone by name or hostname."""
    zones = bunny_request("GET", "/pullzone")
    for z in zones:
        if name and z.get("Name") == name:
            return z
        if hostname:
            for h in z.get("Hostnames", []):
                if h.get("Value") == hostname:
                    return z
    return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def check_env() -> bool:
    """Verify required env vars are set."""
    required = {
        "HETZNER_S3_ACCESS_KEY": "Hetzner S3 access key",
        "HETZNER_S3_SECRET_KEY": "Hetzner S3 secret key",
        "BUNNY_API_KEY": "Bunny.net API key",
    }
    missing = [f"  {k}: {v}" for k, v in required.items() if not os.getenv(k)]
    if missing:
        logger.error("Missing required environment variables:")
        for m in missing:
            logger.error(m)
        logger.error("\nSet them in .env or via Vaultwarden.")
        return False
    return True


def step_create_bucket(bucket: str, region: str, cors_origins: list[str],
                       dry_run: bool = False) -> bool:
    """Create private Hetzner S3 bucket with CORS."""
    import boto3
    from botocore.config import Config as BotoConfig

    endpoint = f"https://{HETZNER_REGIONS[region]}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["HETZNER_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["HETZNER_S3_SECRET_KEY"],
        region_name=region,
        config=BotoConfig(signature_version="s3v4"),
    )

    try:
        client.head_bucket(Bucket=bucket)
        logger.info(f"Bucket '{bucket}' already exists.")
        return True
    except Exception:
        pass

    if dry_run:
        logger.info(f"[DRY RUN] Would create private bucket '{bucket}' in {region}")
        logger.info(f"[DRY RUN] CORS origins: {cors_origins}")
        return False

    logger.info(f"Creating bucket '{bucket}' in {region}...")
    client.create_bucket(Bucket=bucket)
    time.sleep(3)

    client.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [{
                "AllowedHeaders": ["*"],
                "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
                "AllowedOrigins": cors_origins,
                "ExposeHeaders": ["ETag", "x-amz-request-id"],
                "MaxAgeSeconds": 3600,
            }]
        },
    )
    logger.info(f"Bucket '{bucket}' created (private, CORS for {len(cors_origins)} origins).")
    return True


def step_create_pull_zone(bucket: str, region: str, hostname: str,
                          dry_run: bool = False) -> tuple[int | None, str | None]:
    """Create Bunny CDN pull zone with token authentication."""
    origin_url = f"https://{bucket}.{HETZNER_REGIONS[region]}"
    pull_zone_name = bucket

    existing = find_pull_zone(name=pull_zone_name) or find_pull_zone(hostname=hostname)
    if existing:
        pz_id = existing["Id"]
        logger.info(f"Pull zone exists: {pull_zone_name} (ID: {pz_id})")
    elif dry_run:
        logger.info(f"[DRY RUN] Would create pull zone '{pull_zone_name}' -> {origin_url}")
        logger.info(f"[DRY RUN] Would enable token authentication")
        logger.info(f"[DRY RUN] Would add hostname '{hostname}'")
        return None, None
    else:
        logger.info(f"Creating pull zone '{pull_zone_name}' -> {origin_url}")
        result = bunny_request("POST", "/pullzone", {
            "Name": pull_zone_name,
            "OriginUrl": origin_url,
            "Type": 0,
            "EnableGeoZoneUS": True,
            "EnableGeoZoneEU": True,
            "EnableGeoZoneASIA": True,
        })
        pz_id = result["Id"]
        logger.info(f"Pull zone created (ID: {pz_id})")

    if dry_run:
        return pz_id, None

    # Add custom hostname
    try:
        bunny_request("POST", f"/pullzone/{pz_id}/addHostname", {"Hostname": hostname})
        logger.info(f"Hostname '{hostname}' added.")
    except Exception as e:
        if "already" in str(e).lower():
            logger.info(f"Hostname '{hostname}' already registered.")
        else:
            raise

    # Enable token authentication
    logger.info("Enabling token authentication...")
    bunny_request("POST", f"/pullzone/{pz_id}", {
        "ZoneSecurityEnabled": True,
        "ZoneSecurityIncludeHashRemoteIP": False,
    })

    # Enable origin shield (block direct S3 access)
    logger.info("Enabling origin shield...")
    bunny_request("POST", f"/pullzone/{pz_id}", {
        "EnableOriginShield": True,
        "OriginShieldZoneCode": "DE",
    })

    # Fetch token key
    details = bunny_request("GET", f"/pullzone/{pz_id}")
    token_key = details.get("ZoneSecurityKey", "")
    logger.info(f"Token auth enabled. Key: {token_key[:8]}...{token_key[-4:]}")
    return pz_id, token_key


def step_setup_dns(hostname: str, domain: str, pull_zone_name: str,
                   dry_run: bool = False) -> bool:
    """Create Cloudflare CNAME for CDN hostname."""
    import requests

    token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not token:
        logger.warning("CLOUDFLARE_API_TOKEN not set — skipping DNS.")
        logger.warning(f"Create CNAME manually: {hostname} -> {pull_zone_name}.b-cdn.net")
        return False

    cf = "https://api.cloudflare.com/client/v4"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = requests.get(f"{cf}/zones?name={domain}", headers=headers)
    zones = resp.json().get("result", [])
    if not zones:
        logger.warning(f"Cloudflare zone '{domain}' not found.")
        return False

    zone_id = zones[0]["id"]
    target = f"{pull_zone_name}.b-cdn.net"

    if dry_run:
        logger.info(f"[DRY RUN] Would create CNAME: {hostname} -> {target}")
        return True

    resp = requests.get(
        f"{cf}/zones/{zone_id}/dns_records?type=CNAME&name={hostname}", headers=headers
    )
    existing = resp.json().get("result", [])
    data = {"type": "CNAME", "name": hostname, "content": target, "proxied": False, "ttl": 1}

    if existing:
        requests.put(f"{cf}/zones/{zone_id}/dns_records/{existing[0]['id']}",
                     headers=headers, json=data)
        logger.info(f"DNS updated: {hostname} -> {target}")
    else:
        requests.post(f"{cf}/zones/{zone_id}/dns_records", headers=headers, json=data)
        logger.info(f"DNS created: {hostname} -> {target}")
    return True


def step_request_ssl(pull_zone_id: int, hostname: str, dry_run: bool = False) -> bool:
    """Request free SSL certificate from Bunny CDN."""
    if dry_run:
        logger.info(f"[DRY RUN] Would request SSL for {hostname}")
        return True
    import requests
    headers = {"AccessKey": os.environ["BUNNY_API_KEY"]}
    resp = requests.get(
        f"{BUNNY_API}/pullzone/{pull_zone_id}/loadFreeCertificate?hostname={hostname}",
        headers=headers,
    )
    if resp.status_code in (200, 204):
        logger.info(f"SSL certificate requested for {hostname}")
        return True
    logger.warning(f"SSL request returned {resp.status_code}")
    return False


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def generate_vault_csv(settings: list[tuple[str, str, str]],
                       vault_folder: str, output_path: str) -> None:
    """Generate Vaultwarden-importable Bitwarden CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["folder", "favorite", "type", "name", "notes", "fields",
                      "reprompt", "login_uri", "login_username", "login_password", "login_totp"])
    for name, value, desc in settings:
        writer.writerow([
            vault_folder, "", "login", f"{vault_folder}/{name}", desc,
            "", "", "", "", value, "",
        ])
    with open(output_path, "w", newline="") as f:
        f.write(output.getvalue())
    logger.info(f"Vaultwarden CSV written: {output_path}")


def print_results(args, endpoint: str, pull_zone_id: int, token_key: str) -> None:
    """Print all output values."""
    hostname = args.hostname

    logger.info("")
    logger.info("Environment variables:")
    logger.info(f"  HETZNER_S3_ENDPOINT={endpoint}")
    logger.info(f"  HETZNER_S3_BUCKET={args.bucket}")
    logger.info(f"  HETZNER_S3_REGION={args.region}")
    logger.info(f"  BUNNY_CDN_TOKEN_KEY={token_key}")
    logger.info(f"  BUNNY_CDN_PULL_ZONE_ID={pull_zone_id}")
    logger.info(f"  BUNNY_CDN_HOSTNAME={hostname}")

    logger.info("")
    logger.info("Signed URL pattern:")
    logger.info(f"  hash = base64url(sha256(token_key + url_path + expires))")
    logger.info(f"  url  = https://{hostname}/path?token={{hash}}&expires={{ts}}")

    logger.info("")
    logger.info("Python example:")
    logger.info(f'  import hashlib, base64, time')
    logger.info(f'  expires = int(time.time()) + 3600')
    logger.info(f'  path = "/tenant/export/file.pdf"')
    logger.info(f'  raw = hashlib.sha256(f"{{token_key}}{{path}}{{expires}}".encode()).digest()')
    logger.info(f'  token = base64.b64encode(raw).decode().replace("+","-").replace("/","_").rstrip("=")')
    logger.info(f'  url = f"https://{hostname}{{path}}?token={{token}}&expires={{expires}}"')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Set up private CDN: Hetzner S3 + Bunny CDN with token authentication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic setup
  python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn

  # Dry-run
  python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn --dry-run

  # With Vaultwarden CSV export
  python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn \\
      --vault-csv --vault-folder "myproject/infra"

  # Custom CORS
  python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn \\
      --cors-origins "https://app.example.com,http://localhost:3000"

  # Skip DNS (configure manually)
  python setup_private_cdn.py --bucket my-files --domain example.com --subdomain cdn --skip-dns

Environment Variables:
  HETZNER_S3_ACCESS_KEY     Hetzner S3 access key (from Vaultwarden or .env)
  HETZNER_S3_SECRET_KEY     Hetzner S3 secret key
  BUNNY_API_KEY             Bunny.net API key
  CLOUDFLARE_API_TOKEN      Cloudflare DNS token (optional)
        """,
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name (e.g., my-files)")
    parser.add_argument("--domain", required=True, help="Root domain (e.g., example.com)")
    parser.add_argument("--subdomain", required=True, help="CDN subdomain (e.g., cdn)")
    parser.add_argument("--region", default="fsn1", choices=list(HETZNER_REGIONS.keys()),
                        help="Hetzner region (default: fsn1)")
    parser.add_argument("--existing-bucket", action="store_true",
                        help="Skip bucket creation (already exists)")
    parser.add_argument("--cors-origins", default=None,
                        help="Comma-separated CORS origins (default: localhost:3000,5173)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--skip-dns", action="store_true", help="Skip DNS setup")
    parser.add_argument("--skip-ssl", action="store_true", help="Skip SSL certificate request")
    parser.add_argument("--vault-csv", action="store_true",
                        help="Generate Vaultwarden CSV for import")
    parser.add_argument("--csv-only", action="store_true",
                        help="Only generate Vaultwarden CSV from env vars (no infra changes)")
    parser.add_argument("--vault-folder", default=None,
                        help="Vaultwarden folder path (default: <bucket>/infra)")
    parser.add_argument("--output-csv", default=None,
                        help="Vaultwarden CSV output path (default: vaultwarden-import-<bucket>.csv)")
    parser.add_argument("--extra-secrets", default=None,
                        help="Extra env vars to include in CSV (comma-separated KEY=vault-name pairs)")
    return parser.parse_args()


def main():
    args = parse_arguments()

    hostname = f"{args.subdomain}.{args.domain}"
    args.hostname = hostname

    cors_origins = (
        [o.strip() for o in args.cors_origins.split(",")]
        if args.cors_origins
        else DEFAULT_CORS_ORIGINS + [f"https://{args.domain}", f"https://app.{args.domain}"]
    )

    vault_folder = args.vault_folder or f"{args.bucket}/infra"
    csv_path = args.output_csv or f"vaultwarden-import-{args.bucket}.csv"

    # --csv-only: just generate CSV from current env vars, no infra changes
    if args.csv_only:
        endpoint = f"https://{HETZNER_REGIONS.get(args.region, args.region)}"
        settings = [
            ("hetzner-s3-endpoint", os.getenv("HETZNER_S3_ENDPOINT", endpoint), "Hetzner S3 endpoint URL"),
            ("hetzner-s3-access-key", os.getenv("HETZNER_S3_ACCESS_KEY", ""), "Hetzner S3 access key"),
            ("hetzner-s3-secret-key", os.getenv("HETZNER_S3_SECRET_KEY", ""), "Hetzner S3 secret key"),
            ("hetzner-s3-bucket", os.getenv("HETZNER_S3_BUCKET", args.bucket), "Hetzner S3 bucket name"),
            ("hetzner-s3-region", os.getenv("HETZNER_S3_REGION", args.region), "Hetzner S3 region"),
            ("hetzner-s3-prefix", os.getenv("HETZNER_S3_PREFIX", ""), "Hetzner S3 key prefix"),
            ("bunny-cdn-token-key", os.getenv("BUNNY_CDN_TOKEN_KEY", ""), "Bunny CDN token auth key (for signed URLs)"),
            ("bunny-cdn-hostname", hostname, "Bunny CDN hostname"),
        ]
        # Add extra secrets from --extra-secrets
        if args.extra_secrets:
            for pair in args.extra_secrets.split(","):
                env_var, _, vault_name = pair.partition("=")
                vault_name = vault_name or env_var.lower().replace("_", "-")
                settings.append((vault_name, os.getenv(env_var, ""), f"{env_var} (extra)"))

        generate_vault_csv(settings, vault_folder, csv_path)
        filled = sum(1 for _, v, _ in settings if v)
        empty = sum(1 for _, v, _ in settings if not v)
        logger.info(f"  {filled} values from env, {empty} empty (fill before import)")
        logger.info(f"\nImport: Vaultwarden UI -> Tools -> Import Data -> Bitwarden (csv)")
        logger.info(f"DELETE {csv_path} after import!")
        return

    logger.info("=" * 60)
    logger.info("PRIVATE CDN SETUP")
    logger.info("  Hetzner S3 (private) + Bunny CDN (token auth)")
    logger.info("=" * 60)
    logger.info(f"  Bucket:    {args.bucket}")
    logger.info(f"  Hostname:  {hostname}")
    logger.info(f"  Region:    {args.region}")
    logger.info(f"  CORS:      {', '.join(cors_origins)}")
    logger.info(f"  Dry run:   {args.dry_run}")
    logger.info("=" * 60)

    if not check_env():
        sys.exit(1)

    # Step 1: Hetzner S3 bucket
    logger.info("\n--- Step 1: Hetzner S3 Bucket (private) ---")
    if not args.existing_bucket:
        step_create_bucket(args.bucket, args.region, cors_origins, dry_run=args.dry_run)
    else:
        logger.info(f"Using existing bucket: {args.bucket}")

    # Step 2: Bunny CDN pull zone + token auth
    logger.info("\n--- Step 2: Bunny CDN Pull Zone + Token Auth ---")
    pull_zone_id, token_key = step_create_pull_zone(
        args.bucket, args.region, hostname, dry_run=args.dry_run
    )

    # Step 3: DNS
    if not args.skip_dns:
        logger.info("\n--- Step 3: DNS (Cloudflare) ---")
        step_setup_dns(hostname, args.domain, args.bucket, dry_run=args.dry_run)
    else:
        logger.info("\n--- Step 3: DNS (skipped) ---")
        logger.info(f"  Create CNAME manually: {hostname} -> {args.bucket}.b-cdn.net")

    # Step 4: SSL
    if not args.skip_ssl and pull_zone_id and not args.dry_run:
        logger.info("\n--- Step 4: SSL Certificate ---")
        logger.info("Waiting 30s for DNS propagation...")
        time.sleep(30)
        step_request_ssl(pull_zone_id, hostname, dry_run=args.dry_run)
    elif args.dry_run:
        logger.info("\n--- Step 4: SSL Certificate ---")
        step_request_ssl(0, hostname, dry_run=True)

    # Results
    endpoint = f"https://{HETZNER_REGIONS[args.region]}"
    logger.info("\n" + "=" * 60)
    logger.info("SETUP COMPLETE")
    logger.info("=" * 60)

    if token_key:
        print_results(args, endpoint, pull_zone_id, token_key)

        # Vaultwarden CSV
        if args.vault_csv:
            settings = [
                ("hetzner-s3-endpoint", endpoint, "Hetzner S3 endpoint URL"),
                ("hetzner-s3-access-key", os.environ.get("HETZNER_S3_ACCESS_KEY", ""), "Hetzner S3 access key"),
                ("hetzner-s3-secret-key", os.environ.get("HETZNER_S3_SECRET_KEY", ""), "Hetzner S3 secret key"),
                ("hetzner-s3-bucket", args.bucket, "Hetzner S3 bucket name"),
                ("hetzner-s3-region", args.region, "Hetzner S3 region"),
                ("bunny-cdn-api-key", os.environ.get("BUNNY_API_KEY", ""), "Bunny CDN API key"),
                ("bunny-cdn-token-key", token_key, "Bunny CDN token auth key (for signed URLs)"),
                ("bunny-cdn-pull-zone-id", str(pull_zone_id), "Bunny CDN pull zone ID"),
                ("bunny-cdn-hostname", hostname, "Bunny CDN hostname"),
            ]
            generate_vault_csv(settings, vault_folder, csv_path)
            logger.info(f"\nVaultwarden CSV: {csv_path}")
            logger.info(f"  Import: Vaultwarden UI -> Tools -> Import Data -> Bitwarden (csv)")
            logger.info(f"  DELETE the CSV after import!")
    elif args.dry_run:
        logger.info("\nDry run complete. Re-run without --dry-run to execute.")
    else:
        logger.error("No token key returned — check Bunny CDN setup.")
        sys.exit(1)


if __name__ == "__main__":
    main()
