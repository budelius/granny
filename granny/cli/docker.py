"""CLI commands for Docker multi-arch image builds."""
from __future__ import annotations

import sys
from pathlib import Path

import click


@click.group()
def docker() -> None:
    """Docker multi-arch build helpers."""


@docker.command("build-base")
@click.option(
    "--context",
    "context_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    show_default="current directory",
    help="Build context directory (where the Dockerfile lives).",
)
@click.option(
    "--dockerfile",
    type=click.Path(path_type=Path),
    default=Path("Dockerfile.base"),
    show_default=True,
    help="Dockerfile path (relative to --context or absolute).",
)
@click.option(
    "--image",
    required=True,
    help="Image reference without tag, e.g. budelius/icaap-climaterisk-base.",
)
@click.option(
    "--hash-file",
    "hash_files",
    type=click.Path(path_type=Path),
    multiple=True,
    required=True,
    help="File whose content contributes to DEPS_HASH. Pass multiple times. Order matters.",
)
@click.option(
    "--platforms",
    default="linux/amd64,linux/arm64",
    show_default=True,
    help="Comma-separated buildx platforms.",
)
@click.option(
    "--builder",
    default="granny-multiarch",
    show_default=True,
    help="buildx builder name. Created on demand.",
)
@click.option(
    "--secret",
    "secrets",
    multiple=True,
    help=(
        "BuildKit secret, format: id=ENV_VAR. The value is read from the env "
        "var (populated via granny.credentials) and mounted as "
        "/run/secrets/<id> during build. Pass multiple times."
    ),
)
@click.option(
    "--login-docker-hub/--no-login-docker-hub",
    default=True,
    show_default=True,
    help="docker login to Docker Hub using DOCKER_HUB_USER/DOCKER_HUB_TOKEN from granny's vault.",
)
@click.option(
    "--force/--no-force",
    default=False,
    show_default=True,
    help="Rebuild even if the DEPS_HASH tag already exists in the registry.",
)
def build_base(
    context_dir: Path,
    dockerfile: Path,
    image: str,
    hash_files: tuple[Path, ...],
    platforms: str,
    builder: str,
    secrets: tuple[str, ...],
    login_docker_hub: bool,
    force: bool,
) -> None:
    """Build and push a multi-arch Docker base image with deterministic tagging.

    Computes a short sha256 over --hash-file contents, tags the image as
    `<image>:<hash>` and `<image>:latest`, and pushes. Skips the build if the
    hash tag already exists in the registry (unless --force).

    Credentials are fetched from granny's Vaultwarden vault via granny.credentials.
    Secrets are mounted into BuildKit via --mount=type=secret so they never
    enter image history.
    """
    import os
    import subprocess

    from granny.credentials import load_secrets_into_env
    from granny.docker.build_base import BuildBaseConfig, build_base_image

    # Parse --secret flags: each is "id=ENV_VAR"
    secret_map: dict[str, str] = {}
    for s in secrets:
        if "=" not in s:
            click.echo(f"Invalid --secret value {s!r}, expected id=ENV_VAR", err=True)
            sys.exit(2)
        sid, env = s.split("=", 1)
        secret_map[sid.strip()] = env.strip()

    # Load required secrets from vault into os.environ (env var wins if set)
    required_env_vars = set(secret_map.values())
    if login_docker_hub:
        required_env_vars.update({"DOCKER_HUB_USER", "DOCKER_HUB_TOKEN"})
    if required_env_vars:
        results = load_secrets_into_env(list(required_env_vars))
        missing = [k for k, ok in results.items() if not ok]
        if missing:
            click.echo(
                f"Missing secrets (not in env): {', '.join(sorted(missing))}\n"
                f"Set them in .env / .deploy.env or export them in your shell.",
                err=True,
            )
            sys.exit(1)

    # Login to Docker Hub before the build so buildx can push.
    if login_docker_hub:
        user = os.environ["DOCKER_HUB_USER"]
        token = os.environ["DOCKER_HUB_TOKEN"]
        click.echo(f"Logging into Docker Hub as {user}...")
        result = subprocess.run(
            ["docker", "login", "--username", user, "--password-stdin"],
            input=token,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            click.echo(f"docker login failed: {result.stderr.strip()}", err=True)
            sys.exit(result.returncode)

    cfg = BuildBaseConfig(
        context=context_dir,
        dockerfile=dockerfile,
        image=image,
        hash_files=list(hash_files),
        platforms=platforms,
        builder=builder,
        secrets=secret_map,
        force=force,
    )
    try:
        deps_hash, was_built = build_base_image(cfg)
    except Exception as e:
        click.echo(f"build-base failed: {e}", err=True)
        sys.exit(1)

    if was_built:
        click.echo(f"Pushed: {image}:{deps_hash}")
        click.echo(f"Pushed: {image}:latest")
    else:
        click.echo(f"Skipped: {image}:{deps_hash} already exists in registry")
