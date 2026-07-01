"""Amazon Bedrock foundation-model inspection and access helpers.

Thin boto3 wrapper around the Bedrock control-plane (``bedrock``) client.
``list-models`` needs ``bedrock:ListFoundationModels``; enabling a model needs
``bedrock:ListFoundationModelAgreementOffers`` +
``bedrock:CreateFoundationModelAgreement``.

Bedrock has no per-model "deployment" object for on-demand use: once the
account has an agreement for a foundation model in a region, callers invoke it
directly by model id (e.g. ``anthropic.claude-3-5-sonnet-20240620-v1:0``). Some
providers (notably Anthropic) also require a use-case form to be submitted in
the console before an agreement can be created; that surfaces here as a clean
AWS ValidationException rather than being auto-filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3


@dataclass
class FoundationModel:
    model_id: str
    provider_name: str | None
    model_name: str | None
    input_modalities: list[str]
    output_modalities: list[str]
    response_streaming: bool | None
    raw: dict[str, Any]


@dataclass
class ModelAgreementResult:
    model_id: str
    status: str
    offer_token: str | None
    raw: dict[str, Any]


def _client(profile: str | None, region: str | None) -> Any:
    session = boto3.Session(
        profile_name=profile if profile and profile != "default" else None,
        region_name=region,
    )
    return session.client("bedrock")


def _parse_model(item: dict[str, Any]) -> FoundationModel:
    return FoundationModel(
        model_id=item.get("modelId", ""),
        provider_name=item.get("providerName"),
        model_name=item.get("modelName"),
        input_modalities=list(item.get("inputModalities", [])),
        output_modalities=list(item.get("outputModalities", [])),
        response_streaming=item.get("responseStreamingSupported"),
        raw=item,
    )


def list_foundation_models(
    *,
    profile: str | None = None,
    region: str | None = None,
    by_provider: str | None = None,
) -> list[FoundationModel]:
    """List Bedrock foundation models available in the region."""
    client = _client(profile, region)
    kwargs: dict[str, Any] = {}
    if by_provider:
        kwargs["byProvider"] = by_provider
    resp = client.list_foundation_models(**kwargs)
    return [_parse_model(m) for m in resp.get("modelSummaries", [])]


def list_agreement_offers(
    *,
    model_id: str,
    profile: str | None = None,
    region: str | None = None,
) -> list[dict[str, Any]]:
    """Return the agreement offers available for a foundation model."""
    client = _client(profile, region)
    resp = client.list_foundation_model_agreement_offers(modelId=model_id)
    return list(resp.get("offers", []))


def create_agreement(
    *,
    model_id: str,
    offer_token: str,
    profile: str | None = None,
    region: str | None = None,
) -> ModelAgreementResult:
    """Accept an offer to enable on-demand access to a foundation model."""
    client = _client(profile, region)
    resp = client.create_foundation_model_agreement(
        modelId=model_id, offerToken=offer_token
    )
    return ModelAgreementResult(
        model_id=resp.get("modelId", model_id),
        status="agreement-created",
        offer_token=offer_token,
        raw=resp,
    )
