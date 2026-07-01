"""Unit tests for Amazon Bedrock helpers (no network)."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from granny.aws import bedrock as bedrock_mod
from granny.cli.aws import aws


class FakeBedrockClient:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}

    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs = kwargs
        return {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-3-5-sonnet-20240620-v1:0",
                    "providerName": "Anthropic",
                    "modelName": "Claude 3.5 Sonnet",
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"],
                    "responseStreamingSupported": True,
                }
            ]
        }

    def list_foundation_model_agreement_offers(self, modelId: str) -> dict[str, Any]:
        return {"offers": [{"offerToken": "tok-123", "offerId": "o1"}]}

    def create_foundation_model_agreement(self, modelId: str, offerToken: str) -> dict[str, Any]:
        self.created = {"modelId": modelId, "offerToken": offerToken}
        return {"modelId": modelId}


def test_parse_model() -> None:
    fm = bedrock_mod._parse_model(
        {
            "modelId": "meta.llama3-70b-instruct-v1:0",
            "providerName": "Meta",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "responseStreamingSupported": False,
        }
    )
    assert fm.model_id == "meta.llama3-70b-instruct-v1:0"
    assert fm.provider_name == "Meta"
    assert fm.response_streaming is False


def test_list_and_create_agreement(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBedrockClient()
    monkeypatch.setattr(bedrock_mod, "_client", lambda profile, region: fake)

    models = bedrock_mod.list_foundation_models(by_provider="Anthropic")
    assert fake.list_kwargs == {"byProvider": "Anthropic"}
    assert models[0].provider_name == "Anthropic"

    offers = bedrock_mod.list_agreement_offers(model_id="anthropic.x")
    assert offers[0]["offerToken"] == "tok-123"

    result = bedrock_mod.create_agreement(model_id="anthropic.x", offer_token="tok-123")
    assert result.status == "agreement-created"
    assert fake.created == {"modelId": "anthropic.x", "offerToken": "tok-123"}


def test_enable_model_dry_run_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBedrockClient()
    monkeypatch.setattr(bedrock_mod, "_client", lambda profile, region: fake)
    result = CliRunner().invoke(
        aws,
        ["bedrock", "enable-model", "--model-id", "anthropic.x", "--dry-run", "--json-output"],
    )
    assert result.exit_code == 0, result.output
    assert '"would_create_agreement": "anthropic.x"' in result.output
    assert fake.created == {}  # dry-run never creates
