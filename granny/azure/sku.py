"""Azure available VM size lookup.

The Microsoft.Compute ``vmSizes`` endpoint is region-scoped and lighter than
the wider ``skus`` endpoint, so this module exposes it as the primary view of
"what VM sizes can I deploy right now."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_VM_SIZES_API_VERSION = "2024-07-01"
_LOCATIONS_API_VERSION = "2022-12-01"


@dataclass
class VmSize:
    subscription: str
    location: str
    name: str
    number_of_cores: int | None
    memory_in_mb: int | None
    max_data_disk_count: int | None
    os_disk_size_in_mb: int | None
    resource_disk_size_in_mb: int | None
    raw: dict[str, Any]


def list_vm_sizes(subscription: str, location: str) -> list[VmSize]:
    """List VM sizes available for deployment in one region."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/providers/Microsoft.Compute/locations/{location}/vmSizes"
        f"?api-version={_VM_SIZES_API_VERSION}"
    )
    return [
        _parse_vm_size(subscription=subscription, location=location, item=item)
        for item in _client.paginate(url)
    ]


def list_locations(subscription: str) -> list[str]:
    """Return the location codes available to a subscription."""
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}/locations"
        f"?api-version={_LOCATIONS_API_VERSION}"
    )
    return [
        item.get("name", "")
        for item in _client.paginate(url)
        if item.get("name")
    ]


def _parse_vm_size(
    subscription: str,
    location: str,
    item: dict[str, Any],
) -> VmSize:
    return VmSize(
        subscription=subscription,
        location=location,
        name=item.get("name", ""),
        number_of_cores=item.get("numberOfCores"),
        memory_in_mb=item.get("memoryInMB"),
        max_data_disk_count=item.get("maxDataDiskCount"),
        os_disk_size_in_mb=item.get("osDiskSizeInMB"),
        resource_disk_size_in_mb=item.get("resourceDiskSizeInMB"),
        raw=item,
    )
