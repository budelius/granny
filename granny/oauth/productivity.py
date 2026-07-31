"""Provision Google and Microsoft productivity OAuth client credentials.

Credentials can be written to an application's local dotenv file and to an
Obachan OpenBao KV v2 path. Provider-side application registration remains a
cloud-admin operation; Google client JSON downloaded from Google Cloud is
accepted directly.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from granny.credentials import get_secret

_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


@dataclass(frozen=True)
class ProductivityOAuthCredentials:
    """Google and Microsoft OAuth client credentials."""

    google_client_id: str | None = None
    google_client_secret: str | None = None
    azure_client_id: str | None = None
    azure_client_secret: str | None = None

    def as_env(self) -> dict[str, str]:
        """Return configured values using dashboard runtime variable names."""
        values = {
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "AZURE_CLIENT_ID": self.azure_client_id,
            "AZURE_CLIENT_SECRET": self.azure_client_secret,
        }
        return {key: value for key, value in values.items() if value}

    def validate(self) -> None:
        """Require complete client ID/secret pairs for each selected provider."""
        values = self.as_env()
        if not values:
            raise ValueError("At least one Google or Azure OAuth client is required")
        for provider in ("GOOGLE", "AZURE"):
            client_id = values.get(f"{provider}_CLIENT_ID")
            client_secret = values.get(f"{provider}_CLIENT_SECRET")
            if bool(client_id) != bool(client_secret):
                raise ValueError(
                    f"{provider}_CLIENT_ID and {provider}_CLIENT_SECRET must be provided together"
                )


@dataclass(frozen=True)
class ProvisionResult:
    """Locations updated by a productivity OAuth provisioning run."""

    keys: tuple[str, ...]
    env_file: Path | None = None
    obachan_path: str | None = None


def load_google_client_json(path: Path) -> tuple[str, str]:
    """Read a Google Cloud OAuth client download and return ID and secret."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read Google OAuth client JSON {path}: {exc}") from exc

    config = document.get("web") or document.get("installed") or document
    if not isinstance(config, dict):
        raise ValueError(f"Invalid Google OAuth client JSON: {path}")
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise ValueError(
            f"Google OAuth client JSON has no client_id/client_secret: {path}"
        )
    return client_id, client_secret


class ObachanSecretClient:
    """Small OpenBao KV v2 client for Obachan application secrets."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        mount: str = "kv",
    ) -> None:
        self.base_url = (base_url or os.environ.get("OBACHAN_BAO_ADDR") or "").rstrip(
            "/"
        )
        self.token = token or get_secret("OBACHAN_BAO_TOKEN")
        self.mount = mount.strip("/")

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise ValueError("OBACHAN_BAO_ADDR is not set")
        if not self.token:
            raise ValueError("OBACHAN_BAO_TOKEN is not set (env or vault)")
        encoded = "/".join(quote(segment, safe="") for segment in path.split("/"))
        return f"{self.base_url}/v1/{quote(self.mount, safe='')}/data/{encoded}"

    def merge(self, path: str, values: dict[str, str]) -> None:
        """Merge fields into one Obachan OpenBao KV v2 secret using CAS."""
        url = self._url(path)
        headers = {"X-Vault-Token": self.token or ""}
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise RuntimeError(f"Obachan OpenBao GET {path} failed: {exc}") from exc

        version = 0
        if response.status_code == 404:
            existing: dict[str, Any] = {}
        elif response.status_code == 200:
            try:
                body = response.json()
                data = body.get("data", {})
                existing = data.get("data", {})
                metadata = data.get("metadata", {})
                version = metadata.get("version", 0)
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Obachan OpenBao GET {path} returned invalid KV v2 JSON"
                ) from exc
            if not isinstance(existing, dict) or not isinstance(version, int):
                raise RuntimeError(
                    f"Obachan OpenBao GET {path} returned invalid KV v2 data"
                )
        else:
            _raise_bao_error("GET", path, response)

        try:
            response = requests.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "data": {**existing, **values},
                    "options": {"cas": version},
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Obachan OpenBao POST {path} failed: {exc}") from exc
        if response.status_code not in (200, 204):
            _raise_bao_error("POST", path, response)


def provision_productivity_oauth(
    credentials: ProductivityOAuthCredentials,
    *,
    env_file: Path | None = None,
    obachan_env: str | None = None,
    obachan_app: str = "dashboard",
    obachan_client: ObachanSecretClient | None = None,
    dry_run: bool = False,
) -> ProvisionResult:
    """Validate and provision OAuth credentials to selected destinations."""
    credentials.validate()
    values = credentials.as_env()

    if env_file is None and obachan_env is None:
        raise ValueError("Select an env_file or obachan_env destination")

    if env_file is not None and not dry_run:
        update_env_file(env_file, values)

    obachan_path = None
    if obachan_env is not None:
        _validate_segment("Obachan environment", obachan_env)
        _validate_segment("Obachan application", obachan_app)
        obachan_path = f"obachan/{obachan_env}/apps/{obachan_app}/productivity-oauth"
        if not dry_run:
            (obachan_client or ObachanSecretClient()).merge(obachan_path, values)

    return ProvisionResult(
        keys=tuple(sorted(values)), env_file=env_file, obachan_path=obachan_path
    )


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Update dotenv assignments while preserving comments and unrelated lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    replaced: set[str] = set()
    output: list[str] = []
    assignment = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        match = assignment.match(line)
        key = match.group(3) if match else None
        if key in updates:
            if key not in replaced:
                prefix = f"{match.group(1)}{match.group(2) or ''}"
                output.append(f"{prefix}{key}={_quote_env(updates[key])}")
                replaced.add(key)
                remaining.pop(key, None)
            continue
        output.append(line)
    if output and remaining and output[-1]:
        output.append("")
    output.extend(
        f"{key}={_quote_env(value)}" for key, value in sorted(remaining.items())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _validate_segment(label: str, value: str) -> None:
    if not _SEGMENT_RE.fullmatch(value):
        raise ValueError(
            f"{label} must contain only letters, digits, '.', '_', and '-'"
        )


def _quote_env(value: str) -> str:
    if (
        not value
        or any(ch.isspace() for ch in value)
        or any(ch in value for ch in "\"'#$")
    ):
        return f'"{value.replace(chr(34), chr(92) + chr(34))}"'
    return value


def _raise_bao_error(method: str, path: str, response: requests.Response) -> None:
    raise RuntimeError(
        f"Obachan OpenBao {method} {path} failed: "
        f"{response.status_code} {response.text[:1000]}"
    )
