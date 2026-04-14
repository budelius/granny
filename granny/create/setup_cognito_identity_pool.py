#!/usr/bin/env python3
"""
AWS Cognito Identity Pool Setup Script

Creates and configures an AWS Cognito Identity Pool that allows unauthenticated
users to upload files to a specific S3 bucket.

This script:
1. Creates or retrieves a Cognito Identity Pool
2. Sets up IAM roles for unauthenticated users
3. Configures S3 bucket policies
4. Returns the Identity Pool ID for use in client applications
5. Optionally exports all configuration to JSON file

Usage:
    # Create identity pool with S3 bucket access
    python setup_cognito_identity_pool.py --bucket-name my-bucket --pool-name MyAppUploads

    # Use specific AWS profile
    python setup_cognito_identity_pool.py --bucket-name my-bucket --pool-name MyAppUploads --aws-profile production

    # Specify allowed upload paths in the bucket
    python setup_cognito_identity_pool.py --bucket-name my-bucket --pool-name MyAppUploads --upload-path uploads/

    # Fetch existing identity pool ID
    python setup_cognito_identity_pool.py --pool-name MyAppUploads --fetch-only

    # Export configuration to JSON file
    python setup_cognito_identity_pool.py --bucket-name my-bucket --pool-name MyAppUploads --export

    # Export to custom directory
    python setup_cognito_identity_pool.py --bucket-name my-bucket --pool-name MyAppUploads --export --export-dir /path/to/output
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
# boto3 returns dicts for IAM policy documents, but we also handle raw strings.
from urllib.parse import unquote

import boto3

# Default export directory
DEFAULT_EXPORT_DIR = Path(__file__).parent.parent.parent / "out"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Setup AWS Cognito Identity Pool for S3 uploads',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create new identity pool with S3 access
  python setup_cognito_identity_pool.py --bucket-name my-uploads --pool-name MyAppUploads

  # Use specific AWS profile and region
  python setup_cognito_identity_pool.py --bucket-name my-uploads --pool-name MyAppUploads --aws-profile prod --region us-west-2

  # Specify upload path prefix
  python setup_cognito_identity_pool.py --bucket-name my-uploads --pool-name MyAppUploads --upload-path public/uploads/

  # Fetch existing identity pool ID
  python setup_cognito_identity_pool.py --pool-name MyAppUploads --fetch-only

  # Allow authenticated access only
  python setup_cognito_identity_pool.py --bucket-name my-uploads --pool-name MyAppUploads --no-unauthenticated

Environment Variables:
  AWS_PROFILE: Optional - AWS profile to use
  AWS_REGION: Optional - AWS region (default: us-east-1)
        """
    )

    parser.add_argument('--bucket-name',
                        help='S3 bucket name for uploads (required unless --fetch-only)')
    parser.add_argument('--pool-name', required=True,
                        help='Name for the Cognito Identity Pool')
    parser.add_argument('--region', default='us-east-1',
                        help='AWS region (default: us-east-1)')
    parser.add_argument('--aws-profile', default=None,
                        help='AWS profile to use for credentials')
    parser.add_argument('--upload-path', default='uploads/',
                        help='Path prefix for uploads in the bucket (default: uploads/)')
    parser.add_argument('--no-unauthenticated', action='store_true',
                        help='Disable unauthenticated access (only allow authenticated users)')
    parser.add_argument('--fetch-only', action='store_true',
                        help='Only fetch existing identity pool ID, don\'t create or update')
    parser.add_argument('--max-file-size-mb', type=int, default=10,
                        help='Maximum file size in MB for uploads (default: 10)')
    parser.add_argument('--export', action='store_true',
                        help='Export identity pool configuration to JSON file')
    parser.add_argument('--export-dir', type=str, default=str(DEFAULT_EXPORT_DIR),
                        help=f'Directory for exported configuration (default: {DEFAULT_EXPORT_DIR})')

    args = parser.parse_args()

    # Validate arguments
    if not args.fetch_only and not args.bucket_name:
        parser.error('--bucket-name is required unless --fetch-only is specified')

    return args


def create_aws_clients(aws_profile: Optional[str] = None, region: str = 'us-east-1') -> Dict[str, Any]:
    """Create AWS clients with optional profile support."""
    if aws_profile:
        logging.info(f"Creating AWS clients using profile: {aws_profile}")
        session = boto3.Session(profile_name=aws_profile, region_name=region)
    else:
        logging.info(f"Creating AWS clients using default credentials in region: {region}")
        session = boto3.Session(region_name=region)

    return {
        'cognito_identity': session.client('cognito-identity'),
        'iam': session.client('iam'),
        's3': session.client('s3'),
        'sts': session.client('sts')
    }


