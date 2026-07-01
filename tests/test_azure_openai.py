"""Unit tests for Azure OpenAI deployment create/keys helpers (no network)."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from granny.azure import openai as openai_mod
from granny.cli.azure import azure


class TestCreateDeployment:
    def test_builds_put_url_and_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_put(url: str, body: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
            captured["url"] = url
            captured["body"] = body
            return {
                "id": "/subscriptions/s/resourceGroups/rg/providers"
                "/Microsoft.CognitiveServices/accounts/acct/deployments/gpt-4o",
                "name": "gpt-4o",
                "sku": {"name": "GlobalStandard", "capacity": 50},
                "properties": {
                    "model": {"name": "gpt-4o", "version": "2024-08-06"},
                    "provisioningState": "Succeeded",
                },
            }

        monkeypatch.setattr(openai_mod._client, "put", fake_put)

        dep = openai_mod.create_deployment(
            subscription="s",
            resource_group="rg",
            account_name="acct",
            deployment_name="gpt-4o",
            model_name="gpt-4o",
            model_version="2024-08-06",
            sku_name="GlobalStandard",
            sku_capacity=50,
        )

        assert "/accounts/acct/deployments/gpt-4o" in captured["url"]
        assert openai_mod._DEPLOY_API_VERSION in captured["url"]
        assert captured["body"]["sku"] == {"name": "GlobalStandard", "capacity": 50}
        assert captured["body"]["properties"]["model"] == {
            "format": "OpenAI",
            "name": "gpt-4o",
            "version": "2024-08-06",
        }
        assert dep.name == "gpt-4o"
        assert dep.model_version == "2024-08-06"
        assert dep.provisioning_state == "Succeeded"

    def test_omits_version_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            openai_mod._client,
            "put",
            lambda url, body, **_: captured.update(body=body) or {"name": "m", "properties": {}},
        )
        openai_mod.create_deployment(
            subscription="s", resource_group="rg", account_name="a",
            deployment_name="m", model_name="gpt-4o",
        )
        assert "version" not in captured["body"]["properties"]["model"]


class TestAccountKeys:
    def test_filters_to_key1_key2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            openai_mod._client,
            "post_json",
            lambda url, *a, **k: {"key1": "AAA", "key2": "BBB", "extra": "ignore"},
        )
        keys = openai_mod.get_account_keys("s", "rg", "acct")
        assert keys == {"key1": "AAA", "key2": "BBB"}


class TestDeploymentCreateCLI:
    def test_dry_run_json_does_not_call_azure(self) -> None:
        result = CliRunner().invoke(
            azure,
            [
                "openai", "deployment", "create",
                "--subscription", "s", "--resource-group", "rg", "--account", "acct",
                "--name", "gpt-4o", "--model", "gpt-4o", "--capacity", "50",
                "--dry-run", "--json-output",
            ],
        )
        assert result.exit_code == 0, result.output
        assert '"would_create": "gpt-4o"' in result.output
        assert '"capacity": 50' in result.output
