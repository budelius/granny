"""Forward-looking GPU cluster cost estimation for AWS.

Pulls live on-demand rates from the AWS Pricing API and effective hourly
rates from Capacity Block offerings via ``describe_capacity_block_offerings``,
then multiplies by hours and node count.

The Pricing API runs only in ``us-east-1`` and ``ap-south-1`` but returns
prices for every region. It does not need any non-default IAM permissions
in most accounts. Capacity Block discovery does need an authenticated
profile.

Compute dominates an H100/H200 cluster's monthly bill by 2+ orders of
magnitude (one ``p5en.48xlarge`` is ~$98/hr on-demand; the 100GB EBS root
is ~$8/mo). We do not surface storage/network/data-transfer here -- treat
the number as ``+/- 5%``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# AWS Pricing API uses human-readable "location" strings rather than
# region codes. This covers the regions where p5e / p5en (H200) are sold.
_PRICING_LOCATIONS: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-north-1": "Europe (Stockholm)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ca-central-1": "Canada (Central)",
}


@dataclass
class GpuClusterCostEstimate:
    """Per-region, per-pricing-plan estimate for a GPU cluster.

    ``hourly_per_node`` is the canonical figure; ``monthly_*`` are derived
    by multiplying by ``hours_per_month`` and ``node_count``. ``None`` means
    the underlying API returned nothing (region unsupported, no capacity).
    """

    instance_type: str
    region: str
    node_count: int
    plan: str  # 'ondemand' or 'capacity-block'
    hourly_per_node: float | None
    monthly_per_node: float | None
    monthly_total: float | None
    hours_per_month: float
    currency: str
    notes: str


def get_aws_ondemand_hourly(
    instance_type: str,
    region: str,
    profile: str | None = None,
) -> float | None:
    """Return the on-demand USD/hour price for a Linux instance in the region.

    Returns ``None`` if the Pricing API has no matching offer (typically
    means the instance type is not yet sold in that region).
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    location = _PRICING_LOCATIONS.get(region)
    if not location:
        raise ValueError(
            f"Unmapped Pricing API location for region {region!r}; "
            f"add it to _PRICING_LOCATIONS"
        )

    session = boto3.Session(profile_name=profile if profile and profile != "default" else None)
    pricing = session.client("pricing", region_name="us-east-1")

    filters = [
        {"Type": "TERM_MATCH", "Field": "ServiceCode", "Value": "AmazonEC2"},
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "marketoption", "Value": "OnDemand"},
    ]
    try:
        resp = pricing.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=20)
    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"AWS Pricing API call failed: {e}") from e

    for raw in resp.get("PriceList", []):
        doc = json.loads(raw)
        for term in doc.get("terms", {}).get("OnDemand", {}).values():
            for dim in term.get("priceDimensions", {}).values():
                price = dim.get("pricePerUnit", {}).get("USD")
                if price and float(price) > 0:
                    return float(price)
    return None


def _capacity_block_hourly(
    instance_type: str,
    node_count: int,
    region: str,
    profile: str | None,
) -> tuple[float | None, str]:
    """Return ``(effective_hourly_per_node, note)`` from cheapest current block."""
    from granny.analyze.gpus import list_aws_capacity_block_offerings

    offerings = list_aws_capacity_block_offerings(
        instance_type=instance_type,
        instance_count=node_count,
        duration_hours=24,
        start_window_days=30,
        profiles=[profile] if profile else None,
        regions=[region],
    )
    if not offerings:
        return None, "No Capacity Block offerings in the next 30 days"

    cheapest = offerings[0]
    hours = cheapest.duration_hours or 24
    nodes = cheapest.instance_count or node_count or 1
    per_node_hour = cheapest.upfront_fee / (nodes * hours)
    note = (
        f"From cheapest 24h block: ${cheapest.upfront_fee:,.2f} upfront for "
        f"{cheapest.instance_count}x at {cheapest.start_time[:10]}"
    )
    return per_node_hour, note


def estimate_cluster_monthly(
    instance_type: str,
    node_count: int,
    region: str,
    plan: str = "ondemand",
    hours_per_month: float = 730.0,
    profile: str | None = None,
) -> GpuClusterCostEstimate:
    """Estimate the monthly cost of a GPU cluster in one region under one plan.

    Args:
        instance_type: EC2 instance type, e.g. ``p5en.48xlarge`` (H200 x8).
        node_count: Number of nodes the cluster runs at full active load.
        region: AWS region code.
        plan: ``"ondemand"`` (Pricing API) or ``"capacity-block"`` (derives
            an effective hourly from the cheapest 24h block currently sold).
        hours_per_month: 730 = full 24/7 month; 168 = one week per month;
            etc. Use this to model partial-month usage.
        profile: AWS profile (needed only for ``"capacity-block"``).
    """
    notes: list[str] = []
    hourly: float | None = None

    if plan == "ondemand":
        hourly = get_aws_ondemand_hourly(instance_type, region, profile)
        if hourly is None:
            notes.append(
                f"Pricing API has no on-demand offer for {instance_type} in {region}"
            )
    elif plan == "capacity-block":
        hourly, cb_note = _capacity_block_hourly(instance_type, node_count, region, profile)
        notes.append(cb_note)
    else:
        raise ValueError(
            f"plan must be 'ondemand' or 'capacity-block', got {plan!r}"
        )

    monthly_per_node = hourly * hours_per_month if hourly is not None else None
    monthly_total = monthly_per_node * node_count if monthly_per_node is not None else None

    return GpuClusterCostEstimate(
        instance_type=instance_type,
        region=region,
        node_count=node_count,
        plan=plan,
        hourly_per_node=hourly,
        monthly_per_node=monthly_per_node,
        monthly_total=monthly_total,
        hours_per_month=hours_per_month,
        currency="USD",
        notes="; ".join(n for n in notes if n),
    )
