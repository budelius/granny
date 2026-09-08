"""Cloudflare list_records must return every page, not just the first.

Cloudflare caps a page at 100 items and reports the true total in
``result_info``. Requesting ``per_page=100`` and returning ``body["result"]``
looks correct on every small zone and silently truncates large ones. A live
123-record zone answered with 100 on 2026-09-08; the 23 that never arrived read
to the caller as records that do not exist, which turned an ordinary DNS drift
report into false "missing" findings an operator could have acted on.
"""

from unittest.mock import MagicMock

from granny.dns.cloudflare import MAX_PAGES, CloudflareDNSProvider


def _record(name: str) -> dict:
    return {"id": name, "name": name, "type": "A", "content": "1.2.3.4", "ttl": 300}


def _provider(pages: list[list[dict]]) -> tuple[CloudflareDNSProvider, MagicMock]:
    """Provider whose GETs replay one canned page each, with result_info."""
    provider = CloudflareDNSProvider(api_token="test-token")
    replies = []
    for i, page in enumerate(pages, start=1):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "success": True,
            "result": page,
            "result_info": {
                "page": i,
                "per_page": 100,
                "total_pages": len(pages),
                "total_count": sum(len(p) for p in pages),
            },
        }
        resp.text = ""
        replies.append(resp)
    session = MagicMock()
    session.get.side_effect = iter(replies)
    provider.session = session
    return provider, session


def test_returns_records_from_every_page() -> None:
    """The regression: 123 records across two pages, all 123 must come back."""
    page1 = [_record(f"r{i}.example.org") for i in range(100)]
    page2 = [_record(f"r{i}.example.org") for i in range(100, 123)]
    provider, session = _provider([page1, page2])

    records = provider.list_records("zone1")

    assert len(records) == 123
    assert session.get.call_count == 2
    assert "r122.example.org" in {r.name for r in records}


def test_second_page_is_requested_by_number() -> None:
    provider, session = _provider([[_record("a.example.org")] * 100, [_record("b.example.org")]])

    provider.list_records("zone1")

    pages = [c.kwargs["params"]["page"] for c in session.get.call_args_list]
    assert pages == [1, 2]
    assert all(c.kwargs["params"]["per_page"] == 100 for c in session.get.call_args_list)


def test_single_page_zone_makes_one_request() -> None:
    """Small zones must not pay for an extra round-trip."""
    provider, session = _provider([[_record("a.example.org"), _record("b.example.org")]])

    records = provider.list_records("zone1")

    assert len(records) == 2
    assert session.get.call_count == 1


def test_empty_zone_returns_nothing_without_looping() -> None:
    provider, session = _provider([[]])

    assert provider.list_records("zone1") == []
    assert session.get.call_count == 1


def test_name_and_type_filters_survive_pagination() -> None:
    """The filter params must be sent on every page, not just the first."""
    provider, session = _provider([[_record("a.example.org")] * 100, [_record("a.example.org")]])
    # A short `name` makes list_records resolve the zone name first, so that
    # reply has to come before the record pages.
    zone_meta = MagicMock()
    zone_meta.status_code = 200
    zone_meta.json.return_value = {"success": True, "result": {"name": "example.org"}}
    zone_meta.text = ""
    session.get.side_effect = iter([zone_meta, *session.get.side_effect])

    provider.list_records("zone1", name="a", record_type="a")

    record_calls = session.get.call_args_list[1:]
    assert len(record_calls) == 2
    for call in record_calls:
        assert call.kwargs["params"]["type"] == "A"
        assert call.kwargs["params"]["name"] == "a.example.org"


def test_missing_result_info_is_treated_as_one_page() -> None:
    """Defensive: an endpoint that omits result_info must not loop forever."""
    provider = CloudflareDNSProvider(api_token="test-token")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True, "result": [_record("a.example.org")]}
    resp.text = ""
    session = MagicMock()
    session.get.return_value = resp
    provider.session = session

    assert len(provider.list_records("zone1")) == 1
    assert session.get.call_count == 1


def test_runaway_total_pages_is_capped() -> None:
    """A provider reporting endless pages must stop, not spin."""
    provider = CloudflareDNSProvider(api_token="test-token")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "success": True,
        "result": [_record("a.example.org")],
        "result_info": {"total_pages": 10_000},
    }
    resp.text = ""
    session = MagicMock()
    session.get.return_value = resp
    provider.session = session

    provider.list_records("zone1")

    assert session.get.call_count == MAX_PAGES
