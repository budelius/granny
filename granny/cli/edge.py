"""``granny edge`` — Bunny Edge Scripting management."""

from __future__ import annotations

from pathlib import Path

import click


def _client(customer: str | None = None):
    from granny.edge.bunny import BunnyEdgeScriptClient

    return BunnyEdgeScriptClient(customer=customer)


customer_option = click.option("--customer", "-c", default=None)


@click.group()
def edge() -> None:
    """Bunny Edge Scripting management."""


@edge.command("list")
@customer_option
def list_cmd(customer: str | None) -> None:
    """List all edge scripts."""
    c = _client(customer)
    scripts = c.list_scripts()
    if not scripts:
        click.echo("No edge scripts found.")
        return
    for s in scripts:
        click.echo(f"  {s.get('Id', '?'):<10} {s.get('Name', '?'):<30} Type={s.get('ScriptType', '?')}")


@edge.command("create")
@click.argument("name")
@click.option("--code", "code_path", type=click.Path(exists=True), default=None)
@click.option(
    "--type",
    "script_type",
    type=click.Choice(["standalone", "middleware"]),
    default="standalone",
    show_default=True,
)
@customer_option
def create_cmd(
    name: str, code_path: str | None, script_type: str, customer: str | None
) -> None:
    """Create an edge script."""
    c = _client(customer)
    code = Path(code_path).read_text(encoding="utf-8") if code_path else ""
    result = c.create_script(name, code=code, script_type=script_type)
    click.echo(f"Script created: ID={result.get('Id')} Name={result.get('Name')}")
    if result.get("LinkedPullZones"):
        pz = result["LinkedPullZones"][0]
        click.echo(f"Linked pull zone: ID={pz.get('Id')} Name={pz.get('Name')}")


@edge.command("deploy")
@click.argument("script_id", type=int)
@click.argument("code_path", type=click.Path(exists=True))
@customer_option
def deploy_cmd(script_id: int, code_path: str, customer: str | None) -> None:
    """Deploy code to an edge script."""
    c = _client(customer)
    code = Path(code_path).read_text(encoding="utf-8")
    c.deploy_code(script_id, code)
    click.echo(f"Code deployed to script {script_id} ({len(code)} bytes).")


@edge.command("publish")
@click.argument("script_id", type=int)
@customer_option
def publish_cmd(script_id: int, customer: str | None) -> None:
    """Publish the current code as a live release."""
    c = _client(customer)
    c.publish(script_id)
    click.echo(f"Script {script_id} published.")


@edge.command("env")
@click.argument("script_id", type=int)
@click.argument("pairs", nargs=-1)
@customer_option
def env_cmd(script_id: int, pairs: tuple[str, ...], customer: str | None) -> None:
    """Set environment variables (KEY=VALUE pairs).

    If no pairs given, lists current variables.
    """
    c = _client(customer)
    if not pairs:
        variables = c.list_variables(script_id)
        if not variables:
            click.echo("No variables set.")
            return
        for v in variables:
            click.echo(f"  {v.get('Name', '?')}={v.get('DefaultValue', '')}")
        return
    for pair in pairs:
        if "=" not in pair:
            raise click.UsageError(f"Invalid format: {pair!r}. Use KEY=VALUE.")
        key, _, value = pair.partition("=")
        c.upsert_variable(script_id, key, value)
        click.echo(f"  Set {key}")
