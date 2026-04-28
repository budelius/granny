# Granny

**Granny** is a comprehensive Cloud DevOps toolkit designed to simplify and automate infrastructure management across multiple cloud providers. Built for developers and operations teams, Granny provides a unified command-line interface for managing AWS resources, CDN configurations, DNS records, storage solutions, and more.

## What is Granny?

Granny is your **Cloud Companion** — a collection of battle-tested tools that:

- **Analyzes** AWS resources (VPCs, Lambda functions) across all regions
- **Manages** Bunny CDN, including pull zones, cache purging, SSL certificates, and edge rules
- **Controls** DNS records across multiple providers (Cloudflare, Bunny, Hetzner, deSEC, ClouDNS)
- **Provisions** cloud infrastructure with reusable setup scripts
- **Builds** multi-architecture Docker images with deterministic tagging
- **Deploys** serverless functions to Scaleway FaaS
- **Handles** object storage across AWS S3, Bunny Storage, and Hetzner S3
- **Manages** email services (Mailjet sender setup, contact management, WorkMail users)
- **Supports** Bunny Edge Scripting for advanced CDN customization

Whether you're managing a single project or orchestrating infrastructure across multiple cloud providers, Granny provides the tools you need in one cohesive package.

## Features

| Category | Capabilities |
|----------|--------------|
| **Analysis** | List VPCs and Lambda functions across AWS regions |
| **CDN** | Bunny CDN pull zone CRUD, cache purging, SSL certificates, edge rules |
| **DNS** | Multi-provider DNS management (Cloudflare, Bunny, Hetzner, deSEC, ClouDNS) |
| **Storage** | AWS S3, Bunny Storage, Hetzner S3 bucket and file management |
| **Serverless** | Scaleway Functions (FaaS) deployment and management |
| **Docker** | Multi-arch image builds with deterministic hash-based tagging |
| **Email** | Mailjet DNS setup, contact management, AWS WorkMail user administration |
| **Edge** | Bunny Edge Script creation, deployment, and environment management |
| **Credentials** | Secure secret management with environment variable resolution |
| **Infrastructure** | Reusable setup scripts for common cloud patterns |

## Quick Start

### Installation

```shell
# Clone the repository
git clone https://github.com/your-org/granny-public.git
cd granny-public

# Install dependencies with uv
uv sync

# Or install globally from PyPI
pip install granny-devops
```

Tagged releases are published to both pypi.org and the public GitLab PyPI registry. If PyPI has not propagated a fresh release yet, use the GitLab registry fallback:

```shell
pip install --extra-index-url https://gitlab.com/api/v4/projects/81189862/packages/pypi/simple granny-devops
```

### Configuration

Granny reads secrets from environment variables. Copy the example file and fill in the values:

```shell
cp .env.example .env
```

Edit `.env` with your API keys and credentials. The following environment variables are supported:

- **Bunny**: `BUNNY_API_KEY` (plus per-customer `BUNNY_API_KEY_<SLUG>`)
- **Cloudflare**: `CLOUDFLARE_API_TOKEN`
- **deSEC**: `DESEC_API_TOKEN`
- **Hetzner DNS**: `HETZNER_DNS_API_TOKEN`
- **Hetzner S3**: `HETZNER_S3_ACCESS_KEY`, `HETZNER_S3_SECRET_KEY`
- **Scaleway**: `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID`
- **Mailjet**: `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`
- **ClouDNS**: `CLOUDNS_AUTH_ID`, `CLOUDNS_AUTH_PASSWORD`, `CLOUDNS_SUB_AUTH_ID`, `CLOUDNS_SUB_AUTH_USER`
- **Docker Hub**: `DOCKER_HUB_USER`, `DOCKER_HUB_TOKEN`

### Basic Usage

```shell
# Get help for all commands
granny --help

# Analyze AWS resources
granny analyze vpcs              # List all VPCs across regions
granny analyze lambdas          # List all Lambda functions

# Manage Bunny CDN
granny cdn purge 12345           # Purge cache for pull zone 12345
granny cdn list-zones          # List all pull zones
granny cdn create-zone my-zone  # Create a new pull zone

# Manage DNS records
granny dns list --domain example.com --provider cloudflare

granny dns add api.example.com --type A --provider hetzner

# Manage storage
granny storage bunny list      # List Bunny storage zones
granny storage aws create my-bucket --website

# Build Docker images
granny docker build-base --image myapp-base --hash-file requirements.txt

# Deploy serverless functions
granny serverless deploy my-function --source-dir ./dist

# Manage credentials
granny credentials status       # Show which secrets are configured
granny credentials get BUNNY_API_KEY
```

## CLI Command Reference

