"""Fetch secrets with optional Vaultwarden support and env-var fallback.

Resolution order for each secret:

  1. Environment variable (from ``.env``, ``.deploy.env``, or shell) — explicit wins
  2. Vaultwarden vault via Locke — only if the optional ``[vault]`` extra is
     installed and a vault session is reachable

Without the ``[vault]`` extra, secrets are env-var only — the vault-related
helpers are no-ops that return ``None``. Install with vault support via::

    pip install "granny-devops[vault]"

Library usage::

    from granny.credentials import get_secret

    token = get_secret("BUNNY_API_KEY")

    # Or load all registered secrets into os.environ at once:
    from granny.credentials import load_secrets_into_env
    load_secrets_into_env()

Multi-customer Bunny API keys
-----------------------------
When operating several Bunny accounts locally (e.g. one per customer), use
``get_bunny_api_key(customer=...)``. The customer name is slugified and
resolved as:

  1. Vault: ``granny/infra/bunny-api-key-{slug}`` (if vault installed)
  2. Env var: ``BUNNY_API_KEY_{UPPER_SLUG}``

Passing ``customer=None`` resolves the default ``BUNNY_API_KEY``.
"""

import base64
import json
import logging
import os
import platform
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Optional dependency: Locke. Imported lazily inside helpers so that the
# module loads cleanly when the [vault] extra isn't installed.
try:
    import locke  # noqa: F401  -- presence check only

    _LOCKE_AVAILABLE = True
except ImportError:
    _LOCKE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Vault session cache -- persists access_token + symmetric keys across
# process invocations so we don't re-authenticate on every CLI call.
# ---------------------------------------------------------------------------

_SESSION_TTL = 3600  # 1 hour -- Vaultwarden tokens typically last 2h
_SESSION_FILE = "vault_session.json"


def _session_dir() -> Path:
    """Return the directory for the vault session cache file."""
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA", "")
        d = Path(base) / "granny" if base else Path.home() / ".granny"
    else:
        d = Path.home() / ".granny"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_vault_session(
    base_url: str,
    access_token: str,
    enc_key: bytes,
    mac_key: bytes,
) -> None:
    """Persist the vault session to disk for reuse by later processes."""
    try:
        payload = {
            "base_url": base_url,
            "access_token": access_token,
            "enc_key": base64.b64encode(enc_key).decode("ascii"),
            "mac_key": base64.b64encode(mac_key).decode("ascii"),
            "created_at": time.time(),
        }
        target = _session_dir() / _SESSION_FILE
        target.write_text(json.dumps(payload))
        logger.debug("Vault session cached to %s", target)
    except Exception as exc:
        logger.debug("Failed to save vault session: %s", exc)


def _load_vault_session():
    """Load a cached vault session if it exists and is not expired.

    Returns a VaultClient or None. Returns None unconditionally when the
    [vault] extra isn't installed.
    """
    if not _LOCKE_AVAILABLE:
        return None
    try:
        target = _session_dir() / _SESSION_FILE
        if not target.is_file():
            return None
        data = json.loads(target.read_text())
        age = time.time() - float(data["created_at"])
        if age < 0 or age >= _SESSION_TTL:
            logger.debug("Vault session expired (age=%.0fs)", age)
            target.unlink(missing_ok=True)
            return None

        from locke.vault import VaultClient

        client = VaultClient(
            base_url=data["base_url"],
            access_token=data["access_token"],
            enc_key=base64.b64decode(data["enc_key"]),
            mac_key=base64.b64decode(data["mac_key"]),
        )
        logger.debug("Restored vault session from cache (age=%.0fs)", age)
        return client
    except Exception as exc:
        logger.debug("Failed to load vault session: %s", exc)
        return None


def _invalidate_vault_session() -> None:
    """Delete the cached vault session file."""
    try:
        target = _session_dir() / _SESSION_FILE
        target.unlink(missing_ok=True)
    except Exception:
        pass


