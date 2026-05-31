"""AWS Service Quotas inspection and increase-request helpers.

Thin boto3 wrapper around the `service-quotas` API. Read paths (`get`,
`status`) need `servicequotas:GetServiceQuota` /
`servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota`; the write
path (`request`) needs `servicequotas:RequestServiceQuotaIncrease`.

A small registry of commonly-needed EC2 quota codes is included so callers
can say `--gpu` instead of memorising `L-DB2E81BA`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3

# Friendly aliases -> (service_code, quota_code). EC2 on-demand vCPU quotas.
# https://docs.aws.amazon.com/ec2/latest/userguide/ec2-resource-limits.html
QUOTA_ALIASES: dict[str, tuple[str, str]] = {
    # G and VT instances (NVIDIA L4 / A10G / T4 etc) -- the one that gates g6/g5.
    "gpu": ("ec2", "L-DB2E81BA"),
    "g-and-vt": ("ec2", "L-DB2E81BA"),
    # P instances (A100/H100 etc).
    "p": ("ec2", "L-417A185B"),
    # Standard on-demand (A/C/D/H/I/M/R/T/Z).
    "standard": ("ec2", "L-1216C47A"),
    # Inf (Inferentia).
    "inf": ("ec2", "L-1945791B"),
}


@dataclass
class ServiceQuota:
    service_code: str
    quota_code: str
    quota_name: str
    value: float | None
    adjustable: bool
    unit: str | None
    region: str
    raw: dict[str, Any]


@dataclass
class QuotaChangeRequest:
    service_code: str
    quota_code: str
    quota_name: str
    desired_value: float
    status: str | None
    request_id: str | None
    case_id: str | None
    region: str
    raw: dict[str, Any]


def resolve_alias(alias_or_code: str) -> tuple[str, str]:
    """Map a friendly alias (``gpu``) to ``(service_code, quota_code)``.

    If the input already looks like a raw quota code (``L-...``) it is
    returned against the default ``ec2`` service.
    """
    key = alias_or_code.strip().lower()
    if key in QUOTA_ALIASES:
        return QUOTA_ALIASES[key]
    return ("ec2", alias_or_code.strip())


def _client(profile: str | None, region: str | None) -> Any:
    session = boto3.Session(
        profile_name=profile if profile and profile != "default" else None,
        region_name=region,
    )
    return session.client("service-quotas")


def get_quota(
    *,
    service_code: str,
    quota_code: str,
    profile: str | None = None,
    region: str | None = None,
) -> ServiceQuota:
    """Return the current (applied) value for one quota."""
    client = _client(profile, region)
    resolved_region = client.meta.region_name
    resp = client.get_service_quota(ServiceCode=service_code, QuotaCode=quota_code)
    q = resp.get("Quota", {})
    return ServiceQuota(
        service_code=service_code,
        quota_code=quota_code,
        quota_name=q.get("QuotaName", ""),
        value=q.get("Value"),
        adjustable=bool(q.get("Adjustable", False)),
        unit=q.get("Unit"),
        region=resolved_region,
        raw=q,
    )


def request_increase(
    *,
    service_code: str,
    quota_code: str,
    desired_value: float,
    profile: str | None = None,
    region: str | None = None,
) -> QuotaChangeRequest:
    """Submit a quota-increase request. Idempotency is AWS-side: a pending
    request for the same quota will be rejected by the API."""
    client = _client(profile, region)
    resolved_region = client.meta.region_name
    resp = client.request_service_quota_increase(
        ServiceCode=service_code,
        QuotaCode=quota_code,
        DesiredValue=desired_value,
    )
    r = resp.get("RequestedQuota", {})
    return QuotaChangeRequest(
        service_code=service_code,
        quota_code=quota_code,
        quota_name=r.get("QuotaName", ""),
        desired_value=r.get("DesiredValue", desired_value),
        status=r.get("Status"),
        request_id=r.get("Id"),
        case_id=r.get("CaseId"),
        region=resolved_region,
        raw=r,
    )


def list_requested_changes(
    *,
    service_code: str,
    quota_code: str,
    profile: str | None = None,
    region: str | None = None,
) -> list[QuotaChangeRequest]:
    """Return the change-request history for one quota (most recent first)."""
    client = _client(profile, region)
    resolved_region = client.meta.region_name
    resp = client.list_requested_service_quota_change_history_by_quota(
        ServiceCode=service_code,
        QuotaCode=quota_code,
    )
    out: list[QuotaChangeRequest] = []
    for r in resp.get("RequestedQuotas", []):
        out.append(
            QuotaChangeRequest(
                service_code=service_code,
                quota_code=quota_code,
                quota_name=r.get("QuotaName", ""),
                desired_value=r.get("DesiredValue", 0.0),
                status=r.get("Status"),
                request_id=r.get("Id"),
                case_id=r.get("CaseId"),
                region=resolved_region,
                raw=r,
            )
        )
    return out
