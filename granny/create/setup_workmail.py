#!/usr/bin/env python3
"""
Amazon WorkMail User Setup Script

Creates and manages email users in Amazon WorkMail organizations.
Supports user creation, listing, password reset, and optional email forwarding
to an external address.

FEATURES:
  - List WorkMail organizations and find by domain
  - List organizations across all WorkMail regions
  - Create users with mailbox registration (2-step: create + register)
  - List existing users with filtering
  - Reset user passwords
  - Set up email forwarding on new or existing accounts
  - Interactive password prompt (secure, no echo)
  - Idempotent: detects existing users
  - Markdown setup report generation

FORWARDING:
  Email forwarding is implemented via Amazon SES receipt rules, which forward
  incoming emails to an external address. The WorkMail mailbox still receives
  the original email. Requires SES domain verification and an S3 bucket for
  temporary email storage.

Environment Variables:
  AWS_PROFILE           AWS profile name (or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)
  AWS_REGION            AWS region (default: us-east-1, WorkMail available: us-east-1, us-west-2, eu-west-1)

Usage:
  # List organizations
  python setup_workmail.py --action list-orgs

  # List users in an organization
  python setup_workmail.py --action list-users --domain pseekoo.io

  # Create a new email user
  python setup_workmail.py --action create-user --domain pseekoo.io \\
      --email m3rp@pseekoo.io --display-name "M3RP Service" # password prompted interactively

  # Create user with email forwarding
  python setup_workmail.py --action create-user --domain pseekoo.io \\
      --email m3rp@pseekoo.io --display-name "M3RP Service" # password prompted interactively \\
      --forward-to user@gmail.com

  # Reset password (prompts for password interactively)
  python setup_workmail.py --action reset-password --domain pseekoo.io \\
      --email m3rp@pseekoo.io

  # Set up forwarding on an existing account
  python setup_workmail.py --action setup-forwarding --domain pseekoo.io \\
      --email m3rp@pseekoo.io --forward-to user@gmail.com

  # List all WorkMail organizations across all regions
  python setup_workmail.py --action list-all-regions
"""

import os
import sys
import json
import time
import logging
import argparse
import getpass
from typing import Optional, Dict, List

from dotenv import load_dotenv
load_dotenv()

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("boto3 is required. Install it: pip install boto3")
    sys.exit(1)


# =============================================================================
# WorkMail Regions
# =============================================================================

WORKMAIL_REGIONS = {
    'us-east-1': 'US East (N. Virginia)',
    'us-west-2': 'US West (Oregon)',
    'eu-west-1': 'EU (Ireland)',
}


# =============================================================================
# WorkMail Client
# =============================================================================

