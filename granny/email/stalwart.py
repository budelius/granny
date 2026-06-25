"""Stalwart mail server admin client.

Wraps Stalwart's JMAP management endpoint (POST ``/jmap``) and the small
REST surface granny needs. The web admin UI uses the
``urn:stalwart:jmap`` capability with ``x:Domain/*`` and ``x:Account/*``
methods; there is no REST equivalent for domain/mailbox CRUD, so we use
the same JMAP calls.

Auth is HTTP Basic with the recovery (or post-bootstrap) admin
credentials. URLs and credentials resolve via the standard granny
env-var + Vaultwarden pipeline.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import string
from collections.abc import Iterator
from typing import Any

import requests

from granny.credentials import get_secret

logger = logging.getLogger(__name__)

# Stalwart's web admin uses a fixed accountId "d333333" for all domain /
# account management calls. It identifies the directory-management
# principal, not a user account.
_DIRECTORY_ACCOUNT_ID = "d333333"

_JMAP_CAPS = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]


class StalwartError(RuntimeError):
    """Raised on non-2xx responses from Stalwart or JMAP-level errors."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class StalwartClient:
    """Authenticated client for Stalwart's JMAP management API.

    Construct directly when you already have the credentials, or use
    :meth:`from_environment` to resolve them via granny's normal env /
    vault pipeline.
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers.update({"Accept": "application/json"})

    @classmethod
    def from_environment(cls) -> "StalwartClient":
        """Build a client from env vars + Vaultwarden fallback.

        Resolution:

        - ``STALWART_URL`` (env only; default ``https://mail.pseekoo.io``)
        - ``STALWART_ADMIN_USER`` and ``STALWART_ADMIN_PASSWORD`` via the
          normal :func:`granny.credentials.get_secret` chain (env wins,
          Vaultwarden under ``granny/infra/stalwart-admin-*`` as fallback).
        """
        base_url = os.environ.get("STALWART_URL") or "https://mail.pseekoo.io"
        user = get_secret("STALWART_ADMIN_USER")
        password = get_secret("STALWART_ADMIN_PASSWORD")
        if not user or not password:
            raise StalwartError(
                0,
                "STALWART_ADMIN_USER / STALWART_ADMIN_PASSWORD not set "
                "(env or vault). Set them in .env / .deploy.env, or push "
                "them to Vaultwarden at granny/infra/stalwart-admin-user "
                "and granny/infra/stalwart-admin-password.",
            )
        return cls(base_url=base_url, user=user, password=password)

    # -- HTTP plumbing --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        resp = self._session.request(
            method=method.upper(), url=url, json=json, timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise StalwartError(resp.status_code, resp.text)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _jmap(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one JMAP method call and return its response args.

        ``method`` is e.g. ``"x:Domain/get"`` or ``"x:Account/set"``;
        ``args`` is the per-method argument dict. The accountId is filled
        in automatically.
        """
        body = {
            "using": _JMAP_CAPS,
            "methodCalls": [[method, {"accountId": _DIRECTORY_ACCOUNT_ID, **args}, "a"]],
        }
        resp = self._request("POST", "/jmap", json=body)
        if not resp or "methodResponses" not in resp:
            raise StalwartError(0, f"unexpected JMAP response: {resp!r}")
        mc = resp["methodResponses"][0]
        # JMAP-level errors come back as ["error", {type: ..., description: ...}, "a"]
        if mc[0] == "error":
            err = mc[1] or {}
            desc = err.get("description") or err.get("type") or "JMAP error"
            raise StalwartError(0, f"{method}: {desc}")
        return mc[1] or {}

    # -- Domains --------------------------------------------------------------

    def list_domains(self) -> list[dict[str, Any]]:
        """Return all configured local domains."""
        out = self._jmap("x:Domain/get", {})
        return list(out.get("list") or [])

    def get_domain(self, name: str) -> dict[str, Any] | None:
        """Find a domain by its DNS name (e.g. ``"m3rp.ai"``)."""
        for d in self.list_domains():
            if d.get("name") == name:
                return d
        return None

    def add_domain(
        self,
        name: str,
        *,
        certificate: str = "manual",
        dkim_algorithms: tuple[str, ...] = ("Dkim1RsaSha256", "Dkim1Ed25519Sha256"),
        dkim_rotate_after_ms: int = 7_776_000_000,
        dkim_retire_after_ms: int = 604_800_000,
        dkim_delete_after_ms: int = 2_592_000_000,
        dkim_selector_template: str = "v{version}-{algorithm}-{date-%Y%m%d}",
        dns_management: str = "manual",
        allow_relaying: bool = False,
        report_address_uri: str = "mailto:postmaster",
    ) -> dict[str, Any]:
        """Create a new local domain.

        Mirrors the production setup (Manual cert because the MX host
        already has a Let's Encrypt cert via Caddy, Automatic DKIM
        rotation, Manual DNS because records are published at the
        domain's registrar).
        """
        existing = self.get_domain(name)
        if existing is not None:
            logger.info("Domain %s already exists (id=%s)", name, existing["id"])
            return existing

        dkim = {
            "@type": "Automatic",
            "algorithms": {algo: True for algo in dkim_algorithms},
            "selectorTemplate": dkim_selector_template,
            "rotateAfter": dkim_rotate_after_ms,
            "retireAfter": dkim_retire_after_ms,
            "deleteAfter": dkim_delete_after_ms,
        }
        record = {
            "name": name,
            "isEnabled": True,
            "certificateManagement": {"@type": certificate.capitalize()},
            "dkimManagement": dkim,
            "dnsManagement": {"@type": dns_management.capitalize()},
            "subAddressing": {"@type": "Enabled"},
            "allowRelaying": allow_relaying,
            "reportAddressUri": report_address_uri,
        }
        out = self._jmap("x:Domain/set", {"create": {"new": record}})
        not_created = out.get("notCreated") or {}
        if not_created:
            raise StalwartError(0, f"Domain/set notCreated: {not_created!r}")
        created = (out.get("created") or {}).get("new") or {}
        if not created:
            raise StalwartError(0, f"Domain/set returned no created entry: {out!r}")
        return created

    def get_dns_zone(self, name: str) -> str:
        """Return Stalwart's generated DNS zone file for a domain.

        Includes SPF, MX, DKIM (one per active key), DMARC, MTA-STS,
        TLS-RPT, and the autoconfig/SRV records.
        """
        dom = self.get_domain(name)
        if dom is None:
            raise StalwartError(0, f"Domain not found: {name}")
        zone = dom.get("dnsZoneFile")
        if zone is None:
            raise StalwartError(0, f"Domain {name} has no dnsZoneFile field")
        return zone

    # -- Mailboxes (accounts) -------------------------------------------------

    def list_mailboxes(self) -> list[dict[str, Any]]:
        """Return all individual user / mailbox accounts."""
        out = self._jmap("x:Account/get", {})
        return list(out.get("list") or [])

    def get_mailbox(self, email: str) -> dict[str, Any] | None:
        """Find a mailbox by its primary email address."""
        for a in self.list_mailboxes():
            if a.get("emailAddress") == email:
                return a
        return None

    def add_mailbox(
        self,
        email: str,
        *,
        password: str | None = None,
        description: str | None = None,
        encryption_at_rest: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Create a mailbox under an existing local domain.

        Returns ``(record, password)`` so the caller can show the
        generated password to the operator. When ``password`` is None,
        a 24-character random password is generated.
        """
        if "@" not in email:
            raise ValueError("email must be 'local@domain' form")
        local_part, domain = email.split("@", 1)
        dom = self.get_domain(domain)
        if dom is None:
            raise StalwartError(
                0,
                f"Domain '{domain}' is not configured in Stalwart. "
                f"Add it first with add_domain('{domain}').",
            )
        if password is None:
            password = _generate_password()

        record = {
            "name": local_part,
            "domainId": dom["id"],
            "@type": "User",
            "description": description or f"Mailbox {email}",
            "roles": {"@type": "User"},
            "permissions": {"@type": "Inherit"},
            "encryptionAtRest": {
                "@type": "Enabled" if encryption_at_rest else "Disabled",
            },
            "credentials": {"0": {"@type": "Password", "secret": password}},
        }
        out = self._jmap("x:Account/set", {"create": {"new": record}})
        not_created = out.get("notCreated") or {}
        if not_created:
            raise StalwartError(0, f"Account/set notCreated: {not_created!r}")
        created = (out.get("created") or {}).get("new") or {}
        if not created:
            raise StalwartError(0, f"Account/set returned no created entry: {out!r}")
        return created, password

    # -- Delivery / health ----------------------------------------------------

    def get_delivery_token(self) -> str:
        """Mint a short-lived token for the live delivery test SSE stream."""
        resp = self._request("GET", "/api/token/delivery")
        if not isinstance(resp, str) or not resp.strip():
            raise StalwartError(0, f"unexpected delivery token response: {resp!r}")
        return resp.strip()

    def stream_delivery_test(self, recipient: str) -> Iterator[str]:
        """Yield raw SSE lines from Stalwart's live delivery test.

        Mirrors the admin UI's *Live Delivery Test* page (MX lookup,
        MTA-STS, TLS-RPT, banner, STARTTLS, etc.). Useful from CLI
        because the browser EventSource flow returns 401 on this build.
        """
        token = self.get_delivery_token()
        url = f"{self.base_url}/api/live/delivery/{recipient}?token={token}"
        with self._session.get(
            url,
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=self._timeout,
        ) as resp:
            if resp.status_code >= 400:
                raise StalwartError(resp.status_code, resp.text)
            for line in resp.iter_lines(decode_unicode=True):
                if line is not None:
                    yield line


# -- helpers ------------------------------------------------------------------


def _generate_password(length: int = 24) -> str:
    """Generate an URL-safe random password without padding characters.

    Matches the strength of ``openssl rand -base64 24`` and strips the
    same noisy characters (``/+=``) as the bash helper.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(_secrets.choice(alphabet) for _ in range(length))
