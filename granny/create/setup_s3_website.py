import boto3
import os
import json
import time
import logging
import argparse
import requests

try:
    from cloudflare import Cloudflare
except ImportError:
    print("Cloudflare library not found. Please install it: pip install cloudflare")
    exit(1)

from dotenv import load_dotenv
load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass

"""
This script sets up a CDN for static content using AWS S3, CloudFront, and Cloudflare.

SSL Configuration:
- When using --cloudflare-ssl (default): Cloudflare handles SSL termination and proxies to CloudFront.
  CloudFront uses default certificate and NO aliases (Cloudflare points to CloudFront domain).
- When using --acm-ssl: AWS ACM certificate is created and CloudFront uses it with domain aliases.

SPA (Single Page Application) Support:
- Use --spa-mode to configure CloudFront for client-side routing (React Router, Vue Router, etc.)
- 404 errors will be redirected to your index.html (or custom path) with HTTP 200 status
- This allows SPAs to handle routing on the client side

Usage:
    # Basic static site
    python setup_s3_website.py --domain lularge.com --subdomain cdn-staging --cloudflare-ssl
    
    # Single Page Application
    python setup_s3_website.py --domain lularge.com --subdomain app-dev --spa-mode

By default, it uses Cloudflare for SSL (controlled by the --cloudflare-ssl flag).
When --acm-ssl is used instead:
- An ACM certificate is created for the domain
- DNS validation is performed through Cloudflare
- CloudFront uses the ACM certificate and domain aliases
"""

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Setup CDN with S3, CloudFront, and Cloudflare',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic create with Cloudflare SSL (default)
  python setup_s3_website.py --domain lularge.com --subdomain cdn-staging

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
    parser.add_argument('--subdomain', required=True,
                        help='The subdomain for the CDN (e.g., cdn-staging)')
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

    return parser.parse_args()

# Parse command line arguments
args = parse_arguments()

# --- Configuration from command line ---
DOMAIN_NAME = args.domain
SUBDOMAIN = args.subdomain
BUCKET_NAME = f"{SUBDOMAIN}.{DOMAIN_NAME}"
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

# --- Setup logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Log the configuration
logging.info("=== CDN Setup Configuration ===")
logging.info(f"Domain: {DOMAIN_NAME}")
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

def request_acm_certificate(domain_name):
    """Requests an ACM certificate for the domain and returns its ARN and validation CNAME."""
    logging.info(f"Requesting ACM certificate for '{domain_name}'...")
    response = acm_client.request_certificate(
        DomainName=domain_name,
        ValidationMethod='DNS'
    )
    certificate_arn = response['CertificateArn']

    logging.info(f"Waiting for certificate details to become available for {certificate_arn}...")
    time.sleep(10) # Give some time for the validation options to be available

    cert_description = acm_client.describe_certificate(CertificateArn=certificate_arn)
    validation_options = cert_description['Certificate']['DomainValidationOptions'][0]

    while 'ResourceRecord' not in validation_options:
        logging.info("Waiting for DNS validation record to be created by AWS...")
        time.sleep(10)
        cert_description = acm_client.describe_certificate(CertificateArn=certificate_arn)
        validation_options = cert_description['Certificate']['DomainValidationOptions'][0]

    validation_cname = validation_options['ResourceRecord']
    waiter = acm_client.get_waiter('certificate_validated')

    logging.info(f"Certificate requested. ARN: {certificate_arn}")
    logging.info(f"DNS validation record: {validation_cname['Name']} -> {validation_cname['Value']}")

    return certificate_arn, validation_cname, waiter

