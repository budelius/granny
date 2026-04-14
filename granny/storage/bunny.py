"""Bunny Storage zone client — CRUD + file upload.

Lifted from ``tools/create/setup_bunny_storage.py:BunnyStorageClient``.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_bunny_api_key

logger = logging.getLogger(__name__)

API_BASE = "https://api.bunny.net"

BUNNY_STORAGE_REGIONS = {
    "DE": {"name": "Frankfurt, Germany", "hostname": "storage.bunnycdn.com"},
    "UK": {"name": "London, UK", "hostname": "uk.storage.bunnycdn.com"},
    "NY": {"name": "New York, US", "hostname": "ny.storage.bunnycdn.com"},
    "LA": {"name": "Los Angeles, US", "hostname": "la.storage.bunnycdn.com"},
    "SG": {"name": "Singapore", "hostname": "sg.storage.bunnycdn.com"},
    "SE": {"name": "Stockholm, Sweden", "hostname": "se.storage.bunnycdn.com"},
    "BR": {"name": "Sao Paulo, Brazil", "hostname": "br.storage.bunnycdn.com"},
    "JH": {"name": "Johannesburg, SA", "hostname": "jh.storage.bunnycdn.com"},
    "SYD": {"name": "Sydney, Australia", "hostname": "syd.storage.bunnycdn.com"},
}


class BunnyStorageClient:
    """Bunny Storage zone management and file upload."""

    def __init__(self, api_key: str | None = None, customer: str | None = None) -> None:
        api_key = api_key or get_bunny_api_key(customer)
        if not api_key:
            raise ValueError("BUNNY_API_KEY not set (env or vault).")
        self._api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {"AccessKey": api_key, "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, data: dict | None = None) -> Any:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(url, json=data, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Bunny Storage {method} {path}: {resp.status_code} {resp.text}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def list_storage_zones(self) -> list[dict]:
        return self._request("GET", "/storagezone")

    def find_storage_zone(self, name: str) -> dict | None:
        for zone in self.list_storage_zones():
            if zone.get("Name") == name:
                return zone
        return None

    def create_storage_zone(self, name: str, region: str = "DE") -> dict:
        result = self._request(
            "POST",
            "/storagezone",
            {"Name": name, "Region": region, "ZoneTier": 0},
        )
        logger.info("Created storage zone %s (ID %s)", name, result.get("Id"))
        return result

    def delete_storage_zone(self, zone_id: int) -> None:
        self._request("DELETE", f"/storagezone/{zone_id}")
        logger.info("Deleted storage zone %s", zone_id)

    def upload_file(
        self,
        storage_hostname: str,
        storage_password: str,
        zone_name: str,
        path: str,
        content: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Upload a file to a storage zone via the storage endpoint."""
        url = f"https://{storage_hostname}/{zone_name}/{path}"
        headers: dict[str, str] = {"AccessKey": storage_password}
        if content_type:
            headers["Content-Type"] = content_type
        if cache_control:
            headers["Cache-Control"] = cache_control
        resp = requests.put(url, data=content, headers=headers, timeout=30)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Upload {path} failed: {resp.status_code} {resp.text}"
            )
        logger.info("Uploaded %s to %s/%s", path, zone_name, path)