def get_account_id(sts_client) -> str:
    """Get the AWS account ID."""
    response = sts_client.get_caller_identity()
    return response['Account']


def find_identity_pool(cognito_client, pool_name: str) -> Optional[Dict[str, Any]]:
    """Find an existing identity pool by name."""
    logging.info(f"Searching for existing identity pool: '{pool_name}'...")

    try:
        response = cognito_client.list_identity_pools(MaxResults=60)

        for pool in response.get('IdentityPools', []):
            if pool['IdentityPoolName'] == pool_name:
                logging.info(f"Found existing identity pool: {pool['IdentityPoolId']}")
                return pool

        logging.info(f"No existing identity pool found with name '{pool_name}'")
        return None

    except Exception as e:
        logging.error(f"Error searching for identity pool: {e}")
        return None


def create_identity_pool(cognito_client, pool_name: str, allow_unauthenticated: bool = True) -> Dict[str, Any]:
    """Create a new Cognito Identity Pool."""
    logging.info(f"Creating new identity pool: '{pool_name}'...")

    try:
        response = cognito_client.create_identity_pool(
            IdentityPoolName=pool_name,
            AllowUnauthenticatedIdentities=allow_unauthenticated,
            AllowClassicFlow=False
        )

        pool_id = response['IdentityPoolId']
        logging.info(f"Identity pool created successfully: {pool_id}")
        return response

    except Exception as e:
        logging.error(f"Error creating identity pool: {e}")
        raise


def create_iam_role_for_cognito(iam_client, role_name: str, pool_id: str, region: str, account_id: str) -> str:
    """Create IAM role for Cognito Identity Pool."""
    logging.info(f"Creating IAM role: '{role_name}'...")

    # Trust policy for Cognito Identity
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Federated": "cognito-identity.amazonaws.com"
                },
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "cognito-identity.amazonaws.com:aud": pool_id
                    },
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "unauthenticated"
                    }
                }
            }
        ]
    }

    try:
        # Try to get existing role
        try:
            response = iam_client.get_role(RoleName=role_name)
            role_arn = response['Role']['Arn']
            logging.info(f"IAM role already exists: {role_arn}")
            return role_arn
        except iam_client.exceptions.NoSuchEntityException:
            # Role doesn't exist, create it
            response = iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description=f"Role for Cognito Identity Pool {pool_id} unauthenticated users"
            )
            role_arn = response['Role']['Arn']
            logging.info(f"IAM role created: {role_arn}")
            return role_arn

    except Exception as e:
        logging.error(f"Error creating IAM role: {e}")
        raise


def _normalize_upload_path(upload_path: str) -> str:
    """Ensure the upload path has no leading slash and ends with / if not empty."""
    if not upload_path:
        return ""

    normalized = upload_path.lstrip('/')  # remove any leading /
    if normalized and not normalized.endswith('/'):
        normalized += '/'
    return normalized


def _get_policy_arn(account_id: str, policy_name: str) -> str:
    return f"arn:aws:iam::{account_id}:policy/{policy_name}"


def _ensure_policy_capacity(iam_client, policy_arn: str):
    """IAM policies can only have 5 versions. Remove an old non-default version if at capacity."""
    versions = iam_client.list_policy_versions(PolicyArn=policy_arn)['Versions']
    if len(versions) < 5:
        return
    for version in versions:
        if not version.get('IsDefaultVersion'):
            iam_client.delete_policy_version(
                PolicyArn=policy_arn,
                VersionId=version['VersionId']
            )
            logging.info(f"Deleted old policy version {version['VersionId']} to free capacity.")
            return
    logging.warning("All policy versions are default; unable to free capacity automatically.")


def _policy_documents_equal(existing_doc, desired_doc: Dict[str, Any]) -> bool:
    if isinstance(existing_doc, dict):
        return existing_doc == desired_doc

    if isinstance(existing_doc, str):
        try:
            decoded = json.loads(unquote(existing_doc))
            return decoded == desired_doc
        except json.JSONDecodeError:
            return False
    return False


