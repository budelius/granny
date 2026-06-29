"""Cloudflare DNS provider — full CRUD against the v4 REST API.

Ported from ``tools/create/manage-dns.sh`` (the bash script this package
replaces) and the partial Cloudflare adapters in ``setup_bunny_storage.py``
and ``auto_certificate.py``. Uses ``requests`` directly so we don't depend
on the ``cloudflare`` SDK.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareDNSProvider(DNSProvider):
    name = "cloudflare"

    def __init__(self, api_token: str | None = None) -> None:
        api_token = api_token or get_secret("CLOUDFLARE_API_TOKEN")
        if not api_token:
            raise ValueError("CLOUDFLARE_API_TOKEN not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )
        self._zone_cache: dict[str, str] = {}

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(
            url, params=params, json=json, timeout=30
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Cloudflare {method} {path} failed: {resp.status_code} {resp.text}"
            )
        body = resp.json()
        if not body.get("success", True):
            errors = body.get("errors", [])
            raise RuntimeError(f"Cloudflare {method} {path} error: {errors}")
        return body

    def get_zone_id(self, zone_name: str) -> str:
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]
        body = self._request("GET", "/zones", params={"name": zone_name})
        result = body.get("result") or []
        if not result:
            raise RuntimeError(f"Cloudflare zone not found: {zone_name}")
        zone_id = result[0]["id"]
        self._zone_cache[zone_name] = zone_id
        return zone_id

    def get_nameservers(self, zone_name: str) -> list[str]:
        body = self._request("GET", "/zones", params={"name": zone_name})
        result = body.get("result") or []
        if not result:
            return []
        return list(result[0].get("name_servers") or [])

    def _fqdn(self, zone_id: str, name: str) -> str:
        # Cloudflare's record API uses full FQDN for `name`. The caller passes
        # us a short name relative to the zone, so we need the zone name to
        # build the full one. We reverse-lookup via /zones/{id} once.
        body = self._request("GET", f"/zones/{zone_id}")
        zone_name = body["result"]["name"]
        if not name or name == "@":
            return zone_name
        if name.endswith(f".{zone_name}") or name == zone_name:
            return name
        return f"{name}.{zone_name}"

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        params: dict[str, Any] = {"per_page": 100}
        if record_type:
            params["type"] = record_type.upper()
        if name is not None:
            params["name"] = self._fqdn(zone_id, name)
        body = self._request("GET", f"/zones/{zone_id}/dns_records", params=params)
        out: list[DNSRecord] = []
        for rec in body.get("result") or []:
            out.append(
                DNSRecord(
                    name=rec.get("name", ""),
                    type=rec.get("type", ""),
                    value=self._record_value(rec),
                    ttl=int(rec.get("ttl", 300)),
                    id=rec.get("id"),
                    fqdn=rec.get("name"),
                    provider_data=rec,
                )
            )
        return out

    @staticmethod
    def _record_value(rec: dict) -> str:
        """Reconstruct a BIND-style value string from a Cloudflare record.

        MX prepends its ``priority``; SRV expands its ``data`` object to
        ``priority weight port target``. Everything else uses ``content``.
        Keeping the round-tripped value in this shape lets ``upsert_record``
        compare against the value callers pass and stay idempotent.
        """
        rtype = rec.get("type", "")
        content = rec.get("content", "") or ""
        if rtype == "MX" and rec.get("priority") is not None:
            return f"{rec['priority']} {content}".strip()
        if rtype == "SRV":
            data = rec.get("data") or {}
            if data:
                return (
                    f"{data.get('priority', 0)} {data.get('weight', 0)} "
                    f"{data.get('port', 0)} {str(data.get('target', '')).rstrip('.')}"
                )
        return content

    @staticmethod
    def _type_payload(record_type: str, value: str) -> dict:
        """Build the type-specific Cloudflare payload fields.

        MX and SRV can't be expressed with a bare ``content`` string —
        Cloudflare requires a separate ``priority`` (MX) or a structured
        ``data`` object (SRV). Both accept the BIND-style value granny passes
        around: ``"10 mail.example.com"`` for MX and
        ``"0 1 443 mail.example.com"`` (priority weight port target) for SRV.
        A bare MX target defaults to priority 10.
        """
        rtype = record_type.upper()
        parts = value.split()
        if rtype == "MX":
            if len(parts) == 2 and parts[0].isdigit():
                return {"content": parts[1].rstrip("."), "priority": int(parts[0])}
            return {"content": value.rstrip("."), "priority": 10}
        if rtype == "SRV":
            if len(parts) != 4:
                raise ValueError(
                    f"SRV value must be 'priority weight port target', got {value!r}"
                )
            prio, weight, port, target = parts
            return {
                "data": {
                    "priority": int(prio),
                    "weight": int(weight),
                    "port": int(port),
                    "target": target.rstrip("."),
                }
            }
        return {"content": value}

    def get_record(
        self, zone_id: str, name: str, record_type: str
    ) -> DNSRecord | None:
        # Cloudflare stores `name` as the full FQDN on every record, so we
        # must filter by FQDN not short name.
        fqdn = self._fqdn(zone_id, name)
        for rec in self.list_records(zone_id, record_type=record_type):
            if rec.name == fqdn:
                return rec
        return None

    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        fqdn = self._fqdn(zone_id, name)
        payload = {
            "type": record_type.upper(),
            "name": fqdn,
            # Cloudflare uses ttl=1 as the sentinel for "automatic". Anything
            # else must be >= 60. Match manage-dns.sh's behavior: if caller
            # passes ttl<=1, use automatic; otherwise send the value.
            "ttl": 1 if ttl <= 1 else ttl,
            "proxied": proxied,
        }
        # MX needs a separate `priority`, SRV a structured `data` object;
        # everything else is a plain `content` string.
        payload.update(self._type_payload(record_type, value))
        body = self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        rec = body.get("result") or {}
        logger.info("Cloudflare: created %s %s -> %s", record_type, fqdn, value)
        return DNSRecord(
            name=rec.get("name", fqdn),
            type=rec.get("type", record_type.upper()),
            value=self._record_value(rec) or value,
            ttl=int(rec.get("ttl", ttl)),
            id=rec.get("id"),
            fqdn=rec.get("name", fqdn),
            provider_data=rec,
        )

    def update_record(
        self,
        zone_id: str,
        record_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        fqdn = self._fqdn(zone_id, name)
        payload = {
            "type": record_type.upper(),
            "name": fqdn,
            "ttl": 1 if ttl <= 1 else ttl,
            "proxied": proxied,
        }
        payload.update(self._type_payload(record_type, value))
        body = self._request(
            "PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload
        )
        rec = body.get("result") or {}
        logger.info("Cloudflare: updated %s %s -> %s", record_type, fqdn, value)
        return DNSRecord(
            name=rec.get("name", fqdn),
            type=rec.get("type", record_type.upper()),
            value=self._record_value(rec) or value,
            ttl=int(rec.get("ttl", ttl)),
            id=rec.get("id", record_id),
            fqdn=rec.get("name", fqdn),
            provider_data=rec,
        )

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        logger.info("Cloudflare: deleted record %s", record_id)
