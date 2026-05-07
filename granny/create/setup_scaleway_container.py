#!/usr/bin/env python3
"""
Scaleway Serverless Containers Setup Script

Provisions a Scaleway Serverless Containers namespace and (optionally) a
container within it. Intended as the pre-step for a CI pipeline that builds
and pushes Docker images to the namespace's registry endpoint and then
patches the container to use the new image.

Environment Variables:
    SCW_ACCESS_KEY: Scaleway access key
    SCW_SECRET_KEY: Scaleway secret key
    SCW_DEFAULT_PROJECT_ID: Scaleway project ID

Usage:
    # Create namespace + container with placeholder image
    python setup_scaleway_container.py --name my-app --port 3000

    # Namespace only (no container)
    python setup_scaleway_container.py --name my-app --namespace-only

    # Different region
    python setup_scaleway_container.py --name my-app --region nl-ams
"""

import argparse
import logging
import os
import sys
import time
from typing import Optional

import requests

from dotenv import load_dotenv

load_dotenv()
try:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()
except Exception:
    pass


SCALEWAY_REGIONS = ("fr-par", "nl-ams", "pl-waw")

# Terminal states returned by the Scaleway Containers API.
_TERMINAL_NAMESPACE_STATES = {"ready", "error", "locked"}
_TERMINAL_CONTAINER_STATES = {"ready", "error", "pending", "created"}


class ScalewayContainersClient:
    """Minimal client for Scaleway Serverless Containers API."""

    def __init__(
        self,
        *,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        project_id: Optional[str] = None,
        region: str = "fr-par",
    ) -> None:
        self.access_key = access_key or os.environ.get("SCW_ACCESS_KEY")
        self.secret_key = secret_key or os.environ.get("SCW_SECRET_KEY")
        self.project_id = project_id or os.environ.get("SCW_DEFAULT_PROJECT_ID")
        self.region = region

        if not all([self.access_key, self.secret_key, self.project_id]):
            raise ValueError(
                "Missing Scaleway credentials. Set SCW_ACCESS_KEY, SCW_SECRET_KEY, "
                "and SCW_DEFAULT_PROJECT_ID (env or vault)."
            )
        if region not in SCALEWAY_REGIONS:
            raise ValueError(
                f"Unknown region: {region}. Choices: {', '.join(SCALEWAY_REGIONS)}"
            )

        self.api_base = f"https://api.scaleway.com/containers/v1beta1/regions/{region}"
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Auth-Token": self.secret_key, "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, data: Optional[dict] = None) -> dict:
        url = f"{self.api_base}{path}"
        resp = self.session.request(method, url, json=data, timeout=30)
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

    def find_namespace(self, name: str) -> Optional[dict]:
        for ns in self.list_namespaces():
            if ns.get("name") == name:
                return ns
        return None

    def get_namespace(self, namespace_id: str) -> dict:
        return self._request("GET", f"/namespaces/{namespace_id}")

    def create_namespace(self, name: str, description: str = "") -> dict:
        logging.info("Creating Scaleway Containers namespace: %s", name)
        return self._request(
            "POST",
            "/namespaces",
            {
                "name": name,
                "project_id": self.project_id,
                "description": description or f"Container namespace for {name}",
            },
        )

    def wait_for_namespace(self, namespace_id: str, timeout: int = 300) -> dict:
        """Poll until the namespace reaches a terminal state."""
        start = time.monotonic()
        while True:
            ns = self.get_namespace(namespace_id)
            status = ns.get("status")
            if status in _TERMINAL_NAMESPACE_STATES:
                return ns
            if time.monotonic() - start > timeout:
                raise TimeoutError(
                    f"Namespace {namespace_id} still '{status}' after {timeout}s"
                )
            time.sleep(5)

    # -- Containers ------------------------------------------------------------

    def list_containers(self, namespace_id: str) -> list[dict]:
        result = self._request("GET", f"/containers?namespace_id={namespace_id}")
        return result.get("containers", [])

    def find_container(self, namespace_id: str, name: str) -> Optional[dict]:
        for c in self.list_containers(namespace_id):
            if c.get("name") == name:
                return c
        return None

    def create_container(
        self,
        *,
        namespace_id: str,
        name: str,
        registry_image: str,
        port: int = 8080,
        min_scale: int = 0,
        max_scale: int = 1,
        memory_limit: int = 512,
        cpu_limit: int = 560,
        timeout: str = "30s",
        protocol: str = "http1",
        http_option: str = "redirected",
        privacy: str = "public",
        environment_variables: Optional[dict] = None,
    ) -> dict:
        logging.info("Creating container: %s (image=%s)", name, registry_image)
        data = {
            "namespace_id": namespace_id,
            "name": name,
            "registry_image": registry_image,
            "port": port,
            "min_scale": min_scale,
            "max_scale": max_scale,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
            "timeout": timeout,
            "protocol": protocol,
            "http_option": http_option,
            "privacy": privacy,
        }
        if environment_variables:
            data["environment_variables"] = environment_variables
        return self._request("POST", "/containers", data)


