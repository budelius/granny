"""Generic Authentik OAuth2 application provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field

from granny.authentik.client import AuthentikClient

DEFAULT_AUTHORIZATION_FLOW_SLUG = "default-provider-authorization-explicit-consent"
DEFAULT_INVALIDATION_FLOW_SLUG = "default-provider-invalidation-flow"
DEFAULT_OIDC_SCOPES: tuple[str, ...] = ("openid", "profile", "email")


@dataclass(frozen=True)
class OAuthAppSpec:
    """Desired state for an Authentik OAuth2 provider + application."""

    slug: str
    name: str
    redirect_uris: tuple[str, ...]
    launch_url: str | None = None
    group: str | None = None
    provider_name: str | None = None
    scopes: tuple[str, ...] = DEFAULT_OIDC_SCOPES
    authorization_flow_slug: str = DEFAULT_AUTHORIZATION_FLOW_SLUG
    invalidation_flow_slug: str = DEFAULT_INVALIDATION_FLOW_SLUG
    access_token_validity: str = "minutes=10"
    refresh_token_validity: str = "days=30"
    sub_mode: str = "user_username"
    include_claims_in_id_token: bool = True

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("slug must not be empty")
        if not self.name:
            raise ValueError("name must not be empty")
        if not self.redirect_uris:
            raise ValueError("at least one redirect URI is required")


@dataclass
class ProvisionResult:
    """Result of provisioning an Authentik OAuth2 application."""

    base_url: str
    issuer: str
    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]
    scopes: str
    provider_pk: str
    application_slug: str
    group: str | None
    group_pk: str | None
    created_provider: bool
    created_application: bool
    created_binding: bool
    created_group: bool
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.base_url}/application/o/authorize/"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/application/o/token/"

    @property
    def userinfo_endpoint(self) -> str:
        return f"{self.base_url}/application/o/userinfo/"

    def oidc_block(self) -> dict[str, str | list[str] | None]:
        """Return a copy-paste friendly OIDC configuration snippet."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "userinfo_endpoint": self.userinfo_endpoint,
            "client_id": self.client_id,
            "client_secret": self.client_secret or None,
            "redirect_uris": list(self.redirect_uris),
            "scopes": self.scopes,
            "admin_group": self.group,
        }


def provision_oauth_app(
    client: AuthentikClient,
    spec: OAuthAppSpec,
    *,
    rotate_secret: bool = False,
) -> ProvisionResult:
    """Idempotently provision an OAuth2 provider and application in Authentik."""
    provider_name = spec.provider_name or spec.slug
    authorization_flow = client.find_flow_pk(spec.authorization_flow_slug)
    invalidation_flow = client.find_flow_pk(spec.invalidation_flow_slug)
    signing_key = client.find_signing_key_pk()
    scope_mappings = client.find_scope_mappings(spec.scopes)

    group = None
    created_group = False
    if spec.group:
        existing_group = client.find_group_by_name(spec.group)
        created_group = existing_group is None
        group = existing_group or client.ensure_group(spec.group)

    existing_app = client.find_application_by_slug(spec.slug)
    adopt_pk: int | str | None = None
    if existing_app is not None:
        adopt_pk = existing_app.get("provider")
    existing_provider = (
        client.find_provider_by_pk(adopt_pk) if adopt_pk is not None else None
    ) or client.find_provider_by_name(provider_name)

    provider_body: dict[str, object] = {
        "name": provider_name,
        "authorization_flow": authorization_flow,
        "invalidation_flow": invalidation_flow,
        "client_type": "confidential",
        "redirect_uris": [
            {"matching_mode": "strict", "url": uri} for uri in spec.redirect_uris
        ],
        "signing_key": signing_key,
        "property_mappings": scope_mappings,
        "sub_mode": spec.sub_mode,
        "include_claims_in_id_token": spec.include_claims_in_id_token,
        "access_token_validity": spec.access_token_validity,
        "refresh_token_validity": spec.refresh_token_validity,
    }
    if existing_provider:
        if rotate_secret:
            provider_body["client_secret"] = ""
        provider = client.request(
            "PATCH",
            f"/api/v3/providers/oauth2/{existing_provider['pk']}/",
            body=provider_body,
        )
        created_provider = False
    else:
        provider = client.request("POST", "/api/v3/providers/oauth2/", body=provider_body)
        created_provider = True

    app_body: dict[str, object] = {
        "name": spec.name,
        "slug": spec.slug,
        "provider": provider["pk"],
        "policy_engine_mode": "any",
    }
    if spec.launch_url is not None:
        app_body["meta_launch_url"] = spec.launch_url
    if existing_app:
        client.request("PATCH", f"/api/v3/core/applications/{spec.slug}/", body=app_body)
        created_application = False
        target_pk = existing_app["pk"]
    else:
        new_app = client.request("POST", "/api/v3/core/applications/", body=app_body)
        created_application = True
        target_pk = new_app["pk"]

    created_binding = False
    if group is not None:
        bindings = client.request(
            "GET", "/api/v3/policies/bindings/", params={"target": target_pk}
        ).get("results", [])
        has_binding = any(binding.get("group") == group["pk"] for binding in bindings)
        if not has_binding:
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
        base_url=client.base_url,
        issuer=f"{client.base_url}/application/o/{spec.slug}",
        client_id=provider["client_id"],
        client_secret=provider.get("client_secret") or "",
        redirect_uris=spec.redirect_uris,
        scopes=" ".join(spec.scopes),
        provider_pk=str(provider["pk"]),
        application_slug=spec.slug,
        group=spec.group,
        group_pk=str(group["pk"]) if group is not None else None,
        created_provider=created_provider,
        created_application=created_application,
        created_binding=created_binding,
        created_group=created_group,
    )
