"""Mailjet contact management — check, block, unblock, bounce clearing.

Lifted from ``tools/create/manage_mailjet_contacts.py:MailjetContactClient``.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

API_BASE = "https://api.mailjet.com/v3/REST"


class MailjetContactClient:
    """Mailjet REST API client for contact management."""

    def __init__(self) -> None:
        api_key = get_secret("MAILJET_API_KEY")
        secret_key = get_secret("MAILJET_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError(
                "MAILJET_API_KEY / MAILJET_SECRET_KEY not set (env or vault)."
            )
        self.session = requests.Session()
        self.session.auth = (api_key, secret_key)
        self.session.headers.update({"Content-Type": "application/json"})

    def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        url = f"{API_BASE}{endpoint}"
        resp = getattr(self.session, method.lower())(
            url, json=data, params=params, timeout=30
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Mailjet {method} {endpoint}: {resp.status_code} {resp.text}"
            )
        return resp.json() if resp.text else {}

    def get_contact(self, email: str) -> dict | None:
        result = self._request("GET", f"/contact/{email}")
        if not result:
            return None
        contacts = result.get("Data", [])
        return contacts[0] if contacts else None

    def unblock_contact(self, email: str) -> dict:
        result = self._request(
            "PUT", f"/contact/{email}", data={"IsExcludedFromCampaigns": "false"}
        )
        if not result:
            raise RuntimeError(f"Contact not found: {email}")
        return result.get("Data", [{}])[0]

    def block_contact(self, email: str) -> dict:
        result = self._request(
            "PUT", f"/contact/{email}", data={"IsExcludedFromCampaigns": "true"}
        )
        if not result:
            raise RuntimeError(f"Contact not found: {email}")
        return result.get("Data", [{}])[0]

    def list_contacts(
        self, limit: int = 1000, offset: int = 0, is_excluded: bool | None = None
    ) -> list[dict]:
        params: dict[str, Any] = {"Limit": min(limit, 1000), "Offset": offset}
        if is_excluded is not None:
            params["IsExcludedFromCampaigns"] = str(is_excluded).lower()
        result = self._request("GET", "/contact", params=params)
        return result.get("Data", []) if result else []

    def list_all_excluded(self, domain: str | None = None) -> list[dict]:
        """Paginate through all excluded contacts."""
        all_contacts: list[dict] = []
        offset = 0
        while True:
            batch = self.list_contacts(limit=1000, offset=offset, is_excluded=True)
            if not batch:
                break
            if domain:
                batch = [
                    c for c in batch if c.get("Email", "").endswith(f"@{domain}")
                ]
            all_contacts.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        return all_contacts

    def get_message_history(self, contact_id: int, limit: int = 10) -> list[dict]:
        result = self._request(
            "GET", "/message", params={"Contact": contact_id, "Limit": limit}
        )
        return result.get("Data", []) if result else []

    def delete_contact(self, contact_id: int) -> bool:
        """Delete via v4 API (clears bounce history)."""
        url = f"https://api.mailjet.com/v4/contacts/{contact_id}"
        resp = self.session.delete(url, timeout=30)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise RuntimeError(f"Mailjet v4 delete: {resp.status_code} {resp.text}")
