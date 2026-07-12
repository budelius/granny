"""Granny CLI entrypoint.

Thin wrapper around library functions -- no business logic here.
"""

from pathlib import Path

import click
from dotenv import load_dotenv

from granny import __version__

# Auto-load a project-local dotenv from CWD so explicit values override the
# vault. `.deploy.env` is preferred (matches scripts/deploy.sh), `.env` is a
# fallback. override=True means these entries beat anything already exported.
for _candidate in (".deploy.env", ".env"):
    _path = Path.cwd() / _candidate
    if _path.is_file():
        load_dotenv(_path, override=True)
        break


@click.group()
@click.version_option(__version__, prog_name="granny")
@click.option("--verbose", is_flag=True, help="Show debug output")
@click.option("--quiet", is_flag=True, help="Suppress warnings")
def cli(verbose: bool, quiet: bool) -> None:
    """Granny -- Cloud tools collection."""
    import logging

    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    elif quiet:
        logging.basicConfig(level=logging.ERROR)
    else:
        logging.basicConfig(level=logging.WARNING)


# Register command groups -------------------------------------------------

def _register_commands() -> None:
    """Register available command groups."""
    try:
        from granny.cli.credentials import credentials
        cli.add_command(credentials)
    except ImportError:
        pass
    try:
        from granny.cli.analyze import analyze
        cli.add_command(analyze)
    except ImportError:
        pass
    try:
        from granny.cli.cdn import cdn
        cli.add_command(cdn)
    except ImportError:
        pass
    try:
        from granny.cli.create import create
        cli.add_command(create)
    except ImportError:
        pass
    try:
        from granny.cli.docker import docker
        cli.add_command(docker)
    except ImportError:
        pass
    try:
        from granny.cli.dns import dns
        cli.add_command(dns)
    except ImportError:
        pass
    try:
        from granny.cli.cloudflare import cloudflare
        cli.add_command(cloudflare)
    except ImportError:
        pass
    try:
        from granny.cli.storage import storage
        cli.add_command(storage)
    except ImportError:
        pass
    try:
        from granny.cli.edge import edge
        cli.add_command(edge)
    except ImportError:
        pass
    try:
        from granny.cli.serverless import serverless
        cli.add_command(serverless)
    except ImportError:
        pass
    try:
        from granny.cli.email import email
        cli.add_command(email)
    except ImportError:
        pass
    try:
        from granny.cli.authentik import authentik
        cli.add_command(authentik)
    except ImportError:
        pass
    try:
        from granny.cli.elk import elk
        cli.add_command(elk)
    except ImportError:
        pass
    try:
        from granny.cli.azure import azure
        cli.add_command(azure)
    except ImportError:
        pass
    try:
        from granny.cli.aws import aws
        cli.add_command(aws)
    except ImportError:
        pass
    try:
        from granny.cli.indexing import indexing
        cli.add_command(indexing)
    except ImportError:
        pass
    try:
        from granny.cli.zitadel import zitadel
        cli.add_command(zitadel)
    except ImportError:
        pass
    try:
        from granny.cli.oauth import oauth
        cli.add_command(oauth)
    except ImportError:
        pass


_register_commands()

if __name__ == "__main__":
    cli()
