"""INWX DNS provider -- JSON-RPC via https://api.domrobot.com/jsonrpc/.

INWX (inwx.de / inwx.com) is a German domain registrar. Their API is the
DOMRobot JSON-RPC endpoint; there is no REST surface. Auth is username +
password via ``account.login`` returning a session cookie that must be
reused for all subsequent calls. Optional TOTP (Google Authenticator) is
followed by ``account.unlock``.

Zone addressing uses the bare domain name -- no separate zone-id lookup.
Records are referenced by integer ``id`` returned from ``createRecord`` or
listed via ``nameserver.info``. Apex records use the zone name as ``name``
(NOT ``@``); short subnames are joined to the zone for the API call and
split back to short form for :class:`~granny.dns.records.DNSRecord` output.

Example::

    from granny.dns.inwx import INWXDNSProvider
    p = INWXDNSProvider()
    zid = p.get_zone_id("example.com")
    p.upsert_record(zid, "_acme-challenge", "TXT", "abc-token", ttl=300)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
import time
from typing import Any

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_URL = "https://api.domrobot.com/jsonrpc/"
OTE_URL = "https://api.ote.domrobot.com/jsonrpc/"  # sandbox
MIN_TTL = 300  # INWX platform minimum
DEFAULT_TTL = 3600
TFA_GOOGLE_AUTH = "GOOGLE-AUTH"
NAMESERVERS = [
    "ns.inwx.de",
    "ns2.inwx.de",
    "ns3.inwx.eu",
    "ns4.inwx.com",
    "ns5.inwx.net",
]


def _totp(secret_b32: str, when: float | None = None) -> str:
    """Compute the current 6-digit TOTP from a base32 secret (RFC 6238)."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper())
    counter = int((when if when is not None else time.time()) // 30)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**6
    return f"{code:06d}"


class INWXDNSProvider(DNSProvider):
    name = "inwx"

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        shared_secret: str | None = None,
        sandbox: bool = False,
    ) -> None:
        # Resolve credentials lazily so operations that don't hit the API
        # (e.g. get_nameservers) work without configured secrets. _ensure_login
        # raises if creds are missing when the first network call happens.
        self.username = username or get_secret("INWX_USERNAME")
        self.password = password or get_secret("INWX_PASSWORD")
        self.shared_secret = shared_secret or get_secret("INWX_SHARED_SECRET")
        self.url = OTE_URL if sandbox else API_URL
        self.session = requests.Session()
        self._logged_in = False
        self._req_id = 0

    # -- low-level JSON-RPC ---------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        resp = self.session.post(self.url, json=payload, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"INWX HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        code = body.get("code")
        if code != 1000:
            # 2302 = object does not exist; surface as KeyError for callers.
            if code == 2302:
                raise KeyError(method)
            msg = body.get("msg") or body.get("reason") or "unknown"
            raise RuntimeError(f"INWX {method} failed: code={code} msg={msg}")
        return body.get("resData") or {}

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        if not self.username or not self.password:
            raise RuntimeError(
                "INWX_USERNAME and INWX_PASSWORD must be set (env or vault)."
            )
        result = self._call(
            "account.login",
            {"user": self.username, "pass": self.password, "lang": "en"},
        )
        # Some accounts require a follow-up TOTP step.
        tfa = (result or {}).get("tfa")
        if tfa == TFA_GOOGLE_AUTH:
            if not self.shared_secret:
                raise RuntimeError(
                    "INWX account requires 2FA but INWX_SHARED_SECRET is not set."
                )
            # Anti-replay: don't reuse a TAN inside the same 30s window.
            tan = _totp(self.shared_secret)
            self._call("account.unlock", {"tan": tan})
        self._logged_in = True

    # -- zone helpers ---------------------------------------------------------

    @staticmethod
    def _short_to_fqdn(name: str | None, zone_name: str) -> str:
        if name is None or name in ("", "@"):
            return zone_name
        if name == zone_name or name.endswith(f".{zone_name}"):
            return name
        return f"{name}.{zone_name}"

    @staticmethod
    def _fqdn_to_short(fqdn: str, zone_name: str) -> str:
        if fqdn == zone_name:
            return ""
        suffix = f".{zone_name}"
        if fqdn.endswith(suffix):
            return fqdn[: -len(suffix)]
        return fqdn

    # -- DNSProvider impl -----------------------------------------------------

    def get_zone_id(self, zone_name: str) -> str:
        self._ensure_login()
        try:
            self._call("nameserver.info", {"domain": zone_name})
        except KeyError:
            raise RuntimeError(f"INWX domain not hosted on this account: {zone_name}")
        # The bare zone name *is* the addressing key for subsequent calls.
        return zone_name

    def get_nameservers(self, zone_name: str) -> list[str]:
        return list(NAMESERVERS)

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        self._ensure_login()
        params: dict[str, Any] = {"domain": zone_id}
        if record_type:
            params["type"] = record_type.upper()
        if name is not None:
            params["name"] = self._short_to_fqdn(name, zone_id)
        try:
            data = self._call("nameserver.info", params)
        except KeyError:
            return []
        out: list[DNSRecord] = []
        for r in data.get("record") or []:
            short = self._fqdn_to_short(r.get("name", ""), zone_id)
            out.append(
                DNSRecord(
                    name=short,
                    type=r.get("type", ""),
                    value=r.get("content", ""),
                    ttl=int(r.get("ttl") or DEFAULT_TTL),
                    id=str(r.get("id", "")),
                    fqdn=r.get("name"),
                    provider_data=r,
                )
            )
        return out

    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = DEFAULT_TTL,
        proxied: bool = False,
    ) -> DNSRecord:
        self._ensure_login()
        rtype = record_type.upper()
        ttl = max(int(ttl), MIN_TTL)
        fqdn = self._short_to_fqdn(name, zone_id)

        params: dict[str, Any] = {
            "domain": zone_id,
            "type": rtype,
            "name": fqdn,
            "content": value,
            "ttl": ttl,
        }

        # MX priority rides in `prio`; never inline it into `content`. Accept
        # callers that still pass "10 mail.example.com" by splitting once.
        if rtype == "MX":
            head, _, tail = value.strip().partition(" ")
            if head.isdigit() and tail:
                params["prio"] = int(head)
                params["content"] = tail
            else:
                params["prio"] = 10  # sane default

        result = self._call("nameserver.createRecord", params)
        record_id = str(result.get("id", ""))
        logger.info(
            "INWX: created %s %s -> %s (id=%s)", rtype, fqdn, params["content"], record_id
        )
        return DNSRecord(
            name=self._fqdn_to_short(fqdn, zone_id),
            type=rtype,
            value=params["content"],
            ttl=ttl,
            id=record_id,
            fqdn=fqdn,
        )

    def update_record(
        self,
        zone_id: str,
        record_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = DEFAULT_TTL,
        proxied: bool = False,
    ) -> DNSRecord:
        # Native PUT-equivalent: avoids the delete+recreate roundtrip and
        # preserves the record id so callers tracking ids stay consistent.
        self._ensure_login()
        rtype = record_type.upper()
        ttl = max(int(ttl), MIN_TTL)
        fqdn = self._short_to_fqdn(name, zone_id)
        params: dict[str, Any] = {
            "id": int(record_id),
            "name": fqdn,
            "type": rtype,
            "content": value,
            "ttl": ttl,
        }
        if rtype == "MX":
            head, _, tail = value.strip().partition(" ")
            if head.isdigit() and tail:
                params["prio"] = int(head)
                params["content"] = tail
        self._call("nameserver.updateRecord", params)
        logger.info("INWX: updated record id=%s -> %s %s", record_id, rtype, params["content"])
        return DNSRecord(
            name=self._fqdn_to_short(fqdn, zone_id),
            type=rtype,
            value=params["content"],
            ttl=ttl,
            id=record_id,
            fqdn=fqdn,
        )

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._ensure_login()
        try:
            self._call("nameserver.deleteRecord", {"id": int(record_id)})
        except KeyError:
            # Already gone -- treat as success.
            return
        logger.info("INWX: deleted record id=%s", record_id)
