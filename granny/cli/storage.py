"""``granny storage`` — object storage management across providers."""

from __future__ import annotations

import click


@click.group()
def storage() -> None:
    """Object storage management (Bunny, Hetzner S3, AWS S3)."""


# -- Bunny Storage subgroup ---------------------------------------------------


@storage.group("bunny")
def bunny_storage() -> None:
    """Bunny Storage zone management."""


@bunny_storage.command("list")
@click.option("--customer", "-c", default=None, help="Bunny customer profile.")
def bunny_list(customer: str | None) -> None:
    """List all Bunny storage zones."""
    from granny.storage.bunny import BunnyStorageClient

    c = BunnyStorageClient(customer=customer)
    zones = c.list_storage_zones()
    if not zones:
        click.echo("No storage zones found.")
        return
    for z in zones:
        click.echo(
            f"  {z['Id']:<10} {z['Name']:<30} "
            f"Region={z.get('Region', '?')} "
            f"Files={z.get('FilesStored', 0)}"
        )


@bunny_storage.command("create")
@click.argument("name")
@click.option(
    "--region",
    default="DE",
    show_default=True,
    type=click.Choice(
        ["DE", "UK", "NY", "LA", "SG", "SE", "BR", "JH", "SYD"],
        case_sensitive=False,
    ),
)
@click.option("--customer", "-c", default=None)
def bunny_create(name: str, region: str, customer: str | None) -> None:
    """Create a Bunny storage zone."""
    from granny.storage.bunny import BunnyStorageClient

    c = BunnyStorageClient(customer=customer)
    zone = c.create_storage_zone(name, region=region.upper())
    click.echo(f"Storage zone created: ID={zone['Id']} Name={zone['Name']}")
    click.echo(f"Password: {zone.get('Password', 'N/A')}")
    click.echo(f"Hostname: {zone.get('StorageHostname', 'N/A')}")


@bunny_storage.command("upload")
@click.argument("zone_name")
@click.argument("local_path", type=click.Path(exists=True))
@click.option("--remote-path", default=None, help="Remote path (default: filename).")
@click.option("--password", envvar="BUNNY_STORAGE_PASSWORD", required=True,
              help="Storage zone password.")
@click.option("--hostname", default="storage.bunnycdn.com", show_default=True,
              help="Storage endpoint hostname.")
@click.option("--content-type", default=None, help="Override Content-Type.")
def bunny_upload(
    zone_name: str,
    local_path: str,
    remote_path: str | None,
    password: str,
    hostname: str,
    content_type: str | None,
) -> None:
    """Upload a single file to a Bunny storage zone."""
    import os

    from granny.storage.bunny import BunnyStorageClient

    if remote_path is None:
        remote_path = os.path.basename(local_path)

    if content_type is None:
        content_type = _guess_content_type(local_path)

    c = BunnyStorageClient()
    with open(local_path, "rb") as f:
        c.upload_file(hostname, password, zone_name, remote_path, f.read(), content_type)
    click.echo(f"Uploaded {local_path} -> {zone_name}/{remote_path}")


@bunny_storage.command("upload-dir")
@click.argument("zone_name")
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--password", envvar="BUNNY_STORAGE_PASSWORD", required=True,
              help="Storage zone password.")
@click.option("--hostname", default="storage.bunnycdn.com", show_default=True,
              help="Storage endpoint hostname.")
@click.option("--cache-control", default="public, max-age=31536000, immutable",
              show_default=True,
              help="Cache-Control for non-HTML files (hashed assets).")
@click.option("--html-cache-control", default="no-cache, no-store, must-revalidate",
              show_default=True,
              help="Cache-Control for *.html (so SPA entry is never stale).")
def bunny_upload_dir(
    zone_name: str,
    directory: str,
    password: str,
    hostname: str,
    cache_control: str,
    html_cache_control: str,
) -> None:
    """Upload an entire directory to a Bunny storage zone.

    Recursively uploads all files, preserving directory structure.
    Content-Type is auto-detected from file extensions.
    HTML files get ``--html-cache-control`` (default: no-cache) so SPA entry
    reloads fetch the latest asset hashes; everything else gets ``--cache-control``
    (default: immutable long-cache).
    """
    import os

    from granny.storage.bunny import BunnyStorageClient

    c = BunnyStorageClient()
    uploaded = 0
    errors = 0

    for root, _dirs, files in os.walk(directory):
        for fname in files:
            local_path = os.path.join(root, fname)
            remote_path = os.path.relpath(local_path, directory).replace("\\", "/")
            ct = _guess_content_type(local_path)
            cc = html_cache_control if fname.lower().endswith(".html") else cache_control

            try:
                with open(local_path, "rb") as f:
                    c.upload_file(
                        hostname, password, zone_name, remote_path,
                        f.read(), ct, cache_control=cc,
                    )
                uploaded += 1
                if uploaded % 10 == 0:
                    click.echo(f"  Uploaded {uploaded} files...")
            except Exception as e:
                click.echo(f"  ERROR: {remote_path}: {e}", err=True)
                errors += 1

    click.echo(f"Done: {uploaded} uploaded, {errors} errors.")
    if errors:
        raise SystemExit(1)


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
        "eot": "application/vnd.ms-fontobject",
        "xml": "application/xml",
        "txt": "text/plain",
        "map": "application/json",
    }.get(ext, "application/octet-stream")


# -- Hetzner S3 subgroup ------------------------------------------------------


@storage.group("hetzner")
def hetzner_storage() -> None:
    """Hetzner S3-compatible object storage."""


@hetzner_storage.command("create")
@click.argument("bucket_name")
@click.option(
    "--region",
    default="fsn1",
    show_default=True,
    type=click.Choice(["fsn1", "nbg1", "hel1"]),
)
@click.option("--public/--private", default=True, show_default=True)
@click.option("--cors", is_flag=True, help="Enable CORS for web access.")
def hetzner_create(
    bucket_name: str, region: str, public: bool, cors: bool
) -> None:
    """Create a Hetzner S3 bucket."""
    from granny.storage.hetzner import HetznerS3Client

    c = HetznerS3Client(region=region)
    c.create_bucket(bucket_name)
    if public:
        c.set_bucket_policy_public(bucket_name)
    if cors:
        c.set_cors_config(bucket_name)
    click.echo(f"Bucket created: {c.get_bucket_url(bucket_name)}")


# -- AWS S3 subgroup -----------------------------------------------------------


@storage.group("aws")
def aws_storage() -> None:
    """AWS S3 bucket management."""


@aws_storage.command("create")
@click.argument("bucket_name")
@click.option("--region", default=None, help="AWS region (default from profile).")
@click.option("--profile", "aws_profile", default=None, help="AWS profile name.")
@click.option(
    "--website/--no-website",
    default=False,
    help="Enable static website hosting.",
)
def aws_create(
    bucket_name: str,
    region: str | None,
    aws_profile: str | None,
    website: bool,
) -> None:
    """Create an AWS S3 bucket."""
    from granny.storage.aws import AWSS3Client

    c = AWSS3Client(aws_profile=aws_profile, region=region)
    c.create_bucket(bucket_name)
    if website:
        c.enable_website_hosting(bucket_name)
        click.echo(f"Website endpoint: {c.get_website_endpoint(bucket_name)}")
    else:
        click.echo(f"Bucket created: {bucket_name} in {c.region}")
