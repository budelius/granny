"""CLI commands for Azure operations."""

import functools
import json
from collections.abc import Callable
from typing import Any, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])


def _clean_errors(func: F) -> F:
    """Turn ARM RuntimeError/KeyError into a tidy click error (no traceback)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except KeyError as e:
            raise click.ClickException(str(e).strip("\"'")) from e
        except RuntimeError as e:
            raise click.ClickException(str(e)) from e

    return wrapper  # type: ignore[return-value]


@click.group()
def azure() -> None:
    """Manage Azure resources."""


@azure.group()
def quota() -> None:
    """Inspect and request Azure quota limits."""


@quota.command("list")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--location", required=True, help="Azure location, e.g. polandcentral")
@click.option("--filter", "filter_text", default=None, help="Filter quota rows by text")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def quota_list(
    subscription: str,
    location: str,
    filter_text: str | None,
    as_json: bool,
) -> None:
    """List compute quota limits for an Azure region."""
    from dataclasses import asdict

    from granny.azure.quota import list_compute_quotas

    rows = list_compute_quotas(
        subscription=subscription,
        location=location,
        filter_text=filter_text,
    )
    if as_json:
        click.echo(json.dumps([asdict(row) for row in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No matching Azure quota rows found.")
        return

    click.echo(f"\n{'Resource name':<42} {'Limit':>10} {'Unit':<8} Display name")
    click.echo("-" * 110)
    for row in rows:
        limit = "n/a" if row.limit is None else str(row.limit)
        click.echo(
            f"{row.resource_name:<42} {limit:>10} {(row.unit or ''):<8} {row.display_name}"
        )


@quota.command("show")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--location", required=True, help="Azure location, e.g. polandcentral")
@click.option("--resource-name", required=True, help="Quota resource name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def quota_show(
    subscription: str,
    location: str,
    resource_name: str,
    as_json: bool,
) -> None:
    """Show one Azure compute quota limit."""
    from dataclasses import asdict

    from granny.azure.quota import get_compute_quota

    row = get_compute_quota(
        subscription=subscription,
        location=location,
        resource_name=resource_name,
    )
    if as_json:
        click.echo(json.dumps(asdict(row), indent=2, default=str))
        return

    click.echo(f"Resource name: {row.resource_name}")
    click.echo(f"Display name:  {row.display_name}")
    click.echo(f"Subscription:  {row.subscription}")
    click.echo(f"Location:      {row.location}")
    click.echo(f"Limit:         {row.limit} {row.unit or ''}".rstrip())


@quota.command("request")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--location", required=True, help="Azure location, e.g. polandcentral")
@click.option("--resource-name", required=True, help="Quota resource name")
@click.option("--limit", "requested_limit", required=True, type=int, help="Requested limit")
@click.option(
    "--justification",
    default=None,
    help="Recorded in dry-run output; Azure's quota endpoint may not accept it",
)
@click.option("--dry-run", is_flag=True, help="Show the request without submitting it")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def quota_request(
    subscription: str,
    location: str,
    resource_name: str,
    requested_limit: int,
    justification: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Submit an Azure compute quota increase/update request."""
    from dataclasses import asdict

    from granny.azure.quota import get_compute_quota, request_compute_quota

    before = get_compute_quota(
        subscription=subscription,
        location=location,
        resource_name=resource_name,
    )

    if dry_run:
        payload = {
            "subscription": subscription,
            "location": location,
            "resource_name": resource_name,
            "current_limit": before.limit,
            "requested_limit": requested_limit,
            "justification": justification,
            "would_submit": True,
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2, default=str))
        else:
            click.echo(
                f"Would request {resource_name} in {location}: "
                f"{before.limit} -> {requested_limit}"
            )
        return

    result = request_compute_quota(
        subscription=subscription,
        location=location,
        resource_name=resource_name,
        limit=requested_limit,
    )
    if as_json:
        payload = asdict(result)
        payload["justification"] = justification
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    click.echo(
        f"Submitted Azure quota request for {resource_name} in {location}: "
        f"{before.limit} -> {requested_limit}"
    )
    click.echo(f"HTTP status: {result.status_code}")
    if justification:
        click.echo(
            "Justification note: Azure's quota API accepted the limit request endpoint; "
            "use the Portal if Azure requires a separate support justification."
        )
    if result.provisioning_state:
        click.echo(f"Provisioning state: {result.provisioning_state}")
    if result.operation_url:
        click.echo(f"Operation URL: {result.operation_url}")


# --- account / whoami -----------------------------------------------------


