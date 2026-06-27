"""Unit tests for the Zitadel client (no network)."""

from unittest.mock import MagicMock, patch

import pytest

from granny.zitadel import ZitadelClient, ZitadelError


def _client() -> ZitadelClient:
    return ZitadelClient("https://id.example.test", "tok")


class TestFromEnvironment:
    def test_pat_used_directly(self):
        with patch.dict(
            "os.environ",
            {
                "ZITADEL_DOMAIN": "https://id.example.test",
                "ZITADEL_API_TOKEN": "pat-123",
            },
            clear=True,
        ):
            c = ZitadelClient.from_environment()
            assert c._token == "pat-123"
            assert c.base_url == "https://id.example.test"

    def test_missing_domain_raises(self):
        with patch.dict("os.environ", {"ZITADEL_API_TOKEN": "x"}, clear=True):
            with pytest.raises(ZitadelError):
                ZitadelClient.from_environment()

    def test_missing_credential_raises(self):
        with patch.dict(
            "os.environ", {"ZITADEL_DOMAIN": "https://id.example.test"}, clear=True
        ):
            with pytest.raises(ZitadelError):
                ZitadelClient.from_environment()


class TestRequest:
    def test_error_status_raises(self):
        c = _client()
        with patch.object(c._session, "request") as req:
            resp = MagicMock(status_code=403, text="Forbidden", content=b"Forbidden")
            req.return_value = resp
            with pytest.raises(ZitadelError) as exc:
                c.request("GET", "/management/v1/projects")
            assert exc.value.status == 403

    def test_path_normalised_and_json_returned(self):
        c = _client()
        with patch.object(c._session, "request") as req:
            resp = MagicMock(status_code=200, content=b"{}")
            resp.json.return_value = {"ok": True}
            req.return_value = resp
            assert c.request("GET", "management/v1/projects") == {"ok": True}
            assert (
                req.call_args.kwargs["url"]
                == "https://id.example.test/management/v1/projects"
            )


class TestProjectsAndApps:
    def test_ensure_project_reuses_existing(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"result": [{"id": "42", "name": "platform"}]}
            assert c.ensure_project("platform") == "42"
            # only the search call, no create
            assert req.call_count == 1

    def test_create_oidc_app_body_shape(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"appId": "a1", "clientId": "cid", "clientSecret": "sec"}
            out = c.create_oidc_app("p1", "headscale", ["https://h/oidc/callback"])
            body = req.call_args.kwargs["body"]
            assert body["grantTypes"] == [
                "OIDC_GRANT_TYPE_AUTHORIZATION_CODE",
                "OIDC_GRANT_TYPE_REFRESH_TOKEN",
            ]
            assert body["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_BASIC"
            assert out["clientSecret"] == "sec"

    def test_enforce_mfa_sets_force_and_factors(self):
        c = _client()
        calls: list[tuple[str, str]] = []

        def fake_request(method, path, *, body=None, params=None):
            calls.append((method, path))
            if method == "GET":
                return {"policy": {}}
            return {}

        with patch.object(c, "request", side_effect=fake_request):
            res = c.enforce_mfa()
        assert res["forceMfa"] is True
        assert any(p.endswith("/second_factors") for _, p in calls)
        assert any(p.endswith("/multi_factors") for _, p in calls)


class TestUsers:
    def test_create_user_idempotent(self):
        c = _client()
        with patch.object(
            c, "find_user", return_value={"id": "9", "userName": "alice"}
        ):
            with patch.object(c, "request") as req:
                out = c.create_user("alice", email="a@x.test")
        assert out["id"] == "9"
        req.assert_not_called()  # existing user -> no create

    def test_create_user_body_shape(self):
        c = _client()
        with patch.object(c, "find_user", return_value=None):
            with patch.object(c, "request") as req:
                req.return_value = {"userId": "12"}
                c.create_user(
                    "bob", email="b@x.test", password="pw", email_verified=True
                )
                body = req.call_args.kwargs["body"]
        assert body["userName"] == "bob"
        assert body["email"] == {"email": "b@x.test", "isEmailVerified": True}
        assert body["password"] == "pw"

    def test_set_user_password_path(self):
        c = _client()
        with patch.object(c, "request") as req:
            c.set_user_password("42", "newpw")
            assert req.call_args.args == ("POST", "/management/v1/users/42/password")
            assert req.call_args.kwargs["body"]["newPassword"]["password"] == "newpw"


class TestRolesAndGrants:
    def test_ensure_role_idempotent(self):
        c = _client()
        with patch.object(c, "list_roles", return_value=[{"key": "admin"}]):
            with patch.object(c, "request") as req:
                c.ensure_role("p1", "admin")
        req.assert_not_called()

    def test_grant_user_role_creates_when_absent(self):
        c = _client()
        with patch.object(c, "find_user_grant", return_value=None):
            with patch.object(c, "request") as req:
                req.return_value = {"grantId": "g1"}
                c.grant_user_role("p1", "u1", ["admin"])
                assert req.call_args.args == ("POST", "/management/v1/users/u1/grants")
                assert req.call_args.kwargs["body"] == {
                    "projectId": "p1",
                    "roleKeys": ["admin"],
                }

    def test_rotate_app_secret_endpoint(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"clientSecret": "new"}
            out = c.rotate_app_secret("p1", "a1")
            assert "_generate_client_secret" in req.call_args.args[1]
            assert out["clientSecret"] == "new"


class TestPATs:
    def test_create_pat_with_expiration(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"tokenId": "t1", "token": "pat-xyz"}
            out = c.create_pat("u1", expiration_date="2027-06-27T00:00:00Z")
            assert req.call_args.args == ("POST", "/management/v1/users/u1/pats")
            assert req.call_args.kwargs["body"] == {
                "expirationDate": "2027-06-27T00:00:00Z"
            }
            assert out["token"] == "pat-xyz"

    def test_create_pat_without_expiration_sends_empty_body(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"tokenId": "t1", "token": "p"}
            c.create_pat("u1")
            assert req.call_args.kwargs["body"] == {}

    def test_delete_pat_path(self):
        c = _client()
        with patch.object(c, "request") as req:
            c.delete_pat("u1", "t1")
            assert req.call_args.args == ("DELETE", "/management/v1/users/u1/pats/t1")

    def test_list_pats_returns_result(self):
        c = _client()
        with patch.object(c, "request") as req:
            req.return_value = {"result": [{"id": "t1"}]}
            assert c.list_pats("u1") == [{"id": "t1"}]
