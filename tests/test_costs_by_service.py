"""Unit tests for per-service cost history + range total (no network)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

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