@azure.group()
def account() -> None:
    """Inspect the signed-in Azure identity and visible subscriptions."""


@account.command("whoami")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def account_whoami(as_json: bool) -> None:
    """Show the identity behind the current DefaultAzureCredential token."""
    from dataclasses import asdict

    from granny.azure.account import get_signed_in_identity

    identity = get_signed_in_identity()
    if as_json:
        click.echo(json.dumps(asdict(identity), indent=2, default=str))
        return

    click.echo(f"User principal: {identity.user_principal_name or '-'}")
    click.echo(f"Display name:   {identity.display_name or '-'}")
    click.echo(f"Object ID:      {identity.object_id or '-'}")
    click.echo(f"Tenant ID:      {identity.tenant_id or '-'}")
    click.echo(f"App ID:         {identity.app_id or '-'}")
    click.echo(f"Identity type:  {identity.identity_type or '-'}")
    click.echo(f"Audience:       {identity.audience or '-'}")


@account.command("list")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def account_list(as_json: bool) -> None:
    """List subscriptions visible to the signed-in identity."""
    from dataclasses import asdict

    from granny.azure.account import list_subscriptions

    rows = list_subscriptions()
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No subscriptions visible to the signed-in identity.")
        return

    click.echo(f"\n{'Subscription ID':<38} {'State':<10} Display name")
    click.echo("-" * 96)
    for row in rows:
        click.echo(
            f"{row.subscription_id:<38} {(row.state or ''):<10} {row.display_name}"
        )


@account.command("tenants")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def account_tenants(as_json: bool) -> None:
    """List tenants visible to the signed-in identity."""
    from dataclasses import asdict

    from granny.azure.account import list_tenants

    rows = list_tenants()
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No tenants visible to the signed-in identity.")
        return

    click.echo(f"\n{'Tenant ID':<38} {'Default domain':<40} Display name")
    click.echo("-" * 110)
    for row in rows:
        click.echo(
            f"{row.tenant_id:<38} {(row.default_domain or ''):<40} {row.display_name or ''}"
        )


# --- ARM deployments ------------------------------------------------------


@azure.group()
def deployment() -> None:
    """List ARM resource-group deployments."""


