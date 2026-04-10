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

Secrets are read from environment variables. Create a `.env` in your working
directory; it is loaded automatically.

```
BUNNY_API_KEY=...
CLOUDFLARE_API_TOKEN=...
HETZNER_S3_ACCESS_KEY=...
# ...
```

See `CLAUDE.md` for the full list of registered names.

## Standalone scripts

`tools/create/` contains setup scripts for S3+CloudFront, Bunny Storage,
Scaleway FaaS, Mailjet DNS, WorkMail, and more. Run them directly:

```shell
python tools/create/setup_s3_website.py example.com --help
```

## License

See `LICENSE`.
