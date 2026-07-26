"""granny knative / granny faas — Knative function management via obachan FaaS control API."""

from __future__ import annotations

import json
import os

import click


@click.group()
def knative() -> None:
    """Knative function management via obachan FaaS control API."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _guess_content_type(path: str) -> str:
    """Guess Content-Type from file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "html": "text/html; charset=utf-8",
        "css": "text/css; charset=utf-8",
        "js": "application/javascript; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
        "ico": "image/x-icon",
        "woff": "font/woff",
        "woff2": "font/woff2",
        "ttf": "font/ttf",
        "xml": "application/xml",
        "txt": "text/plain",
    }.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@knative.command("deploy")
@click.argument("name")
@click.option(
    "--image", required=True, help="Container image ref (e.g. registry/fn:latest)"
)
@click.option(
    "--env",
    "-e",
    "env_vars",
    multiple=True,
    help="Environment variable in KEY=VAL format. Repeatable.",
)
@click.option("--min-scale", default=0, show_default=True, type=int)
@click.option("--max-scale", default=5, show_default=True, type=int)
@click.option(
    "--api-url",
    envvar="OBACHAN_API_BASE_URL",
    default=None,
    help="FaaS control API base URL. Defaults to OBACHAN_API_BASE_URL env or vault.",
)
@click.option(
    "--api-key",
    envvar="OBACHAN_API_KEY",
    default=None,
    help="API key. Defaults to OBACHAN_API_KEY env or vault.",
)
def knative_deploy(
    name: str,
    image: str,
    env_vars: tuple,
    min_scale: int,
    max_scale: int,
    api_url: str | None,
    api_key: str | None,
) -> None:
    """Deploy (create or update) a Knative function."""
    from granny.knative import KnativeClient

    parsed: dict[str, str] = {}
    for entry in env_vars:
        k, sep, v = entry.partition("=")
        if sep:
            parsed[k.strip()] = v
        else:
            click.echo(f"Warning: skipping malformed env entry: {entry!r}", err=True)

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        result = c.deploy(
            name, image, env_vars=parsed, min_scale=min_scale, max_scale=max_scale
        )
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    url = (result.get("status") or {}).get("url", "(not yet ready)")
    click.echo(f"Deployed: {name}")
    click.echo(f"URL: {url}")


@knative.command("list")
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_list(api_url: str | None, api_key: str | None) -> None:
    """List deployed Knative functions."""
    from granny.knative import KnativeClient

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        items = c.list()
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if not items:
        click.echo("No functions deployed.")
        return

    click.echo(f"{'NAME':<30} {'URL'}")
    for item in items:
        item_name = (item.get("metadata") or {}).get("name") or item.get("name") or "?"
        url = (item.get("status") or {}).get("url", "-")
        click.echo(f"{item_name:<30} {url}")


@knative.command("get")
@click.argument("name")
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_get(name: str, api_url: str | None, api_key: str | None) -> None:
    """Get details of a Knative function."""
    from granny.knative import KnativeClient

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        result = c.get(name)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if result is None:
        click.echo(f"Function '{name}' not found.", err=True)
        raise SystemExit(1)

    click.echo(json.dumps(result, indent=2))


@knative.command("scale")
@click.argument("name")
@click.option("--min-scale", default=None, type=int)
@click.option("--max-scale", default=None, type=int)
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_scale(
    name: str,
    min_scale: int | None,
    max_scale: int | None,
    api_url: str | None,
    api_key: str | None,
) -> None:
    """Update the scale bounds of a Knative function."""
    if min_scale is None and max_scale is None:
        raise click.UsageError(
            "At least one of --min-scale or --max-scale must be provided."
        )

    from granny.knative import KnativeClient

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        c.scale(name, min_scale=min_scale, max_scale=max_scale)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Scaled {name}: min={min_scale} max={max_scale}")


@knative.command("remove")
@click.argument("name")
@click.option(
    "--yes",
    is_flag=True,
    help="Confirm deletion. Required to prevent accidental removal.",
)
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_remove(
    name: str,
    yes: bool,
    api_url: str | None,
    api_key: str | None,
) -> None:
    """Remove a deployed Knative function."""
    if not yes:
        raise click.UsageError(
            "Deletion requires --yes flag to confirm. "
            "Pass --yes to confirm removal of the function."
        )

    from granny.knative import KnativeClient

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        c.delete(name)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Removed: {name}")


