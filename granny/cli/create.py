"""`granny create ...` -- thin dispatch over tools/create/*.py.

Each subcommand loads the matching script from tools/create/ and invokes its
argparse ``main()`` with forwarded argv. The goal is to expose every capability
that lived under tools/create as first-class `granny create <thing>` commands
without duplicating their logic.

Adding a new command: drop the script in tools/create/ and add an entry to
``COMMANDS`` below.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import click

# Repo root is three parents up: granny/cli/create.py -> granny/cli -> granny -> repo
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CREATE_DIR = _REPO_ROOT / "tools" / "create"

# subcommand name -> script filename (relative to tools/create/)
COMMANDS: dict[str, str] = {
    "aws-cloudfront": "setup_aws_cloudfront.py",
    "bunny-storage": "setup_bunny_storage.py",
    "cognito-identity-pool": "setup_cognito_identity_pool.py",
    "hetzner-bunny": "setup_hetzner_bunny.py",
    "mailjet-dns": "setup_mailjet_dns.py",
    "private-cdn": "setup_private_cdn.py",
    "s3-website": "setup_s3_website.py",
    "scaleway-faas": "setup_scaleway_faas.py",
    "workmail": "setup_workmail.py",
    "auto-certificate": "auto_certificate.py",
    "mailjet-contacts": "manage_mailjet_contacts.py",
}


def _load_script(filename: str) -> ModuleType:
    """Import a script from tools/create/ by filename."""
    path = _CREATE_DIR / filename
    if not path.is_file():
        raise click.ClickException(f"Script not found: {path}")

    module_name = f"granny._create_scripts.{path.stem}"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise click.ClickException(f"Cannot load {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Make tools/create/ itself importable so scripts can sibling-import helpers.
    create_dir_str = str(_CREATE_DIR)
    if create_dir_str not in sys.path:
        sys.path.insert(0, create_dir_str)
    spec.loader.exec_module(module)
    return module


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
            f"Run tools/create/{filename}. All arguments are forwarded; "
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
    """Provision and configure cloud resources (wraps tools/create/)."""


for _name, _filename in COMMANDS.items():
    create.add_command(_make_command(_name, _filename))
