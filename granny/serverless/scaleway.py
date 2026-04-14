"""Scaleway Functions (FaaS) client — namespace/function/domain/trigger CRUD.

Lifted from ``tools/create/setup_scaleway_faas.py:ScalewayFunctionsClient``.
"""

import json
import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

SCALEWAY_REGIONS = {
    "fr-par": {
        "name": "Paris, France",
        "functions_api": "https://api.scaleway.com/functions/v1beta1/regions/fr-par",
    },
    "nl-ams": {
        "name": "Amsterdam, Netherlands",
        "functions_api": "https://api.scaleway.com/functions/v1beta1/regions/nl-ams",
    },
    "pl-waw": {
        "name": "Warsaw, Poland",
        "functions_api": "https://api.scaleway.com/functions/v1beta1/regions/pl-waw",
    },
}

SCALEWAY_RUNTIMES = [
    "node20", "node22", "python311", "python312", "go121", "rust165",
]


class ScalewayFunctionsClient:
    """Scaleway Serverless Functions management."""

    def __init__(
        self,
        *,
        access_key: str | None = None,
        secret_key: str | None = None,
        project_id: str | None = None,
        region: str = "fr-par",
    ) -> None:
        self.access_key = access_key or get_secret("SCW_ACCESS_KEY")
        self.secret_key = secret_key or get_secret("SCW_SECRET_KEY")
        self.project_id = project_id or get_secret("SCW_DEFAULT_PROJECT_ID")
        self.region = region

        if not all([self.access_key, self.secret_key, self.project_id]):
            raise ValueError(
                "Missing Scaleway credentials. Set SCW_ACCESS_KEY, "
                "SCW_SECRET_KEY, SCW_DEFAULT_PROJECT_ID (env or vault)."
            )
        if region not in SCALEWAY_REGIONS:
            raise ValueError(
                f"Unknown region: {region}. Choices: {', '.join(SCALEWAY_REGIONS)}"
            )

        self._api_base = SCALEWAY_REGIONS[region]["functions_api"]
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Auth-Token": self.secret_key, "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, data: dict | None = None) -> Any:
        url = f"{self._api_base}{path}"
        resp = getattr(self.session, method.lower())(url, json=data, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Scaleway {method} {path}: {resp.status_code} {resp.text}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    # -- Namespaces ------------------------------------------------------------

    def list_namespaces(self) -> list[dict]:
        result = self._request("GET", f"/namespaces?project_id={self.project_id}")
        return result.get("namespaces", [])

    def find_namespace(self, name: str) -> dict | None:
        for ns in self.list_namespaces():
            if ns.get("name") == name:
                return ns
        return None

    def get_namespace(self, namespace_id: str) -> dict:
        return self._request("GET", f"/namespaces/{namespace_id}")

    def create_namespace(
        self,
        name: str,
        description: str = "",
        *,
        env: dict | None = None,
        secrets: list[dict] | None = None,
    ) -> dict:
        data: dict[str, Any] = {
            "name": name,
            "project_id": self.project_id,
            "description": description or f"Function namespace for {name}",
        }
        if env:
            data["environment_variables"] = env
        if secrets:
            data["secret_environment_variables"] = secrets
        result = self._request("POST", "/namespaces", data)
        logger.info("Created namespace %s (ID %s)", name, result.get("id"))
        return result

    def delete_namespace(self, namespace_id: str) -> None:
        self._request("DELETE", f"/namespaces/{namespace_id}")

    # -- Functions -------------------------------------------------------------

    def list_functions(self, namespace_id: str) -> list[dict]:
        result = self._request("GET", f"/functions?namespace_id={namespace_id}")
        return result.get("functions", [])

    def find_function(self, namespace_id: str, name: str) -> dict | None:
        for fn in self.list_functions(namespace_id):
            if fn.get("name") == name:
                return fn
        return None

    def create_function(
        self,
        namespace_id: str,
        name: str,
        *,
        runtime: str = "node20",
        handler: str = "handler.handler",
        min_scale: int = 0,
        max_scale: int = 5,
        memory_limit: int = 256,
        timeout: str = "30s",
        privacy: str = "public",
        description: str = "",
        env: dict | None = None,
        secrets: list[dict] | None = None,
    ) -> dict:
        data: dict[str, Any] = {
            "name": name,
            "namespace_id": namespace_id,
            "runtime": runtime,
            "handler": handler,
            "min_scale": min_scale,
            "max_scale": max_scale,
            "memory_limit": memory_limit,
            "timeout": timeout,
            "http_option": "enabled",
            "privacy": privacy,
            "description": description or f"Function {name}",
        }
        if env:
            data["environment_variables"] = env
        if secrets:
            data["secret_environment_variables"] = secrets
        result = self._request("POST", "/functions", data)
        logger.info("Created function %s (ID %s)", name, result.get("id"))
        return result

    def delete_function(self, function_id: str) -> None:
        self._request("DELETE", f"/functions/{function_id}")

    def deploy_function(self, function_id: str) -> dict:
        return self._request("POST", f"/functions/{function_id}/deploy")

    def get_upload_url(self, function_id: str, content_length: int) -> str:
        result = self._request(
            "GET", f"/functions/{function_id}/upload-url?content_length={content_length}"
        )
        url = result.get("url")
        if not url:
            raise RuntimeError(f"No upload URL in response: {result}")
        return url

    def update_function(
        self,
        function_id: str,
        *,
        env: dict | None = None,
        secrets: list[dict] | None = None,
    ) -> dict:
        data: dict[str, Any] = {}
        if env is not None:
            data["environment_variables"] = env
        if secrets is not None:
            data["secret_environment_variables"] = secrets
        if not data:
            return {}
        return self._request("PATCH", f"/functions/{function_id}", data)

    def upload_package(self, function_id: str, zip_path: str) -> None:
        """Upload a zipped function package via presigned URL."""
        import os

        size = os.path.getsize(zip_path)
        upload_url = self.get_upload_url(function_id, size)
        with open(zip_path, "rb") as fh:
            resp = requests.put(
                upload_url,
                data=fh,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
                timeout=600,
            )
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Upload failed: HTTP {resp.status_code} {resp.text[:500]}"
            )
        logger.info("Uploaded %d bytes to function %s", size, function_id)

    def deploy_package(
        self,
        function_id: str,
        zip_path: str,
        *,
        env: dict | None = None,
        secrets: list[dict] | None = None,
    ) -> dict:
        """Upload code, sync env vars, and trigger deployment."""
        self.upload_package(function_id, zip_path)
        if env is not None or secrets is not None:
            self.update_function(function_id, env=env, secrets=secrets)
            logger.info("Synced environment variables")
        return self.deploy_function(function_id)

    # -- Domains ---------------------------------------------------------------

    def list_domains(self, function_id: str) -> list[dict]:
        result = self._request("GET", f"/domains?function_id={function_id}")
        return result.get("domains", [])

    def create_domain(self, function_id: str, hostname: str) -> dict:
        result = self._request(
            "POST", "/domains", {"function_id": function_id, "hostname": hostname}
        )
        logger.info("Added domain %s to function %s", hostname, function_id)
        return result

    # -- Triggers --------------------------------------------------------------

    def list_triggers(self, function_id: str) -> list[dict]:
        result = self._request("GET", f"/triggers?function_id={function_id}")
        return result.get("triggers", [])

    def create_cron_trigger(
        self, function_id: str, name: str, schedule: str, args: dict | None = None
    ) -> dict:
        data: dict[str, Any] = {
            "name": name,
            "function_id": function_id,
            "schedule": schedule,
        }
        if args:
            data["args"] = json.dumps(args)
        return self._request("POST", "/triggers", data)
