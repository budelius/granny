"""Hetzner DNS provider — CRUD via the new Cloud API.

The legacy ``dns.hetzner.com`` API (``Auth-API-Token``) is deprecated. This
adapter uses the Hetzner Cloud API (``api.hetzner.cloud/v1``, Bearer auth,
rrset-based record management). One rrset bundles all records of the same
``(name, type)`` pair, so individual ``DNSRecord`` rows are synthesised from
each rrset's ``records`` list, with ``id`` formatted as ``"{name}/{type}"``.
"""

import logging

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_BASE = "https://api.hetzner.cloud/v1"


class HetznerDNSProvider(DNSProvider):
    name = "hetzner"

    def __init__(self, api_token: str | None = None) -> None:
        api_token = api_token or get_secret("HETZNER_DNS_API_TOKEN")
        if not api_token:
            raise ValueError("HETZNER_DNS_API_TOKEN not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        )
        self._zone_cache: dict[str, str] = {}
        self._ns_cache: dict[str, list[str]] = {}

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(
            url, params=params, json=json, timeout=30
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Hetzner DNS {method} {path} failed: {resp.status_code} {resp.text}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def get_zone_id(self, zone_name: str) -> str:
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]
        body = self._request("GET", "/zones", params={"name": zone_name})
        for zone in body.get("zones") or []:
            if zone.get("name") == zone_name:
                zone_id = str(zone["id"])
                self._zone_cache[zone_name] = zone_id
                ns = zone.get("authoritative_nameservers", {}).get("assigned", [])
                self._ns_cache[zone_name] = [n.rstrip(".") for n in ns]
                return zone_id
        raise RuntimeError(f"Hetzner DNS zone not found: {zone_name}")

    def get_nameservers(self, zone_name: str) -> list[str]:
        self.get_zone_id(zone_name)
        return list(self._ns_cache.get(zone_name, []))

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        body = self._request("GET", f"/zones/{zone_id}/rrsets")
        out: list[DNSRecord] = []
        rtype = record_type.upper() if record_type else None
        for rrset in body.get("rrsets") or []:
            if name is not None and rrset.get("name") != name:
                continue
            if rtype is not None and rrset.get("type") != rtype:
                continue
            ttl = rrset.get("ttl") or 300
            for rec in rrset.get("records") or []:
                out.append(
                    DNSRecord(
                        name=rrset.get("name", ""),
                        type=rrset.get("type", ""),
                        value=rec.get("value", ""),
                        ttl=int(ttl),
                        id=rrset.get("id"),  # "{name}/{type}"
                        provider_data=rrset,
                    )
                )
        return out

    def _short_name(self, name: str) -> str:
        return "@" if not name or name == "@" else name

    def _normalise_value(self, record_type: str, value: str) -> str:
        # CNAME / NS / MX targets must be FQDN with trailing dot.
        if record_type.upper() in ("CNAME", "NS"):
            return value if value.endswith(".") else f"{value}."
        return value

    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        short = self._short_name(name)
        rtype = record_type.upper()
        normalised = self._normalise_value(rtype, value)
        payload = {
            "name": short,
            "type": rtype,
            "ttl": ttl,
            "records": [{"value": normalised}],
        }
        body = self._request("POST", f"/zones/{zone_id}/rrsets", json=payload)
        rrset = body.get("rrset") or {}
        logger.info("Hetzner DNS: created %s %s -> %s", rtype, short, normalised)
        return DNSRecord(
            name=rrset.get("name", short),
            type=rrset.get("type", rtype),
            value=normalised,
            ttl=int(rrset.get("ttl") or ttl),
            id=rrset.get("id", f"{short}/{rtype}"),
            provider_data=rrset,
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
        short = self._short_name(name)
        rtype = record_type.upper()
        rrset_id = record_id if "/" in str(record_id) else f"{short}/{rtype}"
        normalised = self._normalise_value(rtype, value)
        payload = {"ttl": ttl, "records": [{"value": normalised}]}
        body = self._request("PUT", f"/zones/{zone_id}/rrsets/{rrset_id}", json=payload)
        rrset = body.get("rrset") or {}
        return DNSRecord(
            name=rrset.get("name", short),
            type=rrset.get("type", rtype),
            value=normalised,
            ttl=int(rrset.get("ttl") or ttl),
            id=rrset.get("id", rrset_id),
            provider_data=rrset,
        )

    def delete_record(self, zone_id: str, record_id: str) -> None:
        # record_id is the rrset id "{name}/{type}"
        self._request("DELETE", f"/zones/{zone_id}/rrsets/{record_id}")
        logger.info("Hetzner DNS: deleted rrset %s", record_id)
