# Granny

> One CLI for the cloud chores you'd otherwise do across six tabs.

`granny` is a pragmatic, multi-provider DevOps toolkit with a strong bias
toward European cloud infrastructure. It wraps the parts of provider APIs
you actually use day-to-day — Bunny pull zones, Cloudflare DNS, Hetzner S3,
Scaleway functions, Mailjet sender setup, INWX zones, AWS Lambda inventory
— behind a single command:

```shell
granny dns add api.example.com --type A --value 203.0.113.4 --provider hetzner
granny cdn purge 12345
granny storage bunny upload my-zone ./dist
granny credentials status
```

## What it is

- **Pragmatic, not exhaustive.** Granny implements the parts of each
  provider API the maintainers needed in production. It will not cover
  every endpoint of every service — and it will not pretend to. Add what
  you need; the [contributor guide](Project_Guidelines.md) explains how.
- **Multi-cloud, with a European tilt.** First-class support for Bunny,
  Cloudflare, Hetzner, deSEC, ClouDNS, INWX, Scaleway, and Mailjet
  alongside AWS S3 / Lambda / WorkMail. The cloud world doesn't end at
  AWS, and granny doesn't pretend it does.
- **Secrets done right.** Every credential goes through one resolver
  chain — environment variable first, optional Vaultwarden vault second,
  never hardcoded. `.env` and `.deploy.env` are auto-loaded; vault support
  is a one-line activation when you want it.
- **Library or CLI.** Every CLI subcommand is a thin shim over a small
  Python module. Import `granny.dns.cloudflare`, `granny.cdn.bunny`,
  `granny.cloudflare.d1` directly when you need to script something the
  CLI doesn't expose yet.
- **Small, composable, no plugin system.** The whole package is one
  `pip install` away. No daemon, no service, no opinionated framework.
  Drop it into your CI image and call it a day.

## Install

```shell
pip install granny-devops
# or
uv add granny-devops
```

Optional extras enable additional providers:

```shell
pip install "granny-devops[gcp]"     # GCP for granny analyze (gpus|credits|costs)
pip install "granny-devops[azure]"   # Azure for granny analyze (gpus|credits|costs)
pip install "granny-devops[cdn]"     # Cloudflare, Hetzner DNS
```

Tagged releases land on **pypi.org** and the public GitLab PyPI registry
in parallel. If PyPI is propagating slowly, fall back to the registry:

```shell
pip install --extra-index-url https://gitlab.com/api/v4/projects/81189862/packages/pypi/simple granny-devops
```

For Vaultwarden (Locke) credential resolution, install separately:

```shell
pip install "locke @ git+https://gitlab.com/martin-wieser/locke.git#subdirectory=python"
```

## Configure

Granny reads secrets from environment variables, falling back to a
Vaultwarden vault when Locke is installed. Copy the example and fill in
what you need:

```shell
cp .env.example .env
```

Common keys:

| Provider | Variables |
|---|---|
| Bunny | `BUNNY_API_KEY` (+ `BUNNY_API_KEY_<CUSTOMER>` for multi-account) |
| Cloudflare | `CLOUDFLARE_API_TOKEN` |
| Hetzner | `HETZNER_S3_ACCESS_KEY`, `HETZNER_S3_SECRET_KEY`, `HETZNER_DNS_API_TOKEN` |
| Scaleway | `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID` |
| Mailjet | `MAILJET_API_KEY`, `MAILJET_SECRET_KEY` |
| deSEC | `DESEC_API_TOKEN` |
| ClouDNS | `CLOUDNS_AUTH_ID`/`_PASSWORD` (or `_SUB_AUTH_ID`/`_SUB_AUTH_USER`) |
| INWX | `INWX_USERNAME`, `INWX_PASSWORD`, `INWX_SHARED_SECRET` (only with 2FA) |
| Docker Hub | `DOCKER_HUB_USER`, `DOCKER_HUB_TOKEN` |

Set only the ones you need. Use `granny credentials status` to verify
what's configured at any time.

## Use

```shell
granny --help                          # see every command group
granny <group> --help                  # drill into one
```

The CLI is organized by capability, not by provider. You pick the
provider per command:

