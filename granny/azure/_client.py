"""Shared Azure Resource Manager HTTP helpers.

Used by every module under ``granny.azure`` except ``quota.py``, which keeps
its own copy for historical reasons. New modules should route through here.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import requests

MANAGEMENT_SCOPE = "https://management.azure.com/.default"
MANAGEMENT_URL = "https://management.azure.com"


def get_management_token() -> str:
    """Return an ARM bearer token from the default credential chain."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as e:
        raise RuntimeError(
            "Azure support requires the [azure] extra: "
            "pip install 'granny-devops[azure]'"
        ) from e

    credential = DefaultAzureCredential()
    return credential.get_token(MANAGEMENT_SCOPE).token


def headers(token: str | None = None) -> dict[str, str]:
    """Authorization headers for ARM REST calls."""
    bearer = token if token is not None else get_management_token()
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }


def get(url: str, *, token: str | None = None) -> dict[str, Any]:
    """Issue a GET against ARM and return the JSON body."""
    response = requests.get(url, headers=headers(token), timeout=60)
    if response.status_code == 404:
        raise KeyError(f"Azure resource not found: {url}")
    if response.status_code != 200:
        raise_response_error("GET", url, response)
    return response.json()


def paginate(url: str, *, token: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield each item from a paginated ARM ``value``/``nextLink`` collection."""
    bearer = token if token is not None else get_management_token()
    next_url: str | None = url
    while next_url:
        data = get(next_url, token=bearer)
        for item in data.get("value", []):
            yield item
        next_url = data.get("nextLink")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Return the unverified JWT payload claims for ``token``."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return {}


def raise_response_error(method: str, url: str, response: requests.Response) -> None:
    """Wrap a non-OK ARM response in a uniform RuntimeError."""
    snippet = response.text[:1000]
    raise RuntimeError(
        f"Azure ARM {method} {url} failed: {response.status_code} {snippet}"
    )
