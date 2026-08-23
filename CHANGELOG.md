# Changelog

Notable changes to granny-devops are documented here.

## [Unreleased]

### Changed

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

[Unreleased]: https://gitlab.com/martin-wieser/granny/-/compare/v0.18.0...main
[0.18.0]: https://gitlab.com/martin-wieser/granny/-/compare/v0.17.4...v0.18.0
[0.17.4]: https://gitlab.com/martin-wieser/granny/-/compare/v0.17.3...v0.17.4