def create_or_update_s3_upload_policy(
    iam_client,
    policy_name: str,
    bucket_name: str,
    upload_path: str,
    account_id: str
) -> str:
    """Create IAM policy for S3 uploads."""
    logging.info(f"Creating S3 upload policy: '{policy_name}'...")

    upload_path = _normalize_upload_path(upload_path)

    # Policy that allows uploads to specific path
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket"
                ],
                "Resource": f"arn:aws:s3:::{bucket_name}",
                "Condition": {
                    "StringLike": {
                        "s3:prefix": f"{upload_path}*"
                    }
                }
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:PutObjectAcl"
                ],
                "Resource": f"arn:aws:s3:::{bucket_name}/{upload_path}*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject"
                ],
                "Resource": f"arn:aws:s3:::{bucket_name}/{upload_path}*"
            }
        ]
    }

    policy_arn = _get_policy_arn(account_id, policy_name)

    try:
        policy = iam_client.get_policy(PolicyArn=policy_arn)['Policy']
        logging.info(f"Policy already exists: {policy_arn}")
        default_version_id = policy['DefaultVersionId']

        version = iam_client.get_policy_version(
            PolicyArn=policy_arn,
            VersionId=default_version_id
        )
        existing_doc = version['PolicyVersion']['Document']

        if _policy_documents_equal(existing_doc, policy_document):
            logging.info("Existing policy document already matches desired permissions.")
            return policy_arn

        logging.info("Updating existing policy document with latest permissions.")
        _ensure_policy_capacity(iam_client, policy_arn)
        iam_client.create_policy_version(
            PolicyArn=policy_arn,
            PolicyDocument=json.dumps(policy_document),
            SetAsDefault=True
        )
        return policy_arn

    except iam_client.exceptions.NoSuchEntityException:
        response = iam_client.create_policy(
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document),
            Description=f"Allow uploads to {bucket_name}/{upload_path}"
        )
        policy_arn = response['Policy']['Arn']
        logging.info(f"Policy created: {policy_arn}")
        return policy_arn

    except Exception as e:
        logging.error(f"Error creating policy: {e}")
        raise


def attach_policy_to_role(iam_client, role_name: str, policy_arn: str):
    """Attach IAM policy to role."""
    logging.info(f"Attaching policy to role '{role_name}'...")

    try:
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn=policy_arn
        )
        logging.info("Policy attached successfully")
    except iam_client.exceptions.EntityAlreadyExistsException:
        logging.info("Policy already attached to role")
    except Exception as e:
        logging.error(f"Error attaching policy: {e}")
        raise


def set_identity_pool_roles(cognito_client, pool_id: str, role_arn: str, allow_unauthenticated: bool = True):
    """Set IAM roles for the identity pool."""
    logging.info(f"Setting identity pool roles for {pool_id}...")

    roles = {}
    if allow_unauthenticated:
        roles['unauthenticated'] = role_arn
    roles['authenticated'] = role_arn  # Use same role for both for simplicity

    try:
        cognito_client.set_identity_pool_roles(
            IdentityPoolId=pool_id,
            Roles=roles
        )
        logging.info("Identity pool roles configured successfully")
    except Exception as e:
        logging.error(f"Error setting identity pool roles: {e}")
        raise


def configure_s3_cors(s3_client, bucket_name: str):
    """Configure CORS on S3 bucket to allow uploads from web browsers."""
    logging.info(f"Configuring CORS for bucket '{bucket_name}'...")

    cors_configuration = {
        'CORSRules': [
            {
                'AllowedHeaders': ['*'],
                'AllowedMethods': ['GET', 'PUT', 'POST', 'DELETE', 'HEAD'],
                'AllowedOrigins': ['*'],
                'ExposeHeaders': ['ETag'],
                'MaxAgeSeconds': 3000
            }
        ]
    }

    try:
        s3_client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration=cors_configuration
        )
        logging.info("CORS configuration applied successfully")
    except Exception as e:
        logging.error(f"Error configuring CORS: {e}")
        raise


def get_identity_pool_details(cognito_client, pool_id: str) -> Dict[str, Any]:
    """Get detailed information about an identity pool."""
    try:
        response = cognito_client.describe_identity_pool(IdentityPoolId=pool_id)
        return response
    except Exception as e:
        logging.error(f"Error getting identity pool details: {e}")
        raise


def get_identity_pool_roles(cognito_client, pool_id: str) -> Dict[str, Any]:
    """Get IAM roles associated with an identity pool."""
    try:
        response = cognito_client.get_identity_pool_roles(IdentityPoolId=pool_id)
        return response
    except Exception as e:
        logging.error(f"Error getting identity pool roles: {e}")
        return {}


