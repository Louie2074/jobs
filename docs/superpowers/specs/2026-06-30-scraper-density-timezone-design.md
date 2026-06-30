# Scraper Density And Timezone Design

## Context

The June 30, 2026 observability sweep found no live app error fire, but it did find three scraper items worth acting on:

- JetBlue logs drop EZE departure times because the vendored airport timezone map lacks EZE.
- Southwest is currently blocked by 403 responses, so density must not increase there.
- Google Flights cash has already moved to the `jobs` GitHub Actions workflow, but old Fly gflights references still exist and the old service may still be emitting metrics.

## Design

Make `jobs` the primary operational change because the scheduled scrapers now live there.

1. Add EZE to every airport timezone map used by jobs award scrapers and cash matching:
   `EZE: America/Argentina/Buenos_Aires`.
2. Increase density only where recent metrics were clean:
   JetBlue gets one additional shard per scheduled run.
   Turkish and Etihad expand from a 3-day to a 5-day near-term window.
3. Leave Southwest unchanged because recent runs are blocked.
4. Leave cash capacity unchanged because the Actions workflow is already the migrated path and the old Fly path needs infra shutdown/verification before more cash load is added.
5. Update docs/comments so the current Google Flights architecture is clear: GitHub Actions is primary; Fly is legacy/bake-in and should not be scaled up.

## Validation

- Add or extend timezone tests so EZE is guarded.
- Run the focused timezone tests.
- Run the repo test suite or the closest available checks.
- After merge, dispatch the affected GitHub Actions workflows and confirm they complete.
