"""IndexNow protocol client.

Submits URLs to search engines that implement IndexNow (Bing, Yandex,
Seznam.cz, Naver, Yep, ...). Submitting to the shared ``api.indexnow.org``
endpoint is the documented way to notify every participating engine in
one call; per-engine endpoints exist for cases where you want to reach
just one.

Setup once per host:

1. Generate a random key (8-128 hex chars). Use :func:`generate_key` or
   ``granny indexing generate-key``.
2. Host the key as plain text at ``https://<host>/<key>.txt`` -- the file
   body MUST be exactly the key. This is how the engine proves the
   submitter controls the host.
3. Store the key as ``INDEXNOW_KEY`` (env var or vault).

Reference: https://www.indexnow.org/documentation
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

# Engine -> submission endpoint. The shared ``indexnow`` endpoint fans out
# to every participating engine; the per-engine entries are mostly useful
# for debugging which provider rejected a submission.
INDEXNOW_ENDPOINTS: dict[str, str] = {
    "indexnow": "https://api.indexnow.org/IndexNow",
    "bing": "https://www.bing.com/IndexNow",
    "yandex": "https://yandex.com/indexnow",
    "seznam": "https://search.seznam.cz/indexnow",
    "naver": "https://searchadvisor.naver.com/indexnow",
    "yep": "https://indexnow.yep.com/IndexNow",
}

DEFAULT_ENDPOINT = "indexnow"

# Per https://www.indexnow.org/documentation: 8-128 chars from [A-Za-z0-9-].
_KEY_PATTERN = re.compile(r"[A-Za-z0-9-]{8,128}")


def validate_key(key: str) -> None:
    """Raise ``ValueError`` if ``key`` doesn't satisfy the IndexNow spec."""
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError(
            f"Invalid IndexNow key {key!r}: must be 8-128 chars from [A-Za-z0-9-]."
        )


def generate_key(length: int = 32) -> str:
    """Generate a fresh IndexNow key (lowercase hex).

    Per the spec the key must be 8-128 chars and may contain only
    [a-zA-Z0-9-]. Hex satisfies that and rotates cleanly.
    """
    if not 8 <= length <= 128:
        raise ValueError("IndexNow key length must be 8-128 chars.")
    return secrets.token_hex(length // 2)


class IndexNowClient:
    """Submit URLs to IndexNow-participating search engines.

    Args:
        key: The IndexNow key. Falls back to ``INDEXNOW_KEY`` (env/vault).
        key_location: Public URL where the key file is hosted. Defaults
            to ``https://<host>/<key>.txt`` -- the spec's canonical
            location -- inferred per submitted host.
        endpoint: Either a short name from :data:`INDEXNOW_ENDPOINTS`
            (``"indexnow"``, ``"bing"``, ...) or a full URL. The default
            ``"indexnow"`` fans submissions out to every engine.
    """

    def __init__(
        self,
        key: str | None = None,
        key_location: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.key = key or get_secret("INDEXNOW_KEY")
        if not self.key:
            raise ValueError("INDEXNOW_KEY not set (env or vault).")
        self.key_location = key_location

        url = INDEXNOW_ENDPOINTS.get(endpoint, endpoint)
        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"Unknown IndexNow endpoint {endpoint!r}. "
                f"Known: {sorted(INDEXNOW_ENDPOINTS)} or pass a full URL."
            )
        self.endpoint = url
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json; charset=utf-8"}
        )

    @staticmethod
    def _host_of(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Not an absolute URL: {url!r}")
        return parsed.netloc

    def _key_location_for(self, host: str) -> str:
        if self.key_location:
            return self.key_location
        return f"https://{host}/{self.key}.txt"

    def submit(self, url: str) -> None:
        """Submit a single URL via IndexNow."""
        self.submit_batch([url])

    def submit_batch(self, urls: Iterable[str]) -> None:
        """Submit a batch of URLs. All URLs must share the same host."""
        url_list = list(urls)
        if not url_list:
            return
        hosts = {self._host_of(u) for u in url_list}
        if len(hosts) != 1:
            raise ValueError(
                f"IndexNow requires a single host per submission; got {sorted(hosts)}"
            )
        host = hosts.pop()
        payload: dict = {
            "host": host,
            "key": self.key,
            "keyLocation": self._key_location_for(host),
            "urlList": url_list,
        }
        resp = self.session.post(self.endpoint, json=payload, timeout=30)
        self._raise_for_status(resp)
        logger.info(
            "IndexNow: submitted %d URL(s) for %s via %s",
            len(url_list),
            host,
            self.endpoint,
        )

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        # 200 OK and 202 Accepted are both success per the spec.
        if resp.status_code in (200, 202):
            return
        if resp.status_code == 403:
            # Key not found at keyLocation or doesn't match -- callers can
            # distinguish this from a transient outage.
            raise RuntimeError(
                f"IndexNow rejected the key (403): {resp.text} -- check that "
                f"the file at keyLocation exists and equals the submitted key."
            )
        if resp.status_code == 422:
            raise RuntimeError(
                f"IndexNow rejected the URLs (422): {resp.text}"
            )
        raise RuntimeError(
            f"IndexNow POST failed: {resp.status_code} {resp.text}"
        )


# -- One-time site setup ------------------------------------------------------


def write_key_file(
    directory: str | Path,
    key: str | None = None,
    *,
    length: int = 32,
    overwrite: bool = False,
) -> tuple[Path, str]:
    """Write the IndexNow verification file into ``directory``.

    The file is named ``{key}.txt`` and contains the key as UTF-8 plain
    text (per the spec). If ``key`` is None a fresh one is generated.

    Returns ``(path, key)`` so callers can persist the key (e.g. into
    ``INDEXNOW_KEY``) after a successful write.
    """
    target_dir = Path(directory)
    if not target_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {target_dir}")
    final_key = key or generate_key(length)
    validate_key(final_key)
    target = target_dir / f"{final_key}.txt"
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} already exists; pass overwrite=True to replace it."
        )
    target.write_text(final_key, encoding="utf-8")
    logger.info("IndexNow: wrote key file %s", target)
    return target, final_key


def verify_key_file(
    host: str,
    key: str | None = None,
    key_location: str | None = None,
) -> str:
    """Fetch the key file URL and confirm it serves the expected key.

    ``host`` may be a bare hostname or a full URL -- only the netloc is used.
    Returns the resolved key location on success. Raises ``RuntimeError`` if
    the file is missing, unreachable, or has the wrong contents.
    """
    key = key or get_secret("INDEXNOW_KEY")
    if not key:
        raise ValueError("INDEXNOW_KEY not set (env or vault).")
    validate_key(key)

    if key_location is None:
        netloc = urlparse(host).netloc or host
        key_location = f"https://{netloc}/{key}.txt"

    resp = requests.get(key_location, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(
            f"IndexNow key file not reachable: HTTP {resp.status_code} at {key_location}"
        )
    body = resp.text.strip()
    if body != key:
        raise RuntimeError(
            f"IndexNow key file content mismatch at {key_location}: "
            f"expected {key!r}, got {body!r}"
        )
    logger.info("IndexNow: verified %s", key_location)
    return key_location