# -- CLI -----------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Setup Scaleway Serverless Containers (namespace + container)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python setup_scaleway_container.py --name my-app --port 3000\n"
            "  python setup_scaleway_container.py --name my-app --namespace-only\n"
        ),
    )
    parser.add_argument("--name", required=True, help="Namespace and container name")
    parser.add_argument(
        "--region", choices=list(SCALEWAY_REGIONS), default="fr-par",
        help="Scaleway region (default: fr-par)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Container listen port (default: 8080)",
    )
    parser.add_argument(
        "--initial-image", default="docker.io/library/nginx:alpine",
        help="Placeholder image used until CI pushes the real one "
             "(default: docker.io/library/nginx:alpine)",
    )
    parser.add_argument("--memory", type=int, default=1024, help="Memory MB (default: 1024). Scaleway requires memory_mb >= cpu_mvcpu.")
    parser.add_argument("--cpu", type=int, default=560, help="CPU mVCPU (default: 560)")
    parser.add_argument("--min-scale", type=int, default=0)
    parser.add_argument("--max-scale", type=int, default=1)
    parser.add_argument(
        "--namespace-only", action="store_true",
        help="Only create the namespace, skip container creation",
    )
    parser.add_argument(
        "--deploy-env", default=".deploy.env",
        help="Path to .deploy.env file (default: .deploy.env)",
    )
    parser.add_argument("--no-deploy-env", action="store_true")
    return parser


def _write_deploy_env(path: str, pairs: dict[str, str]) -> None:
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    existing[k] = v
    existing.update(pairs)
    with open(path, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args()

    client = ScalewayContainersClient(region=args.region)

    # Namespace: reuse or create.
    ns = client.find_namespace(args.name)
    if ns:
        logging.info("Namespace '%s' already exists (id=%s)", args.name, ns["id"])
    else:
        ns = client.create_namespace(args.name)

    if ns.get("status") != "ready":
        logging.info("Waiting for namespace '%s' to become ready...", args.name)
        ns = client.wait_for_namespace(ns["id"])
    if ns.get("status") != "ready":
        logging.error("Namespace did not reach 'ready': status=%s", ns.get("status"))
        return 1

    registry_endpoint = ns.get("registry_endpoint", "")
    logging.info("Namespace ready. Registry endpoint: %s", registry_endpoint)

    deploy_env_pairs = {
        "SCW_REGION": args.region,
        "SCW_CONTAINER_NAMESPACE": args.name,
        "SCW_CONTAINER_NAMESPACE_ID": ns["id"],
        "SCW_REGISTRY_ENDPOINT": registry_endpoint,
    }

    if args.namespace_only:
        logging.info("--namespace-only: skipping container creation")
    else:
        container = client.find_container(ns["id"], args.name)
        if container:
            logging.info("Container '%s' already exists (id=%s)", args.name, container["id"])
        else:
            container = client.create_container(
                namespace_id=ns["id"],
                name=args.name,
                registry_image=args.initial_image,
                port=args.port,
                min_scale=args.min_scale,
                max_scale=args.max_scale,
                memory_limit=args.memory,
                cpu_limit=args.cpu,
            )
            logging.info("Container created. ID: %s", container.get("id"))
        deploy_env_pairs["SCW_CONTAINER_NAME"] = args.name
        deploy_env_pairs["SCW_CONTAINER_ID"] = container["id"]
        if container.get("domain_name"):
            deploy_env_pairs["SCW_CONTAINER_DOMAIN"] = container["domain_name"]

    if not args.no_deploy_env:
        _write_deploy_env(args.deploy_env, deploy_env_pairs)
        logging.info("Wrote %d keys to %s", len(deploy_env_pairs), args.deploy_env)

    print("\n=== Scaleway Containers Provisioned ===")
    for k, v in deploy_env_pairs.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
