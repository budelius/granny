"""Bunny DNS provider — full CRUD against ``api.bunny.net``.

Lifted and generalized from ``tools/create/setup_bunny_storage.py``. The
existing script keeps its local copy; this module is the shared home used by
``granny dns`` and any future callers.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_BASE = "https://api.bunny.net"

# Bunny's numeric record type ids (from the public API docs).
RECORD_TYPES = {
    "A": 0,
    "AAAA": 1,
    "CNAME": 2,
    "TXT": 3,
    "MX": 4,
    "REDIRECT": 5,
    "FLATTEN": 6,
    "PULLZONE": 7,
    "SRV": 8,
    "CAA": 9,
    "PTR": 10,
    "SCRIPT": 11,
    "NS": 12,
}
RECORD_TYPE_NAMES = {v: k for k, v in RECORD_TYPES.items()}


class BunnyDNSProvider(DNSProvider):
    name = "bunny"

    def __init__(self, api_key: str | None = None) -> None:
        api_key = api_key or get_secret("BUNNY_API_KEY")
        if not api_key:
            raise ValueError("BUNNY_API_KEY not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {"AccessKey": api_key, "Content-Type": "application/json"}
        )
        self._zone_cache: dict[str, str] = {}

    def _request(self, method: str, path: str, data: dict | None = None) -> Any:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(url, json=data, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Bunny DNS API {method} {path} failed: {resp.status_code} {resp.text}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def _find_zone(self, zone_name: str) -> dict | None:
        result = self._request("GET", "/dnszone")
        zones = result.get("Items", []) if isinstance(result, dict) else result
        for zone in zones:
            if zone.get("Domain") == zone_name:
                return zone
        return None

    def get_zone_id(self, zone_name: str) -> str:
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]
        zone = self._find_zone(zone_name)
        if not zone:
            raise RuntimeError(f"Bunny DNS zone not found: {zone_name}")
        zone_id = str(zone["Id"])
        self._zone_cache[zone_name] = zone_id
        return zone_id

    def get_nameservers(self, zone_name: str) -> list[str]:
        zone = self._find_zone(zone_name)
        if not zone:
            return []
        return [zone[k] for k in ("NameServer1", "NameServer2") if zone.get(k)]

    def _raw_records(self, zone_id: str) -> list[dict]:
        result = self._request("GET", f"/dnszone/{zone_id}")
        return result.get("Records", []) or []

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        raw = self._raw_records(zone_id)
        type_num = RECORD_TYPES.get(record_type.upper()) if record_type else None
        out: list[DNSRecord] = []
        for rec in raw:
            if name is not None and rec.get("Name", "") != name:
                continue
            if type_num is not None and rec.get("Type") != type_num:
                continue
            out.append(
                DNSRecord(
                    name=rec.get("Name", "") or "",
                    type=RECORD_TYPE_NAMES.get(rec.get("Type", -1), "?"),
                    value=self._record_value(rec),
                    ttl=int(rec.get("Ttl", 300)),
                    id=str(rec.get("Id")),
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
        rtype = record_type.upper()
        type_num = RECORD_TYPES.get(rtype)
        if type_num is None:
            raise ValueError(f"Unknown record type: {record_type}")
        short_name = "" if name in ("@", None) else name
        record_value = value.rstrip(".") if rtype == "CNAME" else value
        priority: int | None = None
        extra: dict[str, int] = {}
        if rtype == "MX":
            # BIND-style "10 mail.example.com" → Priority + target.
            head, _, tail = value.strip().partition(" ")
            if head.isdigit() and tail:
                priority = int(head)
                record_value = tail.rstrip(".")
        elif rtype == "SRV":
            # BIND-style "priority weight port target" → Bunny's separate
            # Priority/Weight/Port fields plus Value=target.
            parts = value.split()
            if len(parts) != 4:
                raise ValueError(
                    f"SRV value must be 'priority weight port target', got {value!r}"
                )
            prio, weight, port, target = parts
            priority = int(prio)
            extra = {"Weight": int(weight), "Port": int(port)}
            record_value = target.rstrip(".")
        payload = {
            "Type": type_num,
            "Name": short_name,
            "Value": record_value,
            "Ttl": ttl,
        }
        if priority is not None:
            payload["Priority"] = priority
        payload.update(extra)
        result = self._request("PUT", f"/dnszone/{zone_id}/records", payload)
        logger.info("Bunny DNS: created %s %s -> %s", record_type, short_name, value)
        # Build the display value from what we sent — Bunny's create response
        # doesn't reliably echo Priority/Weight/Port back.
        if rtype == "MX" and priority is not None:
            display_value = f"{priority} {record_value}"
        elif rtype == "SRV":
            display_value = f"{priority} {extra['Weight']} {extra['Port']} {record_value}"
        else:
            display_value = record_value
        return DNSRecord(
            name=short_name,
            type=rtype,
            value=display_value,
            ttl=ttl,
            id=str(result.get("Id")) if isinstance(result, dict) else None,
            provider_data=result if isinstance(result, dict) else {},
        )

    @staticmethod
    def _record_value(rec: dict) -> str:
        """Reconstruct a BIND-style value from a Bunny record.

        MX prepends ``Priority``; SRV expands to ``priority weight port
        target``. Keeps round-tripped values in the same shape callers pass,
        so ``upsert_record`` compares equal and stays idempotent.
        """
        value = rec.get("Value", "") or ""
        rtype = rec.get("Type")
        if rtype == RECORD_TYPES["MX"]:
            return f"{rec.get('Priority', 0)} {value}".strip()
        if rtype == RECORD_TYPES["SRV"]:
            return (
                f"{rec.get('Priority', 0)} {rec.get('Weight', 0)} "
                f"{rec.get('Port', 0)} {value}"
            )
        return value

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._request("DELETE", f"/dnszone/{zone_id}/records/{record_id}")
        logger.info("Bunny DNS: deleted record %s", record_id)
