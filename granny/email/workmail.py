"""AWS WorkMail user management — org/user CRUD, password reset.

Lifted from ``tools/create/setup_workmail.py:WorkMailManager``.
"""

import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)

WORKMAIL_REGIONS = {
    "us-east-1": "N. Virginia",
    "us-west-2": "Oregon",
    "eu-west-1": "Ireland",
}


class WorkMailManager:
    """Amazon WorkMail organization and user management."""

    def __init__(self, region: str | None = None) -> None:
        import os

        region = region or os.environ.get("AWS_REGION", "us-east-1")
        if region not in WORKMAIL_REGIONS:
            raise ValueError(
                f"WorkMail not available in {region!r}. "
                f"Choices: {', '.join(WORKMAIL_REGIONS)}"
            )
        self.region = region
        self.client = boto3.client("workmail", region_name=region)
        self._org_cache: dict[str, dict] = {}

    def list_organizations(self) -> list[dict]:
        orgs: list[dict] = []
        paginator = self.client.get_paginator("list_organizations")
        for page in paginator.paginate():
            orgs.extend(page.get("OrganizationSummaries", []))
        return orgs

    def find_organization(self, domain: str) -> dict | None:
        if domain in self._org_cache:
            return self._org_cache[domain]
        for org in self.list_organizations():
            if org.get("State") != "Active":
                continue
            if org.get("DefaultMailDomain") == domain or org.get("Alias") == domain:
                self._org_cache[domain] = org
                return org
        return None

    def get_org_id(self, domain: str) -> str:
        org = self.find_organization(domain)
        if not org:
            available = [
                o.get("DefaultMailDomain", o.get("Alias", "?"))
                for o in self.list_organizations()
            ]
            raise RuntimeError(
                f"WorkMail org not found for {domain!r}. Available: {available}"
            )
        return org["OrganizationId"]

    def list_users(self, org_id: str) -> list[dict]:
        users: list[dict] = []
        paginator = self.client.get_paginator("list_users")
        for page in paginator.paginate(OrganizationId=org_id):
            users.extend(page.get("Users", []))
        return users

    def find_user_by_email(self, org_id: str, email: str) -> dict | None:
        for user in self.list_users(org_id):
            if user.get("Email", "").lower() == email.lower():
                return user
        return None

    def describe_user(self, org_id: str, user_id: str) -> dict:
        return self.client.describe_user(OrganizationId=org_id, UserId=user_id)

    def create_user(
        self,
        org_id: str,
        username: str,
        display_name: str,
        password: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> str:
        """Create a user. Returns user ID."""
        params: dict[str, Any] = {
            "OrganizationId": org_id,
            "Name": username,
            "DisplayName": display_name,
            "Password": password,
        }
        if first_name:
            params["FirstName"] = first_name
        if last_name:
            params["LastName"] = last_name
        resp = self.client.create_user(**params)
        user_id = resp["UserId"]
        logger.info("Created user %s (ID %s)", username, user_id)
        return user_id

    def register_to_workmail(self, org_id: str, user_id: str, email: str) -> None:
        self.client.register_to_work_mail(
            OrganizationId=org_id, EntityId=user_id, Email=email
        )
        logger.info("Registered %s for WorkMail", email)

    def reset_password(self, org_id: str, user_id: str, password: str) -> None:
        self.client.reset_password(
            OrganizationId=org_id, UserId=user_id, Password=password
        )
        logger.info("Password reset for user %s", user_id)

    def create_and_register_user(
        self,
        org_id: str,
        email: str,
        display_name: str,
        password: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> str:
        """Create + register (2-step). Returns user ID. Idempotent."""
        existing = self.find_user_by_email(org_id, email)
        if existing:
            logger.info("User %s already exists (ID %s)", email, existing["Id"])
            return existing["Id"]
        username = email.split("@")[0]
        user_id = self.create_user(
            org_id,
            username,
            display_name,
            password,
            first_name=first_name,
            last_name=last_name,
        )
        self.register_to_workmail(org_id, user_id, email)
        return user_id
