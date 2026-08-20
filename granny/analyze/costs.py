"""Month-to-date cost and end-of-month forecast per cloud account.

  * **AWS** -- Cost Explorer ``get_cost_and_usage`` + ``get_cost_forecast``.
    Cost Explorer must be enabled on the account; first call may 400 if
    it isn't.
  * **GCP** -- Cloud Billing API exposes pricing but not per-account spend;
    real spend lives in the BigQuery export. We return billing-account
    metadata and a hint pointing at the export.
  * **Azure** -- Cost Management ``query.usage`` per subscription.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class CostSummary:
    provider: str
    account: str
    currency: str
    period_start: str
    period_end: str
    mtd_cost: float | None
    forecast_eom: float | None
    note: str


def _month_bounds(today: date | None = None) -> tuple[date, date, date]:
    """Return (month_start, today, month_end_exclusive)."""
    today = today or date.today()
    start = today.replace(day=1)
    if today.month == 12:
        end = today.replace(year=today.year + 1, month=1, day=1)
    else:
        end = today.replace(month=today.month + 1, day=1)
    return start, today, end


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


def _ce_client(profile: str):  # noqa: ANN202  (botocore client type is dynamic)
    """Build a Cost Explorer client for a named AWS profile (us-east-1 global endpoint)."""
    import boto3

    session = boto3.Session(profile_name=profile if profile != "default" else None)
    return session.client("ce", region_name="us-east-1")


@dataclass
class ServiceCostHistory:
    account: str
    currency: str
    months: list[str]  # "YYYY-MM" labels, oldest -> newest
    by_service: dict[str, list[float]]  # service -> per-month amount aligned to `months`


def _history_bounds(months: int, today: date) -> tuple[date, date]:
    """First-of-month `months` ago (inclusive) .. first-of-current-month (exclusive)."""
    start = today.replace(day=1)
    for _ in range(months):
        start = (start - timedelta(days=1)).replace(day=1)
    end = today.replace(day=1)
    return start, end


def aws_costs_by_service(
    profile: str, months: int = 3, *, today: date | None = None
) -> ServiceCostHistory:
    """Per-service monthly UnblendedCost for the last `months` complete months."""
    from botocore.exceptions import BotoCoreError, ClientError

    today = today or date.today()
    start, end = _history_bounds(months, today)
    ce = _ce_client(profile)

    currency = "USD"
    labels: list[str] = []
    per_month: list[dict[str, float]] = []
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        for bucket in resp.get("ResultsByTime", []):
            label = bucket.get("TimePeriod", {}).get("Start", "")[:7]
            labels.append(label)
            month_map: dict[str, float] = {}
            for group in bucket.get("Groups", []):
                keys = group.get("Keys", [])
                svc = keys[0] if keys else "unknown"
                metric = group.get("Metrics", {}).get("UnblendedCost", {})
                month_map[svc] = float(metric.get("Amount", 0.0))
                currency = metric.get("Unit", currency)
            per_month.append(month_map)
    except (BotoCoreError, ClientError) as e:
        logger.warning("AWS %s cost-by-service: %s", profile, e)

    services = sorted({svc for m in per_month for svc in m})
    by_service = {
        svc: [per_month[i].get(svc, 0.0) for i in range(len(labels))] for svc in services
    }
    return ServiceCostHistory(
        account=profile, currency=currency, months=labels, by_service=by_service
    )


def aws_cost_total(profile: str, start: date, end: date) -> float | None:
    """Total UnblendedCost over [start, end); None on error."""
    from botocore.exceptions import BotoCoreError, ClientError

    ce = _ce_client(profile)
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        total = 0.0
        for bucket in resp.get("ResultsByTime", []):
            total += float(bucket.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0.0))
        return total
    except (BotoCoreError, ClientError) as e:
        logger.warning("AWS %s cost total: %s", profile, e)
        return None


def aws_costs(profiles: list[str] | None = None) -> list[CostSummary]:
    """Month-to-date spend + month-end forecast via Cost Explorer."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    from granny.analyze.gpus import aws_profiles

    profiles = profiles or aws_profiles()
    start, today, end = _month_bounds()
    out: list[CostSummary] = []

    for profile in profiles:
        session = boto3.Session(profile_name=profile if profile != "default" else None)
        ce = session.client("ce", region_name="us-east-1")

        mtd: float | None = None
        currency = "USD"
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            result = resp.get("ResultsByTime", [])
            if result:
                amount = result[0].get("Total", {}).get("UnblendedCost", {})
                mtd = float(amount.get("Amount", 0.0))
                currency = amount.get("Unit", "USD")
        except (BotoCoreError, ClientError) as e:
            logger.warning("AWS %s cost MTD: %s", profile, e)

        forecast: float | None = None
        try:
            forecast_end = end - timedelta(days=1)
            if forecast_end > today:
                resp = ce.get_cost_forecast(
                    TimePeriod={"Start": today.isoformat(), "End": forecast_end.isoformat()},
                    Granularity="MONTHLY",
                    Metric="UNBLENDED_COST",
                )
                forecast = (
                    float(resp.get("Total", {}).get("Amount", 0.0)) + (mtd or 0.0)
                )
        except (BotoCoreError, ClientError) as e:
            logger.warning("AWS %s cost forecast: %s", profile, e)

        out.append(
            CostSummary(
                provider="aws",
                account=profile,
                currency=currency,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                mtd_cost=mtd,
                forecast_eom=forecast,
                note="Cost Explorer must be enabled on the account",
            )
        )
    return out


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


