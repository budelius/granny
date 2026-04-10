# Granny Cloud Tools Collection - Claude AI Assistant Guidelines

## Project Overview

Granny is a comprehensive AWS infrastructure management and DevOps automation toolkit. When working on this project, follow these guidelines to maintain consistency and quality.

## Core Project Information

- **Primary Purpose**: AWS infrastructure automation, static website deployment, CDN management, and database schema extraction
- **Key Technologies**: Python 3.12+, AWS (boto3), Terraform, CloudFront, S3, MongoDB, Cloudflare
- **Package Manager**: Use `uv` for Python dependencies
- **Infrastructure**: Terraform-based with CloudFormation/Serverless Framework integration

## Project Structure

```
granny/
├── README.md                    # Main project documentation
├── CLAUDE.md                    # This file - Claude AI guidelines
├── Project_Guidelines.md        # Human-readable project guidelines
├── pyproject.toml              # Python project configuration
├── main.tf                     # Terraform infrastructure definitions
├── docs/                       # Detailed documentation
│   ├── AIRTABLE_EXPORT_SETUP.md   # Airtable export guide
│   ├── EXCEL_EXPORT_SETUP.md      # Excel export guide
│   ├── GOOGLE_SHEETS_SETUP.md     # Google Sheets export guide
│   └── *.md                        # Other documentation files
├── tests/                      # Test files
│   ├── test_*.py              # Unit tests (pytest/unittest)
│   └── integration/           # Integration tests
├── tools/                      # Python utility scripts
│   ├── export/                # Export utilities
│   │   ├── csv_to_platforms.py    # Main CSV export tool
│   │   └── *.py                   # Other export utilities
│   └── *.py                   # Other utility scripts
├── scripts/                    # Bash scripts
├── examples/                   # Example configurations
├── keys/                       # API keys and credentials (gitignored)
├── .cursor/rules/             # Development standards
└── modules_*/                  # Python modules
```

## Directory Organization Rules

### Documentation (`docs/`)
- **Purpose**: All technical documentation, setup guides, and API references
- **Naming**: Use `UPPERCASE_TOPIC.md` for main guides, `lowercase-topic.md` for sub-topics
- **Required Files**:
  - Setup guides for each major feature
  - API documentation for public interfaces
  - Troubleshooting guides

### Tests (`tests/`)
- **Purpose**: All test files including unit, integration, and e2e tests
- **Naming**: Test files must start with `test_` prefix
- **Structure**:
  - `test_*.py` - Unit tests for corresponding tools
  - `integration/` - Integration tests
  - `fixtures/` - Test data and mock files
- **Framework**: Use `unittest` or `pytest`
- **Coverage**: Aim for >80% code coverage

### Tools (`tools/`)
- **Purpose**: Standalone Python utilities and scripts
- **Subdirectories**: Group related tools (e.g., `export/`, `migration/`)
- **Requirements**: Each tool should be independently executable
- **Documentation**: Include docstring with usage examples

### Scripts (`scripts/`)
- **Purpose**: Bash/shell scripts for automation and deployment
- **Naming**: Use descriptive names with `.sh` extension
- **Requirements**: Must be executable (`chmod +x`)

### Keys (`keys/`)
- **Purpose**: Store API keys, credentials, and certificates
- **Security**: MUST be in `.gitignore`
- **Naming**: Use descriptive names (e.g., `service-account.json`, `airtable-api-key.txt`)

### Examples (`examples/`)
- **Purpose**: Sample configurations, data files, and usage examples
- **Structure**: Mirror the main project structure where applicable
- **Documentation**: Include README.md explaining each example

## Development Standards

### Python Development

1. **Version**: Use Python 3.12+ features
2. **Style Guide**: Follow PEP 8 strictly
3. **Type Hints**: Always use type hints for function signatures
4. **Imports**: Order as stdlib → third-party → local
5. **Documentation**: Include docstrings for all public functions/classes
6. **Error Handling**: Comprehensive try-except blocks for AWS API calls
7. **AWS Integration**: Use boto3 session management, implement pagination

### Shell Scripting

1. **Shebang**: Start with `#!/bin/bash`
2. **Error Handling**: Use `set -e` and `set -u`
3. **Security**: Never hardcode credentials
4. **Naming**: Use `.sh` extension with descriptive names

### Terraform Standards

1. **Resource Naming**: Use `granny_` prefix for all resources
2. **VPC Configuration**: CIDR 10.0.0.0/16
3. **State Management**: Remote state in S3 with DynamoDB locking
4. **Workspaces**: Use for environment separation

## Key Components and Their Usage

### 1. S3 Website Setup Tool (`tools/setup_s3_website.py`)
- Creates S3 buckets for static hosting
- Configures CloudFront distributions
- Manages SSL certificates (ACM or Cloudflare)
- Automates DNS configuration
- **Commands to run after changes**: Test with dry-run first

### 2. MongoDB Schema Extractor (`tools/mongodb_schema_extractor.py`)
- Extracts JSON schemas from collections
- Generates documentation and diagrams
- **Best for**: Database documentation, migration planning

### 3. AWS Resource Listing Tools
- `list-vpcs.py/sh`: List all VPCs
- `list-lambdas.py/sh`: Inventory Lambda functions
- **Use for**: Resource auditing and inventory

### 4. CloudFront Recovery Scripts
- Located in `scripts/` and documented in `docs/cloudfront-recovery.md`
- **Critical**: Never manually delete CloudFront distributions

