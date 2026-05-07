"""Mailjet REST API client — sender CRUD, DNS settings, verification.

Lifted from ``tools/create/setup_mailjet_dns.py:MailjetClient``.
Also includes SPF parsing/merging utilities used during DNS auth setup.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

API_BASE = "https://api.mailjet.com/v3/REST"


class MailjetClient:
    """Mailjet REST API v3 client for sender and DNS management."""

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

    def _request(self, method: str, endpoint: str, data: dict | None = None) -> Any:
        url = f"{API_BASE}{endpoint}"
        resp = getattr(self.session, method.lower())(url, json=data, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Mailjet {method} {endpoint}: {resp.status_code} {resp.text}"
            )
        return resp.json() if resp.text else {}

    def find_sender(self, domain: str) -> dict | None:
        """Find a catch-all ``*@domain`` sender."""
        result = self._request("GET", "/sender")
        catch_all = f"*@{domain}"
        for sender in result.get("Data", []):
            if sender.get("Email", "").lower() == catch_all.lower():
                return sender
        return None

    def create_sender(self, domain: str) -> dict:
        catch_all = f"*@{domain}"
        result = self._request("POST", "/sender", {"Email": catch_all})
        sender = result.get("Data", [{}])[0]
        logger.info("Created Mailjet sender %s (ID %s)", catch_all, sender.get("ID"))
        return sender

    def get_or_create_sender(self, domain: str) -> dict:
        sender = self.find_sender(domain)
        if sender:
            logger.info("Found existing sender *@%s (ID %s)", domain, sender.get("ID"))
            return sender
        return self.create_sender(domain)

    def get_dns_settings(self, domain: str) -> dict:
        """Retrieve DKIM/SPF/ownership settings from Mailjet API."""
        result = self._request("GET", f"/dns/{domain}")
        return result.get("Data", [{}])[0]

    def check_dns(self, dns_id: str) -> dict:
        """Trigger a Mailjet DNS verification check."""
        result = self._request("POST", f"/dns/{dns_id}/check", {})
        return result.get("Data", [{}])[0]


# -- SPF utilities (used by setup_mailjet_dns.py and granny email mailjet) -----


def parse_spf(spf_record: str) -> tuple[list[str], str]:
    """Parse an SPF record into (mechanisms, qualifier).

    Returns e.g. (["include:_spf.google.com", "include:spf.mailjet.com"], "~all").

    Some DNS providers (notably Cloudflare) return TXT record content with
    surrounding quotes (``"v=spf1 ..."``) and split long records into
    multiple quoted strings (``"v=spf1 ..." "include:other.com -all"``).
    Strip those before tokenizing so callers can pass raw provider values.
    """
    raw = spf_record.strip()
    # Concatenate adjacent quoted strings (RFC 1035 multi-string TXT)
    # and unwrap any single wrapping pair: `"foo" "bar"` -> `foo bar`,
    # `"foo"` -> `foo`. Naive unquote -- SPF values don't contain literal
    # backslash-escaped quotes in practice.
    if '"' in raw:
        chunks: list[str] = []
        in_quote = False
        buf: list[str] = []
        for ch in raw:
            if ch == '"':
                in_quote = not in_quote
                continue
            if in_quote:
                buf.append(ch)
            elif ch.isspace() and buf:
                chunks.append("".join(buf))
                buf = []
        if buf:
            chunks.append("".join(buf))
        if chunks:
            # SPF tokens are whitespace-separated; rejoin with a space so a
            # multi-string TXT split between tokens reconstructs cleanly.
            raw = " ".join(chunks)

    parts = raw.split()
    mechanisms: list[str] = []
    qualifier = "~all"
    for part in parts:
        if part == "v=spf1":
            continue
        if part.endswith("all"):
            qualifier = part
        else:
            mechanisms.append(part)
    return mechanisms, qualifier


def merge_spf(
    existing_spf: str | None,
    target_qualifier: str = "~all",
    include: str = "include:spf.mailjet.com",
) -> tuple[str, list[str]]:
    """Merge a Mailjet include into an existing SPF record.

    Returns (merged_record, warnings).
    """
    warnings: list[str] = []
    if not existing_spf:
        return f"v=spf1 {include} {target_qualifier}", warnings

    mechanisms, qualifier = parse_spf(existing_spf)

    if include not in mechanisms:
        mechanisms.append(include)

    if qualifier == "?all" and target_qualifier in ("~all", "-all"):
        warnings.append(f"Upgraded SPF qualifier from ?all to {target_qualifier}")
        qualifier = target_qualifier

    return f"v=spf1 {' '.join(mechanisms)} {qualifier}", warnings
