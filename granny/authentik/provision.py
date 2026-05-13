"""Per-stage stoz3n-dash app provisioning in Authentik.

The :data:`STOZ3N_DASH_STAGES` map defines the slug + redirect_uri + launch
URL for each environment. Keep this synchronised with the canonical doc at
``stoz3n-agent-dash/docs/authentik-sso.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from granny.authentik.client import AuthentikClient

STOZ3N_DASH_STAGES: dict[str, dict[str, str]] = {
    "development": {
        "slug": "stoz3n-dash-dev",
        "name": "Stoz3n Dashboard (dev)",
        "redirect_uri": "http://localhost:8000/api/auth/oidc/callback",
        "launch_url": "http://localhost:5173",
    },
    "staging": {
        "slug": "stoz3n-dash-staging",
        "name": "Stoz3n Dashboard (staging)",
        "redirect_uri": "https://facts-dev.stozn.ai/api/auth/oidc/callback",
        "launch_url": "https://dash-dev.stozn.ai",
    },
    "production": {
        "slug": "stoz3n-dash",
        "name": "Stoz3n Dashboard",
        "redirect_uri": "https://facts.stozn.ai/api/auth/oidc/callback",
        "launch_url": "https://dash.stozn.ai",
    },
}

ADMIN_GROUP_NAME = "dash_admins"
AUTHORIZATION_FLOW_SLUG = "default-provider-authorization-explicit-consent"
INVALIDATION_FLOW_SLUG = "default-provider-invalidation-flow"
OIDC_SCOPES: tuple[str, ...] = ("openid", "profile", "email")


@dataclass
class ProvisionResult:
    """Outcome of :func:`provision_stoz3n_dash`."""

    stage: str
    base_url: str
    issuer: str
    client_id: str
    client_secret: str  # empty string if Authentik didn't echo it back
    redirect_uri: str
    admin_group: str
    scopes: str
    provider_pk: str
    application_slug: str
    group_pk: str
    created_provider: bool
    created_application: bool
    created_binding: bool
    created_group: bool

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.base_url}/application/o/authorize/"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/application/o/token/"

    @property
    def userinfo_endpoint(self) -> str:
        return f"{self.base_url}/application/o/userinfo/"

    def chat_agent_oidc_block(self) -> dict[str, dict[str, str]]:
        """JSON snippet to paste into the chat-agent's encrypted config."""
        return {
            "oidc": {
                "issuer": self.issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "userinfo_endpoint": self.userinfo_endpoint,
                "client_id": self.client_id,
                "client_secret": self.client_secret
                or "<rerun with --rotate or run granny authentik rotate-secret>",
                "redirect_uri": self.redirect_uri,
                "admin_group": self.admin_group,
                "scopes": self.scopes,
            }
        }

    def dashboard_oidc_block(self) -> dict[str, dict[str, str]]:
        """JSON snippet to paste into the dashboard repo's encrypted config."""
        return {
            "oidc": {
                "client_secret": self.client_secret
                or "<rerun with --rotate or run granny authentik rotate-secret>"
            }
        }


