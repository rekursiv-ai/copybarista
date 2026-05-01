# Architecture :coffee:

Copybarista keeps the export pipeline small and explicit:

```text
config -> WorkflowRunner -> StagedTree -> destination function -> ExportManifest
```

## Workflow

`WorkflowRunner.stage(staging: Path) -> StagedTree`

The workflow runner turns a source checkout into a transformed staging tree.
It owns source-root resolution, file selection, staging, transform execution,
and manifest file mapping. Destinations receive a completed `StagedTree`; they
do not re-run selection or transforms.

This keeps folder and Git exports aligned. If another destination is added, it
should consume `StagedTree` rather than calling an existing export command.

## Module Map

The package is organized around the export pipeline rather than CLI commands:

| Module | Responsibility |
| --- | --- |
| `config.py` | Load and validate native TOML configs. |
| `copy_bara_sky.py` | Statically translate the supported `copy.bara.sky` subset. |
| `globs.py` | Validate and match source-root-relative file patterns. |
| `workflow.py` | Stage selected files, run transforms, and build manifest entries. |
| `transforms.py` | Execute deterministic staged-tree text transforms. |
| `destinations.py` | Write a completed staged tree to a local folder. |
| `git.py` | Write a completed staged tree to a single-commit Git destination. |
| `import_request.py` | Map local public changes back to the source checkout. |
| `manifest.py` | Define stable machine-readable export reports. |
| `commands.py` | Keep subprocess execution behind one typed boundary. |
| `cli.py` | Parse arguments and dispatch to package APIs. |

The main invariant is that config loading and `copy.bara.sky` translation both
produce the same `WorkflowConfig` model. Once the config is typed, the engine
does not need to know whether the user started from TOML or `copy.bara.sky`.

## File Selection

`GlobSet(include, exclude)` matches POSIX-style relative paths. The supported
glob syntax follows a documented Java-style subset for compatibility with
`copy.bara.sky`: `*`, `**`, `?`, brace alternation, character classes, and
escaped literal characters. Config validation still rejects unsafe path forms
so exports fail early instead of producing surprising file sets.

Patterns are always evaluated after `workflow.source_root` or `core.move` has
selected the subtree. That means transform paths and manifest destinations are
root-relative to the exported package, not to the original monorepo checkout.

## Transforms

Transforms mutate the staged tree and report how many files changed.

`apply_transforms` dispatches the supported transform types directly:

- `replace`: literal UTF-8 replacement.
- `strip_block`: removes a marker-delimited text block.

`TransformReport.changed` counts changed files, not replacement occurrences.
If occurrence counts become useful, add a separate field rather than changing
this meaning.

Required transforms are release gates. A required transform that matches no
files or finds no replacement text fails the export because a silent no-op
usually means the source tree drifted away from the expected public shape.
Optional transforms should be used only for known transitional files.

## Destinations

Destination functions write a completed tree and return a small result:

- `write_folder_destination(...)`
- `write_git_destination(...)`
- `DestinationResult(status, ref)`

Folder export replaces the whole destination tree after safety checks and
requires `--force` when the destination already exists. Git export follows the
same exact-tree policy for all non-`.git` worktree contents in its temporary
checkout.

Destination functions should not inspect the original source tree. They receive
`StagedTree` so folder and Git exports stay byte-for-byte aligned for the same
workflow.

## `copy.bara.sky` Syntax

`copy_bara_sky.py` is a static evaluator for a supported subset, not a Starlark
runtime. It accepts simple assignments, helper functions that emit
`core.workflow(...)`, string concatenation, lists, and supported workflow
function calls. Anything outside that shape raises a config error.

This is intentionally strict. Treating unknown constructs as no-ops would
create exports that look successful but differ from the source workflow. When
support for a new construct is added, cover both native TOML config and the
translated `copy.bara.sky` form.

## Import Requests

`import_request.py` implements the supported reverse path for local public
change requests. It compares a public base tree with a public head tree, maps
changed public paths through `workflow.source_root`, reverses literal
replacements in reverse transform order, and re-exports the result to prove it
recreates the public head. Literal replacements can use either automatic
`after` -> `before` reversal or explicit public-to-source reversal fields.

Only reversible transforms can participate. `strip_block` is rejected because
the removed source content is not present in the public tree. This keeps the
importer conservative until richer change-request semantics are needed.

Destination writes are rollback-protected. The importer snapshots touched
paths before applying the diff, restores them on any failure, and rejects VCS
metadata paths, symlink-ancestor escapes, and destination paths outside the
checkout. `--no-verify` exists for diagnosis only; normal sync workflows keep
verification enabled.

## Sync Workflows

Copybarista's own GitHub Actions workflows are thin wrappers around testable
Python scripts. Other repositories can either adapt these scripts or call the
CLI directly as shown in `examples/`:

- Source -> public calls `scripts/sync_export_pr.py`, which runs
  `copybarista export`, preserves public-repository `.github/` metadata,
  validates the exported checkout, then opens or updates a PR from a generated
  export branch in the public repository. Generated package scaffolds use
  package-owned prefixes such as `configgle/export/*`.
- Public -> source calls `scripts/sync_import_change.py`, which runs
  `copybarista import-change` against public base/head checkouts, validates the
  target checkout, then opens a source PR from a generated import branch such as
  `configgle/import/sha-<public-sha>` with public base/head and source base
  metadata when the event is allowed to write. GitHub workflows should run the
  token-bearing PR phase from a trusted helper copy captured before import, not
  from source files after public changes have been applied.

Raw reverse tree copies are intentionally avoided because they bypass path
mapping, reversible-transform checks, metadata filtering, and re-export
verification.

## Command Execution

External process execution is isolated behind `CommandRunner`. This keeps
subprocess handling consistent, avoids shell invocation, and gives tests one
place to replace Git behavior if Git export grows more complex.

Git destination code receives a `GitRuntime` object for Git executable, command
runner, cache root, and source revision label policy. Keeping those values on
the runtime avoids hidden module state and makes tests configure Git behavior
directly.

`GlobSet` also carries syntax policy such as brace-alternation strictness on
the instance. The default matches Copybarista config behavior, while tests can
exercise alternate parser policy without patching module constants.

## Growth Rules

- Add behavior behind the existing boundary first, and prefer features that
  help move code between source and GitHub repositories.
- Add a new abstraction only when one concept has multiple implementations or
  a boundary needs its own tests.
- Keep config validation strict so unsupported fields fail loudly.
- Prefer deterministic output over implicit cleanup or best-effort behavior.
