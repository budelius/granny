"""Bunny Edge Scripting client — script CRUD, code deploy, variables, publish.

Lifted from ``tools/create/setup_bunny_edge_script.py:BunnyEdgeScriptClient``.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_bunny_api_key

logger = logging.getLogger(__name__)

API_BASE = "https://api.bunny.net"
EDGE_SCRIPTING_API = "/api/v1/edgescripting"

SCRIPT_TYPES = {
    "standalone": 0,
    "middleware": 3,
}


class BunnyEdgeScriptClient:
    """Bunny.net Edge Scripting management.

    API reference: https://docs.bunny.net/docs/edge-scripting-overview
    """

    def __init__(self, api_key: str | None = None, customer: str | None = None) -> None:
        api_key = api_key or get_bunny_api_key(customer)
        if not api_key:
            raise ValueError("BUNNY_API_KEY not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "AccessKey": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _request(self, method: str, path: str, data: Any = None) -> Any:
        url = f"{API_BASE}{path}"
        kwargs: dict = {"timeout": 60}
        if data is not None:
            kwargs["json"] = data
        resp = getattr(self.session, method.lower())(url, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Bunny Edge {method} {path}: {resp.status_code} {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    # -- Script CRUD -----------------------------------------------------------

    def list_scripts(self, search: str | None = None) -> list[dict]:
        params = f"?search={requests.utils.quote(search)}" if search else ""
        result = self._request("GET", f"{EDGE_SCRIPTING_API}{params}")
        if isinstance(result, list):
            return result
        return result.get("Items", result.get("items", []))

    def find_script(self, name: str) -> dict | None:
        for s in self.list_scripts(search=name):
            if s.get("Name") == name:
                return s
        return None

    def get_script(self, script_id: int) -> dict:
        return self._request("GET", f"{EDGE_SCRIPTING_API}/{script_id}")

    def create_script(
        self,
        name: str,
        *,
        code: str = "",
        script_type: str = "standalone",
        create_linked_pull_zone: bool = True,
        linked_pull_zone_name: str | None = None,
    ) -> dict:
        type_id = SCRIPT_TYPES.get(script_type, 0)
        payload: dict[str, Any] = {"Name": name, "ScriptType": type_id}
        if code:
            payload["Code"] = code
        if type_id == 0:
            payload["CreateLinkedPullZone"] = create_linked_pull_zone
            if linked_pull_zone_name:
                payload["LinkedPullZoneName"] = linked_pull_zone_name
        result = self._request("POST", EDGE_SCRIPTING_API, payload)
        logger.info("Created edge script %s (ID %s)", name, result.get("Id"))
        return result

    def delete_script(
        self, script_id: int, *, delete_linked_pull_zones: bool = False
    ) -> None:
        flag = "true" if delete_linked_pull_zones else "false"
        self._request(
            "DELETE",
            f"{EDGE_SCRIPTING_API}/{script_id}?deleteLinkedPullZones={flag}",
        )

    # -- Code deployment -------------------------------------------------------

    def get_code(self, script_id: int) -> str:
        result = self._request("GET", f"{EDGE_SCRIPTING_API}/{script_id}/code")
        return result.get("Code", "")

    def deploy_code(self, script_id: int, code: str) -> dict:
        logger.info("Deploying code to script %s (%d bytes)", script_id, len(code))
        return self._request(
            "POST", f"{EDGE_SCRIPTING_API}/{script_id}/code", {"Code": code}
        )

    # -- Releases --------------------------------------------------------------

    def list_releases(self, script_id: int) -> list[dict]:
        result = self._request("GET", f"{EDGE_SCRIPTING_API}/{script_id}/releases")
        if isinstance(result, list):
            return result
        return result.get("Items", result.get("items", []))

    def publish(self, script_id: int) -> None:
        self._request("POST", f"{EDGE_SCRIPTING_API}/{script_id}/publish", {})
        logger.info("Published edge script %s", script_id)

    # -- Variables (env + secrets share the same endpoint) ----------------------

    def list_variables(self, script_id: int) -> list[dict]:
        detail = self.get_script(script_id)
        return detail.get("EdgeScriptVariables", [])

    def upsert_variable(
        self, script_id: int, name: str, value: str, *, required: bool = True
    ) -> dict:
        return self._request(
            "PUT",
            f"{EDGE_SCRIPTING_API}/{script_id}/variables",
            {"Name": name, "DefaultValue": value, "Required": required},
        )

    def delete_variable(self, script_id: int, var_id: int) -> None:
        self._request(
            "DELETE", f"{EDGE_SCRIPTING_API}/{script_id}/variables/{var_id}"
        )
