"""Unit tests for KnativeClient — fully mocked, no live infra required.

Mirrors the test_zitadel.py class-based pattern: one factory function,
one class per public method group, patch.object on the specific session
method (post/get/patch/delete) rather than session.request.

Gitleaks note: all test fixtures use "test-fake-key-value" (low entropy,
no "obf_" prefix) — see 18-03-SUMMARY deviation #3.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from granny.knative import KnativeClient


def _client() -> KnativeClient:
    return KnativeClient(
        base_url="https://faas-api.test.example",
        api_key="test-fake-key-value",
    )


# ---------------------------------------------------------------------------
# TestConstructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_succeeds_with_valid_params(self) -> None:
        c = _client()
        assert c.base_url == "https://faas-api.test.example"

    def test_trailing_slash_stripped_from_base_url(self) -> None:
        c = KnativeClient(
            base_url="https://faas-api.test.example/",
            api_key="test-fake-key-value",
        )
        assert c.base_url == "https://faas-api.test.example"

    def test_x_api_key_header_set(self) -> None:
        c = _client()
        assert c._session.headers["X-API-Key"] == "test-fake-key-value"

    def test_content_type_header_set(self) -> None:
        c = _client()
        assert c._session.headers["Content-Type"] == "application/json"

    def test_empty_base_url_raises_value_error(self) -> None:
        with patch("granny.knative.client.get_secret", return_value=None):
            with pytest.raises(ValueError, match="OBACHAN_API_BASE_URL"):
                KnativeClient(base_url="", api_key="test-fake-key-value")

    def test_empty_api_key_raises_value_error(self) -> None:
        with patch("granny.knative.client.get_secret", return_value=None):
            with pytest.raises(ValueError, match="OBACHAN_API_KEY"):
                KnativeClient(
                    base_url="https://faas-api.test.example",
                    api_key="",
                )


# ---------------------------------------------------------------------------
# TestValidateName
# ---------------------------------------------------------------------------


class TestValidateName:
    def test_valid_hyphenated_name_passes(self) -> None:
        c = _client()
        c._validate_name("my-fn")  # should not raise

    def test_minimal_two_char_name_passes(self) -> None:
        c = _client()
        c._validate_name("a1")  # should not raise

    def test_name_with_space_raises_value_error(self) -> None:
        c = _client()
        with pytest.raises(ValueError):
            c._validate_name("bad name")

    def test_name_with_uppercase_raises_value_error(self) -> None:
        c = _client()
        with pytest.raises(ValueError):
            c._validate_name("BadFn")

    def test_name_starting_with_digit_raises_value_error(self) -> None:
        c = _client()
        with pytest.raises(ValueError):
            c._validate_name("1starts-digit")

    def test_empty_name_raises_value_error(self) -> None:
        c = _client()
        with pytest.raises(ValueError):
            c._validate_name("")

    def test_name_starting_with_hyphen_raises_value_error(self) -> None:
        c = _client()
        with pytest.raises(ValueError):
            c._validate_name("-leading")


# ---------------------------------------------------------------------------
# TestDeploy
# ---------------------------------------------------------------------------


class TestDeploy:
    def _mock_201(self) -> MagicMock:
        resp = MagicMock(status_code=201, text='{"metadata":{"name":"my-fn"}}')
        resp.json.return_value = {"metadata": {"name": "my-fn"}}
        return resp

    def test_deploy_calls_post_to_functions_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "registry/my-fn:latest")
        url = mock_post.call_args.args[0]
        assert url.endswith("/api/faas/functions")

    def test_deploy_body_name_correct(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "registry/my-fn:latest")
        body = mock_post.call_args.kwargs["json"]
        assert body["name"] == "my-fn"

    def test_deploy_body_image_correct(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "registry/my-fn:latest")
        body = mock_post.call_args.kwargs["json"]
        assert body["image"] == "registry/my-fn:latest"

    def test_deploy_body_env_vars_defaults_to_empty_dict(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "img:tag")
        body = mock_post.call_args.kwargs["json"]
        assert body["env_vars"] == {}

    def test_deploy_body_env_vars_custom(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "img:tag", env_vars={"FOO": "bar"})
        body = mock_post.call_args.kwargs["json"]
        assert body["env_vars"] == {"FOO": "bar"}

    def test_deploy_body_scale_defaults(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "img:tag")
        body = mock_post.call_args.kwargs["json"]
        assert body["min_scale"] == 0
        assert body["max_scale"] == 5

    def test_deploy_body_scale_custom(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            c.deploy("my-fn", "img:tag", min_scale=1, max_scale=10)
        body = mock_post.call_args.kwargs["json"]
        assert body["min_scale"] == 1
        assert body["max_scale"] == 10

    def test_deploy_returns_json_dict_on_201(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = self._mock_201()
            result = c.deploy("my-fn", "img:tag")
        assert result == {"metadata": {"name": "my-fn"}}

    def test_deploy_raises_runtime_error_on_400(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=400, text="name taken")
            with pytest.raises(RuntimeError, match="400"):
                c.deploy("my-fn", "img:tag")


# ---------------------------------------------------------------------------
# TestList
# ---------------------------------------------------------------------------


class TestList:
    def test_list_calls_get_to_functions_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            resp = MagicMock(status_code=200, text='{"items":[]}')
            resp.json.return_value = {"items": []}
            mock_get.return_value = resp
            c.list()
        url = mock_get.call_args.args[0]
        assert url.endswith("/api/faas/functions")

    def test_list_returns_items_list(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            resp = MagicMock(status_code=200, text='{"items":[{"name":"fn1"}]}')
            resp.json.return_value = {"items": [{"name": "fn1"}]}
            mock_get.return_value = resp
            result = c.list()
        assert result == [{"name": "fn1"}]

    def test_list_returns_empty_list_when_no_items_key(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            resp = MagicMock(status_code=200, text="{}")
            resp.json.return_value = {}
            mock_get.return_value = resp
            result = c.list()
        assert result == []


# ---------------------------------------------------------------------------
# TestGet
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_calls_get_to_function_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            resp = MagicMock(status_code=200, text='{"name":"my-fn"}')
            resp.json.return_value = {"name": "my-fn"}
            mock_get.return_value = resp
            c.get("my-fn")
        url = mock_get.call_args.args[0]
        assert url.endswith("/api/faas/functions/my-fn")

    def test_get_returns_ksvc_dict_on_200(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            resp = MagicMock(status_code=200, text='{"name":"my-fn"}')
            resp.json.return_value = {"name": "my-fn"}
            mock_get.return_value = resp
            result = c.get("my-fn")
        assert result == {"name": "my-fn"}

    def test_get_returns_none_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, text="Not Found")
            result = c.get("my-fn")
        assert result is None

    def test_get_raises_runtime_error_on_500(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=500, text="Server Error")
            with pytest.raises(RuntimeError, match="500"):
                c.get("my-fn")


# ---------------------------------------------------------------------------
# TestScale
# ---------------------------------------------------------------------------


class TestScale:
    def _mock_200(self) -> MagicMock:
        resp = MagicMock(status_code=200, text='{"scaled":true}')
        resp.json.return_value = {"scaled": True}
        return resp

    def test_scale_calls_patch_to_function_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "patch") as mock_patch:
            mock_patch.return_value = self._mock_200()
            c.scale("my-fn", min_scale=0, max_scale=10)
        url = mock_patch.call_args.args[0]
        assert url.endswith("/api/faas/functions/my-fn")

    def test_scale_body_includes_both_params(self) -> None:
        c = _client()
        with patch.object(c._session, "patch") as mock_patch:
            mock_patch.return_value = self._mock_200()
            c.scale("my-fn", min_scale=0, max_scale=10)
        body = mock_patch.call_args.kwargs["json"]
        assert body == {"min_scale": 0, "max_scale": 10}

    def test_scale_zero_min_scale_included_in_body(self) -> None:
        """min_scale=0 must not be dropped (it is falsy but not None)."""
        c = _client()
        with patch.object(c._session, "patch") as mock_patch:
            mock_patch.return_value = self._mock_200()
            c.scale("my-fn", min_scale=0)
        body = mock_patch.call_args.kwargs["json"]
        assert "min_scale" in body
        assert body["min_scale"] == 0
        assert "max_scale" not in body

    def test_scale_returns_scaled_dict(self) -> None:
        c = _client()
        with patch.object(c._session, "patch") as mock_patch:
            mock_patch.return_value = self._mock_200()
            result = c.scale("my-fn", min_scale=0, max_scale=10)
        assert result == {"scaled": True}


# ---------------------------------------------------------------------------
# TestDelete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_calls_delete_to_function_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "delete") as mock_delete:
            mock_delete.return_value = MagicMock(status_code=204, text="")
            c.delete("my-fn")
        url = mock_delete.call_args.args[0]
        assert url.endswith("/api/faas/functions/my-fn")

    def test_delete_returns_none_on_204(self) -> None:
        c = _client()
        with patch.object(c._session, "delete") as mock_delete:
            mock_delete.return_value = MagicMock(status_code=204, text="")
            result = c.delete("my-fn")
        assert result is None

    def test_delete_raises_runtime_error_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "delete") as mock_delete:
            mock_delete.return_value = MagicMock(status_code=404, text="Not Found")
            with pytest.raises(RuntimeError, match="404"):
                c.delete("my-fn")


# ---------------------------------------------------------------------------
# TestLogs
# ---------------------------------------------------------------------------


class TestLogs:
    def test_logs_calls_get_to_logs_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text="log line")
            c.logs("my-fn")
        url = mock_get.call_args.args[0]
        assert url.endswith("/api/faas/functions/my-fn/logs")

    def test_logs_sends_default_tail_param(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text="")
            c.logs("my-fn")
        params = mock_get.call_args.kwargs.get("params", {})
        assert params.get("tail") == 50

    def test_logs_sends_custom_tail_param(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text="")
            c.logs("my-fn", tail=100)
        params = mock_get.call_args.kwargs.get("params", {})
        assert params.get("tail") == 100

    def test_logs_returns_text_on_200(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, text="line1\nline2")
            result = c.logs("my-fn")
        assert result == "line1\nline2"

    def test_logs_raises_runtime_error_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, text="Not Found")
            with pytest.raises(RuntimeError, match="404"):
                c.logs("my-fn")


# ---------------------------------------------------------------------------
# TestInvoke
# ---------------------------------------------------------------------------


class TestInvoke:
    def test_invoke_calls_post_to_invoke_endpoint(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200, content=b"ok", text="ok"
            )
            c.invoke("my-fn")
        url = mock_post.call_args.args[0]
        assert url.endswith("/api/faas/functions/my-fn/invoke")

    def test_invoke_body_defaults_to_get_method_and_root_path(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200, content=b"ok", text="ok"
            )
            c.invoke("my-fn")
        body = mock_post.call_args.kwargs["json"]
        assert body["method"] == "GET"
        assert body["path"] == "/"

    def test_invoke_body_custom_method_and_path(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200, content=b"ok", text="ok"
            )
            c.invoke("my-fn", method="POST", path="/process")
        body = mock_post.call_args.kwargs["json"]
        assert body["method"] == "POST"
        assert body["path"] == "/process"

    def test_invoke_returns_bytes_on_200(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200, content=b'{"result":"ok"}', text='{"result":"ok"}'
            )
            result = c.invoke("my-fn")
        assert result == b'{"result":"ok"}'

    def test_invoke_raises_runtime_error_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=404, content=b"Not Found", text="Not Found"
            )
            with pytest.raises(RuntimeError, match="404"):
                c.invoke("my-fn")
