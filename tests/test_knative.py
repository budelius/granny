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
from click.testing import CliRunner

from granny.cli.knative import knative
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

    def test_delete_raises_key_error_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "delete") as mock_delete:
            mock_delete.return_value = MagicMock(status_code=404, text="Not Found")
            with pytest.raises(KeyError, match="404"):
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

    def test_logs_raises_key_error_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, text="Not Found")
            with pytest.raises(KeyError, match="404"):
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

    def test_invoke_raises_key_error_on_404(self) -> None:
        c = _client()
        with patch.object(c._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=404, content=b"Not Found", text="Not Found"
            )
            with pytest.raises(KeyError, match="404"):
                c.invoke("my-fn")


# ---------------------------------------------------------------------------
# TestCLI — Click command group via CliRunner (no live infra)
# ---------------------------------------------------------------------------

_CLI_ARGS_BASE = [
    "--api-url",
    "https://faas-api.test.example",
    "--api-key",
    "test-fake-key-value",
]


def _mock_client(deploy_ret=None, list_ret=None, get_ret=None, scale_ret=None):
    """Return a pre-configured mock KnativeClient instance."""
    m = MagicMock()
    m.deploy.return_value = deploy_ret or {"status": {"url": "https://fn.example.com"}}
    m.list.return_value = list_ret if list_ret is not None else []
    m.get.return_value = get_ret
    m.scale.return_value = scale_ret or {"scaled": True}
    return m


class TestCLI:
    def _run(self, args: list[str]):
        return CliRunner().invoke(knative, args)

    # -- deploy ---------------------------------------------------------------

    def test_deploy_command_calls_client(self) -> None:
        mock_instance = _mock_client()
        with patch(
            "granny.knative.KnativeClient", return_value=mock_instance
        ) as mock_cls:
            result = self._run(
                ["deploy", "my-fn", "--image", "img:tag"] + _CLI_ARGS_BASE
            )
        assert result.exit_code == 0, result.output
        mock_cls.assert_called_once_with(
            base_url="https://faas-api.test.example",
            api_key="test-fake-key-value",
        )
        mock_instance.deploy.assert_called_once()
        assert "Deployed: my-fn" in result.output

    def test_deploy_command_env_parsing(self) -> None:
        mock_instance = _mock_client()
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(
                [
                    "deploy",
                    "my-fn",
                    "--image",
                    "img:tag",
                    "--env",
                    "FOO=bar",
                    "--env",
                    "BAZ=qux",
                ]
                + _CLI_ARGS_BASE
            )
        assert result.exit_code == 0, result.output
        call_kwargs = mock_instance.deploy.call_args
        env_vars = call_kwargs.kwargs.get("env_vars", call_kwargs[1].get("env_vars"))
        assert env_vars == {"FOO": "bar", "BAZ": "qux"}

    # -- list -----------------------------------------------------------------

    def test_list_command_prints_table(self) -> None:
        items = [
            {"metadata": {"name": "fn1"}, "status": {"url": "https://fn1.example.com"}}
        ]
        mock_instance = _mock_client(list_ret=items)
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["list"] + _CLI_ARGS_BASE)
        assert result.exit_code == 0, result.output
        assert "fn1" in result.output
        assert "https://fn1.example.com" in result.output

    def test_list_command_empty(self) -> None:
        mock_instance = _mock_client(list_ret=[])
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["list"] + _CLI_ARGS_BASE)
        assert result.exit_code == 0, result.output
        assert "No functions deployed." in result.output

    # -- get ------------------------------------------------------------------

    def test_get_command_found(self) -> None:
        fn_dict = {
            "metadata": {"name": "my-fn"},
            "status": {"url": "https://fn.example.com"},
        }
        mock_instance = _mock_client(get_ret=fn_dict)
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["get", "my-fn"] + _CLI_ARGS_BASE)
        assert result.exit_code == 0, result.output
        assert "my-fn" in result.output

    def test_get_command_not_found(self) -> None:
        mock_instance = _mock_client(get_ret=None)
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["get", "my-fn"] + _CLI_ARGS_BASE)
        assert result.exit_code != 0
        assert (
            "not found" in result.output.lower()
            or "not found" in (result.output + (result.stderr or "")).lower()
        )

    # -- scale ----------------------------------------------------------------

    def test_scale_command(self) -> None:
        mock_instance = _mock_client()
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(
                ["scale", "my-fn", "--min-scale", "1", "--max-scale", "10"]
                + _CLI_ARGS_BASE
            )
        assert result.exit_code == 0, result.output
        mock_instance.scale.assert_called_once_with("my-fn", min_scale=1, max_scale=10)

    def test_scale_requires_at_least_one_option(self) -> None:
        mock_instance = _mock_client()
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["scale", "my-fn"] + _CLI_ARGS_BASE)
        assert result.exit_code != 0

    # -- remove ---------------------------------------------------------------

    def test_remove_requires_yes_flag(self) -> None:
        mock_instance = _mock_client()
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["remove", "my-fn"] + _CLI_ARGS_BASE)
        assert result.exit_code != 0
        mock_instance.delete.assert_not_called()

    def test_remove_calls_delete(self) -> None:
        mock_instance = _mock_client()
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["remove", "my-fn", "--yes"] + _CLI_ARGS_BASE)
        assert result.exit_code == 0, result.output
        mock_instance.delete.assert_called_once_with("my-fn")
        assert "Removed: my-fn" in result.output

    # -- logs -----------------------------------------------------------------

    def test_logs_command(self) -> None:
        mock_instance = _mock_client()
        mock_instance.logs.return_value = "line1\nline2"
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["logs", "my-fn"] + _CLI_ARGS_BASE)
        assert result.exit_code == 0, result.output
        assert "line1" in result.output
        assert "line2" in result.output

    # -- invoke ---------------------------------------------------------------

    def test_invoke_command(self) -> None:
        mock_instance = _mock_client()
        mock_instance.invoke.return_value = b"hello"
        with patch("granny.knative.KnativeClient", return_value=mock_instance):
            result = self._run(["invoke", "my-fn"] + _CLI_ARGS_BASE)
        assert result.exit_code == 0, result.output
        assert "hello" in result.output


