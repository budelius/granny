"""Search-engine indexing notifications.

Two client classes for telling search engines about new or updated URLs:

* :class:`IndexNowClient` -- the IndexNow protocol, supported by Bing,
  Yandex, Seznam.cz, Naver, Yep, and ``api.indexnow.org`` (which fans the
  submission out to every participating engine).
* :class:`GoogleIndexingClient` -- Google's Indexing API (requires a
  Search Console-verified service account; officially intended for
  ``JobPosting`` / ``BroadcastEvent`` structured data).

``GoogleIndexingClient`` is imported lazily so this module loads cleanly
when the ``[seo]`` extra (which pulls in ``google-auth``) isn't installed.
"""

from granny.indexing.indexnow import (
    INDEXNOW_ENDPOINTS,
    IndexNowClient,
    generate_key,
    validate_key,
    verify_key_file,
    write_key_file,
)

__all__ = [
    "INDEXNOW_ENDPOINTS",
    "GoogleIndexingClient",
    "IndexNowClient",
    "generate_key",
    "validate_key",
    "verify_key_file",
    "write_key_file",
]


def __getattr__(name: str):
    if name == "GoogleIndexingClient":
        from granny.indexing.google import GoogleIndexingClient

        return GoogleIndexingClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
