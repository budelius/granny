"""SES email forwarding manager — Lambda + IAM + SES receipt rules.

Lifted from ``tools/create/setup_workmail.py:SESForwardingManager``.
Sets up email forwarding from a WorkMail address to an external address
using SES receipt rules with a Lambda function.
"""

import io
import json
import logging
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class SESForwardingManager:
    """Email forwarding via Amazon SES receipt rules + Lambda."""

    def __init__(self, region: str | None = None) -> None:
        import os

        region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.region = region
        self.ses = boto3.client("ses", region_name=region)
        self.lam = boto3.client("lambda", region_name=region)
        self.iam = boto3.client("iam", region_name=region)

    def check_domain_verified(self, domain: str) -> bool:
        resp = self.ses.list_identities(IdentityType="Domain")
        return domain in resp.get("Identities", [])

    def get_or_create_rule_set(
        self, rule_set_name: str = "workmail-forwarding"
    ) -> str:
        try:
            self.ses.describe_receipt_rule_set(RuleSetName=rule_set_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "RuleSetDoesNotExist":
                self.ses.create_receipt_rule_set(RuleSetName=rule_set_name)
                logger.info("Created SES rule set: %s", rule_set_name)
            else:
                raise
        return rule_set_name

    def find_forwarding_rule(
        self, rule_set_name: str, email: str
    ) -> dict | None:
        try:
            resp = self.ses.describe_receipt_rule_set(RuleSetName=rule_set_name)
            rule_name = f"forward-{email.replace('@', '-at-')}"
            for rule in resp.get("Rules", []):
                if rule.get("Name") == rule_name:
                    return rule
        except ClientError:
            pass
        return None

    def setup_forwarding(
        self,
        email: str,
        forward_to: str,
        rule_set_name: str = "workmail-forwarding",
        *,
        dry_run: bool = False,
    ) -> dict:
        """Set up SES receipt rule + Lambda forwarder.

        Returns a status dict. The WorkMail mailbox still receives the original.
        """
        domain = email.split("@")[1]
        rule_name = f"forward-{email.replace('@', '-at-')}"
        lambda_name = f"workmail-forward-{domain.replace('.', '-')}"
        result = {
            "email": email,
            "forward_to": forward_to,
            "rule_set": rule_set_name,
            "rule_name": rule_name,
            "lambda_name": lambda_name,
        }

        if dry_run:
            result["status"] = "dry_run"
            return result

        if not self.check_domain_verified(domain):
            result["status"] = "domain_not_verified"
            result["fix"] = f"aws ses verify-domain-identity --domain {domain}"
            return result

        self.get_or_create_rule_set(rule_set_name)

        existing = self.find_forwarding_rule(rule_set_name, email)
        if existing:
            result["status"] = "exists"
            return result

        lambda_arn = self._get_or_create_lambda(lambda_name, forward_to)
        result["lambda_arn"] = lambda_arn

        self.ses.create_receipt_rule(
            RuleSetName=rule_set_name,
            Rule={
                "Name": rule_name,
                "Enabled": True,
                "Recipients": [email],
                "Actions": [
                    {
                        "LambdaAction": {
                            "FunctionArn": lambda_arn,
                            "InvocationType": "Event",
                        }
                    }
                ],
                "ScanEnabled": True,
            },
        )
        logger.info("Created forwarding rule: %s -> %s", email, forward_to)

        try:
            self.ses.set_active_receipt_rule_set(RuleSetName=rule_set_name)
        except ClientError as exc:
            logger.warning("Could not activate rule set: %s", exc)

        result["status"] = "created"
        return result

    def _get_or_create_lambda(self, lambda_name: str, forward_to: str) -> str:
        try:
            resp = self.lam.get_function(FunctionName=lambda_name)
            arn = resp["Configuration"]["FunctionArn"]
            self.lam.update_function_configuration(
                FunctionName=lambda_name,
                Environment={
                    "Variables": {"FORWARD_TO": forward_to, "REGION": self.region}
                },
            )
            return arn
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

        role_arn = self._get_or_create_role(lambda_name)

        code = _FORWARDER_LAMBDA_CODE.strip()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("lambda_function.py", code)
        buf.seek(0)

        logger.info("Waiting for IAM role propagation...")
        time.sleep(10)

        resp = self.lam.create_function(
            FunctionName=lambda_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_function.handler",
            Code={"ZipFile": buf.read()},
            Description=f"Email forwarder -> {forward_to}",
            Timeout=30,
            MemorySize=128,
            Environment={
                "Variables": {"FORWARD_TO": forward_to, "REGION": self.region}
            },
        )
        arn = resp["FunctionArn"]
        logger.info("Created Lambda %s", lambda_name)

        try:
            self.lam.add_permission(
                FunctionName=lambda_name,
                StatementId="AllowSES",
                Action="lambda:InvokeFunction",
                Principal="ses.amazonaws.com",
            )
        except ClientError:
            pass

        return arn

    def _get_or_create_role(self, lambda_name: str) -> str:
        role_name = f"{lambda_name}-role"
        try:
            resp = self.iam.get_role(RoleName=role_name)
            return resp["Role"]["Arn"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                raise

        assume = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )
        resp = self.iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=assume,
            Description=f"Role for {lambda_name} forwarder",
        )
        role_arn = resp["Role"]["Arn"]

        self.iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )

        ses_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["ses:SendEmail", "ses:SendRawEmail"],
                        "Resource": "*",
                    }
                ],
            }
        )
        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName="SESForwardingPolicy",
            PolicyDocument=ses_policy,
        )
        return role_arn


_FORWARDER_LAMBDA_CODE = '''
import boto3
import os

FORWARD_TO = os.environ.get('FORWARD_TO', '')
REGION = os.environ.get('REGION', 'us-east-1')


def handler(event, context):
    ses_client = boto3.client('ses', region_name=REGION)
    for record in event.get('Records', []):
        ses_message = record.get('ses', {})
        mail = ses_message.get('mail', {})
        message_id = mail.get('messageId', '')
        if not message_id or not FORWARD_TO:
            continue
        source = mail.get('source', 'noreply@example.com')
        destinations = [addr.strip() for addr in FORWARD_TO.split(',')]
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
                                f"\\n--- Forwarded by WorkMail Forwarder ---"
                            )
                        }
                    }
                }
            )
        except Exception as e:
            print(f"Failed to forward {message_id}: {e}")
    return {'disposition': 'CONTINUE'}
'''
