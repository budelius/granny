"""Unit tests for Amazon Bedrock helpers (no network)."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from granny.aws import bedrock as bedrock_mod
from granny.cli.aws import aws

_SUMMARY = {
    "modelId": "anthropic.claude-sonnet-4-20250514-v1:0",
    "providerName": "Anthropic",
    "modelName": "Claude Sonnet 4",
    "inputModalities": ["TEXT", "IMAGE"],
    "outputModalities": ["TEXT"],
    "responseStreamingSupported": True,
    "inferenceTypesSupported": ["ON_DEMAND"],
    "modelLifecycle": {"status": "ACTIVE"},
}


class FakeBedrockClient:
    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        self.list_kwargs = kwargs
        return {"modelSummaries": [_SUMMARY]}

    def get_foundation_model(self, modelIdentifier: str) -> dict[str, Any]:
        self.got = modelIdentifier
        return {"modelDetails": _SUMMARY}


def test_parse_model_reads_lifecycle_and_inference_types() -> None:
    fm = bedrock_mod._parse_model(_SUMMARY)
    assert fm.model_id == "anthropic.claude-sonnet-4-20250514-v1:0"
    assert fm.provider_name == "Anthropic"
    assert fm.lifecycle == "ACTIVE"
    assert fm.inference_types == ["ON_DEMAND"]


def test_list_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBedrockClient()
    monkeypatch.setattr(bedrock_mod, "_client", lambda profile, region: fake)

    models = bedrock_mod.list_foundation_models(by_provider="Anthropic")
    assert fake.list_kwargs == {"byProvider": "Anthropic"}
    assert models[0].provider_name == "Anthropic"

    fm = bedrock_mod.get_foundation_model(model_id="anthropic.claude-sonnet-4-20250514-v1:0")
    assert fake.got == "anthropic.claude-sonnet-4-20250514-v1:0"
    assert fm.lifecycle == "ACTIVE"


def test_check_access_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bedrock_mod, "_client", lambda profile, region: FakeBedrockClient())
    result = CliRunner().invoke(
        aws,
        ["bedrock", "check-access", "--model-id",
         "anthropic.claude-sonnet-4-20250514-v1:0", "--json-output"],
    )
    assert result.exit_code == 0, result.output
    assert '"on_demand": true' in result.output
    assert '"route_model": "bedrock/anthropic.claude-sonnet-4-20250514-v1:0"' in result.output
