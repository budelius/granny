# Granny - Claude AI Guidelines

Cloud tools collection for AWS/CDN/DNS automation. Python 3.13+, `uv` for deps.

## Layout

```
granny/          # Installable package `granny-devops` (CLI entrypoint: `granny`)
  cli/           # Click command groups: analyze, cdn, cloudflare, create, credentials, dns, docker, edge, email, serverless, storage
  analyze/       # AWS resource listing (vpcs, lambdas)
  cloudflare/    # Workers / D1 / R2 / KV resource clients
  credentials/   # Vault (Locke) + env-var secret resolution
  create/        # Cloud setup scripts (S3/CloudFront, Bunny, Scaleway, ...)
tests/
```

## Conventions

- **Python**: 3.13+, PEP 8, type hints on public functions, stdlib → third-party → local imports.
- **CLI**: Click groups registered in `granny/cli/main.py`. Keep business logic out of CLI modules.
- **Errors**: Wrap AWS/HTTP calls; surface actionable messages.
- **Secrets**: Never hardcode. Read via `granny.credentials.get_secret()` or `os.environ`.

## Credentials

Env-var first; vault (Locke / Vaultwarden) as optional fallback. `.env` / `.deploy.env` is auto-loaded by the CLI; vault support activates automatically when `locke` is importable (declared as a dev dep, so `uv sync` wires it up locally).

```python
from granny.credentials import get_secret, get_bunny_api_key
token = get_secret("BUNNY_API_KEY")
acme = get_bunny_api_key(customer="acme")  # BUNNY_API_KEY_ACME
```

Registered names: `CLOUDFLARE_API_TOKEN`, `DESEC_API_TOKEN`, `HETZNER_S3_ACCESS_KEY`,
`HETZNER_S3_SECRET_KEY`, `HETZNER_DNS_API_TOKEN`, `BUNNY_API_KEY` (+ `_<CUSTOMER>`),
`SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID`, `MAILJET_API_KEY`,
`MAILJET_SECRET_KEY`, `CLOUDNS_AUTH_ID`/`_PASSWORD`, `CLOUDNS_SUB_AUTH_ID`/`_USER`.

CLI: `granny credentials get <NAME>` | `granny credentials status`

## Workflow

- Search before editing; follow existing patterns.
- Install hooks once per clone: `prek install` (runs ruff + pytest on commit).
- Run manually: `prek run --all-files`.
- Test AWS-touching scripts with `--dry-run` where available.
- `.env` must stay gitignored.

## Release

- Bash:       `./scripts/release.sh <patch|minor|major>`
- PowerShell: `./scripts/release.ps1 <patch|minor|major>`
