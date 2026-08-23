"""Unit tests for per-service cost history + range total (no network)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from granny.analyze import costs as costs_mod


class FakeCeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs.get("GroupBy"):
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-06-01", "End": "2026-07-01"},
                        "Groups": [
                            {"Keys": ["AmazonEC2"], "Metrics": {"UnblendedCost": {"Amount": "100.0", "Unit": "USD"}}},
                            {"Keys": ["AmazonS3"], "Metrics": {"UnblendedCost": {"Amount": "2.0", "Unit": "USD"}}},
                        ],
                    },
                    {
                        "TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
                        "Groups": [
                            {"Keys": ["AmazonEC2"], "Metrics": {"UnblendedCost": {"Amount": "120.0", "Unit": "USD"}}},
                        ],
                    },
                ]
            }
        return {
            "ResultsByTime": [
                {"Total": {"UnblendedCost": {"Amount": "42.5", "Unit": "USD"}}}
            ]
        }


def test_aws_costs_by_service_aligns_services_to_months(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCeClient()
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: fake)

    hist = costs_mod.aws_costs_by_service("lularge", months=2, today=date(2026, 8, 20))

    assert hist.account == "lularge"
    assert hist.months == ["2026-06", "2026-07"]
    # EC2 present both months; S3 only in month 0 -> back-filled 0.0 for month 1.
    assert hist.by_service["AmazonEC2"] == [100.0, 120.0]
    assert hist.by_service["AmazonS3"] == [2.0, 0.0]
    # A GroupBy=SERVICE monthly query was issued.
    assert fake.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
    assert fake.calls[0]["Granularity"] == "MONTHLY"
    assert fake.calls[0]["Metrics"] == ["UnblendedCost"]


def test_aws_cost_total_sums_range(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCeClient()
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: fake)

    total = costs_mod.aws_cost_total("lularge", date(2026, 8, 1), date(2026, 8, 20))

    assert total == 42.5
    assert fake.calls[0]["TimePeriod"] == {"Start": "2026-08-01", "End": "2026-08-20"}
    assert "GroupBy" not in fake.calls[0]


class RaisingCeClient:
    """A Cost Explorer client whose call always raises a ClientError."""

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "boom"}},
            "GetCostAndUsage",
        )


def test_aws_costs_by_service_degrades_gracefully_on_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: RaisingCeClient())

    hist = costs_mod.aws_costs_by_service("lularge", months=2, today=date(2026, 8, 20))

    assert hist.months == []
    assert hist.by_service == {}


def test_aws_cost_total_returns_none_on_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: RaisingCeClient())

    total = costs_mod.aws_cost_total("lularge", date(2026, 8, 1), date(2026, 8, 20))

    assert total is None


def test_aws_costs_by_service_degrades_gracefully_on_profile_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_profile_not_found(profile: str) -> Any:
        raise ProfileNotFound(profile="bad")

    monkeypatch.setattr(costs_mod, "_ce_client", _raise_profile_not_found)

    hist = costs_mod.aws_costs_by_service("bad", months=2, today=date(2026, 8, 20))

    assert hist.months == []
    assert hist.by_service == {}


def test_aws_cost_total_returns_none_on_profile_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_profile_not_found(profile: str) -> Any:
        raise ProfileNotFound(profile="bad")

    monkeypatch.setattr(costs_mod, "_ce_client", _raise_profile_not_found)

    total = costs_mod.aws_cost_total("bad", date(2026, 8, 1), date(2026, 8, 20))

    assert total is None


# ---------------------------------------------------------------------------
# Credit/refund exclusion
#
# Cost Explorer's default (unfiltered) response includes every RECORD_TYPE, so
# promotional credits arrive as negative line items that cancel the usage they
# paid for. On a fully credit-covered account that nets every figure to ~0.00,
# which reads as "we are spending nothing" when the account is in fact burning
# credits. These helpers report *spend*, so they must exclude Credit/Refund.
# ---------------------------------------------------------------------------

EXPECTED_FILTER = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}


def test_aws_costs_by_service_excludes_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCeClient()
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: fake)

    costs_mod.aws_costs_by_service("lularge", months=2, today=date(2026, 8, 20))

    assert fake.calls[0]["Filter"] == EXPECTED_FILTER


def test_aws_cost_total_excludes_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCeClient()
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: fake)

    costs_mod.aws_cost_total("lularge", date(2026, 8, 1), date(2026, 8, 20))

    assert fake.calls[0]["Filter"] == EXPECTED_FILTER


class FakeForecastCeClient(FakeCeClient):
    """FakeCeClient that also answers get_cost_forecast."""

    def __init__(self) -> None:
        super().__init__()
        self.forecast_calls: list[dict[str, Any]] = []

    def get_cost_forecast(self, **kwargs: Any) -> dict[str, Any]:
        self.forecast_calls.append(kwargs)
        return {"Total": {"Amount": "10.0", "Unit": "USD"}}


def test_aws_costs_mtd_and_forecast_exclude_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeForecastCeClient()
    monkeypatch.setattr(costs_mod, "_ce_client", lambda profile: fake)

    summaries = costs_mod.aws_costs(["lularge"])

    assert len(summaries) == 1
    assert summaries[0].mtd_cost == 42.5
    # Both the MTD query and the forecast must exclude credit/refund lines,
    # otherwise a credit-covered account forecasts ~0 spend.
    assert fake.calls[0]["Filter"] == EXPECTED_FILTER
    assert fake.forecast_calls[0]["Filter"] == EXPECTED_FILTER
