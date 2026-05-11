"""Surface available cloud credits / balances per provider.

What each cloud actually exposes:

  * **AWS** -- no public API. The credits balance lives in Billing Console
    only. The function returns a structured "unavailable" result with a
    pointer to the console so callers can render it without special-casing.
  * **GCP** -- the Cloud Billing API exposes billing-account state and open
    promotions, but the running credit *balance* lives in the BigQuery
    billing export. We return the per-billing-account state + any active
    promo info we can read.
  * **Azure** -- Consumption Balances API works for Enterprise Agreement
    and Microsoft Customer Agreement accounts and returns a real number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CreditBalance:
    provider: str
    account: str
    currency: str
    available: float | None
    note: str  # short context (e.g. "EA balance", "see console")


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


def aws_credits(profiles: list[str] | None = None) -> list[CreditBalance]:
    """AWS has no public balance API. Return a stub per profile."""
    from granny.analyze.gpus import aws_profiles

    profiles = profiles or aws_profiles()
    out: list[CreditBalance] = []
    for p in profiles:
        out.append(
            CreditBalance(
                provider="aws",
                account=p,
                currency="USD",
                available=None,
                note="No public balance API -- see https://console.aws.amazon.com/billing/home#/credits",
            )
        )
    return out


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


def gcp_credits(billing_accounts: list[str] | None = None) -> list[CreditBalance]:
    """Return per-billing-account state + a hint about the BigQuery export.

    The real per-account credit balance lives in the BigQuery billing export
    (table ``gcp_billing_export_v1_*``, column ``credits``). Without read
    access to that table, the API only exposes the account's open/closed
    state plus master-account info.
    """
    try:
        from google.cloud import billing_v1
    except ImportError as e:
        raise RuntimeError(
            "GCP support requires the [gcp] extra: pip install 'granny-devops[gcp]'"
        ) from e

    client = billing_v1.CloudBillingClient()
    out: list[CreditBalance] = []
    accounts = list(client.list_billing_accounts()) if not billing_accounts else [
        client.get_billing_account(name=a if a.startswith("billingAccounts/") else f"billingAccounts/{a}")
        for a in billing_accounts
    ]
    for ba in accounts:
        out.append(
            CreditBalance(
                provider="gcp",
                account=ba.name,
                currency=ba.currency_code or "USD",
                available=None,
                note=(
                    f"open={ba.open}; master={ba.master_billing_account or 'none'}; "
                    "balance lives in BigQuery billing export"
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------


def azure_credits(subscriptions: list[str] | None = None) -> list[CreditBalance]:
    """Query Consumption Balances per subscription's enrollment.

    Works for Enterprise Agreement and Microsoft Customer Agreement billing.
    Pay-as-you-go subscriptions don't expose a balance and return an
    "unavailable" stub.
    """
    try:
        from azure.core.exceptions import HttpResponseError
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.billing import BillingManagementClient
        from azure.mgmt.consumption import ConsumptionManagementClient
    except ImportError as e:
        raise RuntimeError(
            "Azure support requires the [azure] extra: pip install 'granny-devops[azure]'"
        ) from e

    from granny.analyze.gpus import azure_subscriptions

    credential = DefaultAzureCredential()
    subs = subscriptions or [sid for sid, _ in azure_subscriptions()]
    out: list[CreditBalance] = []

    # Try EA-style balances first (needs an enrollment account number).
    try:
        billing = BillingManagementClient(credential, subscription_id=subs[0] if subs else "")
        for ea in billing.enrollment_accounts.list():
            try:
                consumption = ConsumptionManagementClient(credential, subscription_id=subs[0])
                bal = consumption.balances.get_by_billing_account(billing_account_id=ea.name)
                out.append(
                    CreditBalance(
                        provider="azure",
                        account=f"EA/{ea.name}",
                        currency=bal.currency or "USD",
                        available=float(bal.new_purchases) if bal.new_purchases is not None else None,
                        note=f"EA balance; total_overage={bal.total_overage}",
                    )
                )
            except HttpResponseError as e:
                logger.debug("Azure EA balance %s: %s", ea.name, e)
    except HttpResponseError as e:
        logger.debug("Azure enrollment accounts: %s", e)
    except Exception as e:  # noqa: BLE001 -- EA APIs are notoriously 403-prone
        logger.debug("Azure EA path skipped: %s", e)

    # Per-subscription fallback: report the subscription itself with no balance.
    seen_ea = {b.account for b in out}
    for sub in subs:
        if f"sub/{sub}" in seen_ea:
            continue
        out.append(
            CreditBalance(
                provider="azure",
                account=f"sub/{sub}",
                currency="USD",
                available=None,
                note="Per-subscription balance not exposed (PAYG) -- check EA/MCA billing scope",
            )
        )
    return out
