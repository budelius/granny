"""Azure Virtual Machine inventory and lifecycle operations.

List, deallocate, and delete VMs via the Resource Manager REST API. Routed
through ``granny.azure._client`` to stay consistent with the rest of the
``granny.azure`` package (deployment, account, webapp).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from granny.azure import _client

_API_VERSION = "2024-07-01"


@dataclass
class AzureVM:
    subscription: str
    resource_group: str | None
    name: str
    location: str | None
    vm_size: str | None
    power_state: str | None
    provisioning_state: str | None
    os_type: str | None
    id: str
    tags: dict[str, str]
    raw: dict[str, Any]


def list_vms(subscription: str) -> list[AzureVM]:
    """List every VM in a subscription, including power state.

    Uses ``$expand=instanceView`` so the power state arrives in the same
    paginated response rather than one extra call per VM.
    """
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/providers/Microsoft.Compute/virtualMachines"
        f"?api-version={_API_VERSION}&$expand=instanceView"
    )
    return [
        _parse_vm(subscription=subscription, item=item)
        for item in _client.paginate(url)
    ]


def delete_vm(
    subscription: str,
    resource_group: str,
    name: str,
    *,
    force: bool = False,
) -> int:
    """Delete a VM. Returns the HTTP status (202 = accepted/async).

    The VM's attached disks, NICs, and public IPs are NOT removed -- Azure
    only deletes the VM resource itself. ``force`` maps to forceDeletion.
    """
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{name}"
        f"?api-version={_API_VERSION}"
    )
    if force:
        url += "&forceDeletion=true"
    return _client.delete(url)


def deallocate_vm(subscription: str, resource_group: str, name: str) -> int:
    """Stop and deallocate a VM so compute billing stops. Returns HTTP status.

    Unlike a plain stop, deallocation releases the underlying hardware so the
    sponsorship/credit meter for compute stops. Storage is still billed.
    """
    url = (
        f"{_client.MANAGEMENT_URL}/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{name}/deallocate"
        f"?api-version={_API_VERSION}"
    )
    return _client.post(url)


def _parse_vm(subscription: str, item: dict[str, Any]) -> AzureVM:
    properties = item.get("properties", {}) if isinstance(item, dict) else {}
    hardware = properties.get("hardwareProfile", {}) or {}
    storage = properties.get("storageProfile", {}) or {}
    os_disk = storage.get("osDisk", {}) or {}
    resource_id = item.get("id", "")
    return AzureVM(
        subscription=subscription,
        resource_group=_resource_group_from_id(resource_id),
        name=item.get("name", ""),
        location=item.get("location"),
        vm_size=hardware.get("vmSize"),
        power_state=_power_state(properties.get("instanceView", {}) or {}),
        provisioning_state=properties.get("provisioningState"),
        os_type=os_disk.get("osType"),
        id=resource_id,
        tags=dict(item.get("tags", {}) or {}),
        raw=item,
    )


def _power_state(instance_view: dict[str, Any]) -> str | None:
    for status in instance_view.get("statuses", []) or []:
        code = status.get("code", "")
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return None


def _resource_group_from_id(resource_id: str) -> str | None:
    parts = resource_id.lower().split("/")
    try:
        idx = parts.index("resourcegroups")
    except ValueError:
        return None
    if idx + 1 < len(parts):
        return resource_id.split("/")[idx + 1]
    return None
