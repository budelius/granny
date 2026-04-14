"""DNSRecord — provider-agnostic record representation."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DNSRecord:
    """A DNS record as returned by any provider.

    ``name`` is always the short name (subname) relative to the zone — ``""``
    or ``"@"`` for apex, ``"api"`` for ``api.example.com``. ``fqdn`` carries
    the full name for display. ``id`` is the provider-specific record id used
    for updates and deletes. ``provider_data`` holds the raw provider payload
    for anything the generic shape doesn't capture (Cloudflare ``proxied``,
    Bunny ``Disabled``, etc.).
    """

    name: str
    type: str
    value: str
    ttl: int = 300
    id: str | None = None
    fqdn: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        display = self.fqdn or self.name or "@"
        return f"{self.type:<6} {display:<40} {self.value}  (ttl={self.ttl})"