For detailed documentation of all CLI commands, see the [Documentation](#documentation) section below.

## Use Cases

### 1. Multi-Cloud Infrastructure Analysis

Quickly audit your AWS resources across all regions without logging into the console:

```shell
# List all VPCs with their CIDR blocks and regions
granny analyze vpcs --json-output

# Find all Lambda functions with their runtimes
granny analyze lambdas --region us-east-1 eu-west-1
```

### 2. CDN Management

Manage Bunny CDN pull zones, cache, and SSL certificates:

```shell
# Purge cache for a specific pull zone
granny cdn purge 12345

# List all pull zones
granny cdn list-zones

# Create a new pull zone with origin
granny cdn create-zone "My Website" --origin-url https://my-origin.com

# Add a custom hostname
granny cdn add-hostname 12345 www.example.com

# Request SSL certificate
granny cdn ssl www.example.com --dns01
```

### 3. DNS Management

Manage DNS records across multiple providers with a unified interface:

```shell
# List all records for a domain
granny dns list --domain example.com --provider cloudflare

# Add an A record (auto-detects public IP)
granny dns add api.example.com --provider hetzner --domain example.com

# Add a CNAME record
granny dns add www.example.com --type CNAME --value example.com --provider bunny

# Delete a record
granny dns delete old.example.com --type A --provider cloudflare --yes
```

### 4. Storage Management

Work with object storage across AWS S3, Bunny Storage, and Hetzner:

```shell
# Create a Bunny storage zone
granny storage bunny create my-assets --region DE

# Upload a file
granny storage bunny upload my-zone ./assets/image.png --password my-password

# Create an S3 bucket with website hosting
granny storage aws create my-website --website

# Create a Hetzner S3 bucket
granny storage hetzner create my-bucket --region fsn1 --public
```

### 5. Serverless Deployments

Deploy functions to Scaleway FaaS:

```shell
# Create a namespace
granny serverless create-namespace my-app

# Create a function
granny serverless create-function my-namespace my-function --runtime node20

# Deploy code
granny serverless deploy my-function --source-dir ./dist --namespace my-app
```

### 6. Docker Builds

Build and push multi-architecture Docker images:

```shell
# Build a base image with hash-based tagging
granny docker build-base \
  --image myapp-base \
  --hash-file requirements.txt \
  --hash-file package.json \
  --secret npmtoken=NPM_TOKEN \
  --login-docker-hub
```

### 7. Email Management

Configure Mailjet and AWS WorkMail:

```shell
# Setup Mailjet DNS for a domain
granny email mailjet setup-dns example.com

# Check Mailjet contact status
granny email mailjet check-contact user@example.com

# List WorkMail users
granny email workmail list-users example.com

# Create a WorkMail user
granny email workmail create-user example.com --email user@example.com --display-name "John Doe"
```

## Documentation

Full documentation is available in the `docs/` directory:

- [📋 CLI Reference](docs/cli.md) — Complete list of all CLI commands
- [🔍 Analytics](docs/analytics.md) — AWS resource analysis
- [🚀 CDN Management](docs/cdn.md) — Bunny CDN operations
- [📦 Create Scripts](docs/create.md) — Infrastructure provisioning
- [🔐 Credentials](docs/credentials.md) — Secret management
- [🌐 DNS Management](docs/dns.md) — Domain and record management
- [🐳 Docker](docs/docker.md) — Multi-arch image builds
- [⚡ Edge Scripting](docs/edge.md) — Bunny Edge Script management
- [📧 Email](docs/email.md) — Mailjet and WorkMail
- [☁️ Serverless](docs/serverless.md) — Scaleway FaaS
- [💾 Storage](docs/storage.md) — Object storage management

## Standalone Scripts

The `granny/create/` directory contains standalone setup scripts for various cloud services:

- `setup_aws_cloudfront.py` — Setup AWS CloudFront with security headers
- `setup_bunny_edge_script.py` — Configure Bunny Edge Scripting
- `setup_bunny_storage.py` — Setup Bunny Storage
- `setup_cognito_identity_pool.py` — Create Cognito identity pools
- `setup_hetzner_bunny.py` — Hetzner + Bunny integration
- `setup_mailjet_dns.py` — Configure Mailjet DNS settings
- `setup_private_cdn.py` — Setup private CDN
- `setup_s3_website.py` — Configure S3 website hosting
- `setup_scaleway_faas.py` — Setup Scaleway Functions
- `setup_workmail.py` — Setup AWS WorkMail
- `manage_mailjet_contacts.py` — Manage Mailjet contacts
- `auto_certificate.py` — SSL certificate automation

Run them directly:

```shell
python -m granny.create.setup_s3_website example.com --help
```

Or via the CLI:

```shell
granny create s3-website example.com --help
```

## Project Structure

```
granny/
├── cli/              # CLI command modules
│   ├── analyze.py     # AWS resource analysis
│   ├── cdn.py        # Bunny CDN management
│   ├── create.py     # Infrastructure provisioning
│   ├── credentials.py # Secret management
│   ├── docker.py     # Docker build commands
│   ├── dns.py        # DNS management
│   ├── edge.py       # Bunny Edge Scripting
│   ├── email.py      # Email management
│   ├── main.py       # CLI entrypoint
│   ├── serverless.py # Serverless functions
│   └── storage.py    # Storage management
├── analyze/          # AWS analysis modules
├── cdn/              # CDN provider modules
├── create/           # Setup scripts
├── credentials/      # Secret management
├── dns/              # DNS provider modules
├── docker/           # Docker build utilities
├── edge/             # Edge Scripting modules
├── email/            # Email service modules
├── serverless/       # Serverless provider modules
├── storage/          # Storage provider modules
└── report.py         # Reporting utilities
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests (`uv run pytest`)
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## Support

For issues, questions, or feature requests:

- Open an issue on GitHub
- Check the [documentation](docs/) for detailed guides
- Run `granny --help` or `granny <command> --help` for CLI usage

---

**Granny** — Making cloud infrastructure management simple again.

*Built with ❤️ by Martin Wieser and contributors.*
