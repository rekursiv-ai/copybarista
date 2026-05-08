# Changelog

All notable Copybarista changes are documented here.

## Unreleased

- Added source commit metadata replay for generated export PR title, body, and
  source attribution.
- Added scoped metadata blocks so one source commit can provide different PR
  text for multiple generated package repositories.
- Added `[pull_request]` package sync settings for PR defaults, required
  metadata, replay bootstrap, and public source-revision marker policy.
- Updated generated source export workflows to fetch full source history for
  idempotent PR text replay.

## 0.1.2 - 2026-05-02

- Added transformed-tree leak checks for forbidden paths and text, including
  the `check-leaks` CLI command.
- Added multi-source export assembly with `[[files.copy]]`, destination
  prefixes, and import mapping for copied files.
- Added `move` and `ruff_format` transforms for public tree layout and
  deterministic formatting.
- Hardened GitHub sync scaffolding with validated generated branch namespaces
  and generic source-repo workflow settings.
- Strengthened release-tree validation and added a self-export release
  integration test.
- Expanded export parity coverage and release documentation for multiple
  package sync workflows.

## 0.1.1 - 2026-05-01

- Switched package builds to Hatchling and excluded test modules from wheels.
- Added standalone pre-commit, Codespell, and `ty` checks for contributors.
- Added CI coverage for spelling and `ty` validation.
- Expanded development docs with pre-commit setup and validation commands.
- Refreshed packaging metadata and lockfile for the exported public tree.

## 0.1.0 - 2026-04-30

- Initial public release.
- Added deterministic folder and Git export workflows.
- Added reversible public-change import with verification and rollback.
- Added TOML config support plus supported `copy.bara.sky` translation.
- Added GitHub sync examples, release-tree checks, documentation, and tests.