def setup_dns_validation(cf_client, zone_name, validation_cname):
    """Creates a CNAME record in Cloudflare for ACM validation."""
    logging.info("Setting up DNS validation record in Cloudflare...")
    try:
        zones = cf_client.zones.list(name=zone_name)
        if not zones.result:
            logging.error(f"Zone '{zone_name}' not found in Cloudflare.")
            raise Exception(f"Zone not found: {zone_name}")
        zone_id = zones.result[0].id

        dns_record = {
            'name': validation_cname['Name'],
            'type': validation_cname['Type'],
            'content': validation_cname['Value'].rstrip('.'),
            'proxied': False,
            'ttl': 60
        }

        cf_client.dns.records.create(zone_id=zone_id, **dns_record)
        logging.info(f"DNS validation record created for {validation_cname['Name']}.")
        return zone_id, dns_record
    except Exception as e:
        if "already exists" in str(e):
             logging.warning(f"DNS record for {validation_cname['Name']} already exists.")
             return None, None # Indicate that it already exists
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
            logging.warning(f"DefaultCacheBehavior not found in distribution config, skipping target origin update")

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
                logging.info(f"Adding SPA mode error handling to existing distribution")
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
                logging.info(f"SPA error handling already configured for distribution")

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
    distribution_config['Aliases'] = {
        'Quantity': 1,
        'Items': [bucket_name]
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
                    logging.info(f"  Target proxied: False")

                    # Check if we need to update it
                    if current_content != cname_content or current_proxied != False:
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

def cleanup_dns_validation(cf_client, zone_id, dns_record_to_delete):
    """Removes the DNS validation record from Cloudflare."""
    if not dns_record_to_delete:
        return
    logging.info(f"Cleaning up DNS validation record: {dns_record_to_delete['name']}")
    try:
        # We need the record ID to delete it.
        records = cf_client.dns.records.list(zone_id=zone_id, name=dns_record_to_delete['name'], type=dns_record_to_delete['type'])
        if records.result:
            record_id = records.result[0].id
            cf_client.dns.records.delete(zone_id=zone_id, dns_record_id=record_id)
            logging.info("DNS validation record cleaned up successfully.")
        else:
            logging.warning("Could not find DNS validation record to cleanup.")
    except Exception as e:
        logging.error(f"Error cleaning up DNS record: {e}")

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
                        logging.info(f"✅ Test page test PASSED - Found success message")
                        results[url]['validation'] = 'PASSED'
                    else:
                        logging.warning(f"⚠️  Test page test PARTIAL - Page loaded but missing expected content")
                        results[url]['validation'] = 'PARTIAL'
                else:
                    logging.error(f"❌ Test page test FAILED - Status: {response.status_code}")
                    results[url]['validation'] = 'FAILED'

            elif url.endswith('.txt'):
                # Text file should return 200 and contain expected content
                if response.status_code == 200:
                    if 'CDN test file' in response.text:
                        logging.info(f"✅ Text file test PASSED - Found expected content")
                        results[url]['validation'] = 'PASSED'
                    else:
                        logging.warning(f"⚠️  Text file test PARTIAL - File loaded but unexpected content")
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
                            logging.info(f"✅ SPA 404 redirect test PASSED - Returned index.html with 200")
                            results[url]['validation'] = 'PASSED'
                        else:
                            logging.warning(f"⚠️  SPA 404 redirect test PARTIAL - 200 returned but not index.html")
                            results[url]['validation'] = 'PARTIAL'
                    else:
                        logging.error(f"❌ SPA 404 redirect test FAILED - Status: {response.status_code} (expected 200)")
                        results[url]['validation'] = 'FAILED'
                else:
                    # Normal mode - expect 404 with custom error page
                    if response.status_code == 404:
                        if 'Page Not Found' in response.text:
                            logging.info(f"✅ 404 error page test PASSED - Custom error page displayed")
                            results[url]['validation'] = 'PASSED'
                        else:
                            logging.warning(f"⚠️  404 error page test PARTIAL - 404 returned but no custom error page")
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
                        logging.warning(f"⚠️  SPA routing test PARTIAL - 200 returned but not index.html")
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
                logging.warning(f"  ⚠️  CloudFront headers not detected")

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
    """Main function to orchestrate the create."""
    try:
        cf_client = get_cloudflare_client()
        zone_id = get_cloudflare_zone_id(cf_client, CLOUDFLARE_ZONE_NAME)

        # 1. S3 Bucket
        create_s3_bucket(BUCKET_NAME)

        # 2. Enable S3 Website Hosting
        enable_s3_website_hosting(BUCKET_NAME)

        cert_arn = None
        if not USE_CLOUDFLARE_SSL:
            # 3. ACM Certificate (only if not using Cloudflare SSL)
            cert_arn, validation_cname, cert_waiter = request_acm_certificate(BUCKET_NAME)

            # 4. DNS Validation
            _, validation_dns_record = setup_dns_validation(cf_client, CLOUDFLARE_ZONE_NAME, validation_cname)

            # 5. Wait for Validation
            wait_for_cert_validation(cert_waiter, cert_arn)

            # 6. Cleanup DNS validation record
            cleanup_dns_validation(cf_client, zone_id, validation_dns_record)
        else:
            logging.info("Using Cloudflare for SSL, but checking for existing ACM certificates...")
            # Even with Cloudflare SSL, we need ACM certificates for CloudFront aliases
            cert_arn = find_existing_acm_certificate(BUCKET_NAME)
            if cert_arn:
                logging.info(f"Found existing ACM certificate: {cert_arn}")
            else:
                logging.info("No existing ACM certificate found - creating wildcard certificate for aliases...")
                # Create wildcard certificate for the domain
                wildcard_domain = f"*.{CLOUDFLARE_ZONE_NAME}"
                logging.info(f"Creating ACM certificate for '{wildcard_domain}'...")
                cert_arn, validation_cname, cert_waiter = request_acm_certificate(wildcard_domain)

                # DNS Validation
                _, validation_dns_record = setup_dns_validation(cf_client, CLOUDFLARE_ZONE_NAME, validation_cname)

                # Wait for Validation
                wait_for_cert_validation(cert_waiter, cert_arn)

                # Cleanup DNS validation record
                cleanup_dns_validation(cf_client, zone_id, validation_dns_record)

        # 7. CloudFront Distribution (no OAC needed for S3 website endpoint)
        distribution = create_cloudfront_distribution(BUCKET_NAME, cert_arn, None)
        distribution_domain = distribution['DomainName']

        # 8. Cloudflare CNAME to CloudFront
        create_cloudflare_cname_record(cf_client, zone_id, SUBDOMAIN, distribution_domain)

        # 9. Create test content
        create_test_content(BUCKET_NAME)

        logging.info("\n--- Setup Complete ---")
        logging.info(f"S3 Bucket: {BUCKET_NAME}")
        logging.info(f"S3 Website Endpoint: {get_s3_website_endpoint(BUCKET_NAME, AWS_REGION)}")
        logging.info(f"CloudFront Domain: {distribution_domain}")
        if USE_CLOUDFLARE_SSL:
            logging.info(f"Public URL: https://{BUCKET_NAME} (SSL via Cloudflare)")
        else:
            logging.info(f"Public URL: https://{BUCKET_NAME} (SSL via ACM)")
        logging.info(f"Test URLs:")
        logging.info(f"  - https://{BUCKET_NAME}/index-test.html")
        logging.info(f"  - https://{BUCKET_NAME}/test.txt")
        logging.info("--------------------")

        # 10. Test CDN functionality (unless skipped)
        if not SKIP_TESTS:
            logging.info(f"\nWaiting 30 seconds for DNS/CDN propagation before testing...")
            time.sleep(30)

            test_success = test_cdn_functionality(BUCKET_NAME, TEST_TIMEOUT)

            if test_success:
                logging.info("\n🎉 CDN create completed successfully and is working!")
                return 0
            else:
                logging.warning("\n⚠️  CDN create completed but tests indicate some issues. Check the logs above.")
                return 1
        else:
            logging.info("\n✅ CDN create completed successfully (tests skipped)")
            return 0

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return 1

if __name__ == "__main__":
    exit(main()) 
