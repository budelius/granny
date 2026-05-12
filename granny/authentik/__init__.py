"""Authentik management — REST API wrapper + per-stage stoz3n-dash provisioning.

Powered by the ``AUTHENTIK_API_TOKEN`` secret (resolves via env var or
Vaultwarden under ``granny/infra/authentik-api-token``) and the
non-secret ``AUTHENTIK_URL`` (default ``https://auth.pseekoo.io``).

Public API::

    from granny.authentik import AuthentikClient, STOZ3N_DASH_STAGES, provision_stoz3n_dash

    client = AuthentikClient.from_environment()
    result = provision_stoz3n_dash(client, stage="development")
    print(result.client_id, result.client_secret)
"""

from granny.authentik.client import AuthentikClient, AuthentikError
from granny.authentik.provision import (
    ADMIN_GROUP_NAME,
    OIDC_SCOPES,
    STOZ3N_DASH_STAGES,
    ProvisionResult,
    provision_stoz3n_dash,
)

__all__ = [
    "ADMIN_GROUP_NAME",
    "AuthentikClient",
    "AuthentikError",
    "OIDC_SCOPES",
    "ProvisionResult",
    "STOZ3N_DASH_STAGES",
    "provision_stoz3n_dash",
]
