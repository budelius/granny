"""deSEC DNS provider — CRUD via https://desec.io/api/v1.

deSEC models records as RRsets (one set per subname+type), and uses the
domain name itself as the "zone id". The provider-level ``id`` we expose is
an opaque ``subname|type`` pair so ``delete_record`` can round-trip to the
right rrset endpoint.
"""

import logging
import time

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_BASE = "https://desec.io/api/v1"
MIN_TTL = 3600  # deSEC API minimum


class DeSECDNSProvider(DNSProvider):
    name = "desec"

    def __init__(self, api_token: str | None = None) -> None:
        api_token = api_token or get_secret("DESEC_API_TOKEN")
        if not api_token:
            raise ValueError("DESEC_API_TOKEN not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {api_token}", "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(url, json=json, timeout=30)
        if resp.status_code == 429:
            delay = int(resp.headers.get("Retry-After", 5))
            logger.warning("deSEC rate limited, sleeping %ss", delay)
            time.sleep(delay)
            return self._request(method, path, json=json)
        if resp.status_code == 404:
            raise KeyError(path)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"deSEC {method} {path} failed: {resp.status_code} {resp.text}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def get_zone_id(self, zone_name: str) -> str:
        try:
            self._request("GET", f"/domains/{zone_name}/")
        except KeyError:
            raise RuntimeError(f"deSEC domain not found: {zone_name}")
        return zone_name

    def get_nameservers(self, zone_name: str) -> list[str]:
        # deSEC's nameservers are fixed.
        return ["ns1.desec.io", "ns2.desec.org"]

    def _normalize_subname(self, name: str | None) -> str:
        if name is None or name in ("@", ""):
            return ""
        return name

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        rrsets = self._request("GET", f"/domains/{zone_id}/rrsets/")
        if not isinstance(rrsets, list):
            rrsets = []
        subname_filter = self._normalize_subname(name) if name is not None else None
        rtype = record_type.upper() if record_type else None
        out: list[DNSRecord] = []
        for rrset in rrsets:
            if subname_filter is not None and rrset.get("subname", "") != subname_filter:
                continue
            if rtype is not None and rrset.get("type") != rtype:
                continue
            for value in rrset.get("records") or []:
                out.append(
                    DNSRecord(
                        name=rrset.get("subname", ""),
                        type=rrset.get("type", ""),
                        value=value,
                        ttl=int(rrset.get("ttl", MIN_TTL)),
                        id=f"{rrset.get('subname', '')}|{rrset.get('type', '')}",
                        provider_data=rrset,
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
        subname = self._normalize_subname(name)
        rtype = record_type.upper()
        ttl = max(ttl, MIN_TTL)
        # CNAME values in deSEC must be fully-qualified with trailing dot.
        stored = value if value.endswith(".") else f"{value}."
        payload_value = stored if rtype in ("CNAME", "MX", "NS", "PTR", "SRV") else value
        payload = {
            "subname": subname,
            "type": rtype,
            "records": [payload_value],
            "ttl": ttl,
        }
        try:
            self._request("POST", f"/domains/{zone_id}/rrsets/", json=payload)
        except RuntimeError as exc:
            # If the rrset already exists, upgrade to PATCH (merges records).
            if "already" in str(exc).lower():
                self._request(
                    "PATCH",
                    f"/domains/{zone_id}/rrsets/{subname}.../{rtype}/",
                    json={"records": [payload_value], "ttl": ttl},
                )
            else:
                raise
        logger.info("deSEC: created %s %s -> %s", rtype, subname or "@", payload_value)
        return DNSRecord(
            name=subname,
            type=rtype,
            value=payload_value,
            ttl=ttl,
            id=f"{subname}|{rtype}",
        )

    def delete_record(self, zone_id: str, record_id: str) -> None:
        # record_id is our synthetic "subname|type" pair. Deleting the whole
        # rrset is deSEC-idiomatic — per-value deletes require PATCH with the
        # remaining set.
        subname, _, rtype = record_id.partition("|")
        path = f"/domains/{zone_id}/rrsets/{subname}.../{rtype}/"
        try:
            self._request("DELETE", path)
        except KeyError:
            pass
        logger.info("deSEC: deleted rrset %s/%s", subname or "@", rtype)
