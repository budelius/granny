"""Bunny account billing client.

Bunny is prepaid: money goes in as a balance and the month draws it down. So
``ThisMonthCharges`` is real month-to-date spend, and ``Balance`` is the number
that decides whether the CDN keeps serving next week.

Per-product costs are only partly available. Each pull zone reports its own
``MonthlyCharges``, but storage, DNS and per-region traffic are billed as
account-level totals with no per-zone split. Callers that want a cost per
storage zone cannot have one.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_bunny_api_key

logger = logging.getLogger(__name__)

API_BASE = "https://api.bunny.net"

# The `/billing` payload carries ~30 MonthlyCharges* fields, nearly all zero for
# any given account. This maps the ones worth naming onto human labels; the
# rest are reported generically if they turn out to be non-zero, so a product
# we have never used does not silently vanish from the breakdown.
CHARGE_LABELS = {
    "MonthlyChargesStorage": "Storage",
    "MonthlyChargesDNS": "DNS",
    "MonthlyChargesEUTraffic": "Traffic (EU)",
    "MonthlyChargesUSTraffic": "Traffic (US)",
    "MonthlyChargesASIATraffic": "Traffic (Asia)",
    "MonthlyChargesSATraffic": "Traffic (South America)",
    "MonthlyChargesAFTraffic": "Traffic (Africa)",
    "MonthlyChargesOptimizer": "Optimizer",
    "MonthlyChargesShield": "Shield",
    "MonthlyChargesScripting": "Edge Scripting",
    "MonthlyChargesLoadBalancer": "Load Balancer",
    "MonthlyChargesMagicContainers": "Magic Containers",
    "MonthlyChargesAiGateway": "AI Gateway",
    "MonthlyChargesDB": "Database",
    "MonthlyChargesExtraPullZones": "Extra pull zones",
    "MonthlyChargesExtraStorageZones": "Extra storage zones",
    "MonthlyChargesExtraDnsZones": "Extra DNS zones",
    "MonthlyChargesTaxes": "Taxes",
}

# Not a charge -- it is the sum of them, and adding it to a breakdown would
# double the total.
_TOTAL_FIELD = "ThisMonthCharges"


class BunnyBillingClient:
    """Account balance and month-to-date charges."""

    def __init__(self, api_key: str | None = None, customer: str | None = None) -> None:
        api_key = api_key or get_bunny_api_key(customer)
        if not api_key:
            label = f"BUNNY_API_KEY_{customer.upper()}" if customer else "BUNNY_API_KEY"
            raise ValueError(f"{label} not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update({"AccessKey": api_key, "Content-Type": "application/json"})

    def _request(self, method: str, path: str) -> Any:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(url, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Bunny {method} {path}: {resp.status_code} {resp.text}")
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def get_billing(self) -> dict:
        """Raw ``/billing`` payload: balance, totals and per-product charges."""
        return self._request("GET", "/billing")

    def month_to_date(self, billing: dict | None = None) -> float:
        billing = self.get_billing() if billing is None else billing
        return float(billing.get(_TOTAL_FIELD) or 0.0)

    def balance(self, billing: dict | None = None) -> float:
        billing = self.get_billing() if billing is None else billing
        return float(billing.get("Balance") or 0.0)

    @staticmethod
    def charge_breakdown(billing: dict) -> list[tuple[str, float]]:
        """Non-zero per-product charges, largest first.

        Zero-valued products are dropped rather than rendered as ``0.00``: on
        an account with thirty charge fields and six in use, listing them all
        buries the six that matter.
        """
        out: list[tuple[str, float]] = []
        for key, value in billing.items():
            if not key.startswith("MonthlyCharges") or not value:
                continue
            label = CHARGE_LABELS.get(key)
            if label is None:
                # An unmapped product we are apparently paying for. Better a
                # clumsy label than a missing line.
                label = key.removeprefix("MonthlyCharges")
            out.append((label, float(value)))
        return sorted(out, key=lambda pair: -pair[1])
