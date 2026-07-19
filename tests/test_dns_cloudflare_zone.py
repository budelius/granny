"""Unit tests for Cloudflare zone creation (no network)."""

from unittest.mock import MagicMock

import pytest

from granny.dns.cloudflare import CloudflareDNSProvider


def _provider_with_responses(responses: list[tuple[int, dict]]) -> CloudflareDNSProvider:
    """Build a provider whose session replays canned (status, body) responses."""
    provider = CloudflareDNSProvider(api_token="test-token")
    replies = []
    for status, body in responses:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = str(body)
        replies.append(resp)
    session = MagicMock()
    session.get.side_effect = session.post.side_effect = iter(replies)
    provider.session = session
    return provider


class TestCreateZone:
    def test_creates_zone_and_returns_nameservers(self):
        provider = _provider_with_responses(
            [
                (200, {"success": True, "result": []}),  # name lookup: absent
                (200, {"success": True, "result": [{"id": "acct1"}]}),  # /accounts
                (
                    200,
                    {
                        "success": True,
                        "result": {
                            "id": "zone1",
                            "name": "example.org",
                            "status": "pending",
                            "name_servers": ["a.ns.cloudflare.com", "b.ns.cloudflare.com"],
                        },
                    },
                ),
            ]
        )
        meta = provider.create_zone("example.org")
        assert meta == {
            "id": "zone1",
            "name": "example.org",
            "status": "pending",
            "nameservers": ["a.ns.cloudflare.com", "b.ns.cloudflare.com"],
        }
        payload = provider.session.post.call_args.kwargs["json"]
        assert payload == {
            "name": "example.org",
            "account": {"id": "acct1"},
            "type": "full",
        }

    def test_idempotent_when_zone_exists(self):
        provider = _provider_with_responses(
            [
                (
                    200,
                    {
                        "success": True,
                        "result": [
                            {
                                "id": "zone1",
                                "name": "example.org",
                                "status": "active",
                                "name_servers": ["a.ns.cloudflare.com"],
                            }
                        ],
                    },
                ),
            ]
        )
        meta = provider.create_zone("example.org")
        assert meta["id"] == "zone1"
        assert meta["status"] == "active"
        provider.session.post.assert_not_called()

    def test_account_id_falls_back_to_zone_listing(self):
        provider = _provider_with_responses(
            [
                (200, {"success": True, "result": []}),  # name lookup: absent
                (403, {"success": False, "errors": [{"message": "forbidden"}]}),  # /accounts
                (
                    200,
                    {"success": True, "result": [{"id": "z0", "account": {"id": "acct2"}}]},
                ),  # /zones fallback
                (
                    200,
                    {
                        "success": True,
                        "result": {"id": "zone2", "name": "example.org", "status": "pending"},
                    },
                ),
            ]
        )
        meta = provider.create_zone("example.org")
        assert meta["id"] == "zone2"
        assert provider.session.post.call_args.kwargs["json"]["account"] == {"id": "acct2"}

    def test_missing_permission_raises_runtime_error(self):
        provider = _provider_with_responses(
            [
                (200, {"success": True, "result": []}),  # name lookup: absent
                (200, {"success": True, "result": [{"id": "acct1"}]}),  # /accounts
                (
                    403,
                    {
                        "success": False,
                        "errors": [
                            {
                                "message": 'Requires permission "com.cloudflare.api.account.zone.create"'
                            }
                        ],
                    },
                ),
            ]
        )
        with pytest.raises(RuntimeError, match="zone.create"):
            provider.create_zone("example.org")
