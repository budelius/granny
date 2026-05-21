"""Google Indexing API client.

Submits ``URL_UPDATED`` or ``URL_DELETED`` notifications to Google's
Indexing API (``indexing.googleapis.com/v3/urlNotifications:publish``).

Caveats from Google:

* Officially supported only for pages carrying ``JobPosting`` or
  ``BroadcastEvent`` structured data.
* The calling service account must be added as an owner of the property
  in Google Search Console.

For general-purpose site indexing, submit a sitemap via Search Console and
rely on the crawler -- this API is not a substitute.

Auth: a Google Cloud service account JSON key. Pass it as a dict, a path,
or raw JSON string to the constructor, or set
``GOOGLE_INDEXING_CREDENTIALS`` to the file path / raw JSON.

Requires ``google-auth`` (install ``granny-devops[seo]``).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

API_URL = "https://indexing.googleapis.com/v3/urlNotifications:publish"
SCOPE = "https://www.googleapis.com/auth/indexing"

NotifyType = Literal["URL_UPDATED", "URL_DELETED"]


def _load_service_account(value: dict | str | Path | None) -> dict[str, Any]:
    """Resolve a service account from a dict, file path, or JSON string."""
    if isinstance(value, dict):
        return value
    if value is None:
        value = get_secret("GOOGLE_INDEXING_CREDENTIALS")
    if not value:
        raise ValueError(
            "GOOGLE_INDEXING_CREDENTIALS not set (env or vault). "
            "Provide a service account JSON path, raw JSON, or dict."
        )
    text = str(value).strip()
    if text.startswith("{"):
        return json.loads(text)
    path = Path(text).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Service account file not found: {path}")
    return json.loads(path.read_text())


class GoogleIndexingClient:
    """Wrapper for ``indexing.googleapis.com``'s ``urlNotifications:publish``.

    Args:
        service_account: Service account JSON as a dict, JSON string, or
            filesystem path. Falls back to ``GOOGLE_INDEXING_CREDENTIALS``.
    """

    def __init__(self, service_account: dict | str | Path | None = None) -> None:
        self._sa_info = _load_service_account(service_account)
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self.session = requests.Session()

    def _get_access_token(self) -> str:
        # Reuse the cached token until ~1 minute before expiry.
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError(
                "google-auth is required for the Google Indexing API. "
                "Install with: pip install 'granny-devops[seo]'"
            ) from exc

        creds = service_account.Credentials.from_service_account_info(
            self._sa_info, scopes=[SCOPE]
        )
        creds.refresh(Request())
        self._token = creds.token
        self._token_expiry = (
            creds.expiry.timestamp() if creds.expiry else time.time() + 3000
        )
        return self._token

    def submit(self, url: str, notify_type: NotifyType = "URL_UPDATED") -> dict:
        """Notify Google about a single URL update or deletion."""
        token = self._get_access_token()
        resp = self.session.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"url": url, "type": notify_type},
            timeout=30,
        )
        if resp.status_code == 404:
            raise KeyError(url)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Google Indexing {notify_type} {url}: "
                f"{resp.status_code} {resp.text}"
            )
        logger.info("Google Indexing: %s %s", notify_type, url)
        return resp.json() if resp.text else {}

    def submit_batch(
        self,
        urls: list[str],
        notify_type: NotifyType = "URL_UPDATED",
    ) -> list[dict]:
        """Submit multiple URLs sequentially.

        The Indexing API has no batch endpoint usable without HTTP-batch
        multipart, so this loops one-by-one. Service-account quota is the
        bottleneck, not request count.
        """
        return [self.submit(u, notify_type) for u in urls]