# Map env var name -> vault secret name (kebab-case).
# The full vault path is: {VAULT_FOLDER}/{secret_name}
VAULT_FOLDER = "granny/infra"

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
    # INWX (api.domrobot.com). INWX_SHARED_SECRET is the TOTP base32 secret;
    # only required when 2FA is enabled on the account.
    "INWX_USERNAME": "inwx-username",
    "INWX_PASSWORD": "inwx-password",
    "INWX_SHARED_SECRET": "inwx-shared-secret",
    # Container registries
    "DOCKER_HUB_USER": "docker-hub-user",
    "DOCKER_HUB_TOKEN": "docker-hub-token",
    # GitLab PyPI / Container Registry (for fetching private packages and pushing images)
    "GITLAB_PYPI_USER": "gitlab-pypi-user",
    "GITLAB_PYPI_PASSWORD": "gitlab-pypi-password",
    # Authentik admin API token for `granny authentik …` commands. Long-lived
    # superuser-issued token (Authentik UI → Directory → Tokens). Falls back
    # to AK_TOKEN env var in granny.authentik.AuthentikClient.from_environment.
    "AUTHENTIK_API_TOKEN": "authentik-api-token",
    # Zitadel OIDC server (`granny zitadel …`): an optional PAT and the
    # service-account key JSON used for JWT-profile machine auth. The IdP base
    # URL (ZITADEL_DOMAIN) is plain config, NOT a secret — set it via env /
    # .env / .deploy.env, so it is deliberately not mapped to the vault.
    "ZITADEL_API_TOKEN": "zitadel-api-token",
    "ZITADEL_SA_KEY": "zitadel-sa-key",
    # Elasticsearch / Kibana security API credentials for `granny elk …`.
    "ELASTICSEARCH_API_KEY": "elasticsearch-api-key",
    "ELASTICSEARCH_USERNAME": "elasticsearch-username",
    "ELASTICSEARCH_PASSWORD": "elasticsearch-password",
    # Stalwart mail server admin (Basic auth on JMAP / REST). The username is
    # not secret but is registered alongside the password so both can come from
    # the same vault item. Base URL defaults to https://mail.pseekoo.io in
    # StalwartClient.from_environment() and is overridable via STALWART_URL.
    "STALWART_ADMIN_USER": "stalwart-admin-user",
    "STALWART_ADMIN_PASSWORD": "stalwart-admin-password",
    # obachan FaaS control API (`granny knative` / `granny faas` commands).
    # OBACHAN_API_BASE_URL is not secret per-se but is vault-distributed so
    # the same URL configuration propagates to all developer machines without
    # manual .env updates (research OQ3).
    "OBACHAN_API_KEY": "obachan-api-key",  # vault: granny/infra/obachan-api-key
    "OBACHAN_API_BASE_URL": "obachan-api-base-url",  # vault: granny/infra/obachan-api-base-url
}

# Module-level cache so vault is only contacted once per process
_cache: dict[str, str | None] = {}
_vault_client = None
_vault_checked = False


