"""Scaleway billing consumption client.

Scaleway reports spend per SKU per project, never per resource. A project
running fourteen functions and three containers gets one
``Serverless Function - Requests`` line, not seventeen lines. Callers that want
a cost per deployment cannot have one, and should say so rather than dividing
the total by the resource count.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

API_BASE = "https://api.scaleway.com"

# Scaleway encodes money the way Google's protobuf Money does: an integer unit
# count plus a nanos remainder, both carrying the same sign. Discounts (the
# serverless free tier, most visibly) arrive as negative lines, so the sum over
# a period is already net of them.
_NANOS_PER_UNIT = 1_000_000_000


def amount_of(value: dict) -> float:
    """Convert a Scaleway ``{units, nanos}`` money object to a float."""
    return (value.get("units") or 0) + (value.get("nanos") or 0) / _NANOS_PER_UNIT


class ScalewayBillingClient:
    """Organization-scoped billing consumption.

    ``organization_id`` is optional. Scaleway's own CLI config registers a
    project id far more often than an organization id, so when one is not
    supplied and ``SCW_DEFAULT_ORGANIZATION_ID`` is unset, it is looked up from
    the project instead of being demanded as a new credential.
    """

    def __init__(
        self,
        *,
        secret_key: str | None = None,
        project_id: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        self.secret_key = secret_key or get_secret("SCW_SECRET_KEY")
        self.project_id = project_id or get_secret("SCW_DEFAULT_PROJECT_ID")
        if not self.secret_key:
            raise ValueError("Missing Scaleway credentials. Set SCW_SECRET_KEY (env or vault).")

        self.session = requests.Session()
        self.session.headers.update(
            {"X-Auth-Token": self.secret_key, "Content-Type": "application/json"}
        )
        self.organization_id = (
            organization_id
            or get_secret("SCW_DEFAULT_ORGANIZATION_ID")
            or self._organization_from_project()
        )

    def _request(self, method: str, path: str) -> Any:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(url, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Scaleway {method} {path}: {resp.status_code} {resp.text}")
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def _organization_from_project(self) -> str:
        if not self.project_id:
            raise ValueError(
                "Cannot determine the Scaleway organization. Set "
                "SCW_DEFAULT_ORGANIZATION_ID, or SCW_DEFAULT_PROJECT_ID to "
                "derive it from."
            )
        project = self._request("GET", f"/account/v3/projects/{self.project_id}")
        organization_id = project.get("organization_id")
        if not organization_id:
            raise RuntimeError(
                f"Scaleway project {self.project_id} reported no organization_id."
            )
        return organization_id

    def consumptions(self) -> list[dict]:
        """Month-to-date consumption, one entry per SKU per project.

        Already net of discounts: the free tier arrives as its own negative
        line rather than as an adjustment to the lines it offsets.
        """
        payload = self._request(
            "GET", f"/billing/v2beta1/consumptions?organization_id={self.organization_id}"
        )
        return payload.get("consumptions", [])

    def month_to_date(self) -> tuple[float, str]:
        """Total month-to-date spend and its currency.

        The currency comes from the line items rather than being assumed:
        an account billed in something other than EUR would otherwise be
        reported under the wrong symbol.
        """
        consumptions = self.consumptions()
        total = sum(amount_of(c.get("value", {})) for c in consumptions)
        currencies = {
            c.get("value", {}).get("currency_code") for c in consumptions
        } - {None, ""}
        if len(currencies) > 1:
            raise RuntimeError(
                f"Scaleway returned mixed currencies {sorted(currencies)}; "
                "a single total would be meaningless."
            )
        return total, (currencies.pop() if currencies else "EUR")
