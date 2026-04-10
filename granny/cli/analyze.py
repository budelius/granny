"""CLI commands for AWS resource analysis."""

import json
import sys

import click


@click.group()
def analyze() -> None:
    """Analyze and inventory AWS resources."""


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
