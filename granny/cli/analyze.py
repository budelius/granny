"""CLI commands for cross-cloud resource analysis."""

import json

import click


@click.group()
def analyze() -> None:
    """Analyze and inventory cloud resources (AWS, GCP, Azure)."""


@analyze.command()
@click.option("--region", multiple=True, help="Specific region(s) to query (default: all)")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def lambdas(region: tuple[str, ...], as_json: bool) -> None:
    """List Lambda functions across AWS regions."""
    from granny.analyze.lambdas import list_lambdas

    regions = list(region) if region else None
    results = list_lambdas(regions=regions)

    if not results:
        click.echo("No Lambda functions found.")
        return

    if as_json:
        click.echo(json.dumps([vars(r) for r in results], indent=2))
        return

    current_region = None
    for fn in results:
        if fn.region != current_region:
            current_region = fn.region
            click.echo(f"\nRegion: {current_region}")
            click.echo("-" * 70)
            click.echo(f"{'Function Name':<40} {'Runtime':<15} {'Version':<10}")
            click.echo("-" * 70)
        click.echo(f"{fn.name:<40} {fn.runtime:<15} {fn.version:<10}")


@analyze.command()
@click.option("--region", multiple=True, help="Specific region(s) to query")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def vpcs(region: tuple[str, ...], as_json: bool) -> None:
    """List VPCs in the AWS account."""
    from granny.analyze.vpcs import list_vpcs

    regions = list(region) if region else None
    results = list_vpcs(regions=regions)

    if not results:
        click.echo("No VPCs found.")
        return

    if as_json:
        click.echo(json.dumps([vars(r) for r in results], indent=2))
        return

    click.echo(f"\n{'VPC ID':<20} {'CIDR Block':<18} {'Name':<30} {'Default':<10} {'Region'}")
    click.echo("-" * 95)
    for vpc in results:
        click.echo(
            f"{vpc.vpc_id:<20} {vpc.cidr_block:<18} {vpc.name:<30} "
            f"{str(vpc.is_default):<10} {vpc.region}"
        )


# ---------------------------------------------------------------------------
# Cross-cloud GPU / credits / cost commands
# ---------------------------------------------------------------------------

_PROVIDERS = ("aws", "gcp", "azure")


def _parse_filter(filter_str: str | None) -> set[str] | None:
    if not filter_str:
        return None
    return {p.strip().upper() for p in filter_str.split(",") if p.strip()}


@analyze.command()
@click.option(
    "--provider",
    type=click.Choice([*_PROVIDERS, "all"]),
    default="all",
    help="Restrict to one cloud (default: all three)",
)
@click.option("--profile", "profiles", multiple=True, help="AWS profile (repeatable; default: all)")
@click.option("--project", "projects", multiple=True, help="GCP project (repeatable; default: all)")
@click.option(
    "--subscription",
    "subscriptions",
    multiple=True,
    help="Azure subscription ID (repeatable; default: all)",
)
@click.option("--region", "regions", multiple=True, help="AWS region (default: all)")
@click.option(
    "--filter",
    "gpu_filter",
    default=None,
    help="Comma-separated GPU models to show (e.g. 'h100,h200,a100')",
)
@click.option("--include-stopped", is_flag=True, help="Include non-running instances")
@click.option("--include-reserved", is_flag=True, help="Also list capacity reservations / RIs")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def gpus(
    provider: str,
    profiles: tuple[str, ...],
    projects: tuple[str, ...],
    subscriptions: tuple[str, ...],
    regions: tuple[str, ...],
    gpu_filter: str | None,
    include_stopped: bool,
    include_reserved: bool,
    as_json: bool,
) -> None:
    """List GPU instances (and optionally reservations) across clouds."""
    from dataclasses import asdict

    from granny.analyze import gpus as g

    targets = _PROVIDERS if provider == "all" else (provider,)
    wanted = _parse_filter(gpu_filter)

    instances: list = []
    reservations: list = []

    for p in targets:
        try:
            if p == "aws":
                instances.extend(
                    g.list_aws_gpu_instances(
                        profiles=list(profiles) or None,
                        regions=list(regions) or None,
                        include_stopped=include_stopped,
                    )
                )
                if include_reserved:
                    reservations.extend(
                        g.list_aws_gpu_reservations(
                            profiles=list(profiles) or None,
                            regions=list(regions) or None,
                        )
                    )
            elif p == "gcp":
                instances.extend(
                    g.list_gcp_gpu_instances(
                        projects=list(projects) or None,
                        include_stopped=include_stopped,
                    )
                )
                if include_reserved:
                    reservations.extend(
                        g.list_gcp_gpu_reservations(projects=list(projects) or None)
                    )
            elif p == "azure":
                instances.extend(
                    g.list_azure_gpu_instances(
                        subscriptions=list(subscriptions) or None,
                        include_stopped=include_stopped,
                    )
                )
                if include_reserved:
                    reservations.extend(
                        g.list_azure_gpu_reservations(
                            subscriptions=list(subscriptions) or None
                        )
                    )
        except RuntimeError as e:
            click.echo(f"[{p}] skipped: {e}", err=True)

    if wanted:
        instances = [i for i in instances if i.gpu_model.upper() in wanted]
        reservations = [r for r in reservations if r.gpu_model.upper() in wanted]

    if as_json:
        click.echo(
            json.dumps(
                {
                    "instances": [asdict(i) for i in instances],
                    "reservations": [asdict(r) for r in reservations],
                },
                indent=2,
                default=str,
            )
        )
        return

    if not instances and not reservations:
        click.echo("No GPU resources found.")
        return

    if instances:
        click.echo(
            f"\n{'Provider':<8} {'Account':<24} {'Region':<16} {'Type':<28} "
            f"{'GPU':<7} {'#':>3} {'State':<10} {'Name'}"
        )
        click.echo("-" * 120)
        for i in sorted(
            instances, key=lambda x: (x.provider, x.account, x.region, x.gpu_model)
        ):
            click.echo(
                f"{i.provider:<8} {i.account[:23]:<24} {i.region:<16} "
                f"{i.instance_type[:27]:<28} {i.gpu_model:<7} {i.gpu_count:>3} "
                f"{i.state[:9]:<10} {i.instance_name}"
            )
        totals: dict[str, int] = {}
        for i in instances:
            key = f"{i.provider}/{i.gpu_model}"
            totals[key] = totals.get(key, 0) + i.gpu_count
        click.echo("\nGPU totals (running):")
        for k in sorted(totals):
            click.echo(f"  {k:<20} {totals[k]:>5}")

    if reservations:
        click.echo(
            f"\n{'Provider':<8} {'Account':<24} {'Region':<16} {'Type':<28} "
            f"{'GPU':<7} {'#Inst':>5} {'State':<10} {'Term'}"
        )
        click.echo("-" * 120)
        for r in sorted(reservations, key=lambda x: (x.provider, x.account, x.region)):
            click.echo(
                f"{r.provider:<8} {r.account[:23]:<24} {r.region:<16} "
                f"{r.instance_type[:27]:<28} {r.gpu_model:<7} "
                f"{r.instance_count:>5} {r.state[:9]:<10} {r.commitment_term or ''}"
            )


