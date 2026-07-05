#!/usr/bin/env python3
"""Let's Encrypt certificate issuance via ACME DNS-01, driven by granny's DNS
providers.

Unlike ``auto_certificate.py`` (AWS ACM / Bunny), this issues a real
Let's Encrypt certificate (fullchain + private key PEM) usable by Caddy,
nginx, Kubernetes Secrets, etc. The ACME ``dns-01`` challenge is solved by
creating ``_acme-challenge`` TXT records through **any** granny DNS provider
(``--dns inwx`` / ``cloudflare`` / ``hetzner`` / ...), so it works for zones
like ``obachan.dev`` (INWX) where no cert-manager webhook exists.

Example::

    granny create letsencrypt-cert \\
        --domain '*.obachan.dev' --domain obachan.dev \\
        --dns inwx --zone obachan.dev \\
        --email ops@example.com --out-dir ./certs

Credentials for the DNS provider resolve through granny's normal vault/env
pipeline (e.g. INWX_USERNAME / INWX_PASSWORD / INWX_SHARED_SECRET).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

try:  # granny's vault→env pipeline (same as auto_certificate.py)
    from granny.credentials import load_secrets_into_env

    load_secrets_into_env()
except Exception:  # pragma: no cover - best effort
    pass

_LE_PROD = "https://acme-v02.api.letsencrypt.org/directory"
_LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
_DOH = "https://dns.google/resolve"


def _new_rsa_key():  # noqa: ANN202
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_pem(key) -> bytes:  # noqa: ANN001
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _relative_name(fqdn: str, zone: str) -> str:
    """Strip the trailing ``.<zone>`` so provider APIs get the short name."""
    fqdn = fqdn.rstrip(".")
    suffix = "." + zone.rstrip(".")
    return fqdn[: -len(suffix)] if fqdn.endswith(suffix) else fqdn


def _txt_propagated(fqdn: str, value: str, *, attempts: int, delay: int) -> bool:
    """Poll public DNS (DoH) until the challenge TXT value is visible."""
    for i in range(attempts):
        try:
            resp = requests.get(
                _DOH, params={"name": fqdn, "type": "TXT"}, timeout=10
            ).json()
            answers = [a.get("data", "").strip('"') for a in resp.get("Answer", [])]
            if value in answers:
                return True
        except Exception:  # noqa: BLE001 - transient resolver errors are fine
            pass
        if i < attempts - 1:
            time.sleep(delay)
    return False


def issue_certificate(
    domains: list[str],
    *,
    dns: str,
    zone: str,
    email: str,
    staging: bool = False,
    propagation_attempts: int = 20,
    propagation_delay: int = 15,
) -> tuple[str, str]:
    """Issue a Let's Encrypt cert for ``domains``; return (fullchain_pem, key_pem).

    ``dns`` is a granny DNS provider name; ``zone`` is the registrable zone the
    challenge records live in (e.g. ``obachan.dev``).
    """
    import josepy as jose
    from acme import challenges, client, crypto_util, messages

    from granny.dns.factory import get_provider

    provider = get_provider(dns)
    zone_id = provider.get_zone_id(zone)

    directory_url = _LE_STAGING if staging else _LE_PROD
    account_key = jose.JWKRSA(key=_new_rsa_key())
    net = client.ClientNetwork(account_key, user_agent="granny-letsencrypt")
    directory = client.ClientV2.get_directory(directory_url, net)
    acme = client.ClientV2(directory, net)
    acme.new_account(
        messages.NewRegistration.from_data(email=email, terms_of_service_agreed=True)
    )

    cert_key = _new_rsa_key()
    cert_key_pem = _key_pem(cert_key)
    csr_pem = crypto_util.make_csr(cert_key_pem, domains)
    order = acme.new_order(csr_pem)

    created: list[tuple[str, str, str]] = []  # (fqdn, value, record_id) for cleanup
    try:
        # 1. Publish every dns-01 TXT record first (a base domain + its wildcard
        #    share `_acme-challenge.<domain>`, so multiple values can coexist).
        pending = []
        for authz in order.authorizations:
            dom = authz.body.identifier.value
            chall = next(
                c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01)
            )
            response, validation = chall.response_and_validation(account_key)
            fqdn = chall.chall.validation_domain_name(dom)
            rec = provider.create_record(
                zone_id, _relative_name(fqdn, zone), "TXT", validation
            )
            created.append((fqdn, validation, rec.id))
            pending.append((fqdn, validation, chall, response))
            print(f"  published TXT {fqdn} = {validation[:12]}…", file=sys.stderr)

        # 2. Wait for propagation, then answer each challenge.
        for fqdn, validation, chall, response in pending:
            if not _txt_propagated(
                fqdn, validation, attempts=propagation_attempts, delay=propagation_delay
            ):
                raise TimeoutError(f"TXT for {fqdn} did not propagate in time")
            acme.answer_challenge(chall, response)
            print(f"  answered challenge for {fqdn}", file=sys.stderr)

        # 3. Finalize + download.
        order = acme.poll_and_finalize(order)
    finally:
        for _fqdn, _val, rec_id in created:
            try:
                provider.delete_record(zone_id, rec_id)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    return order.fullchain_pem, cert_key_pem.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue a Let's Encrypt cert via DNS-01.")
    parser.add_argument("--domain", action="append", required=True, help="Domain (repeatable; wildcards ok).")
    parser.add_argument("--dns", required=True, help="granny DNS provider (e.g. inwx, cloudflare).")
    parser.add_argument("--zone", required=True, help="Registrable zone the challenge records live in.")
    parser.add_argument("--email", required=True, help="ACME account contact email.")
    parser.add_argument("--out-dir", default=".", help="Directory to write fullchain.pem + key.pem.")
    parser.add_argument("--staging", action="store_true", help="Use the LE staging CA (no rate limits).")
    parser.add_argument("--propagation-attempts", type=int, default=20)
    parser.add_argument("--propagation-delay", type=int, default=15)
    args = parser.parse_args(argv)

    fullchain, key = issue_certificate(
        args.domain,
        dns=args.dns,
        zone=args.zone,
        email=args.email,
        staging=args.staging,
        propagation_attempts=args.propagation_attempts,
        propagation_delay=args.propagation_delay,
    )

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "fullchain.pem").write_text(fullchain, encoding="utf-8")
    (out / "key.pem").write_text(key, encoding="utf-8")
    print(f"wrote {out / 'fullchain.pem'} and {out / 'key.pem'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
