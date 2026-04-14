"""Docker helpers: multi-arch base image builds with deterministic tagging."""

from granny.docker.build_base import build_base_image, compute_deps_hash

__all__ = ["build_base_image", "compute_deps_hash"]
