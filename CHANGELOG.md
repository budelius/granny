# Changelog

Notable changes to granny-devops are documented here.

## [Unreleased]

### Added

- Add `granny cdn spa-cache-headers` and
  `BunnyCDNClient.configure_spa_cache_headers`, which stop browsers pinning a
  single-page app to a stale build. A pull zone's `CacheControlMaxAgeOverride`
  applies one `max-age` to every response, `index.html` included -- and that
  is the one file which must never be cached, being the map from a URL to the
  content-hashed chunk names. A browser holding an old copy keeps requesting
  the previous build's filenames, which the origin still serves, and runs
  stale code until the header expires; purging the CDN does not help, because
  the browser has been told not to ask. The zone now defaults to no browser
  caching, and the long cache is handed back to the hashed assets alone --
  matched by path rather than by extension, since in a SPA every route
  returns HTML and `/invoice-upload` looks nothing like `*.html`.

## [0.20.0] - 2026-09-24

### Added

- Resolve registered secrets from the Locke OS keystore when the environment
  and Vaultwarden have both missed. Vaultwarden is unreachable from a locked
  workstation or any non-interactive shell, because Windows Hello cannot prompt
  for the vault password there -- but the credentials are not actually absent
  in those sessions: obachan keeps the same infrastructure values in the OS
  keystore under `OBACHAN_<ENV>_<NAME>`, and its object-storage pair holds
  Hetzner Object Storage credentials. `KEYSTORE_ALIASES` maps a granny name to
  its obachan counterpart and `KEYSTORE_ENV_ORDER` expands it across
  environments; `GRANNY_KEYSTORE_ENV` pins one. The backend runs last, so it
  only ever adds an answer where there was none rather than re-pointing an
  already-working secret at a different store, and it logs which entry
  answered, since a credential arriving under a namespaced alias may be a
  production one.
- Add `HetznerS3Client.get_cors_config`, which distinguishes a bucket that has
  never been given a CORS policy (`None`) from one whose policy allows nothing
  (`[]`).

### Changed

- Let `HetznerS3Client.set_cors_config` take `methods`, `expose_headers` and
  `max_age_seconds`. It could previously only express `GET` and `HEAD`, so it
  could not configure a bucket that browsers upload to directly: an app handed
  a presigned URL sends the bytes from the page, making the hop cross-origin,
  and a `PUT` is never a CORS-simple request. The browser preflights it and,
  with no matching rule, refuses to send the upload at all -- surfacing as a
  bare network error carrying no status code, which reads like a connectivity
  fault rather than a policy one. The previous read-only behaviour remains the
  default.
- Route trusted publish jobs to the protected Obachan staging OSS runner.

## [0.19.1] - 2026-08-23

### Fixed

- Exclude `Credit` and `Refund` record types from all Cost Explorer queries
  (`aws_costs`, `aws_costs_by_service`, `aws_cost_total`, and the month-end
  forecast). Cost Explorer returns every record type unless filtered, and
  promotional credits arrive as negative line items carrying the same `SERVICE`
  dimension as the usage they offset -- so a fully credit-covered account
  reported ~0.00 spend both in total and per service, masking real burn.
  Credits/refunds are excluded via `Not` rather than a `Usage`-only whitelist so
  that Savings Plan and Reserved Instance record types are not silently dropped
  once a commitment is purchased.
- Build the Cost Explorer client in `aws_costs` through `_ce_client` instead of
  constructing a session inline. The inline session bypassed the seam tests
  patch, so `aws_costs` unit tests issued real Cost Explorer API calls; a bad or
  missing profile now degrades to a warning instead of raising mid-sweep.

## [0.18.0] - 2026-07-26

### Added

- Add `granny knative` and its `granny faas` alias for deploying, listing, inspecting, scaling, invoking, logging, and removing Knative functions.
- Add `granny knative deploy-ui` for coordinated Bunny Storage UI uploads and Knative backend deployment.
- Register `OBACHAN_API_KEY` and `OBACHAN_API_BASE_URL` for environment and Vaultwarden resolution.
- Add `granny serverless delete-function` with namespace disambiguation and interactive confirmation for Scaleway FaaS.

### Changed

- Normalize Knative API failures to bounded response snippets and distinguish missing resources with `KeyError`.

## [0.17.4] - 2026-07-22

### Added

- Add `granny airtable bases` and `granny airtable tables` for Airtable metadata discovery.
- Add `granny airtable find-text` to locate endpoint fragments in record fields.
- Add dry-run-by-default `granny airtable replace-text`, with explicit `--apply --confirm` safeguards.
- Register `AIRTABLE_API_KEY` for environment and Vaultwarden credential resolution.

[Unreleased]: https://gitlab.com/martin-wieser/granny/-/compare/v0.20.0...main
[0.20.0]: https://gitlab.com/martin-wieser/granny/-/compare/v0.18.0...v0.20.0
[0.18.0]: https://gitlab.com/martin-wieser/granny/-/compare/v0.17.4...v0.18.0
[0.17.4]: https://gitlab.com/martin-wieser/granny/-/compare/v0.17.3...v0.17.4
