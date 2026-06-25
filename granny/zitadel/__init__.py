"""Zitadel management-API helpers for granny.

Thin wrapper around a self-hosted Zitadel (https://github.com/zitadel/zitadel)
OIDC server's REST management/admin APIs, mirroring the ``granny.authentik``
module. Authenticates a machine user via the JWT-profile grant using a
service-account key (the ``iam-admin.json`` Zitadel emits), so no long-lived
bearer needs to be stored.
"""

from __future__ import annotations

from granny.zitadel.client import ZitadelClient, ZitadelError

__all__ = ["ZitadelClient", "ZitadelError"]
