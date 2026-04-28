"""Build and push a multi-arch Docker base image with deterministic tagging.

Pattern:
  1. Hash the files that affect the base image (deps, Dockerfile, etc.) into
     a short DEPS_HASH used as an image tag.
  2. Check if `<image>:<hash>` already exists in the registry. If yes, skip.
  3. Otherwise build multi-arch via `docker buildx` and push.

Secrets (GitLab PyPI token, etc.) are passed via BuildKit `--mount=type=secret`
so they never enter image history. Credentials needed for `docker login` come
from granny.credentials (env-var resolution), not from .netrc.

Typical usage from another project's CLAUDE.md:

    granny docker build-base \\
        --context T:\\projects\\LuLarge\\icaap-climaterisk \\
        --dockerfile Dockerfile.base \\
        --image budelius/icaap-climaterisk-base \\
        --hash-file environment.docker.yml \\
        --hash-file pyproject.toml \\
        --hash-file Dockerfile.base \\
        --secret gitlab_token=GITLAB_PYPI_PASSWORD
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BuildBaseConfig:
    """Inputs for a multi-arch base image build."""

    context: Path
    dockerfile: Path
    image: str
    hash_files: list[Path] = field(default_factory=list)
    platforms: str = "linux/amd64,linux/arm64"
    builder: str = "granny-multiarch"
    # secret_id -> env var name (value read from env; env should be populated
    # via granny.credentials.load_secrets_into_env before calling).
    secrets: dict[str, str] = field(default_factory=dict)
    force: bool = False
    hash_length: int = 16


def compute_deps_hash(files: list[Path], length: int = 16) -> str:
    """Compute a short hex sha256 over the concatenated raw bytes of files.

    Order matters - pass files in a stable order. Matches the shell equivalent:
        cat <files> | sha256sum | cut -c1-<length>
    """
    if not files:
        raise ValueError("hash_files must not be empty")
    h = hashlib.sha256()
    for path in files:
        h.update(path.read_bytes())
    return h.hexdigest()[:length]


def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess and log it (without leaking secret flag values)."""
    # Redact anything after --build-arg or --secret for logging only
    safe_cmd = []
    i = 0
    while i < len(cmd):
        token = cmd[i]
        safe_cmd.append(token)
        if token in ("--build-arg", "--secret") and i + 1 < len(cmd):
            next_tok = cmd[i + 1]
            # For --secret id=foo,src=/tmp/xyz keep as-is (src is a file path, not a value)
            # For --build-arg KEY=VALUE, redact VALUE
            if token == "--build-arg" and "=" in next_tok:
                key, _ = next_tok.split("=", 1)
                safe_cmd.append(f"{key}=***")
            else:
                safe_cmd.append(next_tok)
            i += 2
            continue
        i += 1
    logger.debug("run: %s", " ".join(safe_cmd))
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def _manifest_exists(image_ref: str) -> bool:
    """Return True if the image exists in the remote registry."""
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        raise RuntimeError("docker CLI not found on PATH")


def _ensure_builder(name: str) -> None:
    """Create a docker-container buildx builder if it doesn't exist."""
    result = subprocess.run(
        ["docker", "buildx", "ls"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and name in result.stdout:
        subprocess.run(["docker", "buildx", "use", name], check=False, capture_output=True)
        return
    logger.info("Creating buildx builder %s", name)
    subprocess.run(
        ["docker", "buildx", "create", "--name", name, "--driver", "docker-container", "--use"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["docker", "buildx", "inspect", "--bootstrap"], check=True, capture_output=True)


def build_base_image(config: BuildBaseConfig) -> tuple[str, bool]:
    """Build and push a multi-arch base image.

    Returns:
        (deps_hash, was_built): tuple of the computed hash and whether a new
        build was actually performed (False means it already existed in registry).
    """
    # Resolve files relative to context
    ctx = config.context.resolve()
    if not ctx.is_dir():
        raise FileNotFoundError(f"Context directory not found: {ctx}")

    dockerfile = (ctx / config.dockerfile).resolve() if not config.dockerfile.is_absolute() else config.dockerfile
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")

    hash_paths = [
        (ctx / p).resolve() if not p.is_absolute() else p
        for p in config.hash_files
    ]
    for p in hash_paths:
        if not p.is_file():
            raise FileNotFoundError(f"Hash file not found: {p}")

    deps_hash = compute_deps_hash(hash_paths, length=config.hash_length)
    tagged = f"{config.image}:{deps_hash}"
    latest = f"{config.image}:latest"
    logger.info("DEPS_HASH=%s", deps_hash)

    if not config.force and _manifest_exists(tagged):
        logger.info("%s already exists in registry - skipping build", tagged)
        return deps_hash, False

    _ensure_builder(config.builder)

    # Write each secret to a tempfile so BuildKit can mount them without
    # exposing values on the command line.
    secret_files: dict[str, Path] = {}
    try:
        for secret_id, env_var in config.secrets.items():
            value = os.environ.get(env_var)
            if value is None:
                raise RuntimeError(
                    f"Required secret env var {env_var!r} is not set. "
                    f"Ensure granny.credentials.load_secrets_into_env() was called."
                )
            tmp = Path(tempfile.mkstemp(prefix=f"granny-secret-{secret_id}-")[1])
            tmp.write_text(value, encoding="utf-8")
            secret_files[secret_id] = tmp

        cmd: list[str] = [
            "docker", "buildx", "build",
            "--builder", config.builder,
            "--platform", config.platforms,
            "-t", tagged,
            "-t", latest,
            "-f", str(dockerfile),
        ]
        for secret_id, path in secret_files.items():
            cmd += ["--secret", f"id={secret_id},src={path}"]
        cmd += [
            "--cache-from", f"type=registry,ref={config.image}:cache",
            "--push",
            str(ctx),
        ]
        _run(cmd)
    finally:
        for p in secret_files.values():
            try:
                p.unlink()
            except OSError:
                pass

    return deps_hash, True
