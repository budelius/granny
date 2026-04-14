"""DNS management — provider-agnostic record CRUD.

This package is the shared home for DNS provider adapters that were
previously duplicated across seven ``tools/create/setup_*.py`` scripts.

Public API::

    from granny.dns import get_provider, DNSProvider, DNSRecord

    dns = get_provider("cloudflare")
    zone_id = dns.get_zone_id("example.com")
    dns.upsert_record(zone_id, "api", "A", "1.2.3.4", ttl=300)

The existing ``tools/create/setup_*.py`` scripts keep their local copies
and are intentionally unchanged.
"""

from granny.dns.base import DNSProvider
from granny.dns.factory import get_provider, provider_choices
from granny.dns.records import DNSRecord

__all__ = ["DNSProvider", "DNSRecord", "get_provider", "provider_choices"]
