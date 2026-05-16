"""Authentik management — REST API wrapper."""

from granny.authentik.client import AuthentikClient, AuthentikError

__all__ = [
    "AuthentikClient",
    "AuthentikError",
]