def provision_stoz3n_dash(
    client: AuthentikClient,
    *,
    stage: str,
    rotate_secret: bool = False,
) -> ProvisionResult:
    """End-to-end idempotent provisioning of one stoz3n-dashboard stage.

    Steps:

    1. Resolve authorization flow + signing key + OIDC scope mappings.
    2. Ensure the ``dash_admins`` group exists.
    3. Create or PATCH the OAuth2 provider.
    4. Create or PATCH the application.
    5. Create the application→group policy binding if absent.

    Re-running is safe; nothing is destroyed. ``rotate_secret=True`` forces
    a new ``client_secret`` to be minted on an existing provider.
    """
    if stage not in STOZ3N_DASH_STAGES:
        raise ValueError(
            f"Unknown stage {stage!r}; choose one of {sorted(STOZ3N_DASH_STAGES)}"
        )
    cfg = STOZ3N_DASH_STAGES[stage]
    provider_name = cfg["slug"]
    app_slug = cfg["slug"]

    # 1. Foreign keys
    authorization_flow = client.find_flow_pk(AUTHORIZATION_FLOW_SLUG)
    invalidation_flow = client.find_flow_pk(INVALIDATION_FLOW_SLUG)
    signing_key = client.find_signing_key_pk()
    scope_mappings = client.find_scope_mappings(OIDC_SCOPES)

    # 2. Admin group
    existing_group = client.find_group_by_name(ADMIN_GROUP_NAME)
    created_group = existing_group is None
    group = existing_group or client.ensure_group(ADMIN_GROUP_NAME)

    # 3. Provider — three reconciliation paths:
    #   a. App+provider already exist under our slug → adopt them. We honor
    #      the existing provider's pk so its client_id stays stable; only
    #      rename + reconfigure fields. This is the production path where
    #      a pre-existing app may already be live with consumers.
    #   b. Provider exists by name (our convention) but no app → PATCH it
    #      in place.
    #   c. Nothing exists → POST a fresh provider.
    existing_app_for_adoption = client.find_application_by_slug(app_slug)
    adopt_pk: int | str | None = None
    if existing_app_for_adoption is not None:
        adopt_pk = existing_app_for_adoption.get("provider")
    existing_provider = (
        client.find_provider_by_pk(adopt_pk) if adopt_pk is not None else None
    ) or client.find_provider_by_name(provider_name)

    provider_body: dict[str, object] = {
        "name": provider_name,
        "authorization_flow": authorization_flow,
        "invalidation_flow": invalidation_flow,
        "client_type": "confidential",
        "redirect_uris": [{"matching_mode": "strict", "url": cfg["redirect_uri"]}],
        "signing_key": signing_key,
        "property_mappings": scope_mappings,
        "sub_mode": "user_username",
        "include_claims_in_id_token": True,
        "access_token_validity": "minutes=10",
        "refresh_token_validity": "days=30",
    }
    if existing_provider:
        if rotate_secret:
            provider_body["client_secret"] = ""  # empty → Authentik regenerates
        provider = client.request(
            "PATCH",
            f"/api/v3/providers/oauth2/{existing_provider['pk']}/",
            body=provider_body,
        )
        created_provider = False
    else:
        provider = client.request("POST", "/api/v3/providers/oauth2/", body=provider_body)
        created_provider = True

    # 4. Application
    app_body = {
        "name": cfg["name"],
        "slug": app_slug,
        "provider": provider["pk"],
        "meta_launch_url": cfg["launch_url"],
        "policy_engine_mode": "any",
    }
    if existing_app_for_adoption:
        client.request("PATCH", f"/api/v3/core/applications/{app_slug}/", body=app_body)
        created_application = False
        target_pk = existing_app_for_adoption["pk"]
    else:
        new_app = client.request("POST", "/api/v3/core/applications/", body=app_body)
        created_application = True
        target_pk = new_app["pk"]

    # 5. Group → application policy binding
    bindings = client.request(
        "GET", "/api/v3/policies/bindings/", params={"target": target_pk}
    ).get("results", [])
    has_binding = any(b.get("group") == group["pk"] for b in bindings)
    if has_binding:
        created_binding = False
    else:
        client.request(
            "POST",
            "/api/v3/policies/bindings/",
            body={
                "target": target_pk,
                "group": group["pk"],
                "enabled": True,
                "order": 0,
                "negate": False,
                "timeout": 30,
            },
        )
        created_binding = True

    return ProvisionResult(
        stage=stage,
        base_url=client.base_url,
        issuer=f"{client.base_url}/application/o/{app_slug}",
        client_id=provider["client_id"],
        client_secret=provider.get("client_secret") or "",
        redirect_uri=cfg["redirect_uri"],
        admin_group=ADMIN_GROUP_NAME,
        scopes=" ".join(OIDC_SCOPES),
        provider_pk=str(provider["pk"]),
        application_slug=app_slug,
        group_pk=str(group["pk"]),
        created_provider=created_provider,
        created_application=created_application,
        created_binding=created_binding,
        created_group=created_group,
    )
