"""Tests for the usage-billed providers: Bunny and Scaleway.

Fixtures are trimmed from live API responses captured on 2026-09-03.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from granny.analyze.costs import _straight_line_eom, bunny_costs, scaleway_costs
from granny.cli.serverless import serverless
from granny.scaleway.billing import amount_of


# ---------------------------------------------------------------------------
# Money decoding
# ---------------------------------------------------------------------------


def test_amount_of_combines_units_and_nanos() -> None:
    assert amount_of({"units": 2, "nanos": 340000000}) == pytest.approx(2.34)


def test_amount_of_handles_discount_lines() -> None:
    """Free-tier credits arrive negative, with units and nanos sharing a sign."""
    assert amount_of({"units": -1, "nanos": -500000000}) == pytest.approx(-1.5)


def test_amount_of_treats_missing_fields_as_zero() -> None:
    assert amount_of({}) == 0.0
    assert amount_of({"units": 3}) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Straight-line projection
# ---------------------------------------------------------------------------


def test_straight_line_projects_from_elapsed_days() -> None:
    """Ten days into a 30-day month, spend so far triples by month end."""
    assert _straight_line_eom(10.0, date(2026, 9, 10)) == pytest.approx(30.0)


def test_straight_line_does_not_divide_by_zero_on_the_first() -> None:
    """Day one counts as one elapsed day, not zero."""
    assert _straight_line_eom(2.0, date(2026, 9, 1)) == pytest.approx(60.0)


def test_straight_line_on_the_last_day_equals_actual() -> None:
    assert _straight_line_eom(45.0, date(2026, 9, 30)) == pytest.approx(45.0)


def test_straight_line_uses_the_real_month_length() -> None:
    """February is 28 days in 2026, so the projection must not assume 30."""
    assert _straight_line_eom(7.0, date(2026, 2, 7)) == pytest.approx(28.0)


# ---------------------------------------------------------------------------
# Bunny
# ---------------------------------------------------------------------------

BUNNY_BILLING = {
    "Balance": 13.113975906743292,
    "ThisMonthCharges": 0.00924704425751627,
    "MonthlyChargesStorage": 0.003321933621516268,
    "MonthlyChargesEUTraffic": 0.0046545450079999995,
}


def test_bunny_costs_reports_month_to_date_and_balance() -> None:
    client = MagicMock()
    client.get_billing.return_value = BUNNY_BILLING
    client.month_to_date.return_value = BUNNY_BILLING["ThisMonthCharges"]
    client.balance.return_value = BUNNY_BILLING["Balance"]

    with patch("granny.bunny.billing.BunnyBillingClient", return_value=client):
        rows = bunny_costs()

    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "bunny"
    assert row.currency == "EUR"
    assert row.mtd_cost == pytest.approx(0.00924704425751627)
    # Prepaid balance is the number that actually causes an outage.
    assert "13.11" in row.note


def test_bunny_costs_degrades_to_empty_on_api_failure() -> None:
    """One provider failing must not abort a cross-cloud sweep."""
    with patch("granny.bunny.billing.BunnyBillingClient", side_effect=RuntimeError("401")):
        assert bunny_costs() == []


# ---------------------------------------------------------------------------
# Scaleway
# ---------------------------------------------------------------------------


def test_scaleway_costs_totals_consumption_net_of_discounts() -> None:
    client = MagicMock()
    client.organization_id = "org-1"
    client.month_to_date.return_value = (2.04, "EUR")

    with patch("granny.scaleway.billing.ScalewayBillingClient", return_value=client):
        rows = scaleway_costs()

    assert len(rows) == 1
    assert rows[0].provider == "scaleway"
    assert rows[0].account == "org-1"
    assert rows[0].mtd_cost == pytest.approx(2.04)
    assert "no per-resource split" in rows[0].note


def test_scaleway_costs_degrades_to_empty_on_api_failure() -> None:
    with patch(
        "granny.scaleway.billing.ScalewayBillingClient", side_effect=RuntimeError("403")
    ):
        assert scaleway_costs() == []


def test_month_to_date_refuses_to_total_mixed_currencies() -> None:
    """Summing EUR and USD into one float would be a silently wrong number."""
    from granny.scaleway.billing import ScalewayBillingClient

    client = ScalewayBillingClient.__new__(ScalewayBillingClient)
    with patch.object(
        ScalewayBillingClient,
        "consumptions",
        return_value=[
            {"value": {"units": 1, "nanos": 0, "currency_code": "EUR"}},
            {"value": {"units": 1, "nanos": 0, "currency_code": "USD"}},
        ],
    ):
        with pytest.raises(RuntimeError, match="mixed currencies"):
            client.month_to_date()


def test_month_to_date_sums_and_reports_currency() -> None:
    from granny.scaleway.billing import ScalewayBillingClient

    client = ScalewayBillingClient.__new__(ScalewayBillingClient)
    with patch.object(
        ScalewayBillingClient,
        "consumptions",
        return_value=[
            {"value": {"units": 2, "nanos": 340000000, "currency_code": "EUR"}},
            {"value": {"units": 0, "nanos": -300000000, "currency_code": "EUR"}},
        ],
    ):
        total, currency = client.month_to_date()

    assert total == pytest.approx(2.04)
    assert currency == "EUR"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_paged_follows_pages_until_total_count_is_reached() -> None:
    from granny.serverless.scaleway import ScalewayFunctionsClient

    client = ScalewayFunctionsClient.__new__(ScalewayFunctionsClient)
    pages = [
        {"functions": [{"name": "a"}, {"name": "b"}], "total_count": 3},
        {"functions": [{"name": "c"}], "total_count": 3},
    ]
    with patch.object(ScalewayFunctionsClient, "_request", side_effect=pages) as req:
        items = client._paged("/functions", "functions", page_size=2)

    assert [i["name"] for i in items] == ["a", "b", "c"]
    assert req.call_count == 2


def test_paged_stops_on_an_empty_page_rather_than_looping() -> None:
    """A total_count that overstates the data must not spin forever."""
    from granny.serverless.scaleway import ScalewayFunctionsClient

    client = ScalewayFunctionsClient.__new__(ScalewayFunctionsClient)
    pages = [{"functions": [{"name": "a"}], "total_count": 99}, {"functions": []}]
    with patch.object(ScalewayFunctionsClient, "_request", side_effect=pages):
        assert client._paged("/functions", "functions") == [{"name": "a"}]


def test_bunny_dns_zones_unwrap_the_pagination_envelope() -> None:
    """/dnszone answers an envelope while /pullzone answers a bare list."""
    from granny.dns.bunny import BunnyDNSProvider

    client = BunnyDNSProvider.__new__(BunnyDNSProvider)
    pages = [
        {"Items": [{"Domain": "a.example"}], "HasMoreItems": True},
        {"Items": [{"Domain": "b.example"}], "HasMoreItems": False},
    ]
    with patch.object(BunnyDNSProvider, "_request", side_effect=pages):
        assert [z["Domain"] for z in client.list_zones()] == ["a.example", "b.example"]


# ---------------------------------------------------------------------------
# Containers client
# ---------------------------------------------------------------------------


def test_containers_client_targets_the_containers_api() -> None:
    """The subclass must not inherit the Functions base path."""
    from granny.serverless.scaleway import ScalewayContainersClient

    with patch("granny.serverless.scaleway.get_secret", return_value="x"):
        client = ScalewayContainersClient(region="fr-par")

    assert client._api_base.endswith("/containers/v1beta1/regions/fr-par")


def test_list_deployments_shows_functions_and_containers() -> None:
    functions = [{"name": "api", "status": "ready", "runtime": "node22", "memory_limit": 512}]
    containers = [{"name": "signal", "status": "error", "memory_limit": 1024}]

    fn_client = MagicMock()
    fn_client.list_all_functions.return_value = functions
    ct_client = MagicMock()
    ct_client.list_all_containers.return_value = containers

    with (
        patch("granny.cli.serverless._client", return_value=fn_client),
        patch("granny.cli.serverless._containers_client", return_value=ct_client),
    ):
        result = CliRunner().invoke(serverless, ["list-deployments"])

    assert result.exit_code == 0, result.output
    assert "api" in result.output
    assert "signal" in result.output
    assert "1 function(s), 1 container(s)" in result.output
    # A container in `error` is the whole reason to look at this table.
    assert "error" in result.output


# ---------------------------------------------------------------------------
# Bunny charge breakdown
# ---------------------------------------------------------------------------


def test_charge_breakdown_drops_zero_products_and_sorts_by_size() -> None:
    from granny.bunny.billing import BunnyBillingClient

    rows = BunnyBillingClient.charge_breakdown(
        {
            "Balance": 13.11,
            "ThisMonthCharges": 0.0092,
            "MonthlyChargesStorage": 0.0033,
            "MonthlyChargesEUTraffic": 0.0047,
            "MonthlyChargesDNS": 0.0,
            "MonthlyChargesOptimizer": 0,
        }
    )

    assert rows == [("Traffic (EU)", pytest.approx(0.0047)), ("Storage", pytest.approx(0.0033))]


def test_charge_breakdown_excludes_the_total() -> None:
    """ThisMonthCharges is the sum, not a line; including it would double it."""
    from granny.bunny.billing import BunnyBillingClient

    rows = BunnyBillingClient.charge_breakdown(
        {"ThisMonthCharges": 99.0, "MonthlyChargesStorage": 1.0}
    )

    assert [label for label, _ in rows] == ["Storage"]


def test_charge_breakdown_labels_an_unmapped_product_rather_than_dropping_it() -> None:
    """A product we have never used must not vanish from a bill we are paying."""
    from granny.bunny.billing import BunnyBillingClient

    rows = BunnyBillingClient.charge_breakdown({"MonthlyChargesQuantumFoo": 4.2})

    assert rows == [("QuantumFoo", pytest.approx(4.2))]
