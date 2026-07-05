"""`granny create ...` -- dispatch over granny/create/*.py.

Each subcommand loads the matching script from the in-package ``granny.create``
module and invokes its argparse ``main()`` with forwarded argv. Scripts live
inside the package so the CLI is self-contained (no external ``tools/`` dir).

Adding a new command: drop the script in ``granny/create/`` and add an entry
to ``COMMANDS`` below.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import click

_CREATE_DIR = Path(__file__).resolve().parent.parent / "create"

# subcommand name -> script filename (relative to granny/create/)
COMMANDS: dict[str, str] = {
    "aws-cloudfront": "setup_aws_cloudfront.py",
    "bunny-edge-script": "setup_bunny_edge_script.py",
    "bunny-storage": "setup_bunny_storage.py",
    "cognito-identity-pool": "setup_cognito_identity_pool.py",
    "hetzner-bunny": "setup_hetzner_bunny.py",
    "mailjet-dns": "setup_mailjet_dns.py",
    "private-cdn": "setup_private_cdn.py",
    "s3-website": "setup_s3_website.py",
    "scaleway-faas": "setup_scaleway_faas.py",
    "scaleway-container": "setup_scaleway_container.py",
    "workmail": "setup_workmail.py",
    "auto-certificate": "auto_certificate.py",
    "letsencrypt-cert": "letsencrypt_cert.py",
    "mailjet-contacts": "manage_mailjet_contacts.py",
}


def _load_script(filename: str) -> ModuleType:
    """Import a script from granny.create by filename."""
    path = _CREATE_DIR / filename
    if not path.is_file():
        raise click.ClickException(f"Script not found: {path}")

    module_name = f"granny.create.{path.stem}"
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        raise click.ClickException(f"Cannot load {module_name}: {exc}") from exc


def _make_command(name: str, filename: str) -> click.Command:
    """Build a click command that forwards argv to a script's main()."""

    @click.command(
        name=name,
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
            "help_option_names": [],  # let the script's own -h/--help through
        },
        help=(
            f"Run granny/create/{filename}. All arguments are forwarded; "
            f"use `-- --help` to see the script's own flags."
        ),
    )
    @click.argument("script_args", nargs=-1, type=click.UNPROCESSED)
    def _cmd(script_args: tuple[str, ...]) -> None:
        module = _load_script(filename)
        if not hasattr(module, "main"):
            raise click.ClickException(f"{filename} has no main() function")

        original_argv = sys.argv
        sys.argv = [filename, *script_args]
        try:
            rc = module.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        finally:
            sys.argv = original_argv

        if rc:
            sys.exit(rc)

    return _cmd


@click.group()
def create() -> None:
    """Provision and configure cloud resources."""


for _name, _filename in COMMANDS.items():
    create.add_command(_make_command(_name, _filename))
