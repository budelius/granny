# AGENTS.md — granny-devops

Operational instructions for AI coding agents working in this repository.
Tool-neutral by design (Claude Code, Cursor, Aider, Copilot, etc. all
read this file). Human contributors should read [`Project_Guidelines.md`](Project_Guidelines.md);
end users should read [`README.md`](README.md).

## What this project is

`granny-devops` is a single-package Python toolkit (`import granny`,
distributed as `granny-devops`, CLI entrypoint `granny`) that wraps the
parts of cloud-provider APIs the maintainers actually use day-to-day.
Pragmatic, not exhaustive — narrow, tested wrappers over Bunny, Cloudflare,
Hetzner, deSEC, ClouDNS, INWX, Scaleway, Mailjet, AWS S3, and a few more,
unified behind one CLI. The closed sibling `pseekoo-granny-ops` depends on
this package; nothing flows the other direction. Anything proprietary
stays out of this repo.

## Layout

```
granny/
  cli/           Click command groups -- thin shims, no business logic
  analyze/       Cross-cloud inventory (vpcs, lambdas, gpus, credits, costs)
  cdn/           Bunny CDN (pull zones, edge rules, SSL, cache)
  cloudflare/    Cloudflare Workers / D1 / R2 / KV (account-level)
  create/        Standalone setup scripts dispatched via `granny create <name>`
  credentials/   Env-var + optional Vaultwarden (Locke) secret resolution
  dns/           Provider-agnostic DNS CRUD (cloudflare, bunny, hetzner,
                 desec, cloudns, inwx, manual)
  docker/        Multi-arch buildx with hash-derived tags
  edge/          Bunny Edge Scripting
  email/         Mailjet, AWS WorkMail, SES forwarding
  serverless/    Scaleway FaaS
  storage/       AWS S3, Bunny Storage, Hetzner S3
scripts/         release.sh / release.ps1 / run-tests.py
```

## Conventions

- **Python 3.13+**, type hints on every public function, three-block imports
  (stdlib → third-party → local). Ruff enforces.
- **CLI is a shim.** Business logic lives in the matching subpackage. CLI
  modules accept arguments, call the library, format output. Don't put
  logic inline in `granny.cli.*`.
- **Provider clients accept creds via constructor with env-var fallback.**
  Defer credential validation until the first network call so read-only
  helpers (e.g. `get_nameservers`) work without secrets configured. The
  canonical reference is `granny.dns.inwx.INWXDNSProvider`.
- **Errors:** raise `RuntimeError` for upstream API failures with the
  status code and a snippet of the response body. Reserve `KeyError` for
  "object does not exist" responses (HTTP 404, INWX code 2302, etc.) so
  callers can distinguish missing from broken.
- **Secrets:** never hardcode, never `os.environ[KEY]` for a registered
  key. Always go through `granny.credentials.get_secret()` so the vault
  fallback stays in play.
- **Comments are rare.** Only write a comment when *why* is non-obvious
  (a hidden constraint, a workaround for a specific bug, a subtle
  invariant). Don't restate what the code already says.

## Credentials

Resolution order is defined in `granny/credentials/secrets.py`:

1. Environment variable. `.env` / `.deploy.env` is auto-loaded by the CLI
   (python-dotenv). Shell-exported values override dotenv.
2. Vaultwarden via Locke, if importable and a session is reachable.
   Sessions cache to `%LOCALAPPDATA%\granny\vault_session.json` (Windows)
   or `~/.granny/vault_session.json` (POSIX) for one hour.

```python
from granny.credentials import get_secret, get_bunny_api_key
token = get_secret("BUNNY_API_KEY")
acme = get_bunny_api_key(customer="acme")  # BUNNY_API_KEY_ACME
```

Registered keys (`SECRET_MAP` in `granny/credentials/secrets.py`):
`CLOUDFLARE_API_TOKEN`, `DESEC_API_TOKEN`, `HETZNER_S3_ACCESS_KEY`,
`HETZNER_S3_SECRET_KEY`, `HETZNER_DNS_API_TOKEN`,
`BUNNY_API_KEY` (+ `_<CUSTOMER>`),
`SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID`,
`MAILJET_API_KEY`, `MAILJET_SECRET_KEY`,
`CLOUDNS_AUTH_ID`/`_PASSWORD`, `CLOUDNS_SUB_AUTH_ID`/`_USER`,
`INWX_USERNAME`/`_PASSWORD`/`_SHARED_SECRET` (last only with 2FA),
`DOCKER_HUB_USER`/`_TOKEN`,
`GITLAB_PYPI_USER`/`_PASSWORD`.

Inspect what's set:

```shell
granny credentials status
granny credentials get <NAME>
```

## Workflow

- **Search before editing.** Use `Grep`/`Glob` to find existing patterns
  before introducing new ones. Match neighboring code rather than
  inventing new conventions.
- **Install hooks once per clone:** `prek install`. Runs ruff + pytest on
  commit. Run manually with `prek run --all-files`.
- **`.env` stays gitignored.** Never commit it.
- **AWS-touching scripts** support `--dry-run` where available; use it
  before the real run.

## Adding things

**A DNS provider:**
1. `granny/dns/<name>.py` subclassing `granny.dns.base.DNSProvider`.
   Required: `get_zone_id`, `list_records`, `create_record`, `delete_record`.
   Override `update_record` for atomic mutation, `get_nameservers` when
   stable. Reference: `granny.dns.inwx`.
2. Register in `granny/dns/factory.py` (lazy factory + `_PROVIDERS` entry).
3. Add credential keys to `SECRET_MAP` in `granny/credentials/secrets.py`.
4. Update `README.md` and the registered-keys list above.

**A `create` script:**
1. `granny/create/setup_<thing>.py` with module-level `main()` that parses
   `sys.argv` itself (argparse is fine).
2. Add to `COMMANDS` in `granny/cli/create.py` — key becomes the
   `granny create <key>` subcommand name.

**A registered secret:**
1. Add to `SECRET_MAP` in `granny/credentials/secrets.py` with kebab-case
   vault path. The vault is the primary store -- secrets live in
   Vaultwarden and resolve through Locke at runtime.
2. Update the registered-keys list above and in `README.md`.
3. Do **not** automatically add the new key to `.env.example`. That file
   is reserved for secrets that genuinely benefit from a per-checkout
   override (short-lived tokens, dev rotations). Registry / CI / 2FA-style
   credentials live in the vault and stay out of `.env.example`.

## Release

```shell
# from the dev branch, working tree clean:
./scripts/release.sh <patch|minor|major>      # bash
./scripts/release.ps1 <patch|minor|major>     # powershell
```

This bumps `pyproject.toml`, fast-forwards `main`, tags `vX.Y.Z`, and
pushes both. CI publishes the release wheel to **pypi.org** and the
GitLab project's PyPI registry. Branch pushes publish a `0.Y.Z.devN`
build to the GitLab registry only.

**Important:** the release scripts only update `pyproject.toml`. Bump
`granny/__init__.py`'s `__version__` in the same commit by hand, or
`granny --version` will lie.
