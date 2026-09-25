"""Bunny CDN pull zone client — unified from 4 setup scripts.

Merged superset of ``setup_hetzner_bunny.py:BunnyCDNClient`` (token auth,
origin shield) and ``setup_bunny_storage.py:BunnyCDNClient`` (storage-zone
pull zones, SPA routing, edge rules). The existing scripts keep their local
copies; this module is the shared home.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_bunny_api_key

logger = logging.getLogger(__name__)

API_BASE = "https://api.bunny.net"


class BunnyCDNClient:
    """Bunny CDN pull zone management.

    Covers pull zone CRUD, custom hostnames, SSL certificates, edge rules,
    token authentication, origin shield, and cache purging.
    """

    def __init__(self, api_key: str | None = None, customer: str | None = None) -> None:
        api_key = api_key or get_bunny_api_key(customer)
        if not api_key:
            label = f"BUNNY_API_KEY_{customer.upper()}" if customer else "BUNNY_API_KEY"
            raise ValueError(f"{label} not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {"AccessKey": api_key, "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, data: dict | None = None) -> Any:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(url, json=data, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Bunny CDN {method} {path}: {resp.status_code} {resp.text}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    # -- Pull Zone CRUD --------------------------------------------------------

    def list_pull_zones(self) -> list[dict]:
        return self._request("GET", "/pullzone")

    def find_pull_zone(
        self, *, name: str | None = None, hostname: str | None = None
    ) -> dict | None:
        """Find a pull zone by name or by a registered hostname."""
        for zone in self.list_pull_zones():
            if name and zone.get("Name") == name:
                return zone
            if hostname:
                for h in zone.get("Hostnames", []):
                    if h.get("Value") == hostname:
                        return zone
        return None

    def get_pull_zone(self, pull_zone_id: int) -> dict:
        return self._request("GET", f"/pullzone/{pull_zone_id}")

    def create_pull_zone(
        self,
        name: str,
        *,
        origin_url: str | None = None,
        storage_zone_id: int | None = None,
        enable_geo_zones: bool = True,
    ) -> dict:
        """Create a pull zone.

        Pass ``origin_url`` for an S3/HTTP origin, or ``storage_zone_id``
        for a Bunny Storage-backed pull zone. Exactly one must be provided.
        """
        if origin_url and storage_zone_id:
            raise ValueError("Pass origin_url OR storage_zone_id, not both.")
        if not origin_url and not storage_zone_id:
            raise ValueError("Either origin_url or storage_zone_id is required.")

        data: dict[str, Any] = {
            "Name": name,
            "EnableGeoZoneUS": enable_geo_zones,
            "EnableGeoZoneEU": enable_geo_zones,
            "EnableGeoZoneASIA": enable_geo_zones,
            "EnableGeoZoneSA": enable_geo_zones,
            "EnableGeoZoneAF": enable_geo_zones,
        }
        if storage_zone_id:
            data["OriginType"] = 2
            data["StorageZoneId"] = storage_zone_id
        else:
            data["OriginUrl"] = origin_url
            data["Type"] = 0

        result = self._request("POST", "/pullzone", data)
        logger.info("Created pull zone %s (ID %s)", name, result.get("Id"))
        return result

    def update_pull_zone(self, pull_zone_id: int, **settings: Any) -> dict:
        return self._request("POST", f"/pullzone/{pull_zone_id}", settings)

    def delete_pull_zone(self, pull_zone_id: int) -> None:
        self._request("DELETE", f"/pullzone/{pull_zone_id}")

    # -- Hostnames + SSL -------------------------------------------------------

    def add_hostname(self, pull_zone_id: int, hostname: str) -> dict:
        """Add a custom hostname. Idempotent — returns status dict."""
        try:
            self._request(
                "POST",
                f"/pullzone/{pull_zone_id}/addHostname",
                {"Hostname": hostname},
            )
            logger.info("Added hostname %s to pull zone %s", hostname, pull_zone_id)
            return {"added": True}
        except RuntimeError as exc:
            if "hostname_already_registered" in str(exc):
                logger.info("Hostname %s already registered", hostname)
                return {"added": False, "exists": True}
            raise

    def request_ssl_certificate(
        self, hostname: str, *, use_dns01: bool = False
    ) -> bool:
        """Request a free SSL certificate for a hostname."""
        params = f"hostname={hostname}"
        if use_dns01:
            params += "&useOnlyHttp01=false"
        url = f"{API_BASE}/pullzone/loadFreeCertificate?{params}"
        resp = self.session.get(url, timeout=30)
        ok = resp.status_code in (200, 204)
        if ok:
            logger.info("SSL certificate requested for %s", hostname)
        else:
            logger.warning("SSL request for %s returned %s", hostname, resp.status_code)
        return ok

    # -- Token Authentication + Origin Shield ----------------------------------

    def enable_token_auth(
        self, pull_zone_id: int, *, token_key: str | None = None
    ) -> dict:
        """Enable token authentication (signed URLs). Returns updated zone."""
        data: dict[str, Any] = {
            "ZoneSecurityEnabled": True,
            "ZoneSecurityIncludeHashRemoteIP": False,
        }
        if token_key:
            data["ZoneSecurityKey"] = token_key
        self._request("POST", f"/pullzone/{pull_zone_id}", data)
        logger.info("Token auth enabled on pull zone %s", pull_zone_id)
        return self.get_pull_zone(pull_zone_id)

    def enable_origin_shield(
        self, pull_zone_id: int, zone_code: str = "DE"
    ) -> None:
        """Block direct origin access — route everything through CDN."""
        self._request(
            "POST",
            f"/pullzone/{pull_zone_id}",
            {"EnableOriginShield": True, "OriginShieldZoneCode": zone_code},
        )
        logger.info("Origin shield enabled on pull zone %s", pull_zone_id)

    # -- SPA + Edge Rules ------------------------------------------------------

    def configure_spa_error_page(self, pull_zone_id: int) -> None:
        """Configure custom error page for SPA support."""
        self._request(
            "POST",
            f"/pullzone/{pull_zone_id}",
            {
                "ErrorPageEnableCustomCode": True,
                "ErrorPageCustomCode": (
                    '<!DOCTYPE html><html><head>'
                    '<script>sessionStorage.redirect=location.pathname'
                    '+location.search+location.hash;</script>'
                    '<meta http-equiv="refresh" content="0;url=/"></head></html>'
                ),
                "ErrorPageWhitelabel": True,
            },
        )
        logger.info("SPA error page configured on pull zone %s", pull_zone_id)

    def configure_spa_cache_headers(
        self,
        pull_zone_id: int,
        asset_path: str = "*/assets/*",
        asset_max_age: int = 2592000,
    ) -> None:
        """Stop browsers pinning a single-page app to a stale build.

        A pull zone's ``CacheControlMaxAgeOverride`` applies one ``max-age`` to
        every response, ``index.html`` included. That is the one file which
        must never be cached: it is the map from a URL to the content-hashed
        chunk names, so a browser holding an old copy keeps requesting the
        previous build's filenames -- which the origin still serves -- and runs
        stale code until the header expires. Purging the CDN does not help; the
        browser has been told not to ask.

        So default the zone to no browser caching and hand the long cache back
        to the hashed assets alone, by path rather than by extension: in a SPA
        every route returns HTML, and ``/invoice-upload`` looks nothing like
        ``*.html``. Matching what is safe to cache is reliable in a way that
        enumerating what is not never becomes.
        """
        self.update_pull_zone(pull_zone_id, CacheControlPublicMaxAgeOverride=0)
        self.add_or_update_edge_rule(
            pull_zone_id,
            {
                "ActionType": 16,  # OverrideBrowserCacheTime
                "ActionParameter1": str(asset_max_age),
                "Triggers": [
                    {
                        "Type": 0,  # Url
                        "PatternMatchingType": 0,  # MatchAny
                        "PatternMatches": [asset_path],
                    }
                ],
                "TriggerMatchingType": 0,
                "Description": (
                    "Content-hashed assets may be cached by the browser; "
                    "index.html and SPA routes must not be."
                ),
                "Enabled": True,
            },
        )
        logger.info(
            "SPA cache headers configured on pull zone %s "
            "(no browser cache by default, %ss for %s)",
            pull_zone_id,
            asset_max_age,
            asset_path,
        )

    def configure_spa_routing(self, storage_zone_id: int) -> None:
        """Configure SPA routing on a Bunny storage zone (404 -> /index.html)."""
        self._request(
            "POST",
            f"/storagezone/{storage_zone_id}",
            {"Custom404FilePath": "/index.html", "Rewrite404To200": True},
        )
        logger.info("SPA routing configured on storage zone %s", storage_zone_id)

    def get_edge_rules(self, pull_zone_id: int) -> list[dict]:
        pz = self._request("GET", f"/pullzone/{pull_zone_id}")
        return pz.get("EdgeRules", [])

    def add_or_update_edge_rule(self, pull_zone_id: int, rule: dict) -> dict:
        return self._request(
            "POST", f"/pullzone/{pull_zone_id}/edgerules/addOrUpdate", rule
        )

    def delete_edge_rule(self, pull_zone_id: int, rule_guid: str) -> dict:
        return self._request(
            "DELETE",
            f"/pullzone/{pull_zone_id}/edgerules/{rule_guid}",
        )

    # -- Cache -----------------------------------------------------------------

    def purge_cache(self, pull_zone_id: int) -> None:
        self._request("POST", f"/pullzone/{pull_zone_id}/purgeCache")
        logger.info("Cache purged for pull zone %s", pull_zone_id)

    # -- Helpers ---------------------------------------------------------------

    def get_default_hostname(self, pull_zone: dict) -> str:
        """Return the ``*.b-cdn.net`` hostname for a pull zone."""
        for h in pull_zone.get("Hostnames", []):
            if h.get("Value", "").endswith(".b-cdn.net"):
                return h["Value"]
        return f"{pull_zone['Name']}.b-cdn.net"