```shell
# DNS — same command, any of seven providers
granny dns list example.com --provider cloudflare
granny dns add www.example.com --type CNAME --value example.com --provider bunny
granny dns nameservers example.com --provider inwx

# CDN — Bunny pull zones
granny cdn list-zones
granny cdn purge 12345
granny cdn ssl www.example.com --dns01

# Object storage — three providers, one verb set
granny storage bunny create my-assets --region DE
granny storage hetzner create my-bucket --region fsn1 --public
granny storage aws create my-website --website

# AWS inventory (read-only)
granny analyze vpcs --json-output
granny analyze lambdas --region us-east-1 eu-west-1

# Cross-cloud GPU / credit / cost inventory (AWS + GCP + Azure)
granny analyze gpus                                # running GPUs everywhere
granny analyze gpus --filter h100,h200             # find Hopper clusters
granny analyze gpus --include-reserved             # also capacity blocks / RIs
granny analyze gpus --provider aws --profile prod --profile dev
granny analyze credits                             # available balances
granny analyze costs                               # MTD + month-end forecast

# AWS Capacity Blocks for ML -- discover available H100/H200/A100 blocks
granny analyze capacity-blocks --instance-type p5.48xlarge --hours 24
granny analyze capacity-blocks --instance-type p5e.48xlarge --count 2 --hours 168

# Cloudflare account resources (Workers, D1, R2, KV)
granny cloudflare d1 create my-app
granny cloudflare r2 create my-app-media
granny cloudflare site provision my-app --secret-from-vault MAILJET_API_KEY

# Multi-arch Docker builds with deterministic tags
granny docker build-base --image myapp-base --hash-file requirements.txt

# Scaleway FaaS
granny serverless deploy my-fn --source-dir ./dist --namespace my-app

# Mailjet + WorkMail
granny email mailjet setup-dns example.com
granny email workmail create-user example.com --email user@example.com

# One-shot infrastructure provisioning (dispatches granny/create/*.py)
granny create s3-website example.com --help
granny create scaleway-container --name my-app --port 3000
granny create mailjet-dns example.com
```

## Capability matrix

| Capability | Providers |
|---|---|
| DNS | Cloudflare, Bunny, Hetzner, deSEC, ClouDNS, INWX, manual |
| CDN | Bunny |
| Edge scripting | Bunny |
| Object storage | AWS S3, Bunny Storage, Hetzner S3 |
| Serverless functions | Scaleway FaaS, Scaleway Containers |
| Workers / KV / D1 / R2 | Cloudflare |
| Email send infra | Mailjet, AWS SES, AWS WorkMail |
| AWS inventory | VPCs, Lambdas |
| Cross-cloud inventory | GPU instances + reservations, credit balances, MTD spend + forecast (AWS, GCP, Azure) |
| SSL automation | Bunny, Cloudflare, ACM |

## As a library

Every CLI command is a thin wrapper. Import the underlying module when
the CLI doesn't have what you need:

```python
from granny.dns.factory import get_provider
from granny.cloudflare.d1 import D1Client
from granny.credentials import get_secret, load_secrets_into_env

# Multi-provider DNS
dns = get_provider("inwx")
zone = dns.get_zone_id("example.com")
dns.upsert_record(zone, "_acme-challenge", "TXT", "abc-token", ttl=300)

# Cloudflare D1 directly
db = D1Client().create_database("my-app", primary_location_hint="weur")

# Bulk-load registered secrets into os.environ for downstream tools
load_secrets_into_env()
```

## Project layout

```
granny/
  cli/           Click command groups (granny <group> <verb>)
  analyze/      Cross-cloud inventory (AWS, GCP, Azure)
  cdn/          Bunny CDN
  cloudflare/   Cloudflare Workers / D1 / R2 / KV
  create/       Standalone setup scripts (granny create <name>)
  credentials/  Env + vault secret resolution
  dns/          Provider-agnostic DNS CRUD
  docker/       Multi-arch image builds
  edge/         Bunny Edge Scripting
  email/        Mailjet, WorkMail, SES forwarding
  serverless/   Scaleway FaaS
  storage/      Object storage (AWS / Bunny / Hetzner)
```

## Where to look next

- [`Project_Guidelines.md`](Project_Guidelines.md) — contributor toolchain,
  conventions, extension checklists, release flow.
- [`AGENTS.md`](AGENTS.md) — instructions for AI coding agents working
  in the repository (Claude Code reads `CLAUDE.md` which imports this).
- `.env.example` — full list of supported environment variables.
- `granny --help` and `granny <group> --help` — authoritative command docs,
  always in sync with the installed version.

## License

MIT — see [`LICENSE`](LICENSE). Built and maintained by Martin Wieser.