# ---------------------------------------------------------------------------
# TestDeployUi — deploy-ui command (upload + CDN purge + backend deploy)
# ---------------------------------------------------------------------------


class TestDeployUi:
    """Tests for 'granny knative deploy-ui'."""

    def _run(self, args: list[str]):
        return CliRunner().invoke(knative, args)

    def _dist_args(self, dist_dir: str) -> list[str]:
        return [
            "deploy-ui",
            "my-fn",
            dist_dir,
            "--image",
            "img:tag",
            "--zone",
            "my-zone",
            "--storage-password",
            "fake-storage-pw",
        ] + _CLI_ARGS_BASE

    def test_upload_dist_calls_upload_file_per_file(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "main.js").write_text("console.log('hi')")

        with (
            patch("granny.storage.bunny.BunnyStorageClient") as mock_storage_cls,
            patch("granny.knative.KnativeClient") as mock_knative_cls,
        ):
            mock_storage_cls.return_value.upload_file.return_value = None
            mock_knative_cls.return_value.deploy.return_value = {
                "status": {"url": "https://fn.example.com"}
            }
            result = self._run(self._dist_args(str(tmp_path)))

        assert result.exit_code == 0, result.output
        assert mock_storage_cls.return_value.upload_file.call_count == 2

    def test_deploy_ui_html_cache_control(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")

        with (
            patch("granny.storage.bunny.BunnyStorageClient") as mock_storage_cls,
            patch("granny.knative.KnativeClient") as mock_knative_cls,
        ):
            mock_storage_cls.return_value.upload_file.return_value = None
            mock_knative_cls.return_value.deploy.return_value = {}
            self._run(self._dist_args(str(tmp_path)))

        calls = mock_storage_cls.return_value.upload_file.call_args_list
        assert len(calls) == 1
        # cache_control is the 7th positional arg (index 6) or 'cache_control' kwarg
        cc = calls[0].kwargs.get("cache_control") or calls[0].args[6]
        assert cc == "no-cache, no-store, must-revalidate"

    def test_deploy_ui_asset_cache_control(self, tmp_path) -> None:
        (tmp_path / "main.js").write_text("// js")

        with (
            patch("granny.storage.bunny.BunnyStorageClient") as mock_storage_cls,
            patch("granny.knative.KnativeClient") as mock_knative_cls,
        ):
            mock_storage_cls.return_value.upload_file.return_value = None
            mock_knative_cls.return_value.deploy.return_value = {}
            self._run(self._dist_args(str(tmp_path)))

        calls = mock_storage_cls.return_value.upload_file.call_args_list
        assert len(calls) == 1
        cc = calls[0].kwargs.get("cache_control") or calls[0].args[6]
        assert cc == "public, max-age=31536000, immutable"

    def test_deploy_ui_calls_knative_deploy(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")

        with (
            patch("granny.storage.bunny.BunnyStorageClient") as mock_storage_cls,
            patch("granny.knative.KnativeClient") as mock_knative_cls,
        ):
            mock_storage_cls.return_value.upload_file.return_value = None
            mock_knative_cls.return_value.deploy.return_value = {
                "status": {"url": "https://fn.example.com"}
            }
            result = self._run(self._dist_args(str(tmp_path)))

        assert result.exit_code == 0, result.output
        mock_knative_cls.return_value.deploy.assert_called_once()
        deploy_call = mock_knative_cls.return_value.deploy.call_args
        assert deploy_call.args[0] == "my-fn"
        assert deploy_call.args[1] == "img:tag"

    def test_deploy_ui_purges_cdn_when_pull_zone_id_given(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")

        with (
            patch("granny.storage.bunny.BunnyStorageClient") as mock_storage_cls,
            patch("granny.cdn.bunny.BunnyCDNClient") as mock_cdn_cls,
            patch("granny.knative.KnativeClient") as mock_knative_cls,
        ):
            mock_storage_cls.return_value.upload_file.return_value = None
            mock_cdn_cls.return_value.purge_cache.return_value = None
            mock_knative_cls.return_value.deploy.return_value = {}
            result = self._run(
                self._dist_args(str(tmp_path)) + ["--pull-zone-id", "42"]
            )

        assert result.exit_code == 0, result.output
        mock_cdn_cls.return_value.purge_cache.assert_called_once_with(42)

    def test_deploy_ui_skips_cdn_purge_when_no_pull_zone_id(self, tmp_path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")

        with (
            patch("granny.storage.bunny.BunnyStorageClient") as mock_storage_cls,
            patch("granny.cdn.bunny.BunnyCDNClient") as mock_cdn_cls,
            patch("granny.knative.KnativeClient") as mock_knative_cls,
        ):
            mock_storage_cls.return_value.upload_file.return_value = None
            mock_cdn_cls.return_value.purge_cache.return_value = None
            mock_knative_cls.return_value.deploy.return_value = {}
            result = self._run(self._dist_args(str(tmp_path)))

        assert result.exit_code == 0, result.output
        mock_cdn_cls.return_value.purge_cache.assert_not_called()
