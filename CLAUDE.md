# Granny - Claude AI Guidelines

Cloud tools collection for AWS/CDN/DNS automation. Python 3.13+, `uv` for deps.

## Layout

```
granny/          # Installable package (CLI entrypoint: `granny`)
  cli/           # Click command groups: analyze, cdn, create, credentials
  analyze/       # AWS resource listing (vpcs, lambdas)
  credentials/   # Env-var secret resolution
tools/create/    # Standalone setup scripts (S3/CloudFront, Bunny, Scaleway, ...)
tests/
```

## Conventions

- **Python**: 3.13+, PEP 8, type hints on public functions, stdlib → third-party → local imports.
- **CLI**: Click groups registered in `granny/cli/main.py`. Keep business logic out of CLI modules.
- **Errors**: Wrap AWS/HTTP calls; surface actionable messages.
- **Secrets**: Never hardcode. Read via `granny.credentials.get_secret()` or `os.environ`.

## Credentials

Env-var only. `.env` / `.deploy.env` is auto-loaded by the CLI.

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
- Run `ruff check .` before committing.
- Test AWS-touching scripts with `--dry-run` where available.
- `.env` must stay gitignored.