## Credential Strategy

Secrets (API keys, tokens, passwords) are resolved from **environment variables only**.
Use a project-local `.env` or `.deploy.env` file, loaded automatically by the CLI
via `python-dotenv` at startup. Vault/Locke integration has been removed from
the public repo.

### How Scripts Access Secrets

Use the `granny.credentials` API, which is a thin wrapper around `os.environ`:

```python
from dotenv import load_dotenv
load_dotenv()

from granny.credentials import get_secret, get_bunny_api_key

token = get_secret("BUNNY_API_KEY")
acme_key = get_bunny_api_key(customer="acme")  # reads BUNNY_API_KEY_ACME
```

### Registered Secret Env Vars

| Environment Variable |
|---------------------|
| `CLOUDFLARE_API_TOKEN` |
| `DESEC_API_TOKEN` |
| `HETZNER_S3_ACCESS_KEY` |
| `HETZNER_S3_SECRET_KEY` |
| `HETZNER_DNS_API_TOKEN` |
| `BUNNY_API_KEY` (or `BUNNY_API_KEY_<CUSTOMER>` for per-customer keys) |
| `SCW_ACCESS_KEY` |
| `SCW_SECRET_KEY` |
| `SCW_DEFAULT_PROJECT_ID` |
| `MAILJET_API_KEY` |
| `MAILJET_SECRET_KEY` |
| `CLOUDNS_AUTH_ID` / `CLOUDNS_AUTH_PASSWORD` |
| `CLOUDNS_SUB_AUTH_ID` / `CLOUDNS_SUB_AUTH_USER` |

### CLI Commands

```bash
granny credentials get BUNNY_API_KEY       # print a single secret
granny credentials status                  # show which registered secrets are set
```

### Required Dependencies

```
python-dotenv
```

## Security Best Practices

1. **Credentials**: Read from environment variables (`.env` / `.deploy.env`). Never hardcode.
2. **IAM**: Follow least privilege principle
3. **Encryption**: Enable for data at rest and in transit
4. **SSL/TLS**: Required for all web properties
5. **Security Headers**: Implement via CloudFront Lambda@Edge
6. **`.env` files**: Must be listed in `.gitignore` and never committed

## AWS Service Configuration

### IAM Permissions Required
- S3: Full access for bucket operations
- CloudFront: Create/modify distributions
- ACM: Certificate management
- Route53: DNS management (if used)
- Lambda: Function listing and management
- VPC: Read access for listing

## Testing and Validation

1. **Before Deployment**:
   - Run Python linting: `ruff check .`
   - Test Terraform: `terraform plan`
   - Validate AWS credentials: `aws sts get-caller-identity`

2. **After Changes**:
   - Test S3 website setup with `--dry-run` flag
   - Verify CloudFront distribution status
   - Check security headers implementation

## Common Tasks and Commands

### Deploy Static Website
```bash
python tools/setup_s3_website.py example.com --cloudflare-ssl
```

### Extract MongoDB Schema
```bash
python tools/mongodb_schema_extractor.py -u "mongodb://localhost:27017" -d mydb
```

### List AWS Resources
```bash
python tools/list-vpcs.py
python tools/list-lambdas.py
```

### Terraform Workflow
```bash
terraform init
terraform plan
terraform apply
```

## Error Handling and Recovery

1. **CloudFront Issues**: Use recovery scripts in `scripts/`
2. **S3 Bucket Conflicts**: Use `--force-recreate` flag
3. **SSL Certificate Problems**: Check ACM or Cloudflare configuration
4. **Stack Failures**: Monitor CloudFormation events

## Documentation Standards

1. **Code Comments**: Minimal, only for complex logic
2. **README Files**: Keep updated with setup instructions
3. **API Documentation**: Use docstrings with examples
4. **Diagram Generation**: Use Mermaid for visualizations

## CI/CD Integration

1. **GitLab CI**: Reference `.gitlab-ci.yml` examples
2. **Security Headers**: Apply via CI pipeline
3. **Deployment Order**: staging → production
4. **Recovery**: Automated rollback on failures

## Best Practices for Claude AI

1. **File Search**: Use search tools extensively before making changes
2. **Pattern Matching**: Follow existing code patterns in the project
3. **Testing**: Always suggest testing commands after implementations
4. **Documentation**: Update relevant docs when changing functionality
5. **Security**: Never expose credentials or sensitive data
6. **Infrastructure**: Use Terraform for all infrastructure changes
7. **Error Messages**: Provide helpful context and recovery steps

## Module-Specific Guidelines

### modules_core
- Core AWS functionality
- Shared utilities

### modules_eye
- Monitoring and observation tools
- Resource inventory scripts

### modules_heart
- Central processing and orchestration
- Workflow management

### modules_llm
- AI/ML integrations
- LangChain implementations

## Workflow Recommendations

1. **Research First**: Search codebase before implementing
2. **Test Locally**: Validate changes before deployment
3. **Document Changes**: Update relevant documentation
4. **Security Check**: Ensure no credentials are exposed
5. **Error Handling**: Implement comprehensive error handling
6. **Code Review**: Follow existing patterns and standards

## References

- Main README: `/home/budelius/granny/README.md`
- Cursor Rules: `/home/budelius/granny/.cursor/rules/`
- Documentation: `/home/budelius/granny/docs/`
- Examples: `/home/budelius/granny/examples/`