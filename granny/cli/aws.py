"""CLI commands for AWS operations."""

import json
from contextlib import contextmanager
from collections.abc import Iterator

import click


@contextmanager
def _friendly_errors() -> Iterator[None]:
    """Convert botocore failures into clean click errors (no tracebacks)."""
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        ProfileNotFound,
    )

    try:
        yield
    except ClientError as exc:
        err = exc.response.get("Error", {})
        code = err.get("Code", "")
        msg = err.get("Message", str(exc))
        if code in ("AccessDenied", "AccessDeniedException"):
            raise click.ClickException(
                "Access denied: this profile's IAM principal lacks the required "
                "servicequotas permission.\n  " + msg
            ) from exc
        raise click.ClickException(f"AWS error [{code}]: {msg}") from exc
    except (NoCredentialsError, ProfileNotFound, BotoCoreError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def aws() -> None:
    """Manage AWS resources."""


@aws.group()
def quota() -> None:
    """Inspect and request AWS service-quota limits."""


def _resolve(quota_ref: str, service_code: str | None) -> tuple[str, str]:
    """Resolve a friendly alias or raw quota code to (service_code, quota_code)."""
    from granny.aws.quota import resolve_alias

    svc, code = resolve_alias(quota_ref)
    if service_code:
        svc = service_code
    return svc, code


@quota.command("show")
@click.argument("quota_ref", default="gpu")
@click.option("--service-code", default=None, help="Override service code (default: ec2)")
@click.option("--profile", default=None, help="AWS profile name")
@click.option("--region", default=None, help="AWS region, e.g. eu-central-1")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def quota_show(
    quota_ref: str,
    service_code: str | None,
    profile: str | None,
    region: str | None,
    as_json: bool,
) -> None:
    """Show one AWS service quota.

    QUOTA_REF is a friendly alias (gpu, p, standard, inf) or a raw quota
    code (e.g. L-DB2E81BA). Defaults to `gpu` (Running On-Demand G and VT
    instances).
    """
    from dataclasses import asdict

    from granny.aws.quota import get_quota

    svc, code = _resolve(quota_ref, service_code)
    with _friendly_errors():
        row = get_quota(service_code=svc, quota_code=code, profile=profile, region=region)
    if as_json:
        click.echo(json.dumps(asdict(row), indent=2, default=str))
        return

    click.echo(f"Quota name:  {row.quota_name}")
    click.echo(f"Service:     {row.service_code}")
    click.echo(f"Quota code:  {row.quota_code}")
    click.echo(f"Region:      {row.region}")
    value = "n/a" if row.value is None else str(row.value)
    click.echo(f"Value:       {value} {row.unit or ''}".rstrip())
    click.echo(f"Adjustable:  {'yes' if row.adjustable else 'no'}")


@quota.command("request")
@click.argument("quota_ref", default="gpu")
@click.option("--value", "desired_value", required=True, type=float, help="Requested limit")
@click.option("--service-code", default=None, help="Override service code (default: ec2)")
@click.option("--profile", default=None, help="AWS profile name")
@click.option("--region", default=None, help="AWS region, e.g. eu-central-1")
@click.option("--dry-run", is_flag=True, help="Show the request without submitting it")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def quota_request(
    quota_ref: str,
    desired_value: float,
    service_code: str | None,
    profile: str | None,
    region: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Submit an AWS service-quota increase request.

    Example: granny aws quota request gpu --value 8 --profile lularge --region eu-central-1
    """
    from dataclasses import asdict

    from granny.aws.quota import get_quota, request_increase

    svc, code = _resolve(quota_ref, service_code)
    with _friendly_errors():
        before = get_quota(service_code=svc, quota_code=code, profile=profile, region=region)

    if dry_run:
        payload = {
            "service_code": svc,
            "quota_code": code,
            "quota_name": before.quota_name,
            "region": before.region,
            "current_value": before.value,
            "desired_value": desired_value,
            "adjustable": before.adjustable,
            "would_submit": True,
        }
        if as_json:
            click.echo(json.dumps(payload, indent=2, default=str))
        else:
            click.echo(
                f"Would request {before.quota_name} ({code}) in {before.region}: "
                f"{before.value} -> {desired_value}"
            )
        return

    if not before.adjustable:
        raise click.ClickException(
            f"Quota {code} ({before.quota_name}) is not adjustable via the API."
        )

    with _friendly_errors():
        result = request_increase(
            service_code=svc,
            quota_code=code,
            desired_value=desired_value,
            profile=profile,
            region=region,
        )
    if as_json:
        click.echo(json.dumps(asdict(result), indent=2, default=str))
        return

    click.echo(
        f"Submitted request for {result.quota_name} ({code}) in {result.region}: "
        f"{before.value} -> {result.desired_value}"
    )
    click.echo(f"Status:     {result.status or '-'}")
    click.echo(f"Request ID: {result.request_id or '-'}")
    if result.case_id:
        click.echo(f"Case ID:    {result.case_id}")


@quota.command("status")
@click.argument("quota_ref", default="gpu")
@click.option("--service-code", default=None, help="Override service code (default: ec2)")
@click.option("--profile", default=None, help="AWS profile name")
@click.option("--region", default=None, help="AWS region, e.g. eu-central-1")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def quota_status(
    quota_ref: str,
    service_code: str | None,
    profile: str | None,
    region: str | None,
    as_json: bool,
) -> None:
    """Show the increase-request history for one quota."""
    from dataclasses import asdict

    from granny.aws.quota import list_requested_changes

    svc, code = _resolve(quota_ref, service_code)
    with _friendly_errors():
        rows = list_requested_changes(
            service_code=svc, quota_code=code, profile=profile, region=region
        )
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo(f"No quota change requests found for {code}.")
        return

    click.echo(f"\n{'Status':<14} {'Desired':>10}  Request ID")
    click.echo("-" * 60)
    for row in rows:
        click.echo(f"{(row.status or '-'):<14} {row.desired_value:>10}  {row.request_id or '-'}")