def gcp_costs(billing_accounts: list[str] | None = None) -> list[CostSummary]:
    """Return billing-account metadata; real spend is in BigQuery export."""
    try:
        from google.cloud import billing_v1
    except ImportError as e:
        raise RuntimeError(
            "GCP support requires the [gcp] extra: pip install 'granny-devops[gcp]'"
        ) from e

    client = billing_v1.CloudBillingClient()
    start, _, end = _month_bounds()
    accounts = list(client.list_billing_accounts()) if not billing_accounts else [
        client.get_billing_account(name=a if a.startswith("billingAccounts/") else f"billingAccounts/{a}")
        for a in billing_accounts
    ]
    return [
        CostSummary(
            provider="gcp",
            account=ba.name,
            currency=ba.currency_code or "USD",
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            mtd_cost=None,
            forecast_eom=None,
            note=(
                "Cloud Billing API has no spend endpoint -- enable the BigQuery "
                "billing export and query gcp_billing_export_v1_* for actuals"
            ),
        )
        for ba in accounts
    ]


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------


def azure_costs(subscriptions: list[str] | None = None) -> list[CostSummary]:
    """Month-to-date spend via Cost Management ``query.usage``."""
    try:
        from azure.core.exceptions import HttpResponseError
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.costmanagement import CostManagementClient
        from azure.mgmt.costmanagement.models import (
            QueryAggregation,
            QueryDataset,
            QueryDefinition,
            QueryTimePeriod,
            TimeframeType,
        )
    except ImportError as e:
        raise RuntimeError(
            "Azure support requires the [azure] extra: pip install 'granny-devops[azure]'"
        ) from e

    from granny.analyze.gpus import azure_subscriptions

    credential = DefaultAzureCredential()
    subs = subscriptions or [sid for sid, _ in azure_subscriptions()]
    start, today, end = _month_bounds()
    out: list[CostSummary] = []

    for sub in subs:
        cm = CostManagementClient(credential)
        scope = f"/subscriptions/{sub}"
        mtd: float | None = None
        currency = "USD"
        try:
            query = QueryDefinition(
                type="ActualCost",
                timeframe=TimeframeType.CUSTOM,
                time_period=QueryTimePeriod(
                    from_property=datetime.combine(start, datetime.min.time()),
                    to=datetime.combine(today, datetime.min.time()),
                ),
                dataset=QueryDataset(
                    granularity="None",
                    aggregation={
                        "totalCost": QueryAggregation(name="Cost", function="Sum"),
                    },
                ),
            )
            resp = cm.query.usage(scope=scope, parameters=query)
            for row in resp.rows or []:
                # rows: [cost, currency, ...] -- column order from resp.columns
                cols = [c.name for c in (resp.columns or [])]
                cost_idx = cols.index("totalCost") if "totalCost" in cols else 0
                curr_idx = cols.index("Currency") if "Currency" in cols else -1
                mtd = float(row[cost_idx])
                if curr_idx >= 0:
                    currency = row[curr_idx]
        except HttpResponseError as e:
            logger.warning("Azure sub %s cost: %s", sub, e)

        out.append(
            CostSummary(
                provider="azure",
                account=sub,
                currency=currency,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                mtd_cost=mtd,
                forecast_eom=None,
                note="Forecast via separate Cost Management Forecast API (not surfaced yet)",
            )
        )
    return out
