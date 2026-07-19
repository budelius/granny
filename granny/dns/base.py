"""DNSProvider — abstract base class for DNS record CRUD."""

from abc import ABC, abstractmethod

from granny.dns.records import DNSRecord


class DNSProvider(ABC):
    """Provider-agnostic DNS record CRUD.

    Implementations raise ``NotImplementedError`` for operations their upstream
    API does not support (e.g. easyname has no DNS record endpoints — it is a
    registrar only and is therefore not a DNS provider at all).

    All ``name`` arguments are short names relative to the zone: ``""`` or
    ``"@"`` for apex, ``"api"`` for ``api.example.com``. Zone resolution is the
    caller's job via :meth:`get_zone_id`.
    """

    name: str = "generic"

    @abstractmethod
    def get_zone_id(self, zone_name: str) -> str:
        """Return the provider-specific zone identifier for ``zone_name``.

        Raises if the zone doesn't exist. Providers that use the domain name
        as the identifier (deSEC, ClouDNS) return ``zone_name`` unchanged.
        """

    def get_nameservers(self, zone_name: str) -> list[str]:
        """Return the authoritative nameservers advertised by the provider."""
        raise NotImplementedError(
            f"{type(self).__name__} does not expose nameservers via API"
        )

    def create_zone(self, zone_name: str) -> dict:
        """Create ``zone_name`` at the provider (idempotent where supported).

        Returns provider metadata for the zone — at minimum ``id``, ``name``,
        ``status``, and ``nameservers`` (the pair the registrar must delegate
        to). Providers whose API has no zone-creation endpoint (or where zones
        are registrar-managed) keep this default.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support zone creation via API"
        )

    @abstractmethod
    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        """List records in a zone, optionally filtered by name and/or type."""

    def get_record(
        self, zone_id: str, name: str, record_type: str
    ) -> DNSRecord | None:
        """Return the first record matching ``name`` + ``record_type``, else None."""
        matches = self.list_records(zone_id, name=name, record_type=record_type)
        return matches[0] if matches else None

    @abstractmethod
    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        """Create a record. Returns the created record with its provider id."""

    @abstractmethod
    def delete_record(self, zone_id: str, record_id: str) -> None:
        """Delete a record by its provider-specific id."""

    def update_record(
        self,
        zone_id: str,
        record_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        """Update a record in-place.

        Default implementation is delete + create. Providers that support
        native PUT (Cloudflare, Bunny) override this for atomicity.
        """
        self.delete_record(zone_id, record_id)
        return self.create_record(
            zone_id, name, record_type, value, ttl=ttl, proxied=proxied
        )

    def upsert_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        """Create the record if it doesn't exist, else update it."""
        existing = self.get_record(zone_id, name, record_type)
        if existing is None:
            return self.create_record(
                zone_id, name, record_type, value, ttl=ttl, proxied=proxied
            )
        if existing.value == value and existing.ttl == ttl:
            return existing
        return self.update_record(
            zone_id,
            existing.id,
            name,
            record_type,
            value,
            ttl=ttl,
            proxied=proxied,
        )