def export_configuration(
    export_dir: str,
    pool_id: str,
    pool_name: str,
    region: str,
    account_id: str,
    bucket_name: Optional[str] = None,
    upload_path: Optional[str] = None,
    role_arn: Optional[str] = None,
    policy_arn: Optional[str] = None,
    allow_unauthenticated: bool = True,
    pool_details: Optional[Dict[str, Any]] = None,
    pool_roles: Optional[Dict[str, Any]] = None
) -> str:
    """Export identity pool configuration to JSON file."""
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)

    # Create sanitized filename from pool name
    safe_pool_name = pool_name.replace(' ', '_').replace('-', '_').lower()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"cognito_identity_pool_{safe_pool_name}_{timestamp}.json"
    filepath = export_path / filename

    # Build configuration object
    config = {
        "metadata": {
            "exported_at": datetime.now().isoformat(),
            "script_version": "1.0.0",
            "description": "AWS Cognito Identity Pool configuration for S3 uploads"
        },
        "identity_pool": {
            "id": pool_id,
            "name": pool_name,
            "region": region,
            "allow_unauthenticated": allow_unauthenticated,
            "arn": f"arn:aws:cognito-identity:{region}:{account_id}:identitypool/{pool_id}"
        },
        "aws": {
            "account_id": account_id,
            "region": region
        },
        "iam": {
            "role_arn": role_arn,
            "policy_arn": policy_arn
        },
        "s3": {
            "bucket_name": bucket_name,
            "upload_path": upload_path,
            "bucket_arn": f"arn:aws:s3:::{bucket_name}" if bucket_name else None,
            "upload_resource_arn": f"arn:aws:s3:::{bucket_name}/{upload_path}*" if bucket_name and upload_path else None
        },
        "client_config": {
            "javascript_sdk_v3": {
                "region": region,
                "identity_pool_id": pool_id,
                "imports": [
                    "@aws-sdk/client-cognito-identity",
                    "@aws-sdk/credential-provider-cognito-identity",
                    "@aws-sdk/client-s3"
                ]
            },
            "javascript_sdk_v2": {
                "region": region,
                "identity_pool_id": pool_id
            }
        },
        "endpoints": {
            "cognito_identity": f"https://cognito-identity.{region}.amazonaws.com",
            "s3": f"https://s3.{region}.amazonaws.com" if region != "us-east-1" else "https://s3.amazonaws.com",
            "s3_bucket": f"https://{bucket_name}.s3.{region}.amazonaws.com" if bucket_name else None
        }
    }

    # Add detailed pool information if available
    if pool_details:
        # Remove ResponseMetadata from details
        clean_details = {k: v for k, v in pool_details.items() if k != 'ResponseMetadata'}
        config["identity_pool_details"] = clean_details

    # Add role mappings if available
    if pool_roles:
        clean_roles = {k: v for k, v in pool_roles.items() if k != 'ResponseMetadata'}
        config["identity_pool_roles"] = clean_roles

    # Write to file
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, default=str)

    logging.info(f"Configuration exported to: {filepath}")
    return str(filepath)


def print_configuration_summary(pool_id: str, pool_name: str, region: str, bucket_name: Optional[str] = None, upload_path: Optional[str] = None):
    """Print configuration summary and usage instructions."""
    logging.info("\n" + "="*60)
    logging.info("COGNITO IDENTITY POOL CONFIGURATION COMPLETE")
    logging.info("="*60)
    logging.info(f"Identity Pool Name: {pool_name}")
    logging.info(f"Identity Pool ID: {pool_id}")
    logging.info(f"Region: {region}")

    if bucket_name:
        logging.info(f"S3 Bucket: {bucket_name}")
        logging.info(f"Upload Path: {upload_path}")

    logging.info("\n" + "-"*60)
    logging.info("CLIENT CONFIGURATION (JavaScript/Web)")
    logging.info("-"*60)

    print(f"""
// AWS SDK v3 (recommended)
import {{ CognitoIdentityClient }} from "@aws-sdk/client-cognito-identity";
import {{ fromCognitoIdentityPool }} from "@aws-sdk/credential-provider-cognito-identity";
import {{ S3Client, PutObjectCommand }} from "@aws-sdk/client-s3";

const s3Client = new S3Client({{
  region: "{region}",
  credentials: fromCognitoIdentityPool({{
    client: new CognitoIdentityClient({{ region: "{region}" }}),
    identityPoolId: "{pool_id}",
  }})
}});

// Upload file
async function uploadFile(file) {{
  const params = {{
    Bucket: "{bucket_name or 'YOUR_BUCKET'}",
    Key: "{upload_path or 'uploads/'}${{file.name}}",
    Body: file,
    ContentType: file.type
  }};

  const command = new PutObjectCommand(params);
  const response = await s3Client.send(command);
  return response;
}}
""")

    logging.info("-"*60)
    logging.info("AWS SDK v2 (older version)")
    logging.info("-"*60)

    print(f"""
AWS.config.region = '{region}';
AWS.config.credentials = new AWS.CognitoIdentityCredentials({{
  IdentityPoolId: '{pool_id}'
}});

const s3 = new AWS.S3();

// Upload file
function uploadFile(file) {{
  const params = {{
    Bucket: '{bucket_name or "YOUR_BUCKET"}',
    Key: '{upload_path or "uploads/"}' + file.name,
    Body: file,
    ContentType: file.type
  }};

  return s3.upload(params).promise();
}}
""")

    logging.info("="*60)


