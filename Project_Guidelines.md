# Granny — Contributor Guidelines

Engineering conventions for working on `granny-devops` itself. For end-user
documentation see [`README.md`](README.md). For AI-agent conventions see
[`CLAUDE.md`](CLAUDE.md).

## Scope and shape

Granny is a single Python package (`granny`, distributed as `granny-devops`)
exposing a `granny` CLI. It wraps a handful of cloud APIs behind small,
focused subpackages:

| Subpackage | What lives here |
|---|---|
| `granny.analyze` | Read-only AWS inventory (VPCs, Lambdas) |
| `granny.cdn` | Bunny CDN client (pull zones, edge rules, SSL, cache) |
| `granny.cli` | Click command groups — thin wrappers over library code |
| `granny.cloudflare` | Cloudflare account-level resources (Workers, D1, R2, KV) |
| `granny.create` | Standalone setup scripts dispatched via `granny create <name>` |
| `granny.credentials` | Env-var + optional Vaultwarden secret resolution |
| `granny.dns` | Provider-agnostic DNS CRUD (cloudflare, bunny, hetzner, desec, cloudns, inwx, manual) |
| `granny.docker` | Multi-arch buildx with hash-derived tags |
| `granny.edge` | Bunny Edge Scripting |
| `granny.email` | Mailjet, AWS WorkMail, SES forwarding |
| `granny.serverless` | Scaleway FaaS |
| `granny.storage` | AWS S3, Bunny Storage, Hetzner S3 |

The `granny` import name is shared with the closed `pseekoo-granny-ops`
operations repo. Closed depends on `granny-devops` from PyPI; nothing
flows the other direction. Anything proprietary stays out of this repo.

## Toolchain

- Python **3.13+** (declared in `pyproject.toml`).
- `uv` for everything dependency-related; never `pip install` directly into
  the project venv.
- `ruff` for lint + format. No black, no isort.
- `prek` for pre-commit hooks (`prek install` once per clone).
- `hatchling` as the build backend.

```shell
uv sync                       # base deps
uv sync --extra dev           # dev tooling (ruff, prek, pytest, hatch, twine)
uv sync --extra all           # everything (cdn + data + ml + browser + extras)
```

Vault support (Locke) is wired in via `[tool.uv]` dev-dependencies, so
`uv sync` activates `granny.credentials`'s vault path automatically. The
published `[vault]` extra stays empty because PyPI rejects direct git URLs.

## Code conventions

- Type hints on every public function. `from __future__ import annotations`
  in modules where forward references would otherwise be awkward.
- Imports in three blocks: stdlib → third-party → local. Ruff enforces it.
- Business logic does **not** live in `granny.cli.*`. CLI modules accept
  arguments, call into the corresponding library subpackage, and format
  results. Every Click subcommand is a thin shim.
- Provider clients accept their credentials as constructor args with env-var
  fallback. Defer credential validation until the first network call so
  read-only helpers (e.g. `get_nameservers`) work without secrets configured.
  See `granny.dns.inwx.INWXDNSProvider` for the canonical pattern.
- Errors from external APIs surface as `RuntimeError` with the upstream
  status code and a snippet of the response body. Don't swallow exceptions.
- `KeyError` is reserved for "object does not exist" responses (404, INWX
  code 2302, etc.) so callers can distinguish missing from broken.
- New comments only when the *why* is non-obvious. Don't restate what the
  code already says.

## Credentials

Resolution order, defined in `granny.credentials.secrets`:

1. Environment variable (`.env` / `.deploy.env` is auto-loaded by the CLI;
   shell-exported values win over dotenv).
2. Vaultwarden via Locke, if Locke is importable and a session is reachable.
   Sessions are cached in `%LOCALAPPDATA%\granny\vault_session.json` (or
   `~/.granny/vault_session.json` on POSIX) for one hour.

Never hardcode a secret. Never call `os.environ[...]` for a registered key
— go through `get_secret(...)` so the vault path stays in play.

