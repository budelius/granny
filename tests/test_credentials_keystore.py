"""Keystore fallback in credential resolution, and Hetzner CORS options.

The keystore backend exists because Vaultwarden is not reachable from every
session -- a locked workstation, or a non-interactive shell where Windows Hello
cannot prompt. obachan keeps the same infrastructure credentials in the OS
keystore under ``OBACHAN_<ENV>_<NAME>``, so granny can still answer there.
"""

import logging

import pytest

from granny.credentials import secrets as sec


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr(sec, "_cache", {})
    monkeypatch.delenv("GRANNY_KEYSTORE_ENV", raising=False)
    yield


class TestKeystoreCandidates:
    def test_the_credentials_own_name_comes_first(self):
        candidates = sec._keystore_candidates("HETZNER_S3_ACCESS_KEY")
        assert candidates[0] == "HETZNER_S3_ACCESS_KEY", (
            "an entry stored under exactly the requested name must win over a "
            "namespaced guess"
        )

    def test_an_alias_expands_across_environments(self):
        candidates = sec._keystore_candidates("HETZNER_S3_ACCESS_KEY")
        assert "OBACHAN_PROD_S3_ACCESS_KEY" in candidates
        assert "OBACHAN_STAGING_S3_ACCESS_KEY" in candidates

    def test_pinning_an_environment_narrows_it_to_that_one(self, monkeypatch):
        monkeypatch.setenv("GRANNY_KEYSTORE_ENV", "staging")
        candidates = sec._keystore_candidates("HETZNER_S3_ACCESS_KEY")
        assert candidates == ["HETZNER_S3_ACCESS_KEY", "OBACHAN_STAGING_S3_ACCESS_KEY"]

    def test_an_unaliased_secret_is_looked_up_under_its_own_name_only(self):
        assert sec._keystore_candidates("BUNNY_API_KEY") == ["BUNNY_API_KEY"]


class TestKeystoreFetch:
    def test_reports_which_entry_answered(self, monkeypatch, caplog):
        """A production credential arriving under an alias is never silent."""
        monkeypatch.setattr(
            sec,
            "_keystore_candidates",
            lambda name: ["HETZNER_S3_ACCESS_KEY", "OBACHAN_PROD_S3_ACCESS_KEY"],
        )
        _install_fake_keystore(monkeypatch, {"OBACHAN_PROD_S3_ACCESS_KEY": "AKIA"})

        with caplog.at_level(logging.INFO, logger=sec.__name__):
            assert sec._fetch_from_keystore("HETZNER_S3_ACCESS_KEY") == "AKIA"

        assert "OBACHAN_PROD_S3_ACCESS_KEY" in caplog.text

    def test_a_keystore_that_cannot_be_consulted_is_a_miss_not_a_crash(
        self, monkeypatch
    ):
        def boom(_name):
            raise RuntimeError("Windows Hello cannot prompt")

        _install_fake_keystore(monkeypatch, {}, get=boom)
        assert sec._fetch_from_keystore("HETZNER_S3_ACCESS_KEY") is None


class TestResolutionOrder:
    def test_vault_still_wins_so_existing_secrets_are_unaffected(self, monkeypatch):
        monkeypatch.setattr(sec, "_fetch_from_vault", lambda name: "from-vault")
        monkeypatch.setattr(sec, "_fetch_from_keystore", lambda name: "from-keystore")
        assert sec.get_secret("BUNNY_API_KEY") == "from-vault"

    def test_keystore_answers_only_once_vault_has_missed(self, monkeypatch):
        monkeypatch.setattr(sec, "_fetch_from_vault", lambda name: None)
        monkeypatch.setattr(sec, "_fetch_from_keystore", lambda name: "from-keystore")
        assert sec.get_secret("HETZNER_S3_ACCESS_KEY") == "from-keystore"

    def test_an_explicit_env_var_beats_both(self, monkeypatch):
        monkeypatch.setenv("HETZNER_S3_ACCESS_KEY", "from-env")
        monkeypatch.setattr(sec, "_fetch_from_vault", lambda name: "from-vault")
        monkeypatch.setattr(sec, "_fetch_from_keystore", lambda name: "from-keystore")
        assert sec.get_secret("HETZNER_S3_ACCESS_KEY") == "from-env"

    def test_an_unregistered_name_reaches_neither_backend(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            sec, "_fetch_from_vault", lambda name: called.append("vault")
        )
        monkeypatch.setattr(
            sec, "_fetch_from_keystore", lambda name: called.append("keystore")
        )
        assert sec.get_secret("NOT_REGISTERED_ANYWHERE") is None
        assert called == []


def _install_fake_keystore(monkeypatch, entries, get=None):
    """Stand in for ``locke.keystore`` without requiring locke to be present."""
    import sys
    import types

    module = types.ModuleType("locke.keystore")
    module.get_credential = get or (lambda name: entries.get(name))
    monkeypatch.setitem(sys.modules, "locke.keystore", module)