@knative.command("logs")
@click.argument("name")
@click.option("--tail", default=50, show_default=True, type=int)
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_logs(
    name: str,
    tail: int,
    api_url: str | None,
    api_key: str | None,
) -> None:
    """Retrieve recent log lines for a Knative function."""
    from granny.knative import KnativeClient

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        log_text = c.logs(name, tail=tail)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(log_text)


@knative.command("invoke")
@click.argument("name")
@click.option("--method", default="GET", show_default=True)
@click.option("--path", "invoke_path", default="/", show_default=True)
@click.option("--body", default=None, help="Request body string")
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_invoke(
    name: str,
    method: str,
    invoke_path: str,
    body: str | None,
    api_url: str | None,
    api_key: str | None,
) -> None:
    """Invoke a Knative function via the control-plane proxy."""
    from granny.knative import KnativeClient

    body_bytes = body.encode() if body else None

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        result = c.invoke(name, method=method, path=invoke_path, body=body_bytes)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(result.decode("utf-8", errors="replace"))


@knative.command("deploy-ui")
@click.argument("name")
@click.argument(
    "dist_dir",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
)
@click.option("--image", required=True, help="Backend container image ref")
@click.option("--zone", required=True, help="Bunny Storage zone name")
@click.option(
    "--storage-password",
    envvar="BUNNY_STORAGE_PASSWORD",
    required=True,
    help="Bunny Storage zone password.",
)
@click.option("--hostname", default="storage.bunnycdn.com", show_default=True)
@click.option(
    "--pull-zone-id",
    default=None,
    type=int,
    help="Bunny CDN pull zone ID for cache purge after upload.",
)
@click.option(
    "--env", "-e", "env_vars", multiple=True, help="Backend env vars KEY=VAL."
)
@click.option("--api-url", envvar="OBACHAN_API_BASE_URL", default=None)
@click.option("--api-key", envvar="OBACHAN_API_KEY", default=None)
def knative_deploy_ui(
    name: str,
    dist_dir: str,
    image: str,
    zone: str,
    storage_password: str,
    hostname: str,
    pull_zone_id: int | None,
    env_vars: tuple,
    api_url: str | None,
    api_key: str | None,
) -> None:
    """Deploy a UI to Bunny Storage and a backend function to Knative.

    Uploads all files in DIST_DIR to Bunny Storage (with Cache-Control headers
    appropriate for SPA assets), optionally purges the CDN cache, then deploys
    the backend image as a Knative service.
    """
    from granny.storage.bunny import BunnyStorageClient

    storage_client = BunnyStorageClient()
    uploaded = 0
    errors = 0

    for root, _dirs, files in os.walk(dist_dir):
        for fname in files:
            local = os.path.join(root, fname)
            remote = os.path.relpath(local, dist_dir).replace("\\", "/")
            ct = _guess_content_type(local)
            cc = (
                "no-cache, no-store, must-revalidate"
                if fname.lower().endswith(".html")
                else "public, max-age=31536000, immutable"
            )
            try:
                with open(local, "rb") as fh:
                    storage_client.upload_file(
                        hostname,
                        storage_password,
                        zone,
                        remote,
                        fh.read(),
                        ct,
                        cache_control=cc,
                    )
                uploaded += 1
            except Exception as exc:
                click.echo(f"  ERROR uploading {remote}: {exc}", err=True)
                errors += 1

    click.echo(f"Uploaded {uploaded} files to Bunny zone '{zone}' ({errors} errors).")
    if errors:
        raise SystemExit(1)

    if pull_zone_id is not None:
        from granny.cdn.bunny import BunnyCDNClient

        BunnyCDNClient().purge_cache(pull_zone_id)
        click.echo(f"CDN cache purged for pull zone {pull_zone_id}.")

    parsed_env: dict[str, str] = {}
    for entry in env_vars:
        k, sep, v = entry.partition("=")
        if sep:
            parsed_env[k.strip()] = v

    from granny.knative import KnativeClient

    try:
        c = KnativeClient(base_url=api_url, api_key=api_key)
        result = c.deploy(name, image, env_vars=parsed_env)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"Error deploying {name}: {exc}", err=True)
        raise SystemExit(1)

    url = (result.get("status") or {}).get("url", "(not yet ready)")
    click.echo(f"Deployed: {name}")
    click.echo(f"URL: {url}")