To register a new secret:

1. Add `MY_NEW_KEY` to `SECRET_MAP` in
   `granny/credentials/secrets.py` with its kebab-case vault path.
2. Update the registered-names list in `CLAUDE.md` and the configuration
   section of `README.md`.
3. If the secret is multi-customer (per-account fanout), add a helper next
   to `get_bunny_api_key` rather than re-implementing slug logic.

## Adding a DNS provider

1. Implement `granny/dns/<name>.py`, subclassing
   `granny.dns.base.DNSProvider`. Required: `get_zone_id`, `list_records`,
   `create_record`, `delete_record`. Override `update_record` if the
   upstream API supports atomic mutation. Override `get_nameservers`
   instead of returning `NotImplementedError` whenever the provider's NS
   set is documented and stable.
2. Register the provider in `granny/dns/factory.py` (lazy factory + entry
   in `_PROVIDERS`).
3. Add the credential keys to `SECRET_MAP` (see above).
4. Update `README.md` Features and the credentials list, and the registered
   names line in `CLAUDE.md`.
5. Smoke-test with the real CLI:
   ```shell
   granny dns nameservers <zone> --provider <name>
   granny dns list <zone> --provider <name>
   ```

`granny.dns.inwx` is the most current reference implementation (JSON-RPC,
session cookies, optional TOTP, lazy credential validation, native update).

## Adding a `create` script

1. Drop the script in `granny/create/setup_<thing>.py`. It must expose a
   module-level `main()` that parses `sys.argv` itself (argparse is fine —
   the dispatcher rebinds `sys.argv` before invoking).
2. Add an entry to `COMMANDS` in `granny/cli/create.py`. The key becomes
   `granny create <key>`.
3. If the script needs new secrets, register them as above.
4. Verify the dispatcher loads the script: `granny create <key> --help`
   should print the script's own argparse help.

## Branching, commits, release

The published flow lives in `scripts/release.sh` (Bash) and
`scripts/release.ps1` (PowerShell):

1. Day-to-day work happens on `dev`. Feature/fix branches merge into `dev`.
2. `./scripts/release.sh <patch|minor|major>` from `dev` bumps the version
   in `pyproject.toml`, commits, fast-forwards `main`, tags `vX.Y.Z`, and
   pushes both. CI (see below) takes over.
3. Tagged releases publish to **pypi.org** *and* the GitLab project's PyPI
   registry. Branch pushes publish a `0.Y.Z.devN` build to the GitLab
   registry only.

Version bumps must match across `pyproject.toml` *and*
`granny/__init__.py`'s `__version__`. The release scripts only update
`pyproject.toml`; bump `__init__.py` in the same commit by hand.

Commit messages: imperative subject under 72 chars, then a blank line and
prose explaining *why*. No conventional-commits prefix is enforced.

## CI

`.gitlab-ci.yml` has two stages:

- `lint` — `ruff check .` on MRs and `dev` branch pushes.
- `publish` — runs on default-branch pushes (dev wheel to GitLab registry)
  and on `vX.Y.Z` tags (release wheel to GitLab registry **and** PyPI).
  Publishing to PyPI requires `PYPI_API_TOKEN`; this is configured at the
  GitLab group level so individual projects inherit it.

If a tagged release retries (the GitLab registry rejects duplicate
versions), the script logs a warning and continues to the PyPI step rather
than failing the pipeline.

## Tests

There is no `tests/` directory yet. New tests should go there as
`test_*.py` and run with `uv run pytest`. Until coverage exists, the
pre-commit hook's pytest stage is effectively a no-op; that's fine — don't
add fake tests just to populate the directory.

For provider work that touches a real account, prefer a smoke-test in your
PR description (commands you ran, output you observed) over mocked unit
tests. Mocks of `requests.Session` against an upstream we don't control
tend to encode our misunderstanding of the API rather than the API itself.

## License

MIT, see [`LICENSE`](LICENSE).
