from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from granny.cli.oauth import oauth
from granny.oauth import (
    ObachanSecretClient,
    ProductivityOAuthCredentials,
    load_google_client_json,
    provision_productivity_oauth,
)


def test_load_google_client_json_reads_web_download(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text('{"web":{"client_id":"google-id","client_secret":"google-secret"}}')

    assert load_google_client_json(path) == ("google-id", "google-secret")


def test_provision_updates_env_without_destroying_comments(tmp_path: Path) -> None:
    path = tmp_path / ".deploy.env"
    path.write_text("# keep me\nOTHER=value\nGOOGLE_CLIENT_ID=old\n")

    result = provision_productivity_oauth(
        ProductivityOAuthCredentials("new-id", "new secret"), env_file=path
    )

    assert result.keys == ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    assert path.read_text() == (
        "# keep me\nOTHER=value\nGOOGLE_CLIENT_ID=new-id\n\n"
        'GOOGLE_CLIENT_SECRET="new secret"\n'
    )


def test_credentials_require_complete_provider_pair() -> None:
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        ProductivityOAuthCredentials(google_client_id="id").validate()


def test_obachan_merge_preserves_existing_fields() -> None:
    get_response = Mock(
        status_code=200,
        json=lambda: {"data": {"data": {"OTHER": "keep"}, "metadata": {"version": 7}}},
    )
    post_response = Mock(status_code=204)

    with (
        patch("granny.oauth.productivity.requests.get", return_value=get_response),
        patch(
            "granny.oauth.productivity.requests.post", return_value=post_response
        ) as post,
    ):
        client = ObachanSecretClient("https://bao.example", "token")
        client.merge(
            "obachan/staging/apps/dashboard/productivity-oauth",
            {"GOOGLE_CLIENT_ID": "id"},
        )

    assert post.call_args.kwargs["json"] == {
        "data": {"OTHER": "keep", "GOOGLE_CLIENT_ID": "id"},
        "options": {"cas": 7},
    }
    assert "token" not in str(post.call_args.args)


def test_env_update_replaces_export_and_removes_stale_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text("export GOOGLE_CLIENT_ID=old\nGOOGLE_CLIENT_ID=stale\nKEEP=yes\n")

    provision_productivity_oauth(
        ProductivityOAuthCredentials("new", "secret"), env_file=path
    )

    assert path.read_text() == (
        "export GOOGLE_CLIENT_ID=new\nKEEP=yes\n\nGOOGLE_CLIENT_SECRET=secret\n"
    )


def test_cli_accepts_google_cloud_client_json(tmp_path: Path) -> None:
    client = tmp_path / "client.json"
    client.write_text(
        '{"installed":{"client_id":"google-id","client_secret":"google-secret"}}'
    )
    env_file = tmp_path / ".env"

    result = CliRunner().invoke(
        oauth,
        [
            "provision-productivity",
            "--env-file",
            str(env_file),
            "--google-client-json",
            str(client),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "google-secret" not in result.output
    assert "GOOGLE_CLIENT_SECRET=google-secret" in env_file.read_text()


def test_provision_cli_dry_run_does_not_write_destinations(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    result = CliRunner().invoke(
        oauth,
        [
            "provision-productivity",
            "--env-file",
            str(env_file),
            "--obachan-env",
            "staging",
            "--google-client-id",
            "google-id",
            "--google-client-secret",
            "google-secret",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not env_file.exists()
    assert "Would update OpenBao path" in result.output
    assert "google-secret" not in result.output


def test_compatibility_command_still_accepts_partial_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    result = CliRunner().invoke(
        oauth,
        [
            "configure-productivity-local",
            "--env-file",
            str(env_file),
            "--google-client-id",
            "google-id",
        ],
    )

    assert result.exit_code == 0, result.output
    assert env_file.read_text() == "GOOGLE_CLIENT_ID=google-id\n"
