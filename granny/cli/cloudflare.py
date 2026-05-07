"""``granny cloudflare`` — Workers / D1 / R2 resource management."""

from __future__ import annotations

import json as _json

import click


@click.group("cloudflare")
def cloudflare() -> None:
    """Cloudflare Workers / D1 / R2 / KV resource management.

    For DNS-only operations see ``granny dns`` (Cloudflare is one of the
    supported providers there). This group handles the account-level
    resources that Workers-based apps depend on.
    """


# -- D1 -----------------------------------------------------------------------


@cloudflare.group("d1")
def d1_group() -> None:
    """D1 database management."""


@d1_group.command("list")
def d1_list() -> None:
    """List D1 databases in the account."""
    from granny.cloudflare.d1 import D1Client

    for db in D1Client().list_databases():
        click.echo(f"  {db.get('uuid', '?'):<38} {db.get('name', '?')}")


@d1_group.command("create")
@click.argument("name")
@click.option("--location", default=None, help="Primary location hint (e.g. weur).")
def d1_create(name: str, location: str | None) -> None:
    """Create a D1 database (idempotent)."""
    from granny.cloudflare.d1 import D1Client

    db = D1Client().create_database(name, primary_location_hint=location)
    click.echo(f"D1 database: {db['name']} uuid={db['uuid']}")


# -- R2 -----------------------------------------------------------------------


@cloudflare.group("r2")
def r2_group() -> None:
    """R2 bucket management."""


@r2_group.command("list")
def r2_list() -> None:
    """List R2 buckets in the account."""
    from granny.cloudflare.r2 import R2Client

    for b in R2Client().list_buckets():
        click.echo(f"  {b.get('name'):<40} created={b.get('creation_date', '?')}")


@r2_group.command("create")
@click.argument("name")
@click.option("--location", default=None, help="Location hint (e.g. WEUR).")
def r2_create(name: str, location: str | None) -> None:
    """Create an R2 bucket (idempotent)."""
    from granny.cloudflare.r2 import R2Client

    b = R2Client().create_bucket(name, location=location)
    click.echo(f"R2 bucket: {b.get('name')}")


# -- Workers secrets ----------------------------------------------------------


@cloudflare.group("workers")
def workers_group() -> None:
    """Workers script + secrets management."""


@workers_group.command("list")
def workers_list() -> None:
    """List Worker scripts in the account."""
    from granny.cloudflare.workers import WorkersClient

    for s in WorkersClient().list_scripts():
        click.echo(f"  {s.get('id'):<40} modified={s.get('modified_on', '?')}")


@workers_group.command("secret-put")
@click.argument("script_name")
@click.argument("secret_name")
@click.option(
    "--from-vault",
    is_flag=True,
    help="Resolve SECRET_NAME via granny credentials (vault/env) instead of prompting.",
)
@click.option("--value", default=None, help="Plaintext value (avoid in shell history).")
def workers_secret_put(
    script_name: str, secret_name: str, from_vault: bool, value: str | None
) -> None:
    """Create or update a Worker secret."""
    from granny.cloudflare.workers import WorkersClient

    if value is None:
        if from_vault:
            from granny.credentials.secrets import get_secret

            value = get_secret(secret_name)
            if not value:
                raise click.ClickException(f"Vault lookup for {secret_name} returned nothing.")
        else:
            value = click.prompt(f"Value for {secret_name}", hide_input=True)
    WorkersClient().put_secret(script_name, secret_name, value)
    click.echo(f"Secret set: {script_name}.{secret_name}")


@workers_group.command("secret-list")
@click.argument("script_name")
def workers_secret_list(script_name: str) -> None:
    """List Worker secret names (values are not retrievable)."""
    from granny.cloudflare.workers import WorkersClient

    for s in WorkersClient().list_secrets(script_name):
        click.echo(f"  {s.get('name'):<32} type={s.get('type', '?')}")


# -- Composite: Workers site provisioning -------------------------------------


@cloudflare.group("site")
def site_group() -> None:
    """Workers site provisioning (D1 + R2 + secrets in one pass)."""


@site_group.command("provision")
@click.argument("site_name")
@click.option(
    "--media-bucket",
    default=None,
    help="R2 bucket name (default: <site>-media).",
)
@click.option(
    "--d1-location",
    default=None,
    help="D1 primary location hint (e.g. weur).",
)
@click.option(
    "--secret",
    "secrets",
    multiple=True,
    help="Secret to push (format: NAME or NAME=VALUE). Repeatable.",
)
@click.option(
    "--secret-from-vault",
    "vault_secrets",
    multiple=True,
    help="Secret name to resolve via granny vault and push. Repeatable.",
)
@click.option("--json", "json_out", is_flag=True, help="Emit JSON summary.")
def site_provision(
    site_name: str,
    media_bucket: str | None,
    d1_location: str | None,
    secrets: tuple[str, ...],
    vault_secrets: tuple[str, ...],
    json_out: bool,
) -> None:
    """One-shot provisioning for a Workers site.

    Creates (idempotently) the D1 database, R2 bucket, and pushes the
    requested secrets to the Worker. The Worker script itself must already
    have been deployed once via ``wrangler deploy`` -- this command does not
    upload code. Typical flow::

        wrangler deploy                                      # first deploy
        granny cloudflare site provision my-site \\
          --secret-from-vault MAILJET_API_KEY \\
          --secret-from-vault MAILJET_SECRET_KEY \\
          --secret AUTH_SECRET=$(openssl rand -hex 32)
    """
    from granny.cloudflare.d1 import D1Client
    from granny.cloudflare.r2 import R2Client
    from granny.cloudflare.workers import WorkersClient
    from granny.credentials.secrets import get_secret

    bucket = media_bucket or f"{site_name}-media"

    summary: dict[str, object] = {"site": site_name}

    db = D1Client().create_database(site_name, primary_location_hint=d1_location)
    summary["d1"] = {"name": db["name"], "uuid": db["uuid"]}

    b = R2Client().create_bucket(bucket)
    summary["r2"] = {"name": b.get("name", bucket)}

    pushed: list[str] = []
    w = WorkersClient()
    for spec in secrets:
        if "=" in spec:
            name, _, val = spec.partition("=")
        else:
            name = spec
            val = click.prompt(f"Value for {name}", hide_input=True)
        w.put_secret(site_name, name, val)
        pushed.append(name)
    for name in vault_secrets:
        val = get_secret(name)
        if not val:
            click.echo(f"warn: vault lookup for {name} returned empty; skipping", err=True)
            continue
        w.put_secret(site_name, name, val)
        pushed.append(name)
    summary["secrets_pushed"] = pushed

    if json_out:
        click.echo(_json.dumps(summary, indent=2))
    else:
        click.echo(f"D1:      {db['name']} ({db['uuid']})")
        click.echo(f"R2:      {summary['r2']['name']}")
        click.echo(f"Secrets: {', '.join(pushed) if pushed else '(none)'}")
        click.echo("Now paste the D1 uuid into your wrangler.jsonc and redeploy.")