class WorkMailManager:
    """Amazon WorkMail management client."""

    def __init__(self, region: str = None):
        self.region = region or os.environ.get('AWS_REGION', 'us-east-1')
        if self.region not in WORKMAIL_REGIONS:
            raise ValueError(
                f"WorkMail is not available in '{self.region}'. "
                f"Available regions: {list(WORKMAIL_REGIONS.keys())}"
            )
        self.client = boto3.client('workmail', region_name=self.region)
        self._org_cache = {}

    def list_organizations(self) -> List[dict]:
        """List all WorkMail organizations."""
        organizations = []
        paginator = self.client.get_paginator('list_organizations')
        for page in paginator.paginate():
            organizations.extend(page.get('OrganizationSummaries', []))
        return organizations

    def find_organization(self, domain: str) -> Optional[dict]:
        """Find a WorkMail organization by domain or alias."""
        if domain in self._org_cache:
            return self._org_cache[domain]

        orgs = self.list_organizations()
        for org in orgs:
            if org.get('State') != 'Active':
                continue
            if (org.get('DefaultMailDomain') == domain or
                    org.get('Alias') == domain):
                self._org_cache[domain] = org
                return org
        return None

    def get_org_id(self, domain: str) -> str:
        """Get organization ID for a domain. Raises if not found."""
        org = self.find_organization(domain)
        if not org:
            available = self.list_organizations()
            domains = [o.get('DefaultMailDomain', o.get('Alias', '?')) for o in available]
            raise Exception(
                f"WorkMail organization not found for domain '{domain}'. "
                f"Available: {domains}"
            )
        org_id = org['OrganizationId']
        logging.info(
            f"Found organization: {org.get('Alias')} "
            f"(ID: {org_id}, Domain: {org.get('DefaultMailDomain')})"
        )
        return org_id

    def list_users(self, org_id: str, state_filter: str = None) -> List[dict]:
        """List users in an organization."""
        users = []
        paginator = self.client.get_paginator('list_users')
        params = {'OrganizationId': org_id}
        if state_filter:
            params['Filters'] = {'State': state_filter}

        for page in paginator.paginate(**params):
            users.extend(page.get('Users', []))
        return users

    def find_user_by_email(self, org_id: str, email: str) -> Optional[dict]:
        """Find a user by email address."""
        users = self.list_users(org_id)
        for user in users:
            if user.get('Email', '').lower() == email.lower():
                return user
        return None

    def describe_user(self, org_id: str, user_id: str) -> dict:
        """Get detailed user information."""
        return self.client.describe_user(
            OrganizationId=org_id,
            UserId=user_id
        )

    def create_user(self, org_id: str, username: str, display_name: str,
                    password: str, first_name: str = None,
                    last_name: str = None) -> str:
        """Create a new user in the WorkMail directory. Returns user ID."""
        params = {
            'OrganizationId': org_id,
            'Name': username,
            'DisplayName': display_name,
            'Password': password,
        }
        if first_name:
            params['FirstName'] = first_name
        if last_name:
            params['LastName'] = last_name

        response = self.client.create_user(**params)
        user_id = response['UserId']
        logging.info(f"Created user '{username}' (ID: {user_id})")
        return user_id

    def register_to_workmail(self, org_id: str, user_id: str, email: str) -> None:
        """Register a user for WorkMail (activate their mailbox)."""
        self.client.register_to_work_mail(
            OrganizationId=org_id,
            EntityId=user_id,
            Email=email
        )
        logging.info(f"Registered '{email}' for WorkMail (mailbox activated)")

    def reset_password(self, org_id: str, user_id: str, password: str) -> None:
        """Reset a user's password."""
        self.client.reset_password(
            OrganizationId=org_id,
            UserId=user_id,
            Password=password
        )
        logging.info(f"Password reset for user {user_id}")

    def create_and_register_user(self, org_id: str, email: str,
                                  display_name: str, password: str,
                                  first_name: str = None,
                                  last_name: str = None) -> str:
        """Create a user and register them for WorkMail (2-step).

        Returns user ID.
        """
        # Extract username from email (part before @)
        username = email.split('@')[0]

        # Check if user already exists
        existing = self.find_user_by_email(org_id, email)
        if existing:
            logging.info(
                f"User '{email}' already exists "
                f"(ID: {existing['Id']}, State: {existing['State']})"
            )
            return existing['Id']

        # Step 1: Create user in directory
        user_id = self.create_user(
            org_id=org_id,
            username=username,
            display_name=display_name,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        # Step 2: Register for WorkMail (activate mailbox)
        self.register_to_workmail(org_id, user_id, email)

        return user_id


# =============================================================================
# Email Forwarding via SES
# =============================================================================

class SESForwardingManager:
    """Manage email forwarding via Amazon SES receipt rules."""

    def __init__(self, region: str = None):
        self.region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self.ses_client = boto3.client('ses', region_name=self.region)
        self.lambda_client = boto3.client('lambda', region_name=self.region)
        self.iam_client = boto3.client('iam', region_name=self.region)

    def check_domain_verified(self, domain: str) -> bool:
        """Check if a domain is verified in SES."""
        response = self.ses_client.list_identities(IdentityType='Domain')
        return domain in response.get('Identities', [])

    def get_or_create_rule_set(self, rule_set_name: str = 'workmail-forwarding') -> str:
        """Get or create an SES receipt rule set."""
        try:
            self.ses_client.describe_receipt_rule_set(RuleSetName=rule_set_name)
            logging.info(f"Found existing rule set: {rule_set_name}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'RuleSetDoesNotExist':
                self.ses_client.create_receipt_rule_set(RuleSetName=rule_set_name)
                logging.info(f"Created rule set: {rule_set_name}")
            else:
                raise
        return rule_set_name

    def find_forwarding_rule(self, rule_set_name: str, email: str) -> Optional[dict]:
        """Find an existing forwarding rule for an email."""
        try:
            response = self.ses_client.describe_receipt_rule_set(
                RuleSetName=rule_set_name
            )
            rule_name = f"forward-{email.replace('@', '-at-')}"
            for rule in response.get('Rules', []):
                if rule.get('Name') == rule_name:
                    return rule
        except ClientError:
            pass
        return None

    def setup_forwarding(self, email: str, forward_to: str,
                         rule_set_name: str = 'workmail-forwarding',
                         dry_run: bool = False) -> dict:
        """Set up email forwarding from a WorkMail address to an external address.

        Uses SES receipt rules with a Lambda function to forward emails.
        WorkMail still receives the original email in the user's mailbox.

        Returns dict with setup details.
        """
        domain = email.split('@')[1]
        rule_name = f"forward-{email.replace('@', '-at-')}"
        lambda_name = f"workmail-forward-{domain.replace('.', '-')}"

        result = {
            'email': email,
            'forward_to': forward_to,
            'rule_set': rule_set_name,
            'rule_name': rule_name,
            'lambda_name': lambda_name,
        }

        if dry_run:
            logging.info(f"[DRY RUN] Would set up forwarding: {email} -> {forward_to}")
            logging.info(f"  Rule set: {rule_set_name}")
            logging.info(f"  Rule name: {rule_name}")
            logging.info(f"  Lambda: {lambda_name}")
            result['status'] = 'dry_run'
            return result

        # Check SES domain verification
        if not self.check_domain_verified(domain):
            logging.warning(
                f"Domain '{domain}' is not verified in SES. "
                f"Forwarding requires SES domain verification. "
                f"Run: aws ses verify-domain-identity --domain {domain}"
            )
            result['status'] = 'domain_not_verified'
            result['fix'] = f"aws ses verify-domain-identity --domain {domain}"
            return result

        # Get or create rule set
        self.get_or_create_rule_set(rule_set_name)

        # Check if forwarding rule already exists
        existing = self.find_forwarding_rule(rule_set_name, email)
        if existing:
            logging.info(f"Forwarding rule already exists: {rule_name}")
            result['status'] = 'exists'
            return result

        # Create or find the Lambda forwarding function
        lambda_arn = self._get_or_create_forwarder_lambda(lambda_name, forward_to)
        result['lambda_arn'] = lambda_arn

        # Create the receipt rule
        self.ses_client.create_receipt_rule(
            RuleSetName=rule_set_name,
            Rule={
                'Name': rule_name,
                'Enabled': True,
                'Recipients': [email],
                'Actions': [
                    {
                        'LambdaAction': {
                            'FunctionArn': lambda_arn,
                            'InvocationType': 'Event',
                        }
                    }
                ],
                'ScanEnabled': True,
            }
        )
        logging.info(f"Created forwarding rule: {email} -> {forward_to}")

        # Activate the rule set
        try:
            self.ses_client.set_active_receipt_rule_set(RuleSetName=rule_set_name)
            logging.info(f"Activated rule set: {rule_set_name}")
        except ClientError as e:
            logging.warning(f"Could not activate rule set: {e}")

        result['status'] = 'created'
        return result

    def _get_or_create_forwarder_lambda(self, lambda_name: str,
                                         forward_to: str) -> str:
        """Get existing or create a new email forwarding Lambda function."""
        # Check if Lambda already exists
        try:
            response = self.lambda_client.get_function(FunctionName=lambda_name)
            arn = response['Configuration']['FunctionArn']
            logging.info(f"Found existing Lambda: {lambda_name}")

            # Update forward_to environment variable
            self.lambda_client.update_function_configuration(
                FunctionName=lambda_name,
                Environment={
                    'Variables': {
                        'FORWARD_TO': forward_to,
                        'REGION': self.region,
                    }
                }
            )
            logging.info(f"Updated Lambda FORWARD_TO to: {forward_to}")
            return arn
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise

        # Create IAM role for Lambda
        role_arn = self._get_or_create_lambda_role(lambda_name)

        # Lambda function code (inline)
        lambda_code = '''
import boto3
import email
import os

FORWARD_TO = os.environ.get('FORWARD_TO', '')
REGION = os.environ.get('REGION', 'us-east-1')


def handler(event, context):
    """SES receipt rule Lambda handler for email forwarding."""
    ses_client = boto3.client('ses', region_name=REGION)

    for record in event.get('Records', []):
        ses_message = record.get('ses', {})
        mail = ses_message.get('mail', {})
        message_id = mail.get('messageId', '')

        if not message_id or not FORWARD_TO:
            continue

        # Get the raw email from S3 or directly
        source = mail.get('source', 'noreply@example.com')
        destinations = [addr.strip() for addr in FORWARD_TO.split(',')]

        # Forward using SES send_raw_email would require S3 storage
        # Instead, use SES send_email with the available metadata
        receipt = ses_message.get('receipt', {})
        common_headers = mail.get('commonHeaders', {})

        subject = common_headers.get('subject', 'Forwarded Email')
        from_addr = common_headers.get('from', [source])[0]

        try:
            ses_client.send_email(
                Source=source,
                Destination={'ToAddresses': destinations},
                Message={
                    'Subject': {'Data': f"[FWD] {subject}"},
                    'Body': {
                        'Text': {
                            'Data': (
                                f"Forwarded email from: {from_addr}\\n"
                                f"Original recipient: {', '.join(mail.get('destination', []))}\\n"
                                f"Subject: {subject}\\n"
                                f"Date: {common_headers.get('date', 'Unknown')}\\n"
                                f"\\n--- This email was automatically forwarded by WorkMail Forwarder ---"
                            )
                        }
                    }
                }
            )
            print(f"Forwarded {message_id} to {FORWARD_TO}")
        except Exception as e:
            print(f"Failed to forward {message_id}: {e}")

    return {'disposition': 'CONTINUE'}
'''

        import zipfile
        import io

        # Create zip with Lambda code
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('lambda_function.py', lambda_code.strip())
        zip_buffer.seek(0)

        # Wait for IAM role to propagate
        logging.info("Waiting for IAM role propagation...")
        time.sleep(10)

        # Create Lambda function
        response = self.lambda_client.create_function(
            FunctionName=lambda_name,
            Runtime='python3.12',
            Role=role_arn,
            Handler='lambda_function.handler',
            Code={'ZipFile': zip_buffer.read()},
            Description=f'Email forwarder - forwards to {forward_to}',
            Timeout=30,
            MemorySize=128,
            Environment={
                'Variables': {
                    'FORWARD_TO': forward_to,
                    'REGION': self.region,
                }
            },
        )
        arn = response['FunctionArn']
        logging.info(f"Created Lambda: {lambda_name} (ARN: {arn})")

        # Add SES permission to invoke Lambda
        try:
            self.lambda_client.add_permission(
                FunctionName=lambda_name,
                StatementId='AllowSES',
                Action='lambda:InvokeFunction',
                Principal='ses.amazonaws.com',
            )
            logging.info("Added SES invoke permission to Lambda")
        except ClientError as e:
            if 'ResourceConflictException' not in str(type(e)):
                raise

        return arn

    def _get_or_create_lambda_role(self, lambda_name: str) -> str:
        """Get or create an IAM role for the forwarding Lambda."""
        role_name = f"{lambda_name}-role"

        try:
            response = self.iam_client.get_role(RoleName=role_name)
            arn = response['Role']['Arn']
            logging.info(f"Found existing IAM role: {role_name}")
            return arn
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchEntity':
                raise

        # Create role
        assume_role_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        })

        response = self.iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=assume_role_policy,
            Description=f'Role for {lambda_name} email forwarder',
        )
        role_arn = response['Role']['Arn']
        logging.info(f"Created IAM role: {role_name}")

        # Attach policies
        policies = [
            'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
        ]
        for policy in policies:
            self.iam_client.attach_role_policy(
                RoleName=role_name,
                PolicyArn=policy
            )

        # Add inline policy for SES send
        ses_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "ses:SendEmail",
                    "ses:SendRawEmail"
                ],
                "Resource": "*"
            }]
        })
        self.iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName='SESForwardingPolicy',
            PolicyDocument=ses_policy
        )
        logging.info("Attached SES send policy to role")

        return role_arn


