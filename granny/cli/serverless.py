"""``granny serverless`` — Scaleway Functions (FaaS) management."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import click


def _client(region: str):
    from granny.serverless.scaleway import ScalewayFunctionsClient

    return ScalewayFunctionsClient(region=region)


def _containers_client(region: str):
    from granny.serverless.scaleway import ScalewayContainersClient

    return ScalewayContainersClient(region=region)


region_option = click.option(
    "--region",
    default="fr-par",
    show_default=True,
    type=click.Choice(["fr-par", "nl-ams", "pl-waw"]),
)


@click.group()
def serverless() -> None:
    """Scaleway Functions (FaaS) management."""


@serverless.command("list-namespaces")
@region_option
def list_namespaces(region: str) -> None:
    """List all function namespaces."""
    c = _client(region)
    for ns in c.list_namespaces():
        click.echo(f"  {ns.get('id', '?')[:12]}  {ns.get('name', '?'):<30} {ns.get('status', '?')}")


@serverless.command("list-deployments")
@region_option
def list_deployments(region: str) -> None:
    """List every function and container in the region.

    One sweep across both products, because "what is deployed on Scaleway"
    is rarely a question about only one of them.
    """
    functions = _client(region).list_all_functions()
    containers = _containers_client(region).list_all_containers()
    if not functions and not containers:
        click.echo(f"No functions or containers in {region}.")
        return

    click.echo(f"  {'Kind':<10} {'Name':<38} {'Status':<10} {'Runtime':<10} Memory")
    for fn in sorted(functions, key=lambda f: f.get("name", "")):
        click.echo(
            f"  {'function':<10} {fn.get('name', ''):<38} {fn.get('status', ''):<10} "
            f"{fn.get('runtime', '') or '-':<10} {fn.get('memory_limit', 0)}MB"
        )
    for ct in sorted(containers, key=lambda c: c.get("name", "")):
        click.echo(
            f"  {'container':<10} {ct.get('name', ''):<38} {ct.get('status', ''):<10} "
            f"{'-':<10} {ct.get('memory_limit', 0)}MB"
        )
    click.echo(
        f"\n  {len(functions)} function(s), {len(containers)} container(s) in {region}."
    )
    # Scaleway bills per SKU per project, so no line above can carry a price.
    click.echo("  Spend is per-SKU, not per-deployment: `granny analyze costs --provider scaleway`.")


@serverless.command("list-containers")
@region_option
def list_containers(region: str) -> None:
    """List all serverless containers."""
    for ct in _containers_client(region).list_all_containers():
        click.echo(
            f"  {ct.get('id', '?')[:12]}  {ct.get('name', '?'):<34} {ct.get('status', '?')}"
        )


@serverless.command("create-namespace")
@click.argument("name")
@click.option("--description", default="")
@region_option
def create_namespace(name: str, description: str, region: str) -> None:
    """Create a function namespace."""
    c = _client(region)
    result = c.create_namespace(name, description=description)
    click.echo(f"Namespace created: {result.get('id')} ({result.get('name')})")


@serverless.command("list-functions")
@click.argument("namespace_id")
@region_option
def list_functions(namespace_id: str, region: str) -> None:
    """List functions in a namespace."""
    c = _client(region)
    for fn in c.list_functions(namespace_id):
        click.echo(
            f"  {fn.get('id', '?')[:12]}  {fn.get('name', '?'):<20} "
            f"runtime={fn.get('runtime', '?')} status={fn.get('status', '?')}"
        )


@serverless.command("create-function")
@click.argument("namespace_id")
@click.argument("name")
@click.option(
    "--runtime",
    default="node20",
    show_default=True,
    type=click.Choice(
        ["node20", "node22", "python311", "python312", "go121", "rust165"]
    ),
)
@click.option("--handler", default="handler.handler", show_default=True)
@click.option("--memory", default=256, show_default=True, type=int)
@click.option("--timeout", default="30s", show_default=True)
@click.option("--min-scale", default=0, show_default=True, type=int)
@click.option("--max-scale", default=5, show_default=True, type=int)
@region_option
def create_function(
    namespace_id: str,
    name: str,
    runtime: str,
    handler: str,
    memory: int,
    timeout: str,
    min_scale: int,
    max_scale: int,
    region: str,
) -> None:
    """Create a function in a namespace."""
    c = _client(region)
    result = c.create_function(
        namespace_id,
        name,
        runtime=runtime,
        handler=handler,
        memory_limit=memory,
        timeout=timeout,
        min_scale=min_scale,
        max_scale=max_scale,
    )
    click.echo(f"Function created: {result.get('id')} ({result.get('name')})")
    if result.get("domain_name"):
        click.echo(f"Endpoint: https://{result['domain_name']}")


@serverless.command("delete-function")
@click.argument("function_name")
@click.option(
    "--namespace", "namespace_name",
    help="Namespace name. If omitted, the function must be uniquely named across all namespaces.",
)
@click.confirmation_option(prompt="Really delete this Scaleway function?")
@region_option
def delete_function(
    function_name: str,
    namespace_name: str | None,
    region: str,
) -> None:
    """Delete a Scaleway function by name.

    Resolves ``function_name`` to a function id within ``--namespace`` (or
    across all namespaces if unique) and issues a DELETE against the
    Functions API. Custom domains attached to the function are removed by
    Scaleway as part of the cascade.
    """
    c = _client(region)

    function = None
    if namespace_name:
        ns = c.find_namespace(namespace_name)
        if not ns:
            raise click.ClickException(f"Namespace '{namespace_name}' not found")
        function = c.find_function(ns["id"], function_name)
    else:
        for ns in c.list_namespaces():
            fn = c.find_function(ns["id"], function_name)
            if fn:
                if function:
                    raise click.ClickException(
                        f"Function '{function_name}' exists in multiple namespaces; use --namespace"
                    )
                function = fn

    if not function:
        raise click.ClickException(f"Function '{function_name}' not found")

    click.echo(f"Deleting {function['name']} (id={function['id']}, status={function.get('status', '?')})")
    c.delete_function(function["id"])
    click.echo("Deleted.")


# Patterns that bloat a function package without contributing to runtime.
# Matches both whole path segments and trailing-name globs.
_DEFAULT_EXCLUDES = (
    "*.map", "*.d.ts", "*.d.ts.map",
    "node_modules/.cache/*",
    "node_modules/@types/*",
    "node_modules/typescript/*",
    "node_modules/jest*/*",
    "node_modules/ts-jest/*",
    "node_modules/eslint*/*",
    "node_modules/prettier/*",
    "node_modules/@typescript-eslint/*",
    "node_modules/supertest/*",
    "node_modules/jest-junit/*",
    "node_modules/tsx/*",
    "node_modules/.yarn-state.yml",
    "node_modules/*/README.md",
    "node_modules/*/CHANGELOG.md",
    "node_modules/*/LICENSE*",
    "node_modules/*/.github/*",
    "node_modules/*/test/*",
    "node_modules/*/tests/*",
    "node_modules/*/__tests__/*",
)


def _zip_package(source_dir: Path, out_path: Path) -> None:
    """Zip the contents of source_dir, applying default excludes.

    Prefers the `zip` CLI if available (preserves symlinks / perms correctly
    on Linux CI); falls back to zipfile.
    """
    if shutil.which("zip"):
        cmd = ["zip", "-qr", str(out_path), "."]
        for pat in _DEFAULT_EXCLUDES:
            cmd += ["-x", pat]
        subprocess.run(cmd, cwd=source_dir, check=True)
        return

    # Python fallback — doesn't apply excludes (best-effort), just zips everything
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in source_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir))


def _parse_env_pairs(pairs: tuple[str, ...]) -> dict[str, str]:
    env = {}
    for p in pairs:
        if "=" not in p:
            raise click.BadParameter(f"Expected KEY=VALUE, got: {p}")
        k, v = p.split("=", 1)
        env[k.strip()] = v
    return env


def _parse_secret_pairs(pairs: tuple[str, ...]) -> list[dict]:
    secrets = []
    for p in pairs:
        if "=" not in p:
            raise click.BadParameter(f"Expected KEY=VALUE, got: {p}")
        k, v = p.split("=", 1)
        secrets.append({"key": k.strip(), "value": v})
    return secrets


@serverless.command("deploy")
@click.argument("function_name")
@click.option(
    "--source-dir", "source_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory to package (zipped and uploaded verbatim).",
)
@click.option(
    "--namespace", "namespace_name",
    help="Namespace name. If omitted, the function must be uniquely named across all namespaces.",
)
@click.option(
    "--env", "env_pairs", multiple=True, metavar="KEY=VALUE",
    help="Non-secret environment variable (repeatable).",
)
@click.option(
    "--secret", "secret_pairs", multiple=True, metavar="KEY=VALUE",
    help="Secret environment variable (repeatable).",
)
@click.option(
    "--env-json",
    help="Path to JSON file containing {env: {...}, secrets: [{key,value}]}.",
)
@region_option
def deploy(
    function_name: str,
    source_dir: Path,
    namespace_name: str | None,
    env_pairs: tuple[str, ...],
    secret_pairs: tuple[str, ...],
    env_json: str | None,
    region: str,
) -> None:
    """Zip ``--source-dir``, upload, sync env vars, and deploy.

    Example:
        granny serverless deploy my-api \\
            --source-dir backend/dist-bundle \\
            --namespace my-app \\
            --env NODE_ENV=production \\
            --secret API_TOKEN=$API_TOKEN
    """
    c = _client(region)

    # Resolve function id
    function = None
    if namespace_name:
        ns = c.find_namespace(namespace_name)
        if not ns:
            raise click.ClickException(f"Namespace '{namespace_name}' not found")
        function = c.find_function(ns["id"], function_name)
    else:
        for ns in c.list_namespaces():
            fn = c.find_function(ns["id"], function_name)
            if fn:
                if function:
                    raise click.ClickException(
                        f"Function '{function_name}' exists in multiple namespaces; use --namespace"
                    )
                function = fn

    if not function:
        raise click.ClickException(
            f"Function '{function_name}' not found. Create it first with `granny serverless create-function`."
        )

    click.echo(f"Target: {function['name']} (id={function['id']})")

    # Merge env/secret inputs
    env_vars = _parse_env_pairs(env_pairs)
    secrets = _parse_secret_pairs(secret_pairs)
    if env_json:
        payload = json.loads(Path(env_json).read_text())
        env_vars.update(payload.get("env", {}))
        secrets.extend(payload.get("secrets", []))

    # Package + deploy
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{function_name}.zip"
        click.echo(f"Packaging {source_dir} -> {zip_path.name}")
        _zip_package(source_dir, zip_path)
        size_mb = zip_path.stat().st_size / 1024 / 1024
        click.echo(f"Package size: {size_mb:.1f} MB")
        if size_mb > 80:
            click.echo("WARN: package > 80 MB (Scaleway limit is 100 MB)", err=True)

        result = c.deploy_package(
            function["id"],
            str(zip_path),
            env=env_vars or None,
            secrets=secrets or None,
        )

    status = result.get("status", "unknown")
    click.echo(f"Deploy initiated: status={status}")
    if result.get("domain_name"):
        click.echo(f"Endpoint: https://{result['domain_name']}")
    if status not in ("pending", "ready", "creating"):
        sys.exit(1)
