"""ClouDNS provider — CRUD via https://api.cloudns.net.

Lifted from ``tools/create/setup_bunny_storage.py``. ClouDNS uses form-encoded
POST requests for every operation, including reads. The zone identifier is
the domain name itself. TTLs are quantized to a fixed set.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudns.net"
VALID_TTLS = [
    60, 300, 900, 1800, 3600, 21600, 43200, 86400,
    172800, 259200, 604800, 1209600, 2592000,
]


def _quantize_ttl(ttl: int) -> int:
    for valid in VALID_TTLS:
        if ttl <= valid:
            return valid
    return VALID_TTLS[-1]


class ClouDNSDNSProvider(DNSProvider):
    name = "cloudns"

    def __init__(
        self,
        auth_id: str | None = None,
        auth_password: str | None = None,
        sub_auth_id: str | None = None,
        sub_auth_user: str | None = None,
    ) -> None:
        self.auth_id = auth_id or get_secret("CLOUDNS_AUTH_ID")
        self.auth_password = auth_password or get_secret("CLOUDNS_AUTH_PASSWORD")
        self.sub_auth_id = sub_auth_id or get_secret("CLOUDNS_SUB_AUTH_ID")
        self.sub_auth_user = sub_auth_user or get_secret("CLOUDNS_SUB_AUTH_USER")
        if not self.auth_password:
            raise ValueError("CLOUDNS_AUTH_PASSWORD not set (env or vault).")
        if not self.auth_id and not self.sub_auth_id and not self.sub_auth_user:
            raise ValueError(
                "ClouDNS requires auth-id, sub-auth-id, or sub-auth-user."
            )

    def _auth_params(self) -> dict[str, str]:
        p = {"auth-password": self.auth_password}
        if self.auth_id:
            p["auth-id"] = self.auth_id
        elif self.sub_auth_id:
            p["sub-auth-id"] = self.sub_auth_id
        elif self.sub_auth_user:
            p["sub-auth-user"] = self.sub_auth_user
        return p

    def _request(self, endpoint: str, params: dict | None = None) -> Any:
        all_params = self._auth_params()
        if params:
            all_params.update(params)
        resp = requests.post(f"{API_BASE}{endpoint}", data=all_params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"ClouDNS {endpoint} failed: {resp.status_code} {resp.text}"
            )
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "Failed":
            raise RuntimeError(
                f"ClouDNS {endpoint} error: {data.get('statusDescription')}"
            )
        return data

    def get_zone_id(self, zone_name: str) -> str:
        result = self._request(
            "/dns/list-zones.json", {"page": 1, "rows-per-page": 100}
        )
        zones = result if isinstance(result, list) else list(result.values() or [])
        for zone in zones:
            if isinstance(zone, dict) and zone.get("name") == zone_name:
                return zone_name
        raise RuntimeError(f"ClouDNS zone not found: {zone_name}")

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        result = self._request("/dns/records.json", {"domain-name": zone_id})
        if isinstance(result, dict):
            records = list(result.values())
        elif isinstance(result, list):
            records = result
        else:
            records = []
        short_name = "" if name in ("@", None) else name
        rtype = record_type.upper() if record_type else None
        out: list[DNSRecord] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if name is not None and rec.get("host", "") != short_name:
                continue
            if rtype is not None and rec.get("type") != rtype:
                continue
            out.append(
                DNSRecord(
                    name=rec.get("host", ""),
                    type=rec.get("type", ""),
                    value=rec.get("record", ""),
                    ttl=int(rec.get("ttl", 3600)),
                    id=str(rec.get("id")),
                    provider_data=rec,
                )
            )
        return out

    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        short = "" if not name or name == "@" else name
        result = self._request(
            "/dns/add-record.json",
            {
                "domain-name": zone_id,
                "record-type": record_type.upper(),
                "host": short,
                "record": value.rstrip(".") if record_type.upper() == "CNAME" else value,
                "ttl": _quantize_ttl(ttl),
            },
        )
        record_id = None
        if isinstance(result, dict):
            record_id = str(result.get("data", {}).get("id") or "")
        logger.info("ClouDNS: created %s %s -> %s", record_type, short, value)
        return DNSRecord(
            name=short,
            type=record_type.upper(),
            value=value,
            ttl=_quantize_ttl(ttl),
            id=record_id or None,
        )

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._request(
            "/dns/delete-record.json",
            {"domain-name": zone_id, "record-id": record_id},
        )
        logger.info("ClouDNS: deleted record %s", record_id)