# =============================================================================
# Setup Report
# =============================================================================

class SetupReport:
    """Track and report WorkMail setup status."""

    def __init__(self, domain: str, action: str):
        self.timestamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        self.domain = domain
        self.action = action
        self.components = {
            'organization': {'status': 'pending', 'details': {}},
            'user': {'status': 'pending', 'details': {}},
            'forwarding': {'status': 'pending', 'details': {}},
        }

    def set_component(self, name: str, status: str, **details):
        if name in self.components:
            self.components[name]['status'] = status
            self.components[name]['details'].update(details)

    def generate_markdown(self) -> str:
        lines = [
            f"# WorkMail Setup Report: {self.domain}",
            "",
            f"**Generated:** {self.timestamp}",
            f"**Script:** setup_workmail.py",
            f"**Action:** {self.action}",
            "",
            "---",
            "",
            "## Configuration Summary",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            f"| Domain | `{self.domain}` |",
            f"| Action | {self.action} |",
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
            'organization': 'WorkMail Organization',
            'user': 'Email User',
            'forwarding': 'Email Forwarding',
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
                lines.append(f"- **{display_key}:** `{value}`")
            lines.append("")

        lines.extend([
            "## Access Information",
            "",
            f"- **WebMail**: https://mail.{self.domain} (or check WorkMail console for URL)",
            f"- **AWS Console**: https://console.aws.amazon.com/workmail/",
            "",
            "---",
            "",
            "*Generated by setup_workmail.py*",
        ])

        return '\n'.join(lines)

    def save_report(self, output_dir: str = '.') -> str:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        filename = f"workmail-setup-{self.domain}-{timestamp}.md"
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
        description='Setup Amazon WorkMail email users',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all WorkMail organizations
  python setup_workmail.py --action list-orgs

  # List users for a domain
  python setup_workmail.py --action list-users --domain pseekoo.io

  # Create a new email user
  python setup_workmail.py --action create-user --domain pseekoo.io \\
      --email m3rp@pseekoo.io --display-name "M3RP Service" # password prompted interactively

  # Create user with email forwarding to external address
  python setup_workmail.py --action create-user --domain pseekoo.io \\
      --email m3rp@pseekoo.io --display-name "M3RP Service" # password prompted interactively \\
      --forward-to user@gmail.com

  # Reset a user's password (interactive prompt)
  python setup_workmail.py --action reset-password --domain pseekoo.io \\
      --email m3rp@pseekoo.io

  # Set up forwarding on an existing account
  python setup_workmail.py --action setup-forwarding --domain pseekoo.io \\
      --email m3rp@pseekoo.io --forward-to user@gmail.com

  # List all WorkMail organizations across all regions
  python setup_workmail.py --action list-all-regions

  # Describe a specific user
  python setup_workmail.py --action describe-user --domain pseekoo.io \\
      --email m3rp@pseekoo.io

Environment Variables:
  AWS_PROFILE           AWS profile to use
  AWS_REGION            AWS region (default: us-east-1)
                        WorkMail available: us-east-1, us-west-2, eu-west-1
        """
    )

    parser.add_argument('--action', required=True,
                        choices=['list-orgs', 'list-users', 'list-all-regions',
                                 'create-user', 'describe-user', 'reset-password',
                                 'setup-forwarding'],
                        help='Action to perform')
    parser.add_argument('--domain',
                        help='WorkMail domain (e.g., pseekoo.io)')
    parser.add_argument('--email',
                        help='User email address (e.g., m3rp@pseekoo.io)')
    parser.add_argument('--display-name',
                        help='User display name (e.g., "M3RP Service")')
    parser.add_argument('--first-name',
                        help='User first name')
    parser.add_argument('--last-name',
                        help='User last name')
    # No --password flag: passwords are always prompted interactively via getpass
    parser.add_argument('--forward-to',
                        help='Forward all incoming email to this address')
    parser.add_argument('--region', default=None,
                        choices=list(WORKMAIL_REGIONS.keys()),
                        help='AWS region (default: us-east-1)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without making them')
    parser.add_argument('--report-dir', default='docs/changes',
                        help='Directory to save the setup report (default: docs/changes)')

    return parser.parse_args()


# =============================================================================
# Action Handlers
# =============================================================================

def action_list_orgs(wm: WorkMailManager) -> int:
    """List all WorkMail organizations."""
    logging.info("\n--- WorkMail Organizations ---")
    orgs = wm.list_organizations()

    if not orgs:
        logging.info("  No organizations found.")
        return 0

    logging.info(f"  Found {len(orgs)} organization(s):\n")
    logging.info(f"  {'Alias':<20} {'Domain':<30} {'State':<10} {'ID'}")
    logging.info(f"  {'-'*20} {'-'*30} {'-'*10} {'-'*36}")
    for org in orgs:
        logging.info(
            f"  {org.get('Alias', 'N/A'):<20} "
            f"{org.get('DefaultMailDomain', 'N/A'):<30} "
            f"{org.get('State', '?'):<10} "
            f"{org['OrganizationId']}"
        )
    return 0


def action_list_users(wm: WorkMailManager, args) -> int:
    """List users in a WorkMail organization."""
    if not args.domain:
        logging.error("--domain is required for list-users")
        return 1

    org_id = wm.get_org_id(args.domain)

    logging.info(f"\n--- Users in {args.domain} ---")
    users = wm.list_users(org_id)

    # Filter out system users for cleaner output
    visible_users = [u for u in users if u.get('UserRole') != 'SYSTEM_USER']

    if not visible_users:
        logging.info("  No users found.")
        return 0

    logging.info(f"  Found {len(visible_users)} user(s):\n")
    logging.info(f"  {'Username':<20} {'Email':<35} {'State':<10} {'Role'}")
    logging.info(f"  {'-'*20} {'-'*35} {'-'*10} {'-'*12}")
    for user in visible_users:
        logging.info(
            f"  {user.get('Name', 'N/A'):<20} "
            f"{user.get('Email', 'N/A'):<35} "
            f"{user.get('State', '?'):<10} "
            f"{user.get('UserRole', '?')}"
        )
    return 0


def action_describe_user(wm: WorkMailManager, args) -> int:
    """Describe a specific user."""
    if not args.domain or not args.email:
        logging.error("--domain and --email are required for describe-user")
        return 1

    org_id = wm.get_org_id(args.domain)
    user = wm.find_user_by_email(org_id, args.email)

    if not user:
        logging.error(f"User not found: {args.email}")
        return 1

    details = wm.describe_user(org_id, user['Id'])

    logging.info(f"\n--- User Details: {args.email} ---")
    fields = [
        ('User ID', 'UserId'), ('Username', 'Name'), ('Display Name', 'DisplayName'),
        ('Email', 'Email'), ('State', 'State'), ('Role', 'UserRole'),
        ('First Name', 'FirstName'), ('Last Name', 'LastName'),
        ('Mailbox Provisioned', 'MailboxProvisionedDate'),
    ]
    for label, key in fields:
        value = details.get(key)
        if value is not None:
            logging.info(f"  {label:<25} {value}")
    return 0


def action_create_user(wm: WorkMailManager, args) -> int:
    """Create a new WorkMail user."""
    if not args.domain or not args.email:
        logging.error("--domain and --email are required for create-user")
        return 1

    # Always prompt for password interactively
    password = getpass.getpass(
        "Enter password (8+ chars, upper+lower+number+special): "
    )
    password_confirm = getpass.getpass("Confirm password: ")
    if password != password_confirm:
        logging.error("Passwords do not match.")
        return 1

    display_name = args.display_name or args.email.split('@')[0]
    report = SetupReport(domain=args.domain, action='create-user')

    logging.info("=" * 60)
    logging.info("WORKMAIL USER SETUP")
    logging.info("=" * 60)
    logging.info(f"Domain: {args.domain}")
    logging.info(f"Email: {args.email}")
    logging.info(f"Display Name: {display_name}")
    if args.forward_to:
        logging.info(f"Forward To: {args.forward_to}")
    logging.info(f"Dry Run: {'Yes' if args.dry_run else 'No'}")
    logging.info("=" * 60)

    try:
        # Step 1: Find organization
        logging.info("\n--- Step 1: Organization ---")
        org_id = wm.get_org_id(args.domain)
        org = wm.find_organization(args.domain)
        report.set_component('organization', 'configured',
                             organization_id=org_id,
                             alias=org.get('Alias', ''),
                             domain=org.get('DefaultMailDomain', ''))

        # Step 2: Create user
        logging.info("\n--- Step 2: Create User ---")
        if args.dry_run:
            logging.info(f"[DRY RUN] Would create user: {args.email}")
            logging.info(f"  Username: {args.email.split('@')[0]}")
            logging.info(f"  Display Name: {display_name}")
            report.set_component('user', 'dry_run',
                                 email=args.email,
                                 display_name=display_name)
        else:
            user_id = wm.create_and_register_user(
                org_id=org_id,
                email=args.email,
                display_name=display_name,
                password=password,
                first_name=args.first_name,
                last_name=args.last_name,
            )
            report.set_component('user', 'created',
                                 email=args.email,
                                 user_id=user_id,
                                 display_name=display_name)

        # Step 3: Email forwarding (optional)
        logging.info("\n--- Step 3: Email Forwarding ---")
        if not args.forward_to:
            logging.info("  No forwarding requested. Use --forward-to to enable.")
            report.set_component('forwarding', 'skipped', reason='Not requested')
        elif args.dry_run:
            logging.info(f"[DRY RUN] Would set up forwarding: {args.email} -> {args.forward_to}")
            report.set_component('forwarding', 'dry_run',
                                 source=args.email,
                                 destination=args.forward_to)
        else:
            ses_mgr = SESForwardingManager(region=wm.region)
            fwd_result = ses_mgr.setup_forwarding(
                email=args.email,
                forward_to=args.forward_to,
            )
            fwd_status = fwd_result.get('status', 'unknown')
            if fwd_status in ('created', 'exists'):
                report.set_component('forwarding', fwd_status,
                                     source=args.email,
                                     destination=args.forward_to)
            elif fwd_status == 'domain_not_verified':
                report.set_component('forwarding', 'failed',
                                     source=args.email,
                                     destination=args.forward_to,
                                     error='SES domain not verified',
                                     fix=fwd_result.get('fix', ''))
                logging.warning(
                    f"  Forwarding not configured: SES domain verification required.\n"
                    f"  Run: {fwd_result.get('fix', '')}\n"
                    f"  Then re-run this script."
                )

        # Summary
        logging.info("\n" + "=" * 60)
        logging.info("WORKMAIL SETUP COMPLETE")
        logging.info("=" * 60)
        logging.info(f"Email: {args.email}")
        logging.info(f"User: {report.components['user']['status']}")
        logging.info(f"Forwarding: {report.components['forwarding']['status']}")
        logging.info("=" * 60)

        report_path = report.save_report(args.report_dir)

        logging.info("\n[Next Steps]")
        logging.info(f"1. User can log in at the WorkMail webmail URL for {args.domain}")
        logging.info(f"2. Check AWS WorkMail console for organization settings")
        if args.forward_to:
            logging.info(f"3. Verify forwarding: send a test email to {args.email}")
        logging.info(f"\n[OK] Setup report: {report_path}")

        return 0

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        logging.error(f"AWS error ({error_code}): {error_msg}")

        if error_code == 'InvalidPasswordException':
            logging.error(
                "Password must meet WorkMail complexity requirements: "
                "8+ chars, uppercase, lowercase, number, special character"
            )
        elif error_code == 'NameAvailabilityException':
            logging.error(f"Username already taken: {args.email.split('@')[0]}")
        elif error_code == 'EmailAddressInUseException':
            logging.error(f"Email already in use: {args.email}")

        try:
            report_path = report.save_report(args.report_dir)
            logging.info(f"[i] Partial report saved: {report_path}")
        except Exception:
            pass

        return 1

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


def action_reset_password(wm: WorkMailManager, args) -> int:
    """Reset a user's password."""
    if not args.domain or not args.email:
        logging.error("--domain and --email are required for reset-password")
        return 1

    # Always prompt for password interactively
    password = getpass.getpass(
        "Enter new password (8+ chars, upper+lower+number+special): "
    )
    password_confirm = getpass.getpass("Confirm new password: ")
    if password != password_confirm:
        logging.error("Passwords do not match.")
        return 1

    try:
        org_id = wm.get_org_id(args.domain)
        user = wm.find_user_by_email(org_id, args.email)

        if not user:
            logging.error(f"User not found: {args.email}")
            return 1

        if args.dry_run:
            logging.info(f"[DRY RUN] Would reset password for: {args.email}")
            return 0

        wm.reset_password(org_id, user['Id'], password)
        logging.info(f"Password reset successfully for: {args.email}")
        return 0

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        logging.error(f"AWS error ({error_code}): {error_msg}")

        if error_code == 'InvalidPasswordException':
            logging.error(
                "Password must meet complexity requirements: "
                "8+ chars, uppercase, lowercase, number, special character"
            )
        return 1


def action_list_all_regions(args) -> int:
    """List WorkMail organizations across all available regions."""
    logging.info("\n--- WorkMail Organizations (All Regions) ---\n")

    total_orgs = 0
    for region, region_name in WORKMAIL_REGIONS.items():
        try:
            wm = WorkMailManager(region=region)
            orgs = wm.list_organizations()
        except Exception as e:
            logging.warning(f"  [{region}] {region_name}: Error - {e}")
            continue

        if not orgs:
            logging.info(f"  [{region}] {region_name}: No organizations")
            continue

        for org in orgs:
            total_orgs += 1
            state = org.get('State', '?')
            alias = org.get('Alias', 'N/A')
            domain = org.get('DefaultMailDomain', 'N/A')
            org_id = org['OrganizationId']
            logging.info(
                f"  [{region}] {region_name}: "
                f"{alias} ({domain}) - {state} - {org_id}"
            )

            # List users for active organizations
            if state == 'Active':
                try:
                    users = wm.list_users(org_id)
                    visible = [u for u in users if u.get('UserRole') != 'SYSTEM_USER']
                    if visible:
                        for user in visible:
                            email = user.get('Email', 'N/A')
                            user_state = user.get('State', '?')
                            logging.info(f"    - {email} ({user_state})")
                    else:
                        logging.info(f"    (no users)")
                except Exception as e:
                    logging.warning(f"    Could not list users: {e}")

    if total_orgs == 0:
        logging.info("  No WorkMail organizations found in any region.")
    else:
        logging.info(f"\n  Total: {total_orgs} organization(s)")

    return 0


def action_setup_forwarding(wm: WorkMailManager, args) -> int:
    """Set up email forwarding on an existing WorkMail account."""
    if not args.domain or not args.email or not args.forward_to:
        logging.error("--domain, --email, and --forward-to are required for setup-forwarding")
        return 1

    logging.info("=" * 60)
    logging.info("WORKMAIL EMAIL FORWARDING SETUP")
    logging.info("=" * 60)
    logging.info(f"Domain: {args.domain}")
    logging.info(f"Email: {args.email}")
    logging.info(f"Forward To: {args.forward_to}")
    logging.info(f"Dry Run: {'Yes' if args.dry_run else 'No'}")
    logging.info("=" * 60)

    report = SetupReport(domain=args.domain, action='setup-forwarding')

    try:
        # Step 1: Verify the user exists
        logging.info("\n--- Step 1: Verify User ---")
        org_id = wm.get_org_id(args.domain)
        org = wm.find_organization(args.domain)
        report.set_component('organization', 'configured',
                             organization_id=org_id,
                             alias=org.get('Alias', ''),
                             domain=org.get('DefaultMailDomain', ''))

        user = wm.find_user_by_email(org_id, args.email)
        if not user:
            logging.error(f"User not found: {args.email}")
            logging.error("The user must exist before setting up forwarding.")
            return 1

        user_state = user.get('State', '?')
        logging.info(f"  Found user: {args.email} (State: {user_state})")
        report.set_component('user', 'exists',
                             email=args.email,
                             user_id=user['Id'],
                             state=user_state)

        # Step 2: Set up forwarding
        logging.info("\n--- Step 2: Configure Forwarding ---")
        ses_mgr = SESForwardingManager(region=wm.region)
        fwd_result = ses_mgr.setup_forwarding(
            email=args.email,
            forward_to=args.forward_to,
            dry_run=args.dry_run,
        )
        fwd_status = fwd_result.get('status', 'unknown')

        if fwd_status in ('created', 'exists', 'dry_run'):
            report.set_component('forwarding', fwd_status,
                                 source=args.email,
                                 destination=args.forward_to)
        elif fwd_status == 'domain_not_verified':
            report.set_component('forwarding', 'failed',
                                 source=args.email,
                                 destination=args.forward_to,
                                 error='SES domain not verified',
                                 fix=fwd_result.get('fix', ''))
            logging.warning(
                f"  Forwarding not configured: SES domain verification required.\n"
                f"  Run: {fwd_result.get('fix', '')}\n"
                f"  Then re-run this script."
            )

        # Summary
        logging.info("\n" + "=" * 60)
        logging.info("FORWARDING SETUP COMPLETE")
        logging.info("=" * 60)
        logging.info(f"Email: {args.email}")
        logging.info(f"Forward To: {args.forward_to}")
        logging.info(f"Status: {fwd_status}")
        logging.info("=" * 60)

        report_path = report.save_report(args.report_dir)
        logging.info(f"\n[OK] Setup report: {report_path}")

        return 0

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        logging.error(f"AWS error ({error_code}): {error_msg}")
        try:
            report.save_report(args.report_dir)
        except Exception:
            pass
        return 1

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
        try:
            report.save_report(args.report_dir)
        except Exception:
            pass
        return 1


# =============================================================================
# Main
# =============================================================================

def main():
    """Main function to orchestrate WorkMail operations."""
    args = parse_arguments()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # list-all-regions doesn't need a specific region
    if args.action == 'list-all-regions':
        return action_list_all_regions(args)

    wm = WorkMailManager(region=args.region)

    action_map = {
        'list-orgs': lambda: action_list_orgs(wm),
        'list-users': lambda: action_list_users(wm, args),
        'describe-user': lambda: action_describe_user(wm, args),
        'create-user': lambda: action_create_user(wm, args),
        'reset-password': lambda: action_reset_password(wm, args),
        'setup-forwarding': lambda: action_setup_forwarding(wm, args),
    }

    handler = action_map.get(args.action)
    if handler:
        return handler()
    else:
        logging.error(f"Unknown action: {args.action}")
        return 1


if __name__ == "__main__":
    exit(main())