@analyze.command()
@click.option(
    "--provider",
    type=click.Choice([*_PROVIDERS, "all"]),
    default="all",
    help="Restrict to one cloud (default: all three)",
)
@click.option("--profile", "profiles", multiple=True, help="AWS profile (repeatable)")
@click.option(
    "--billing-account",
    "billing_accounts",
    multiple=True,
    help="GCP billing account ID (repeatable)",
)
@click.option(
    "--subscription", "subscriptions", multiple=True, help="Azure subscription ID (repeatable)"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def credits(
    provider: str,
    profiles: tuple[str, ...],
    billing_accounts: tuple[str, ...],
    subscriptions: tuple[str, ...],
    as_json: bool,
) -> None:
    """Show available credit balances per provider where APIs allow."""
    from dataclasses import asdict

    from granny.analyze import credits as c

    targets = _PROVIDERS if provider == "all" else (provider,)
    rows: list = []
    for p in targets:
        try:
            if p == "aws":
                rows.extend(c.aws_credits(profiles=list(profiles) or None))
            elif p == "gcp":
                rows.extend(c.gcp_credits(billing_accounts=list(billing_accounts) or None))
            elif p == "azure":
                rows.extend(c.azure_credits(subscriptions=list(subscriptions) or None))
        except RuntimeError as e:
            click.echo(f"[{p}] skipped: {e}", err=True)

    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No credit information available.")
        return

    click.echo(f"\n{'Provider':<8} {'Account':<40} {'Currency':<8} {'Available':>14} {'Note'}")
    click.echo("-" * 120)
    for r in rows:
        avail = f"{r.available:>14,.2f}" if r.available is not None else f"{'n/a':>14}"
        click.echo(f"{r.provider:<8} {r.account[:39]:<40} {r.currency:<8} {avail} {r.note}")


@analyze.command()
@click.option(
    "--provider",
    type=click.Choice([*_PROVIDERS, "all"]),
    default="all",
    help="Restrict to one cloud (default: all three)",
)
@click.option("--profile", "profiles", multiple=True, help="AWS profile (repeatable)")
@click.option(
    "--billing-account",
    "billing_accounts",
    multiple=True,
    help="GCP billing account ID (repeatable)",
)
@click.option(
    "--subscription", "subscriptions", multiple=True, help="Azure subscription ID (repeatable)"
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def costs(
    provider: str,
    profiles: tuple[str, ...],
    billing_accounts: tuple[str, ...],
    subscriptions: tuple[str, ...],
    as_json: bool,
) -> None:
    """Show month-to-date spend and end-of-month forecast per account."""
    from dataclasses import asdict

    from granny.analyze import costs as co

    targets = _PROVIDERS if provider == "all" else (provider,)
    rows: list = []
    for p in targets:
        try:
            if p == "aws":
                rows.extend(co.aws_costs(profiles=list(profiles) or None))
            elif p == "gcp":
                rows.extend(co.gcp_costs(billing_accounts=list(billing_accounts) or None))
            elif p == "azure":
                rows.extend(co.azure_costs(subscriptions=list(subscriptions) or None))
        except RuntimeError as e:
            click.echo(f"[{p}] skipped: {e}", err=True)

    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No cost information available.")
        return

    click.echo(
        f"\n{'Provider':<8} {'Account':<40} {'Currency':<8} {'MTD':>14} "
        f"{'Forecast EOM':>16} {'Note'}"
    )
    click.echo("-" * 130)
    for r in rows:
        mtd = f"{r.mtd_cost:>14,.2f}" if r.mtd_cost is not None else f"{'n/a':>14}"
        fc = f"{r.forecast_eom:>16,.2f}" if r.forecast_eom is not None else f"{'n/a':>16}"
        click.echo(
            f"{r.provider:<8} {r.account[:39]:<40} {r.currency:<8} {mtd} {fc} {r.note}"
        )
