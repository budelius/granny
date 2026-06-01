"""Unit tests for Mailjet DNS auth helpers (no network)."""

from granny.cli.email import _short_name
from granny.email.mailjet import merge_spf, parse_spf


class TestShortName:
    def test_subdomain_record(self):
        assert _short_name("mailjet._domainkey.mezzanin.eu", "mezzanin.eu") == "mailjet._domainkey"

    def test_apex(self):
        assert _short_name("mezzanin.eu", "mezzanin.eu") == "@"

    def test_trailing_dots_and_case(self):
        assert _short_name("Mailjet._F0A9.MEZZANIN.EU.", "mezzanin.eu") == "mailjet._f0a9"

    def test_already_short(self):
        # If it doesn't end with the domain, return as-is (caller passed short).
        assert _short_name("mailjet._domainkey", "mezzanin.eu") == "mailjet._domainkey"


class TestMergeSpf:
    def test_create_when_absent(self):
        merged, warnings = merge_spf(None, target_qualifier="~all")
        assert merged == "v=spf1 include:spf.mailjet.com ~all"
        assert warnings == []

    def test_idempotent_when_already_present(self):
        existing = "v=spf1 include:spf.mailjet.com ~all"
        merged, _ = merge_spf(existing, target_qualifier="~all")
        assert merged == existing

    def test_adds_include_to_existing(self):
        merged, _ = merge_spf("v=spf1 include:_spf.google.com ~all")
        assert "include:_spf.google.com" in merged
        assert "include:spf.mailjet.com" in merged
        assert merged.endswith("~all")

    def test_upgrades_neutral_qualifier(self):
        # Mailjet hands out `?all`; we prefer `~all`.
        merged, warnings = merge_spf("v=spf1 include:spf.mailjet.com ?all", target_qualifier="~all")
        assert merged.endswith("~all")
        assert any("?all" in w for w in warnings)

    def test_unwraps_quoted_provider_value(self):
        mechs, qual = parse_spf('"v=spf1 include:spf.mailjet.com" "?all"')
        assert "include:spf.mailjet.com" in mechs
        assert qual == "?all"
