# Changelog

Notable changes to granny-devops are documented here.

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

[0.18.0]: https://gitlab.com/martin-wieser/granny/-/compare/v0.17.4...v0.18.0
[0.17.4]: https://gitlab.com/martin-wieser/granny/-/compare/v0.17.3...v0.17.4