def main():
    """Main function to orchestrate the create."""
    args = parse_arguments()

    try:
        # Create AWS clients
        clients = create_aws_clients(args.aws_profile, args.region)
        cognito_client = clients['cognito_identity']
        iam_client = clients['iam']
        s3_client = clients['s3']
        sts_client = clients['sts']

        # Get AWS account ID
        account_id = get_account_id(sts_client)
        logging.info(f"AWS Account ID: {account_id}")

        # Find or create identity pool
        existing_pool = find_identity_pool(cognito_client, args.pool_name)

        # Track created resources for export
        role_arn = None
        policy_arn = None

        if existing_pool:
            pool_id = existing_pool['IdentityPoolId']

            if args.fetch_only:
                logging.info(f"Fetch-only mode: Found identity pool {pool_id}")
                pool_details = get_identity_pool_details(cognito_client, pool_id)
                pool_roles = get_identity_pool_roles(cognito_client, pool_id)
                print_configuration_summary(
                    pool_id=pool_id,
                    pool_name=args.pool_name,
                    region=args.region
                )

                # Export if requested
                if args.export:
                    export_configuration(
                        export_dir=args.export_dir,
                        pool_id=pool_id,
                        pool_name=args.pool_name,
                        region=args.region,
                        account_id=account_id,
                        allow_unauthenticated=pool_details.get('AllowUnauthenticatedIdentities', True),
                        pool_details=pool_details,
                        pool_roles=pool_roles
                    )
                return 0

            logging.info(f"Using existing identity pool: {pool_id}")
        else:
            if args.fetch_only:
                logging.error(f"Identity pool '{args.pool_name}' not found")
                return 1

            # Create new identity pool
            allow_unauth = not args.no_unauthenticated
            pool = create_identity_pool(cognito_client, args.pool_name, allow_unauth)
            pool_id = pool['IdentityPoolId']

        # If not fetch-only, configure IAM roles and S3
        if not args.fetch_only:
            normalized_upload_path = _normalize_upload_path(args.upload_path)

            # Create IAM role
            role_name = f"Cognito_{args.pool_name.replace(' ', '_')}_Unauth_Role"
            role_arn = create_iam_role_for_cognito(
                iam_client,
                role_name,
                pool_id,
                args.region,
                account_id
            )

            # Create S3 upload policy
            policy_name = f"Cognito_{args.pool_name.replace(' ', '_')}_S3_Upload"
            policy_arn = create_or_update_s3_upload_policy(
                iam_client,
                policy_name,
                args.bucket_name,
                normalized_upload_path,
                account_id
            )

            # Attach policy to role
            attach_policy_to_role(iam_client, role_name, policy_arn)

            # Set identity pool roles
            allow_unauth = not args.no_unauthenticated
            set_identity_pool_roles(cognito_client, pool_id, role_arn, allow_unauth)

            # Configure S3 CORS
            configure_s3_cors(s3_client, args.bucket_name)

            # Print summary
            print_configuration_summary(
                pool_id=pool_id,
                pool_name=args.pool_name,
                region=args.region,
                bucket_name=args.bucket_name,
                upload_path=normalized_upload_path
            )

            # Export configuration if requested
            if args.export:
                pool_details = get_identity_pool_details(cognito_client, pool_id)
                pool_roles = get_identity_pool_roles(cognito_client, pool_id)
                export_configuration(
                    export_dir=args.export_dir,
                    pool_id=pool_id,
                    pool_name=args.pool_name,
                    region=args.region,
                    account_id=account_id,
                    bucket_name=args.bucket_name,
                    upload_path=normalized_upload_path,
                    role_arn=role_arn,
                    policy_arn=policy_arn,
                    allow_unauthenticated=allow_unauth,
                    pool_details=pool_details,
                    pool_roles=pool_roles
                )

        return 0

    except Exception as e:
        logging.error(f"Setup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
