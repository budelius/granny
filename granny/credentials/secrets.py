"""Fetch secrets from environment variables.

Resolution is env-var only: the value is read from the process environment
(populated by a project-local ``.env`` or ``.deploy.env`` via
``python-dotenv``, or by the shell). Vault integration has been removed.

Library usage::

    from granny.credentials import get_secret

    token = get_secret("BUNNY_API_KEY")

    # Or load all registered secrets (no-op unless they're already set):
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()

Multi-customer Bunny API keys
-----------------------------
When operating several Bunny accounts locally, use
``get_bunny_api_key(customer=...)``. The customer name is slugified and
resolved from ``BUNNY_API_KEY_{UPPER_SLUG}``. Passing ``customer=None``
resolves the default ``BUNNY_API_KEY``.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# Registered secret names. Values are resolved from os.environ only.
SECRET_MAP: dict[str, str] = {
    "CLOUDFLARE_API_TOKEN": "cloudflare-api-token",
    "DESEC_API_TOKEN": "desec-api-token",
    "HETZNER_S3_ACCESS_KEY": "hetzner-s3-access-key",
    "HETZNER_S3_SECRET_KEY": "hetzner-s3-secret-key",
    "HETZNER_DNS_API_TOKEN": "hetzner-dns-api-token",
    "BUNNY_API_KEY": "bunny-api-key",
    "SCW_ACCESS_KEY": "scw-access-key",
    "SCW_SECRET_KEY": "scw-secret-key",
    "SCW_DEFAULT_PROJECT_ID": "scw-default-project-id",
    "MAILJET_API_KEY": "mailjet-api-key",
    "MAILJET_SECRET_KEY": "mailjet-secret-key",
    "CLOUDNS_AUTH_ID": "cloudns-auth-id",
    "CLOUDNS_AUTH_PASSWORD": "cloudns-auth-password",
    "CLOUDNS_SUB_AUTH_ID": "cloudns-sub-auth-id",
    "CLOUDNS_SUB_AUTH_USER": "cloudns-sub-auth-user",
}


def get_secret(env_var: str) -> str | None:
    """Get a secret by its environment variable name.

    Args:
        env_var: The environment variable name (e.g. "BUNNY_API_KEY").

    Returns:
        The value from os.environ, or None if unset/empty.
    """
    value = os.environ.get(env_var)
    return value or None


def _slugify_customer(customer: str) -> str:
    """Normalise a customer name for use in env vars.

    Lower-cases, collapses non-alphanumerics to hyphens, strips ends.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", customer.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Invalid customer name: {customer!r}")
    return slug


def get_bunny_api_key(customer: str | None = None) -> str | None:
    """Get a Bunny CDN API key, optionally for a specific customer.

    Resolution: ``BUNNY_API_KEY_{UPPER_SLUG}`` env var, or
    ``BUNNY_API_KEY`` when ``customer`` is None.

    Args:
        customer: Customer identifier, e.g. ``"acme"``. Case/spacing-insensitive.

    Returns:
        The API key, or None if not found.
    """
    if customer is None:
        return get_secret("BUNNY_API_KEY")

    slug = _slugify_customer(customer)
    env_var = "BUNNY_API_KEY_" + slug.replace("-", "_").upper()
    return get_secret(env_var)


def list_bunny_customers() -> list[str]:
    """List known Bunny customer profiles.

    Sources (merged, deduplicated, sorted):
      * ``GRANNY_BUNNY_CUSTOMERS`` env var (comma-separated names)
      * Any ``BUNNY_API_KEY_{NAME}`` env vars currently set

    Returns:
        Sorted list of slugified customer names.
    """
    customers: set[str] = set()

    registry = os.environ.get("GRANNY_BUNNY_CUSTOMERS", "")
    for name in registry.split(","):
        name = name.strip()
        if name:
            try:
                customers.add(_slugify_customer(name))
            except ValueError:
                continue

    for key in os.environ:
        if key.startswith("BUNNY_API_KEY_") and key != "BUNNY_API_KEY_":
            suffix = key[len("BUNNY_API_KEY_") :]
            customers.add(suffix.lower().replace("_", "-"))

    return sorted(customers)


def load_secrets_into_env(keys: list[str] | None = None) -> dict[str, bool]:
    """Report which registered secrets are present in os.environ.

    Kept for backwards compatibility with older call sites. Since secrets
    are env-var only, this no longer mutates os.environ -- it simply
    reports presence.

    Args:
        keys: Specific env var names to check. Defaults to all SECRET_MAP keys.

    Returns:
        Dict mapping env var name to True (set) or False (missing).
    """
    if keys is None:
        keys = list(SECRET_MAP.keys())
    return {key: bool(os.environ.get(key)) for key in keys}
