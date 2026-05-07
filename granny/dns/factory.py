"""DNS provider factory — name -> DNSProvider instance.

Providers are instantiated lazily so importing :mod:`granny.dns` does not
trigger credential lookups until someone actually wants to use a provider.
"""

from collections.abc import Callable

from granny.dns.base import DNSProvider


def _bunny() -> DNSProvider:
    from granny.dns.bunny import BunnyDNSProvider

    return BunnyDNSProvider()


def _cloudflare() -> DNSProvider:
    from granny.dns.cloudflare import CloudflareDNSProvider

    return CloudflareDNSProvider()


def _hetzner() -> DNSProvider:
    from granny.dns.hetzner import HetznerDNSProvider

    return HetznerDNSProvider()


def _desec() -> DNSProvider:
    from granny.dns.desec import DeSECDNSProvider

    return DeSECDNSProvider()


def _cloudns() -> DNSProvider:
    from granny.dns.cloudns import ClouDNSDNSProvider

    return ClouDNSDNSProvider()


def _inwx() -> DNSProvider:
    from granny.dns.inwx import INWXDNSProvider

    return INWXDNSProvider()


def _manual() -> DNSProvider:
    from granny.dns.manual import ManualDNSProvider

    return ManualDNSProvider()


_PROVIDERS: dict[str, Callable[[], DNSProvider]] = {
    "bunny": _bunny,
    "cloudflare": _cloudflare,
    "hetzner": _hetzner,
    "desec": _desec,
    "cloudns": _cloudns,
    "inwx": _inwx,
    "manual": _manual,
}


def provider_choices() -> list[str]:
    """Return the list of provider names for argparse/click."""
    return sorted(_PROVIDERS.keys())


def get_provider(name: str) -> DNSProvider:
    """Instantiate the named provider. Raises ``ValueError`` if unknown."""
    key = (name or "").strip().lower()
    factory = _PROVIDERS.get(key)
    if factory is None:
        raise ValueError(
            f"Unknown DNS provider: {name!r}. "
            f"Choices: {', '.join(provider_choices())}"
        )
    return factory()
