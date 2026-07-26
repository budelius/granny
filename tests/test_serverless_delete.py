"""Tests for Scaleway function deletion commands."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from granny.cli.serverless import serverless


def test_delete_function_in_named_namespace() -> None:
    client = MagicMock()
    client.find_namespace.return_value = {"id": "ns-1", "name": "prod"}
    client.find_function.return_value = {
        "id": "fn-1",
        "name": "worker",
        "status": "ready",
    }

    with patch("granny.cli.serverless._client", return_value=client):
        result = CliRunner().invoke(
            serverless,
            ["delete-function", "worker", "--namespace", "prod", "--yes"],
        )

    assert result.exit_code == 0, result.output
    client.find_function.assert_called_once_with("ns-1", "worker")
    client.delete_function.assert_called_once_with("fn-1")


def test_delete_function_requires_unique_name_without_namespace() -> None:
    client = MagicMock()
    client.list_namespaces.return_value = [{"id": "ns-1"}, {"id": "ns-2"}]
    client.find_function.side_effect = [
        {"id": "fn-1", "name": "worker"},
        {"id": "fn-2", "name": "worker"},
    ]

    with patch("granny.cli.serverless._client", return_value=client):
        result = CliRunner().invoke(
            serverless,
            ["delete-function", "worker", "--yes"],
        )

    assert result.exit_code != 0
    assert "multiple namespaces" in result.output
    client.delete_function.assert_not_called()


def test_delete_function_reports_missing_namespace() -> None:
    client = MagicMock()
    client.find_namespace.return_value = None

    with patch("granny.cli.serverless._client", return_value=client):
        result = CliRunner().invoke(
            serverless,
            ["delete-function", "worker", "--namespace", "missing", "--yes"],
        )

    assert result.exit_code != 0
    assert "Namespace 'missing' not found" in result.output
    client.delete_function.assert_not_called()
