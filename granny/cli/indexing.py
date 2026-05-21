"""``granny indexing`` -- submit URLs to search-engine indexes.

Thin shim over :mod:`granny.indexing`. Subcommands:

* ``granny indexing submit URL...`` -- notify one or more engines
* ``granny indexing indexnow URL...`` -- IndexNow only (Bing/Yandex/...)
* ``granny indexing google URL...`` -- Google Indexing API only
* ``granny indexing generate-key`` -- mint a fresh IndexNow key
"""

from __future__ import annotations

import click


@click.group()
def indexing() -> None:
    """Submit URLs to search-engine indexes (IndexNow, Google)."""


@indexing.command("indexnow")
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--key",
    default=None,
    help="IndexNow key. Defaults to INDEXNOW_KEY (env or vault).",
)
@click.option(
    "--key-location",
    default=None,
    help="Public URL of the key file. Defaults to https://<host>/<key>.txt.",
)
@click.option(
    "--endpoint",
    default="indexnow",
    type=click.Choice(["indexnow", "bing", "yandex", "seznam", "naver", "yep"]),
    show_default=True,
    help=(
        "Submission endpoint. The shared 'indexnow' endpoint fans out to "
        "every participating engine; pick a specific one to target only it."
    ),
)
def indexnow_cmd(
    urls: tuple[str, ...],
    key: str | None,
    key_location: str | None,
    endpoint: str,
) -> None:
    """Submit URLs via IndexNow (Bing/Yandex/Seznam/Naver/Yep)."""
    from granny.indexing.indexnow import IndexNowClient

    client = IndexNowClient(key=key, key_location=key_location, endpoint=endpoint)
    client.submit_batch(list(urls))
    click.echo(f"IndexNow: submitted {len(urls)} URL(s) via {endpoint}.")


@indexing.command("google")
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--service-account",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Path to GCP service account JSON. Defaults to "
        "GOOGLE_INDEXING_CREDENTIALS (env or vault)."
    ),
)
@click.option(
    "--deleted",
    is_flag=True,
    help="Notify that URLs were removed (URL_DELETED instead of URL_UPDATED).",
)
def google_cmd(
    urls: tuple[str, ...],
    service_account: str | None,
    deleted: bool,
) -> None:
    """Submit URLs to Google's Indexing API.

    Note: Google officially supports this API only for JobPosting and
    BroadcastEvent structured data. For general site indexing, submit a
    sitemap via Search Console and rely on the crawler.
    """
    from granny.indexing.google import GoogleIndexingClient

    client = GoogleIndexingClient(service_account=service_account)
    notify = "URL_DELETED" if deleted else "URL_UPDATED"
    for url in urls:
        client.submit(url, notify_type=notify)
    click.echo(f"Google Indexing: submitted {len(urls)} URL(s) ({notify}).")


@indexing.command("submit")
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--engine",
    "engines",
    multiple=True,
    type=click.Choice(["indexnow", "google"]),
    default=("indexnow",),
    show_default=True,
    help="Engine(s) to notify. May be passed multiple times.",
)
@click.option(
    "--key",
    default=None,
    help="IndexNow key. Defaults to INDEXNOW_KEY (env or vault).",
)
@click.option(
    "--service-account",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to GCP service account JSON (for --engine google).",
)
def submit_cmd(
    urls: tuple[str, ...],
    engines: tuple[str, ...],
    key: str | None,
    service_account: str | None,
) -> None:
    """Submit URLs to one or more search-engine indexes."""
    selected = list(dict.fromkeys(engines))  # de-dupe, preserve order

    if "indexnow" in selected:
        from granny.indexing.indexnow import IndexNowClient

        IndexNowClient(key=key).submit_batch(list(urls))
        click.echo(f"  IndexNow: {len(urls)} URL(s) submitted.")

    if "google" in selected:
        from granny.indexing.google import GoogleIndexingClient

        gclient = GoogleIndexingClient(service_account=service_account)
        for url in urls:
            gclient.submit(url)
        click.echo(f"  Google:   {len(urls)} URL(s) submitted.")


@indexing.command("generate-key")
@click.option(
    "--length",
    default=32,
    type=int,
    show_default=True,
    help="Key length in hex chars (8-128).",
)
def generate_key_cmd(length: int) -> None:
    """Print a fresh random IndexNow key.

    Save the output as INDEXNOW_KEY and host the value at
    https://<your-host>/<key>.txt so search engines can verify ownership.
    """
    from granny.indexing.indexnow import generate_key

    click.echo(generate_key(length))


@indexing.command("init")
@click.option(
    "--directory",
    "-d",
    default=".",
    type=click.Path(exists=True, file_okay=False),
    show_default=True,
    help="Directory to write the key file into (e.g. your static-assets root).",
)
@click.option(
    "--key",
    default=None,
    help="Use this key instead of generating a new one.",
)
@click.option(
    "--length",
    default=32,
    type=int,
    show_default=True,
    help="Length of the generated key in hex chars (8-128).",
)
@click.option(
    "--host",
    default=None,
    help="Site hostname (for the printed deploy/submit hints).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing key file in the directory.",
)
def init_cmd(
    directory: str,
    key: str | None,
    length: int,
    host: str | None,
    force: bool,
) -> None:
    """Generate an IndexNow key and write the verification file.

    The file is created as ``<directory>/<key>.txt`` (UTF-8) containing
    the key itself. Deploy your site so the file is reachable at
    ``https://<host>/<key>.txt``, then set ``INDEXNOW_KEY`` so
    ``granny indexing submit`` picks it up.
    """
    from granny.indexing.indexnow import write_key_file

    path, final_key = write_key_file(
        directory, key=key, length=length, overwrite=force
    )
    host_hint = host or "<your-host>"
    click.echo(f"Wrote {path}")
    click.echo("")
    click.echo("Next steps:")
    click.echo(f"  1. Deploy so the file is reachable at https://{host_hint}/{path.name}")
    click.echo(f"  2. export INDEXNOW_KEY={final_key}")
    if host:
        click.echo(f"  3. granny indexing verify {host}")
        click.echo(f"  4. granny indexing submit https://{host}/<path>")
    else:
        click.echo("  3. granny indexing verify <your-host>")
        click.echo("  4. granny indexing submit https://<your-host>/<path>")


@indexing.command("verify")
@click.argument("host")
@click.option(
    "--key",
    default=None,
    help="IndexNow key to expect. Defaults to INDEXNOW_KEY.",
)
@click.option(
    "--key-location",
    default=None,
    help="Full URL of the key file. Defaults to https://<host>/<key>.txt.",
)
def verify_cmd(
    host: str,
    key: str | None,
    key_location: str | None,
) -> None:
    """Fetch the deployed key file and confirm it matches the key.

    Use after deploying the file written by ``granny indexing init`` --
    IndexNow rejects submissions with 403 when this check fails.
    """
    from granny.indexing.indexnow import verify_key_file

    url = verify_key_file(host, key=key, key_location=key_location)
    click.echo(f"OK: {url} serves the expected key.")
