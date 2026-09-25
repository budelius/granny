"""Browser cache headers for a single-page app behind Bunny.

A pull zone applies one ``max-age`` to every response, ``index.html``
included -- which pins returning browsers to the previous build's chunk
filenames. Purging the CDN cannot fix it, because the browser has been told
not to ask.
"""

from unittest.mock import MagicMock

import pytest

from granny.cdn.bunny import BunnyCDNClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        BunnyCDNClient, "__init__", lambda self, api_key=None, customer=None: None
    )
    instance = BunnyCDNClient()
    instance._request = MagicMock(return_value={})
    return instance


def _calls(client):
    return [(c.args[0], c.args[1], c.args[2] if len(c.args) > 2 else None)
            for c in client._request.call_args_list]


class TestConfigureSpaCacheHeaders:
    def test_turns_off_the_zones_default_browser_cache(self, client):
        client.configure_spa_cache_headers(123)
        settings = next(
            body for method, path, body in _calls(client)
            if path == "/pullzone/123" and body
        )
        assert settings["CacheControlPublicMaxAgeOverride"] == 0

    def test_gives_the_long_cache_back_to_hashed_assets(self, client):
        client.configure_spa_cache_headers(123)
        rule = next(
            body for method, path, body in _calls(client)
            if path.endswith("/edgerules/addOrUpdate")
        )
        assert rule["ActionType"] == 16, "OverrideBrowserCacheTime"
        assert rule["ActionParameter1"] == "2592000"
        assert rule["Enabled"] is True

    def test_matches_assets_by_path_not_by_extension(self, client):
        """Every SPA route returns HTML; only the asset path is a safe signal.

        `/invoice-upload` serves index.html and looks nothing like `*.html`,
        so enumerating what must not be cached cannot be made reliable.
        """
        client.configure_spa_cache_headers(123)
        rule = next(
            body for method, path, body in _calls(client)
            if path.endswith("/edgerules/addOrUpdate")
        )
        trigger = rule["Triggers"][0]
        assert trigger["Type"] == 0, "Url trigger"
        assert trigger["PatternMatches"] == ["*/assets/*"]
        assert not any(
            "html" in pattern for pattern in trigger["PatternMatches"]
        )

    def test_the_asset_path_and_lifetime_are_overridable(self, client):
        client.configure_spa_cache_headers(
            123, asset_path="*/static/*", asset_max_age=60
        )
        rule = next(
            body for method, path, body in _calls(client)
            if path.endswith("/edgerules/addOrUpdate")
        )
        assert rule["ActionParameter1"] == "60"
        assert rule["Triggers"][0]["PatternMatches"] == ["*/static/*"]

    def test_it_does_not_purge_on_its_own(self, client):
        """Purging is the caller's decision; the header change is the point."""
        client.configure_spa_cache_headers(123)
        assert not any(
            "purge" in path for _method, path, _body in _calls(client)
        )
