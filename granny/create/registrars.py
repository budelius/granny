"""Domain registrar adapters for setup_bunny_storage.py.

Registrar operations are conceptually separate from DNS record operations:
a registrar owns the WHOIS record for a domain and controls which nameservers
the TLD delegates to. A DNS provider owns the zone and controls the records
inside it.

When granny creates a Bunny DNS zone for a domain, the zone gets a pair of
auto-assigned Bunny nameservers. Someone still has to walk over to the
registrar and paste those NS values into the domain's WHOIS record. That's
what a Registrar adapter automates.

Currently implemented:
    - ManualRegistrar:  no-op, logs a checklist. Default for unsupported
                        registrars.
    - EasynameRegistrar: easyname.eu REST API. Used for .at/.eu/.com domains
                        registered at Austria's easyname.

Adding a new registrar: subclass ``Registrar``, implement the two methods,
register in ``_REGISTRARS``. See the factory ``get_registrar()``.

Credentials: read from env vars, same convention as the DNS providers in
setup_bunny_storage.py. No Vault coupling — callers can source .deploy.env
before invoking granny.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import quote_plus

import requests


# =============================================================================
# Registrar ABC
# =============================================================================

class Registrar(ABC):
    """Abstract base class for domain registrar adapters.

    Minimal interface: look up a domain's registrar-level id (registrars
    number their registered domains), then replace its authoritative
    nameserver list. Everything else (WHOIS contacts, auto-renew, transfer
    locks) is out of scope — granny doesn't manage those yet.
    """

    @abstractmethod
    def get_domain_id(self, domain: str) -> Optional[str]:
        """Look up the registrar's internal id for ``domain``.

        Returns ``None`` if the domain is not registered in this account.
        Callers should treat ``None`` as a hard error: you can't flip
        nameservers for a domain you don't own.
        """

    @abstractmethod
    def set_nameservers(self, domain: str, nameservers: list[str]) -> None:
        """Replace the delegated nameservers for ``domain``.

        ``nameservers`` is an ordered list of FQDNs (e.g.
        ``['kiki.bunny.net', 'buckx.bunny.net']``). Implementations should:

        - Validate length (most TLDs allow 2-6 NS records).
        - Reject empty / duplicate / unresolvable entries if the underlying
          API does.
        - Treat "already set correctly" as a success (idempotent).
        """


# =============================================================================
# ManualRegistrar — default, no-op, prints a checklist
# =============================================================================

class ManualRegistrar(Registrar):
    """No-op registrar adapter. Prints instructions for the user to follow.

    Used when the domain is at a registrar that granny doesn't automate yet,
    or when the user wants to flip NS records by hand.
    """

    def get_domain_id(self, domain: str) -> Optional[str]:
        # Pretend the domain exists so the caller can still call set_nameservers.
        return "manual"

    def set_nameservers(self, domain: str, nameservers: list[str]) -> None:
        logging.info("=" * 60)
        logging.info(f"MANUAL ACTION REQUIRED: nameserver change for {domain}")
        logging.info("Log into your registrar and set these NS records:")
        for ns in nameservers:
            logging.info(f"  {ns}")
        logging.info("=" * 60)


# =============================================================================
# EasynameRegistrar — easyname.eu REST API
# =============================================================================

class EasynameRegistrar(Registrar):
    """easyname.eu registrar adapter.

    Uses the public REST API at ``https://api.easyname.com``. The API only
    supports domain-level operations (not DNS record CRUD — that's a gap
    on easyname's side, not ours). We use:

        GET  /domain                            -> list all owned domains
        POST /domain/{id}/nameserverchange      -> replace NS records

    Authentication
    --------------
    Two layers, both mirroring the official easyname PHP SDK
    (https://github.com/easyname/php-sdk):

    1. ``X-User-ApiKey`` header with the raw API key.
    2. ``X-User-Authentication`` header computed as
       ``base64(md5(auth_salt_template % (user_id, email)))``
       where ``auth_salt_template`` is a printf-style string with two ``%s``
       placeholders (e.g. ``"abc%sdef%sghi"``). The MD5 digest is taken as
       its *hex* string, and then base64 is applied to that hex — this
       replicates PHP's ``base64_encode(md5(...))`` which operates on the
       hex form by default.

    Plus, every POST body is a JSON envelope::

        {"data": {...}, "timestamp": <unix>, "signature": <sig>}

    where ``signature`` is computed from the sorted concatenation of data
    values + timestamp, split in half, with the signing salt inserted in
    the middle, MD5'd (hex), and base64'd. Again, exactly matching the PHP
    SDK so we don't have to reverse-engineer anything.

    **Final oddity**: the POST body is ``urlencode(json_encode(body))`` even
    though the ``Content-Type`` header says ``application/json``. The server
    URL-decodes before parsing. We replicate this with ``quote_plus``.

    Credentials (env vars)
    ----------------------
    ``EASYNAME_USER_ID``        numeric user id
    ``EASYNAME_EMAIL``          account email
    ``EASYNAME_API_KEY``        the "API key" from easyname account settings
    ``EASYNAME_AUTH_SALT``      the "API authentication salt" (contains %s %s)
    ``EASYNAME_SIGN_SALT``      the "API signing salt"
    """

    API_BASE = "https://api.easyname.com"

    def __init__(self) -> None:
        self.user_id = os.environ.get("EASYNAME_USER_ID")
        self.email = os.environ.get("EASYNAME_EMAIL")
        self.api_key = os.environ.get("EASYNAME_API_KEY")
        self.auth_salt = os.environ.get("EASYNAME_AUTH_SALT")
        self.sign_salt = os.environ.get("EASYNAME_SIGN_SALT")

        missing = [
            k for k, v in {
                "EASYNAME_USER_ID": self.user_id,
                "EASYNAME_EMAIL": self.email,
                "EASYNAME_API_KEY": self.api_key,
                "EASYNAME_AUTH_SALT": self.auth_salt,
                "EASYNAME_SIGN_SALT": self.sign_salt,
            }.items() if not v
        ]
        if missing:
            raise ValueError(
                f"EasynameRegistrar: missing env vars: {', '.join(missing)}. "
                "Grab them from https://my.easyname.com/en/account/api — you need "
                "User ID, email, API Key, Authentication Salt, and Signing Salt."
            )

        # Defensive: the auth salt must contain two %s placeholders or the
        # sprintf fill below will ValueError / produce garbage.
        if self.auth_salt.count("%s") != 2:
            raise ValueError(
                "EASYNAME_AUTH_SALT must be a printf template with exactly two "
                "%s placeholders (for user_id and email). Got: "
                + repr(self.auth_salt)
            )

        self.session = requests.Session()

    # ------------------------------------------------------------------ auth

    def _auth_header(self) -> str:
        """Compute the ``X-User-Authentication`` header value."""
        filled = self.auth_salt % (str(self.user_id), self.email)
        hex_digest = hashlib.md5(filled.encode("utf-8")).hexdigest()
        return base64.b64encode(hex_digest.encode("ascii")).decode("ascii")

    def _sign_body(self, data: dict, timestamp: int) -> str:
        """Compute the ``signature`` field of the POST body envelope.

        Replicates the PHP SDK's ``signRequest`` method byte-for-byte so
        signatures validate server-side.
        """
        keys = sorted(list(data.keys()) + ["timestamp"])
        buf = ""
        for k in keys:
            if k == "timestamp":
                buf += str(timestamp)
            else:
                buf += str(data[k])

        length = len(buf)
        # PHP: if odd length, first half gets the extra char.
        half = length // 2 if length % 2 == 0 else (length // 2) + 1
        first, second = buf[:half], buf[half:]
        payload = first + self.sign_salt + second
        hex_digest = hashlib.md5(payload.encode("utf-8")).hexdigest()
        return base64.b64encode(hex_digest.encode("ascii")).decode("ascii")

    def _build_body(self, data: dict) -> str:
        """Build the urlencoded JSON envelope for POST requests."""
        timestamp = int(time.time())
        envelope = {
            "data": data,
            "timestamp": timestamp,
            "signature": self._sign_body(data, timestamp),
        }
        # PHP uses compact JSON (no spaces). Match that so the wire format
        # matches — the signature itself is computed from `data` values not
        # the encoded JSON, so the separators don't affect validity, but we
        # stay consistent for debuggability.
        json_str = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True)
        return quote_plus(json_str)

    def _headers(self) -> dict:
        return {
            "X-User-ApiKey": self.api_key,
            "X-User-Authentication": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        resp = self.session.get(
            f"{self.API_BASE}{path}",
            headers=self._headers(),
            timeout=30,
        )
        return self._parse(resp)

    def _post(self, path: str, data: dict) -> dict:
        body = self._build_body(data)
        resp = self.session.post(
            f"{self.API_BASE}{path}",
            headers=self._headers(),
            data=body,
            timeout=30,
        )
        return self._parse(resp)

    def _parse(self, resp: requests.Response) -> dict:
        if resp.status_code >= 400:
            raise RuntimeError(
                f"easyname API {resp.request.method} {resp.request.url} "
                f"-> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(
                f"easyname API returned non-JSON: {resp.text[:500]}"
            )
        # easyname responses look like {"status": {"code": 200, ...}, "data": ...}
        status = payload.get("status") if isinstance(payload, dict) else None
        if status and isinstance(status, dict):
            code = status.get("code", 200)
            if int(code) >= 400:
                raise RuntimeError(
                    f"easyname API error: status={code} "
                    f"message={status.get('message')}"
                )
        return payload

    # ------------------------------------------------------------ Registrar

    def get_domain_id(self, domain: str) -> Optional[str]:
        """Scan all owned domains for a name match and return its id."""
        logging.info(f"Looking up domain '{domain}' at easyname...")

        offset = 0
        page_size = 100
        while True:
            resp = self._get(f"/domain?limit={page_size}&offset={offset}")
            data = resp.get("data") if isinstance(resp, dict) else None
            items = data if isinstance(data, list) else (
                data.get("items") if isinstance(data, dict) else []
            )
            if not items:
                return None
            for item in items:
                # easyname may return the domain under "domain" or "name"
                name = item.get("domain") or item.get("name")
                if name and name.lower() == domain.lower():
                    domain_id = item.get("id")
                    if domain_id is None:
                        return None
                    logging.info(f"Found '{domain}' at easyname (id: {domain_id})")
                    return str(domain_id)
            if len(items) < page_size:
                return None
            offset += page_size

    def set_nameservers(self, domain: str, nameservers: list[str]) -> None:
        if not nameservers:
            raise ValueError("set_nameservers: nameservers list is empty")
        if len(nameservers) > 6:
            raise ValueError(
                f"set_nameservers: easyname accepts at most 6 nameservers, got {len(nameservers)}"
            )
        if len(set(ns.lower() for ns in nameservers)) != len(nameservers):
            raise ValueError(f"set_nameservers: duplicate nameservers in {nameservers}")

        domain_id = self.get_domain_id(domain)
        if domain_id is None:
            raise RuntimeError(
                f"easyname: domain '{domain}' not found in this account. "
                "Check EASYNAME_USER_ID is correct and the domain is registered there."
            )

        logging.info(f"Setting nameservers for {domain} -> {nameservers}")
        data = {}
        for i, ns in enumerate(nameservers, start=1):
            data[f"nameserver{i}"] = ns

        self._post(f"/domain/{domain_id}/nameserverchange", data)
        logging.info(f"easyname: nameserver change submitted for {domain}")


# =============================================================================
# Factory
# =============================================================================

_REGISTRARS: dict[str, type[Registrar]] = {
    "manual": ManualRegistrar,
    "easyname": EasynameRegistrar,
}


def get_registrar(name: str) -> Registrar:
    """Factory: registrar name -> instance.

    Raises ValueError if ``name`` is not registered. Env-var-missing errors
    bubble up from the constructor so they're surfaced at CLI invocation
    time, not lazily.
    """
    if name not in _REGISTRARS:
        raise ValueError(
            f"Unknown registrar: {name!r}. Known: {sorted(_REGISTRARS.keys())}"
        )
    return _REGISTRARS[name]()


def registrar_choices() -> list[str]:
    """For argparse ``choices=``."""
    return sorted(_REGISTRARS.keys())