def _get_vault_client():
    """Lazily create a Locke VaultClient.

    Tries a cached session first to avoid re-authenticating on every CLI
    invocation. Falls back to a fresh ``VaultClient.connect()`` and
    persists the new session for later processes.

    Returns a connected client or None if vault is unavailable (including
    when the [vault] extra isn't installed).
    """
    global _vault_client, _vault_checked
    if _vault_checked:
        return _vault_client
    _vault_checked = True

    if not _LOCKE_AVAILABLE:
        logger.debug("locke not installed — vault disabled (env-var only mode)")
        return None

    # 1. Try cached session (avoids hitting the identity endpoint)
    _vault_client = _load_vault_session()
    if _vault_client is not None:
        # Verify the token still works with a lightweight probe
        try:
            import httpx

            resp = httpx.get(
                f"{_vault_client._base_url}/api/sync",
                headers=_vault_client._headers,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Reusing cached vault session")
                return _vault_client
            else:
                logger.debug(
                    "Cached vault token expired (HTTP %s), re-authenticating",
                    resp.status_code,
                )
                _invalidate_vault_session()
                _vault_client = None
        except Exception as exc:
            logger.debug("Cached session probe failed: %s", exc)
            _invalidate_vault_session()
            _vault_client = None

    # 2. Fresh authentication
    try:
        from locke.vault import VaultClient

        _vault_client = VaultClient.connect()
        logger.info("Connected to Vaultwarden via Locke for secret resolution")
        # Persist session for subsequent processes
        _save_vault_session(
            _vault_client._base_url,
            _vault_client._headers["Authorization"].removeprefix("Bearer "),
            _vault_client._enc_key,
            _vault_client._mac_key,
        )
        return _vault_client
    except Exception as e:
        logger.debug("Vault unavailable: %s", e)
        return None


def _fetch_from_vault(env_var: str) -> str | None:
    """Fetch a single secret from Vaultwarden by its env var name."""
    secret_name = SECRET_MAP.get(env_var)
    if not secret_name:
        return None

    client = _get_vault_client()
    if not client:
        return None

    vault_path = f"{VAULT_FOLDER}/{secret_name}"
    try:
        return client.get_secret(vault_path)
    except Exception as e:
        logger.debug("Failed to fetch %s from vault: %s", vault_path, e)
        return None


def get_secret(env_var: str) -> str | None:
    """Get a secret by its environment variable name.

    Resolution order:
      1. Process cache (avoids repeated vault lookups)
      2. Environment variable / .env / .deploy.env (explicit wins)
      3. Vaultwarden vault via Locke (only if [vault] extra installed)

    Args:
        env_var: The environment variable name (e.g. "BUNNY_API_KEY").

    Returns:
        The secret value, or None if not found anywhere.
    """
    if env_var in _cache:
        return _cache[env_var]

    # Env var wins so a project-local .deploy.env can override vault.
    value = os.environ.get(env_var)
    if value:
        _cache[env_var] = value
        return value

    # Fall back to vault (only for registered secrets, only if locke installed)
    if env_var in SECRET_MAP:
        value = _fetch_from_vault(env_var)
        if value:
            _cache[env_var] = value
            return value

    return None


def _slugify_customer(customer: str) -> str:
    """Normalise a customer name for use in vault paths and env vars.

    Lower-cases, collapses non-alphanumerics to hyphens, strips ends.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", customer.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Invalid customer name: {customer!r}")
    return slug


def get_bunny_api_key(customer: str | None = None) -> str | None:
    """Get a Bunny CDN API key, optionally for a specific customer.

    Resolution order (for a given ``customer``):
      1. Process cache
      2. Env var: ``BUNNY_API_KEY_{UPPER_SLUG}`` (hyphens → underscores)
      3. Vault: ``granny/infra/bunny-api-key-{slug}`` (if [vault] installed)

    When ``customer`` is None, resolves the default ``BUNNY_API_KEY`` via
    :func:`get_secret`.

    Args:
        customer: Customer identifier, e.g. ``"acme"``. Case/spacing-insensitive.

    Returns:
        The API key, or None if not found.
    """
    if customer is None:
        return get_secret("BUNNY_API_KEY")

    slug = _slugify_customer(customer)
    env_var = "BUNNY_API_KEY_" + slug.replace("-", "_").upper()

    if env_var in _cache:
        return _cache[env_var]

    # Env var wins so a project-local .deploy.env can override vault.
    value = os.environ.get(env_var)
    if value:
        _cache[env_var] = value
        return value

    # Fall back to vault
    client = _get_vault_client()
    if client:
        vault_path = f"{VAULT_FOLDER}/bunny-api-key-{slug}"
        try:
            value = client.get_secret(vault_path)
            if value:
                _cache[env_var] = value
                return value
        except Exception as e:
            logger.debug("Failed to fetch %s from vault: %s", vault_path, e)

    return None


def list_bunny_customers() -> list[str]:
    """List known Bunny customer profiles.

    Sources (merged, deduplicated, sorted):
      * ``GRANNY_BUNNY_CUSTOMERS`` env var (comma-separated names)
      * Any ``BUNNY_API_KEY_{NAME}`` env vars currently set

    Note: the vault client does not expose a listing API, so vault-only
    customers must be registered via ``GRANNY_BUNNY_CUSTOMERS`` to show up.

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
    """Load secrets from vault into os.environ for all registered keys.

    Useful for scripts that already use os.environ.get() everywhere.
    Call this once at startup after load_dotenv().

    When the [vault] extra isn't installed, this only reports presence
    of pre-existing env vars without mutating os.environ.

    Args:
        keys: Specific env var names to load. Defaults to all SECRET_MAP keys.

    Returns:
        Dict mapping env var name to True (found) or False (missing).
    """
    if keys is None:
        keys = list(SECRET_MAP.keys())

    results = {}
    for key in keys:
        value = get_secret(key)
        if value:
            os.environ[key] = value
            results[key] = True
        else:
            results[key] = False
    return results
