# granny

Cloud tools collection — AWS infrastructure, CDN, DNS, and DevOps automation.

## Install

```shell
uv sync
```

## CLI

```shell
granny --help
granny analyze vpcs               # list VPCs across regions
granny analyze lambdas            # list Lambdas across regions
granny cdn purge <pull-zone-id>   # purge Bunny CDN cache
granny credentials status         # show which secrets are set
```

## Configuration

Secrets are read from environment variables. Copy the example file and fill in
the values you need — `.env` and `.deploy.env` are loaded automatically.

```shell
cp .env.example .env
```

Registered names: `BUNNY_API_KEY` (+ per-customer `BUNNY_API_KEY_<SLUG>`),
`CLOUDFLARE_API_TOKEN`, `DESEC_API_TOKEN`, `HETZNER_S3_ACCESS_KEY`,
`HETZNER_S3_SECRET_KEY`, `HETZNER_DNS_API_TOKEN`, `SCW_ACCESS_KEY`,
`SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID`, `MAILJET_API_KEY`,
`MAILJET_SECRET_KEY`, `CLOUDNS_AUTH_ID`, `CLOUDNS_AUTH_PASSWORD`,
`CLOUDNS_SUB_AUTH_ID`, `CLOUDNS_SUB_AUTH_USER`.

Library access:

```python
from granny.credentials import get_secret, get_bunny_api_key

token = get_secret("BUNNY_API_KEY")
acme = get_bunny_api_key(customer="acme")  # reads BUNNY_API_KEY_ACME
```

## Standalone scripts

`tools/create/` contains setup scripts for S3+CloudFront, Bunny Storage,
Scaleway FaaS, Mailjet DNS, WorkMail, and more. Run them directly:

```shell
python tools/create/setup_s3_website.py example.com --help
```

## License

See `LICENSE`.
