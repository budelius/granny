"""CORS policy options on the Hetzner object-storage client.

``PUT`` is the entry that matters. An app handed a presigned URL sends the
bytes from the browser, so the hop is cross-origin, and a PUT is never a
CORS-simple request: the browser preflights it and, with no matching rule,
refuses to send the upload at all.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from granny.storage.hetzner import HetznerS3Client


@pytest.fixture()
def client(monkeypatch):
    """A HetznerS3Client with its constructor's credential + boto3 work stubbed."""
    monkeypatch.setattr(HetznerS3Client, "__init__", lambda self, region="nbg1": None)
    instance = HetznerS3Client()
    instance.client = MagicMock()
    return instance


def _rule(client) -> dict:
    return client.client.put_bucket_cors.call_args.kwargs["CORSConfiguration"][
        "CORSRules"
    ][0]


class TestSetCorsConfig:
    def test_defaults_stay_read_only(self, client):
        """An existing caller that asked for nothing gets what it always got."""
        client.set_cors_config("m3rp")
        rule = _rule(client)
        assert rule["AllowedMethods"] == ["GET", "HEAD"]
        assert rule["AllowedOrigins"] == ["*"]
        assert "ExposeHeaders" not in rule

    def test_put_can_be_requested_for_browser_uploads(self, client):
        client.set_cors_config(
            "m3rp",
            origins=["https://app.m3rp.ai"],
            methods=["GET", "PUT", "POST", "HEAD"],
        )
        rule = _rule(client)
        assert "PUT" in rule["AllowedMethods"]
        assert rule["AllowedOrigins"] == ["https://app.m3rp.ai"]

    def test_exposed_headers_are_only_sent_when_asked_for(self, client):
        client.set_cors_config("m3rp", expose_headers=["ETag"])
        assert _rule(client)["ExposeHeaders"] == ["ETag"]

    def test_max_age_is_configurable(self, client):
        client.set_cors_config("m3rp", max_age_seconds=60)
        assert _rule(client)["MaxAgeSeconds"] == 60


class TestGetCorsConfig:
    def test_returns_the_rules_when_a_policy_exists(self, client):
        client.client.get_bucket_cors.return_value = {"CORSRules": [{"ID": "r"}]}
        assert client.get_cors_config("m3rp") == [{"ID": "r"}]

    def test_no_policy_at_all_reads_as_none_not_as_empty(self, client):
        """None and [] are different answers: never configured vs allows nothing."""
        client.client.get_bucket_cors.side_effect = ClientError(
            {"Error": {"Code": "NoSuchCORSConfiguration"}}, "GetBucketCors"
        )
        assert client.get_cors_config("m3rp") is None

    def test_other_errors_are_not_swallowed(self, client):
        client.client.get_bucket_cors.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "GetBucketCors"
        )
        with pytest.raises(ClientError):
            client.get_cors_config("m3rp")