@deployment.command("list")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option(
    "--resource-group",
    default=None,
    help="Limit to a single resource group; omit for the whole subscription",
)
@click.option(
    "--all-groups",
    is_flag=True,
    help="Enumerate deployments per resource group plus subscription scope",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def deployment_list(
    subscription: str,
    resource_group: str | None,
    all_groups: bool,
    as_json: bool,
) -> None:
    """List ARM deployments at subscription or resource-group scope."""
    from dataclasses import asdict

    from granny.azure.deployment import (
        list_deployments,
        list_deployments_all_groups,
    )

    if all_groups and resource_group:
        raise click.UsageError("--all-groups and --resource-group are mutually exclusive")

    if all_groups:
        rows = list_deployments_all_groups(subscription=subscription)
    else:
        rows = list_deployments(
            subscription=subscription,
            resource_group=resource_group,
        )

    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No ARM deployments found.")
        return

    click.echo(
        f"\n{'Resource group':<28} {'State':<12} {'Mode':<10} {'Timestamp':<28} Name"
    )
    click.echo("-" * 120)
    for row in rows:
        click.echo(
            f"{(row.resource_group or '-'):<28} "
            f"{(row.provisioning_state or ''):<12} "
            f"{(row.mode or ''):<10} "
            f"{(row.timestamp or ''):<28} "
            f"{row.name}"
        )


# --- Cognitive Services (Azure OpenAI) deployments ------------------------


@azure.group()
def openai() -> None:
    """Cognitive Services accounts and model deployments (incl. Azure OpenAI)."""


@openai.command("accounts")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def openai_accounts(subscription: str, as_json: bool) -> None:
    """List Cognitive Services accounts in the subscription."""
    from dataclasses import asdict

    from granny.azure.openai import list_accounts

    rows = list_accounts(subscription=subscription)
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No Cognitive Services accounts found.")
        return

    click.echo(
        f"\n{'Name':<32} {'Kind':<18} {'Location':<16} {'Resource group':<28} Endpoint"
    )
    click.echo("-" * 130)
    for row in rows:
        click.echo(
            f"{row.name:<32} {(row.kind or ''):<18} "
            f"{(row.location or ''):<16} {(row.resource_group or ''):<28} "
            f"{row.endpoint or ''}"
        )


@openai.command("deployments")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option(
    "--kind",
    default=None,
    help="Filter accounts by kind, e.g. OpenAI / FormRecognizer / SpeechServices",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def openai_deployments(
    subscription: str,
    kind: str | None,
    as_json: bool,
) -> None:
    """List model deployments across every Cognitive Services account."""
    from dataclasses import asdict

    from granny.azure.openai import list_all_deployments

    rows = list_all_deployments(subscription=subscription, kind_filter=kind)
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No Cognitive Services model deployments found.")
        return

    click.echo(
        f"\n{'Account':<28} {'Deployment':<28} {'Model':<28} "
        f"{'Version':<10} {'SKU':<14} Capacity"
    )
    click.echo("-" * 130)
    for row in rows:
        capacity = "-" if row.sku_capacity is None else str(row.sku_capacity)
        click.echo(
            f"{row.account_name:<28} {row.name:<28} "
            f"{(row.model_name or ''):<28} {(row.model_version or ''):<10} "
            f"{(row.sku or ''):<14} {capacity}"
        )


@openai.command("keys")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--resource-group", required=True, help="Resource group name")
@click.option("--account", "account_name", required=True, help="Cognitive Services account")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def openai_keys(
    subscription: str,
    resource_group: str,
    account_name: str,
    as_json: bool,
) -> None:
    """Print the account's access keys (key1/key2)."""
    from granny.azure.openai import get_account_keys

    keys = get_account_keys(subscription, resource_group, account_name)
    if as_json:
        click.echo(json.dumps(keys, indent=2))
        return
    click.echo(f"key1: {keys.get('key1', '')}")
    click.echo(f"key2: {keys.get('key2', '')}")


@openai.group("deployment")
def openai_deployment() -> None:
    """Create Azure OpenAI model deployments."""


@openai_deployment.command("create")
@click.option("--subscription", required=True, help="Azure subscription ID (regular, not sponsorship)")
@click.option("--resource-group", required=True, help="Resource group name")
@click.option("--account", "account_name", required=True, help="Azure OpenAI (Cognitive Services) account")
@click.option("--name", "deployment_name", required=True, help="Deployment name callers request")
@click.option("--model", "model_name", required=True, help="Model, e.g. gpt-4o")
@click.option("--model-version", default=None, help="Model version, e.g. 2024-08-06")
@click.option("--model-format", default="OpenAI", show_default=True)
@click.option("--sku", "sku_name", default="GlobalStandard", show_default=True, help="Deployment SKU")
@click.option("--capacity", "sku_capacity", default=1, type=int, show_default=True, help="SKU capacity (thousands of TPM)")
@click.option("--dry-run", is_flag=True, help="Show the action without running it")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def openai_deployment_create(
    subscription: str,
    resource_group: str,
    account_name: str,
    deployment_name: str,
    model_name: str,
    model_version: str | None,
    model_format: str,
    sku_name: str,
    sku_capacity: int,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Create (or update) an Azure OpenAI model deployment.

    Billing follows the resource's subscription — pass the regular subscription
    to avoid spending sponsorship credit. JSON output includes the account
    ``endpoint`` so a caller can build a gateway route in one step.
    """
    from dataclasses import asdict

    from granny.azure.openai import create_deployment, get_account

    if dry_run:
        payload = {
            "would_create": deployment_name,
            "account": account_name,
            "resource_group": resource_group,
            "model": model_name,
            "model_version": model_version,
            "sku": sku_name,
            "capacity": sku_capacity,
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2, default=str))
        else:
            click.echo(
                f"Would create deployment {deployment_name!r} "
                f"({model_name} {model_version or ''}) in account {account_name!r}"
            )
        return

    dep = create_deployment(
        subscription=subscription,
        resource_group=resource_group,
        account_name=account_name,
        deployment_name=deployment_name,
        model_name=model_name,
        model_version=model_version,
        model_format=model_format,
        sku_name=sku_name,
        sku_capacity=sku_capacity,
    )
    account = get_account(subscription, resource_group, account_name)
    if as_json:
        out = asdict(dep)
        out["endpoint"] = account.endpoint
        click.echo(json.dumps(out, indent=2, default=str))
        return
    click.echo(
        f"Created deployment {dep.name} ({dep.model_name} {dep.model_version or ''}) "
        f"[{dep.provisioning_state or 'unknown'}]"
    )
    click.echo(f"Endpoint: {account.endpoint or '-'}")


# --- App Service deployments ----------------------------------------------


@azure.group()
def webapp() -> None:
    """Azure App Service (Microsoft.Web) sites and deployment history."""


@webapp.command("list")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def webapp_list(subscription: str, as_json: bool) -> None:
    """List App Service sites in the subscription."""
    from dataclasses import asdict

    from granny.azure.webapp import list_web_apps

    rows = list_web_apps(subscription=subscription)
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No App Service sites found.")
        return

    click.echo(
        f"\n{'Name':<32} {'Kind':<22} {'State':<12} {'Location':<16} Default host"
    )
    click.echo("-" * 130)
    for row in rows:
        click.echo(
            f"{row.name:<32} {(row.kind or ''):<22} "
            f"{(row.state or ''):<12} {(row.location or ''):<16} "
            f"{row.default_host_name or ''}"
        )


@webapp.command("deployments")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--resource-group", required=True, help="Resource group name")
@click.option("--app-name", required=True, help="App Service site name")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def webapp_deployments(
    subscription: str,
    resource_group: str,
    app_name: str,
    as_json: bool,
) -> None:
    """List the deployment history of a single App Service site."""
    from dataclasses import asdict

    from granny.azure.webapp import list_site_deployments

    rows = list_site_deployments(
        subscription=subscription,
        resource_group=resource_group,
        app_name=app_name,
    )
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo(f"No deployments found for {app_name}.")
        return

    click.echo(
        f"\n{'Deployment ID':<42} {'Status':<8} {'Active':<7} "
        f"{'Author':<24} Message"
    )
    click.echo("-" * 130)
    for row in rows:
        status = "-" if row.status is None else str(row.status)
        active = "-" if row.active is None else ("yes" if row.active else "no")
        click.echo(
            f"{row.deployment_id:<42} {status:<8} {active:<7} "
            f"{(row.author or ''):<24} {(row.message or '').splitlines()[0] if row.message else ''}"
        )


# --- Available VM sizes ---------------------------------------------------


@azure.group("vm-sizes")
def vm_sizes() -> None:
    """List available VM sizes per region (what you can deploy)."""


@vm_sizes.command("list")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--location", required=True, help="Azure location, e.g. polandcentral")
@click.option(
    "--filter",
    "filter_text",
    default=None,
    help="Substring filter on the size name",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def vm_sizes_list(
    subscription: str,
    location: str,
    filter_text: str | None,
    as_json: bool,
) -> None:
    """List available VM sizes in one region."""
    from dataclasses import asdict

    from granny.azure.sku import list_vm_sizes

    rows = list_vm_sizes(subscription=subscription, location=location)
    if filter_text:
        needle = filter_text.lower()
        rows = [r for r in rows if needle in r.name.lower()]

    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo(f"No VM sizes available in {location}.")
        return

    click.echo(f"\n{'Size':<32} {'Cores':>6} {'Memory (MB)':>12} {'Max disks':>10}")
    click.echo("-" * 70)
    for row in rows:
        cores = "-" if row.number_of_cores is None else str(row.number_of_cores)
        mem = "-" if row.memory_in_mb is None else str(row.memory_in_mb)
        disks = "-" if row.max_data_disk_count is None else str(row.max_data_disk_count)
        click.echo(f"{row.name:<32} {cores:>6} {mem:>12} {disks:>10}")


@vm_sizes.command("locations")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def vm_sizes_locations(subscription: str, as_json: bool) -> None:
    """List locations available to a subscription."""
    from granny.azure.sku import list_locations

    rows = list_locations(subscription=subscription)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return

    if not rows:
        click.echo("No locations available.")
        return

    for name in rows:
        click.echo(name)


# --- Virtual machines -----------------------------------------------------


@azure.group()
def vm() -> None:
    """List and remove Azure virtual machines."""


@vm.command("list")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option(
    "--filter",
    "filter_text",
    default=None,
    help="Substring filter on the VM name",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def vm_list(subscription: str, filter_text: str | None, as_json: bool) -> None:
    """List virtual machines in a subscription, with power state."""
    from dataclasses import asdict

    from granny.azure.vm import list_vms

    rows = list_vms(subscription=subscription)
    if filter_text:
        needle = filter_text.lower()
        rows = [r for r in rows if needle in r.name.lower()]

    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No virtual machines found.")
        return

    click.echo(
        f"\n{'Name':<28} {'Size':<22} {'Power':<14} "
        f"{'Location':<16} Resource group"
    )
    click.echo("-" * 120)
    for row in rows:
        click.echo(
            f"{row.name:<28} {(row.vm_size or '-'):<22} "
            f"{(row.power_state or '-'):<14} {(row.location or '-'):<16} "
            f"{row.resource_group or '-'}"
        )


@vm.command("deallocate")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--resource-group", required=True, help="Resource group name")
@click.option("--name", required=True, help="VM name")
@click.option("--dry-run", is_flag=True, help="Show the action without running it")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def vm_deallocate(
    subscription: str,
    resource_group: str,
    name: str,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Stop and deallocate a VM so compute billing stops (storage still bills)."""
    from granny.azure.vm import deallocate_vm

    if dry_run:
        msg = f"Would deallocate {name} in {resource_group}"
        click.echo(json.dumps({"would_deallocate": name}) if as_json else msg)
        return

    status = deallocate_vm(subscription, resource_group, name)
    if as_json:
        click.echo(json.dumps({"vm": name, "status": status}))
        return
    click.echo(f"Deallocate accepted for {name} (HTTP {status}).")


@vm.command("delete")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--resource-group", required=True, help="Resource group name")
@click.option("--name", required=True, help="VM name")
@click.option("--force", is_flag=True, help="Use Azure forceDeletion (skip graceful)")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--dry-run", is_flag=True, help="Show the action without running it")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def vm_delete(
    subscription: str,
    resource_group: str,
    name: str,
    force: bool,
    yes: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Delete a VM. Attached disks/NICs/public IPs are NOT removed."""
    from granny.azure.vm import delete_vm

    if dry_run:
        msg = f"Would delete VM {name} in {resource_group} (force={force})"
        click.echo(json.dumps({"would_delete": name, "force": force}) if as_json else msg)
        return

    if not yes and not click.confirm(
        f"Delete VM {name!r} in resource group {resource_group!r}? "
        "This cannot be undone"
    ):
        click.echo("Aborted.")
        return

    status = delete_vm(subscription, resource_group, name, force=force)
    if as_json:
        click.echo(json.dumps({"vm": name, "status": status}))
        return
    click.echo(f"Delete accepted for {name} (HTTP {status}).")


# --- RBAC role assignments ------------------------------------------------


@azure.group()
def role() -> None:
    """Inspect and grant Azure RBAC role assignments."""


@role.command("list")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--principal-id", default=None, help="Filter to one principal (object ID)")
@click.option("--scope", default=None, help="Explicit scope; default is the subscription")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def role_list(
    subscription: str,
    principal_id: str | None,
    scope: str | None,
    as_json: bool,
) -> None:
    """List role assignments at a scope, optionally for one principal."""
    from dataclasses import asdict

    from granny.azure.role import list_role_assignments

    rows = list_role_assignments(
        subscription=subscription, principal_id=principal_id, scope=scope
    )
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No role assignments found.")
        return

    click.echo(f"\n{'Principal ID':<38} {'Type':<16} {'Role definition ID':<38} Scope")
    click.echo("-" * 130)
    for row in rows:
        click.echo(
            f"{(row.principal_id or '-'):<38} {(row.principal_type or '-'):<16} "
            f"{(row.role_name or '-'):<38} {row.scope or '-'}"
        )


@role.command("assign")
@click.option("--subscription", required=True, help="Azure subscription ID")
@click.option("--role-name", required=True, help="Built-in role, e.g. 'Reader'")
@click.option(
    "--principal-id",
    default=None,
    help="Object ID to grant to; defaults to the signed-in identity",
)
@click.option(
    "--principal-type",
    default="User",
    type=click.Choice(["User", "Group", "ServicePrincipal"]),
    help="Principal type (default: User)",
)
@click.option("--scope", default=None, help="Explicit scope; default is the subscription")
@click.option("--dry-run", is_flag=True, help="Show the action without running it")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@_clean_errors
def role_assign(
    subscription: str,
    role_name: str,
    principal_id: str | None,
    principal_type: str,
    scope: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Grant a built-in role to a principal (defaults to your own account)."""
    from dataclasses import asdict

    from granny.azure.account import get_signed_in_identity
    from granny.azure.role import assign_role

    if not principal_id:
        principal_id = get_signed_in_identity().object_id
        if not principal_id:
            raise click.UsageError(
                "Could not resolve signed-in object ID; pass --principal-id"
            )

    target = scope or f"subscription {subscription}"
    if dry_run:
        payload = {
            "would_assign": role_name,
            "principal_id": principal_id,
            "principal_type": principal_type,
            "scope": target,
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(
                f"Would grant {role_name!r} to {principal_id} on {target}"
            )
        return

    result = assign_role(
        subscription=subscription,
        role_name=role_name,
        principal_id=principal_id,
        scope=scope,
        principal_type=principal_type,
    )
    if as_json:
        click.echo(json.dumps(asdict(result), indent=2, default=str))
        return
    click.echo(f"Granted {role_name!r} to {principal_id} on {target}.")
    click.echo(f"Assignment ID: {result.name}")
