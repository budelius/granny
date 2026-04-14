"""ManualDNSProvider — no-op provider that prints instructions.

Use when the authoritative DNS lives with a provider granny does not
support via API (e.g. easyname's web-UI-only DNS). Every mutating call
logs the record the operator must configure manually.
"""

import logging

from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)


class ManualDNSProvider(DNSProvider):
    name = "manual"

    def get_zone_id(self, zone_name: str) -> str:
        return zone_name

    def get_nameservers(self, zone_name: str) -> list[str]:
        return []

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        logger.warning("ManualDNSProvider: cannot list records for %s", zone_id)
        return []

    def _announce(self, action: str, record: DNSRecord) -> None:
        logger.warning("=" * 70)
        logger.warning("MANUAL DNS ACTION REQUIRED (%s)", action)
        logger.warning("Configure at your DNS provider:")
        logger.warning("  %s", record)
        logger.warning("=" * 70)

    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        short = "@" if not name or name == "@" else name
        record = DNSRecord(
            name=short,
            type=record_type.upper(),
            value=value,
            ttl=ttl,
            fqdn=(
                f"{short}.{zone_id}" if short != "@" else zone_id
            ),
        )
        self._announce("create", record)
        return record

    def delete_record(self, zone_id: str, record_id: str) -> None:
        logger.warning("MANUAL DNS ACTION REQUIRED (delete): remove record %s", record_id)
