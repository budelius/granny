"""Knative / obachan FaaS control API client.

Exposes :class:`KnativeClient` — a thin ``requests.Session`` wrapper
around the 7 ``/api/faas/*`` HTTP verbs provided by the obachan FaaS
control plane (Phase 18).
"""

from granny.knative.client import KnativeClient

__all__ = ["KnativeClient"]
