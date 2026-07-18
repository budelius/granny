"""Unit tests for the Authentik client (no network)."""

from unittest.mock import patch

import pytest

from granny.authentik import AuthentikClient, AuthentikError


def _client() -> AuthentikClient:
    return AuthentikClient("https://auth.example.test", "tok")


class TestApplications:
    def test_delete_application_deletes_slug_detail_endpoint(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.side_effect = [{"slug": "app", "name": "My App"}, None]

            assert c.delete_application("app", expected_name="My App") is True

            assert req.call_args_list[0].args == ("GET", "/api/v3/core/applications/app/")
            assert req.call_args_list[1].args == ("DELETE", "/api/v3/core/applications/app/")

    def test_delete_application_noops_when_absent(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.side_effect = AuthentikError(404, "missing")

            assert c.delete_application("app", expected_name="My App") is False

            assert req.call_count == 1

    def test_delete_application_refuses_name_mismatch(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"slug": "app", "name": "Other App"}

            with pytest.raises(AuthentikError) as exc:
                c.delete_application("app", expected_name="My App")

            assert exc.value.status == 409
            assert req.call_count == 1
