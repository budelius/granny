"""Amazon Bedrock foundation-model inspection helpers.

Thin boto3 wrapper around the Bedrock control-plane (``bedrock``) client.
``list-models`` / ``check-access`` need ``bedrock:ListFoundationModels`` /
``bedrock:GetFoundationModel``.

Bedrock has no per-model "deployment" object and **no public API to grant
on-demand foundation-model access** — that is done once in the console (Model
access). Once granted, callers invoke a model directly by id
(e.g. ``anthropic.claude-sonnet-4-20250514-v1:0``); the gateway routes it as
``bedrock/<model-id>``. These helpers therefore discover model ids and report a
model's availability/lifecycle so a caller can build a route; they do not (and
cannot) enable access. The only API-driven "deployment" is Provisioned
Throughput (``create_provisioned_model_throughput``), which is a paid,
committed capacity resource and out of scope here.
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
    inference_types: list[str]
    lifecycle: str | None
    raw: dict[str, Any]


def _client(profile: str | None, region: str | None) -> Any:
    session = boto3.Session(
        profile_name=profile if profile and profile != "default" else None,
        region_name=region,
    )
    return session.client("bedrock")


def _parse_model(item: dict[str, Any]) -> FoundationModel:
    lifecycle = item.get("modelLifecycle", {})
    return FoundationModel(
        model_id=item.get("modelId", ""),
        provider_name=item.get("providerName"),
        model_name=item.get("modelName"),
        input_modalities=list(item.get("inputModalities", [])),
        output_modalities=list(item.get("outputModalities", [])),
        response_streaming=item.get("responseStreamingSupported"),
        inference_types=list(item.get("inferenceTypesSupported", [])),
        lifecycle=lifecycle.get("status") if isinstance(lifecycle, dict) else None,
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


def get_foundation_model(
    *,
    model_id: str,
    profile: str | None = None,
    region: str | None = None,
) -> FoundationModel:
    """Fetch one foundation model's details (id, modalities, lifecycle).

    Reports the model's availability, not the account's access grant — Bedrock
    exposes no per-account on-demand access status via API.
    """
    client = _client(profile, region)
    resp = client.get_foundation_model(modelIdentifier=model_id)
    return _parse_model(resp.get("modelDetails", {}))
