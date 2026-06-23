"""Unit tests for Azure VM and RBAC parsing helpers (no network)."""

from granny.azure.role import _parse_assignment
from granny.azure.vm import _parse_vm, _power_state, _resource_group_from_id


class TestParseVm:
    def test_full_vm(self):
        item = {
            "id": (
                "/subscriptions/sub1/resourceGroups/rg-prod"
                "/providers/Microsoft.Compute/virtualMachines/web-1"
            ),
            "name": "web-1",
            "location": "polandcentral",
            "tags": {"env": "prod"},
            "properties": {
                "provisioningState": "Succeeded",
                "hardwareProfile": {"vmSize": "Standard_D2s_v3"},
                "storageProfile": {"osDisk": {"osType": "Linux"}},
                "instanceView": {
                    "statuses": [
                        {"code": "ProvisioningState/succeeded"},
                        {"code": "PowerState/running"},
                    ]
                },
            },
        }
        vm = _parse_vm(subscription="sub1", item=item)
        assert vm.name == "web-1"
        assert vm.resource_group == "rg-prod"
        assert vm.vm_size == "Standard_D2s_v3"
        assert vm.power_state == "running"
        assert vm.os_type == "Linux"
        assert vm.location == "polandcentral"
        assert vm.tags == {"env": "prod"}

    def test_missing_instance_view(self):
        item = {
            "id": (
                "/subscriptions/s/resourceGroups/g"
                "/providers/Microsoft.Compute/virtualMachines/x"
            ),
            "name": "x",
            "properties": {"hardwareProfile": {"vmSize": "Standard_B1s"}},
        }
        vm = _parse_vm(subscription="s", item=item)
        assert vm.power_state is None
        assert vm.vm_size == "Standard_B1s"


class TestPowerState:
    def test_extracts_power_state(self):
        iv = {"statuses": [{"code": "PowerState/deallocated"}]}
        assert _power_state(iv) == "deallocated"

    def test_none_when_absent(self):
        assert _power_state({"statuses": [{"code": "ProvisioningState/succeeded"}]}) is None
        assert _power_state({}) is None


class TestResourceGroupFromId:
    def test_case_insensitive_match_preserves_original_case(self):
        rid = (
            "/subscriptions/s/resourceGroups/RG-Mixed"
            "/providers/Microsoft.Compute/virtualMachines/v"
        )
        assert _resource_group_from_id(rid) == "RG-Mixed"

    def test_no_group(self):
        assert _resource_group_from_id("/subscriptions/s") is None


class TestParseAssignment:
    def test_role_name_is_definition_guid_tail(self):
        item = {
            "name": "assign-guid",
            "id": "/subscriptions/s/.../roleAssignments/assign-guid",
            "properties": {
                "scope": "/subscriptions/s",
                "roleDefinitionId": (
                    "/subscriptions/s/providers/Microsoft.Authorization"
                    "/roleDefinitions/acdd72a7-3385-48ef-bd42-f606fba81ae7"
                ),
                "principalId": "user-oid",
                "principalType": "User",
            },
        }
        ra = _parse_assignment(item)
        assert ra.principal_id == "user-oid"
        assert ra.principal_type == "User"
        assert ra.role_name == "acdd72a7-3385-48ef-bd42-f606fba81ae7"
        assert ra.scope == "/subscriptions/s"
