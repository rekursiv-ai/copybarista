# Copybarista :coffee:

<p align="center">
  Publish and sync clean standalone repositories from private or monorepo source trees.
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/rekursiv-ai/copybarista/main/assets/copybarista-splash.webp" alt="Copybarista mascot" width="520">
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12-blue.svg">
  <a href="https://pypi.org/project/copybarista/"><img alt="PyPI" src="https://img.shields.io/pypi/v/copybarista.svg"></a>
  <a href="https://github.com/rekursiv-ai/copybarista/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/badge/ci-GitHub%20Actions-blue.svg"></a>
  <a href="https://github.com/rekursiv-ai/copybarista/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
</p>

<p align="center">
  <a href="https://github.com/rekursiv-ai/copybarista/blob/main/docs/config-reference.md">Config reference</a>
  ·
  <a href="https://github.com/rekursiv-ai/copybarista/blob/main/docs/tutorial.md">Tutorial</a>
  ·
  <a href="https://github.com/rekursiv-ai/copybarista/tree/main/examples">Examples</a>
  ·
  <a href="https://github.com/rekursiv-ai/copybarista/blob/main/docs/github-setup.md">GitHub setup</a>
  ·
  <a href="https://github.com/rekursiv-ai/copybarista/blob/main/CHANGELOG.md">Changelog</a>
  ·
  <a href="https://rekursiv.ai/blog/i-built-copybarista-in-a-day/">Blog post</a>
</p>

## Why should I use this?

Copybarista is for teams running Python that publish OSS packages from private or monorepo
source trees:

- Primary source code is embedded inside another repository, often private.
- The standalone OSS repository should be automatically assembled, not hand-maintained.
- Public fixes should sync and flow back through normal pull requests.
- The team wants Python-native tooling that fits Python packaging and CI.

Copybarista turns a selected source subtree into a clean repository tree. It
copies only the files you choose, rewrites text deterministically, and writes
either a local folder or one squash commit on a Git branch.

Use it when the private or monorepo checkout should stay canonical, but a
package, tool, or library needs to live in a separate repository. Syncs are
reviewed as pull requests, and public changes can be imported back only when
Copybarista can map and verify them safely.

## What Copybarista Does

- Publish a package from a larger repository without moving files by hand or
  maintaining custom sync scripts.
- Keep the source checkout canonical while exporting an exact public tree.
- Rewrite imports, docs, or generated blocks as part of the export.
- Use the example GitHub workflows to review syncs as pull requests instead of
  pushing to `main`.
- Bring public fixes back into source only when the reverse mapping verifies.
- Reject unsupported config instead of guessing and producing a surprising
  export.

<p align="center">
  <img src="https://raw.githubusercontent.com/rekursiv-ai/copybarista/main/assets/copybarista-sync.webp" alt="Copybarista bidirectional sync: source repository exports through copybarista export to an Export PR that auto-merges into the standalone package; public PRs and merged public changes flow back through copybarista import-change as a verified Source import PR." width="480">
</p>

Generated export PRs are workflow-owned and can auto-merge after required
checks. Public changes flow back through separate source PRs so maintainers can
review what enters the private or monorepo source of truth.

## Why not just use an Alternative Tool?

Two adjacent tools solve related parts of this problem:

- Git subtree and `git-filter-repo` with custom tooling for code transformations.
- Copybara, a mature general-purpose migration tool with a Java-based runtime.

Start with those tools first when their tradeoffs match your requirements, see
the table below for the specific differences.

Copybarista is intentionally narrowly scoped: it's a Python program which
publishes clean OSS packages from private or monorepo sources while rewriting
the exported tree and syncing through GitHub pull requests. Built and
maintained by [rekursiv.ai](https://rekursiv.ai), it manages syncs between
repositories while fitting a Python and GitHub toolchain.

Short version:

| Need | Copybarista | Copybara | Git subtree / `git-filter-repo` |
| --- | --- | --- | --- |
| Python package workflow | :white_check_mark: Built for Python repos, TOML config, Python CI | :warning: Powerful, but Java/Starlark-based | :warning: Git-first; project behavior becomes scripts |
| Rewrite imports/docs/private blocks | :white_check_mark: Built in | :white_check_mark: Broad transform model | :x: Requires custom scripts |
| GitHub PR sync in both directions | :white_check_mark: Example export/import workflows | :warning: Requires workflow glue | :x: No built-in PR workflow |
| Preserve full history | :x: Squash-style export focus | :white_check_mark: Yes | :white_check_mark: Yes |
| General migration engine | :x: Intentionally scoped | :white_check_mark: Yes | :x: No |

- Use Copybarista when the hard part is not splitting history, but producing a
  clean OSS package repository with deterministic rewrites, private-name
  checks, and GitHub PR syncs.
- Use Copybara when you need a more general purpose, broader migration
  engine.
- Use Git subtree or `git-filter-repo` when history preservation is the
  main goal and the subtree is already self-contained.

The [detailed comparison](#detailed-comparison) below lists the specific
workflow capabilities.

## Install

```bash
uv tool install copybarista
copybarista --help
```

Other options:

```bash
uv add copybarista
pipx install copybarista
pip install copybarista
```

Copybarista requires Python 3.12. Git exports also require the system
`git` executable.

## Quick Start

Create `copy.barista.toml` at the root of the source checkout:

```toml
[workflow]
name = "widget"
mode = "squash"
source_root = "packages/widget"

[destination.folder]
path = "/tmp/widget-oss"

[files]
include = ["**"]
exclude = [
  ".pytest_cache/**",
  "**/.pytest_cache/**",
  ".ruff_cache/**",
  "**/.ruff_cache/**",
  ".venv/**",
  "**/__pycache__/**",
  "*.pyc",
  "**/*.pyc",
  "dist/**",
]

[[transform]]
type = "replace"
path = "tests/test_widget.py"
before = "from monorepo.packages.widget import"
after = "from widget import"
```

Validate the config and export the standalone tree:

```bash
copybarista validate copy.barista.toml
copybarista export copy.barista.toml /path/to/source \
  --folder-dir /tmp/widget-oss
```

If `/tmp/widget-oss` already exists, pass `--force` to replace it after
Copybarista's destination safety checks:

```bash
copybarista export copy.barista.toml /path/to/source \
  --folder-dir /tmp/widget-oss \
  --force \
  --json
```

The `source_ref` argument is the checkout root. `workflow.source_root` is
resolved relative to that root, and exported files land at the destination
root. Use `[[files.copy]]` when a public package should also include selected
shared files from elsewhere in the same checkout. Use `[[files.write]]` for
small export-only files that should be materialized in the public tree without
creating fake source files.

## Common Workflows

### Set Up Package Sync

Use `init-sync` to create the package-local sync files that every exported
package can keep public:

```bash
copybarista init-sync . \
  --package-name configgle \
  --sync-label Configgle \
  --source-root packages/configgle \
  --public-repo example/configgle \
  --source-repo example/source \
  --copybarista-project-path tools/copybarista \
  --smoke-import configgle \
  --type-check-target configgle \
  --type-check-target tests
```

This writes `copy.barista.toml`, `copybarista.sync.toml`, the public import
workflow `.github/workflows/sync-to-source.yml`, and the public package
validation workflow `.github/workflows/package-validation.yml`. The package name
lives in `copybarista.sync.toml`; script and workflow names stay stable so new
packages do not need `sync_<package>.py` files or package-specific environment
names. Use `init-sync --overwrite` only when intentionally regenerating existing
sync files.

The validation workflow runs package-owned commands from `copybarista.sync.toml`.
By default it syncs dependencies, runs Ruff, codespell, ty, basedpyright,
pytest, a smoke import, and `uv build`. Override it at setup time when a package
needs different public correctness gates:

```bash
copybarista init-sync . \
  ... \
  --validation-python-version 3.12 \
  --validation-command 'uv sync --all-groups' \
  --validation-command 'uv run pytest'
```

Validate the scaffolding before wiring GitHub Actions:

```bash
copybarista check-sync-config .
copybarista write-export-workflow copybarista.sync.toml \
  --output configgle-export.yml
```

Generated export PRs can use public-safe commit metadata for their title and
body:

```text
Copybarista-PR-Scope: configgle
Copybarista-PR-Title: Prepare package release checks
Copybarista-PR-Body-Mode: append
Copybarista-PR-Body:
Adds release-tree validation and refreshes generated workflow defaults.
```

Copybarista replays relevant source commits on every export run. Missing fields
preserve previous PR state, titles use latest-value-wins semantics, and body
entries append unless `Copybarista-PR-Body-Mode: replace` is used. Source
attribution is git-native: Copybarista uses replayed source authors as the
generated export commit author and `Co-authored-by` trailers.
Ordinary commit subjects and bodies are not used as generated PR text. Only the
`Copybarista-PR-*` fields opt a string into public PR rendering; if no matching
metadata is present, Copybarista keeps the configured generic title and body.
When the public repository has `.github/PULL_REQUEST_TEMPLATE.md`, Copybarista
fills the template's `## Summary` section, marks validation/testing checklists
as complete after public validation passes, and omits human `## Checklist`
sections from automated export PRs. See
[GitHub setup](https://github.com/rekursiv-ai/copybarista/blob/main/docs/github-setup.md#pull-request-text)
for scoped multi-package blocks, the full commit-message workflow, and privacy
rules.

### Export To A Folder

Use folder export for local inspection, release checks, and tests:

```bash
copybarista export copy.barista.toml /path/to/source \
  --folder-dir /tmp/widget-oss \
  --force
```

Folder export replaces the destination contents after safety checks. Existing
destinations require `--force`; the flag never disables those safety checks.

### Export To Git

Use Git export when the standalone repository should receive one clean sync
commit. GitHub PR sync usually uses folder export into a checked-out public
repository and then opens a PR branch; `publish-git` is for local mirrors,
unprotected destinations, or deliberate non-PR workflows.

Add a Git destination:

```toml
[destination.git]
url = "file:///tmp/widget-oss.git"
branch = "main"
committer_name = "Widget Export"
committer_email = "opensource@example.com"
```

Then run:

```bash
copybarista publish-git copy.barista.toml /path/to/source
```

Git export updates a cached bare mirror, copies the transformed tree into a
temporary checkout, creates one commit when the destination changes, and pushes
it to the configured branch. Generated commits include `Copybarista-Source-Rev` when
the source checkout has a Git `HEAD`. Local `file://` remotes must already be
Git repositories or existing empty directories that Copybarista can initialize
as bare repositories.

### Import Public Changes

Use `import-change` when a public repository change needs to move back into the
source checkout:

```bash
copybarista import-change copy.barista.toml \
  --public-base /tmp/public-base \
  --public-head /tmp/public-head \
  --source-base /tmp/source-base \
  --destination /tmp/source-worktree
```

Verification is enabled by default. The source base must reproduce the public
base, and the imported destination must re-export to the public head. Failed
imports roll back touched destination paths.

### Use A Supported `copy.bara.sky`

Copybarista can directly run the supported subset of `copy.bara.sky`
workflows:

```bash
copybarista export copy.bara.sky /path/to/source \
  --folder-dir /tmp/out \
  --force
```

Or translate the workflow to TOML first:

```bash
copybarista translate copy.bara.sky --workflow export \
  --output copy.barista.toml
copybarista validate copy.barista.toml
```

## What It Supports

- Local source checkouts, passed as a CLI path.
- Local folder exports with explicit `--force` for existing destinations.
- Single-commit Git exports.
- Include/exclude globs using `*`, `**`, `?`, braces, character classes, and
  escaped literals.
- Source-root selection that moves the selected subtree to destination root.
- Multi-source assembly with `[[files.copy]]` for selected shared files.
- Literal whole-file text replacements.
- Staged file or directory moves for public tree layout adjustments.
- Marker-delimited block stripping for exact file paths.
- Native TOML configs for Copybarista workflows.
- Direct export from supported `copy.bara.sky` workflows via internal
  translation.
- Manual translation from supported `copy.bara.sky` workflows to TOML.
- Local change-request import for public repository edits.
- JSON export manifests with file hashes and transform reports.
- Strict config validation so unsupported fields fail loudly.

Copybarista supports a documented `copy.bara.sky` subset for common repository
export workflows. It is not a full migration-engine clone. Unsupported
constructs fail with explicit errors instead of being ignored.

## CLI

```text
copybarista validate CONFIG [--workflow NAME]
copybarista translate COPY_BARA_SKY [--workflow NAME] [--output CONFIG]
copybarista export CONFIG SOURCE_REF [--workflow NAME] \
  [--folder-dir DIR] [--force] [--json]
copybarista publish-git CONFIG SOURCE_REF [--workflow NAME] [--json]
copybarista check-leaks CONFIG ROOT [--workflow NAME]
copybarista import-change CONFIG --public-base DIR --public-head DIR \
  --source-base DIR --destination DIR [--workflow NAME] [--no-verify] [--json]
copybarista init-sync ROOT --package-name NAME --source-root PATH \
  --public-repo OWNER/REPO --source-repo OWNER/REPO \
  --copybarista-project-path PATH --smoke-import MODULE \
  [--sync-label LABEL] [--type-check-target PATH] \
  [--forbidden-pr-text TEXT] [--validation-python-version VERSION] \
  [--validation-command COMMAND] [--sync-user-name NAME] \
  [--sync-user-email EMAIL] [--overwrite]
copybarista check-sync-config ROOT
copybarista write-export-workflow copybarista.sync.toml [--output PATH]
```

`CONFIG` can be a Copybarista TOML file or a supported `copy.bara.sky` file.
`export` uses `--folder-dir` when supplied, otherwise
`destination.folder.path` from the config. Folder export replaces destination
contents after safety checks. Existing destinations require `--force`; the flag
never disables safety checks.

`import-change` imports a public change-request tree into a source-of-truth
checkout using the supported reversible transform subset. It requires
local public base, public head, source base, and destination checkouts so tests
and workflows can run without network access. Verification is enabled by
default: the source base must reproduce the public base, and the imported
destination must re-export to the public head. Failed imports roll back touched
destination paths.

`--workflow` selects a named workflow from `copy.bara.sky`; `publish-git`
defaults to `export_git`, and other commands default to `export`.
`check-leaks` runs the config's `[leak_check]` policy against an existing
exported tree.

## Bidirectional Sync Model

Copybarista sync is PR-based in both directions:

- Source to public: the source workflow exports the configured source root into
  a temporary tree, validates the exported checkout, and opens a PR in
  the public repository from a generated package export branch, e.g.
  `configgle/export/main`. Reruns can update the same branch so there is one
  active export PR per project branch. The generated branch is replaced with
  `git push --force-with-lease`. Export PR title/body text is rendered by
  replaying `Copybarista-PR-*` commit metadata plus configured defaults.
- Public to source: the public workflow checks out a public base and public
  head, runs `copybarista import-change`, validates the target checkout, and
  opens a source PR from a generated package import branch, e.g.
  `configgle/import/sha-<public-sha>`. Regenerating that import branch also
  uses `git push --force-with-lease`.

The public repository CI checks release-tree policy, lint, formatting, types,
unit tests, package build, and installed-wheel import. The reverse-sync
workflow also runs on trusted public PRs as an import validation check, except
for generated export PRs whose source of truth is the export workflow. Public
`main` pushes from merged generated export PRs are skipped for the same reason
when the push commit is authored by `copybarista` or the commit message
identifies a generated export branch. Reverse sync only opens a
source PR for direct public changes or manual-dispatch runs.

No workflow should push directly to a protected default branch. Generated sync
branches are workflow-owned artifacts; do not push manual commits to them.
`--force-with-lease` lets reruns replace generated commits while refusing to
overwrite unexpected remote updates. Conflicts are handled in two layers:
GitHub blocks textual PR conflicts, and
`copybarista import-change` blocks semantic conflicts such as unmapped files,
excluded paths, metadata writes, non-reversible transforms, public-base
mismatches, and re-export mismatches. VCS and `.copybarista` metadata are
ignored while diffing and refused as write targets. If both repositories
changed the same public file, import the public PR to source first or
re-export source and resolve the PR diff explicitly.

Source-to-public auto-merge is safe only as PR auto-merge after required checks
pass, not as direct default-branch pushes. Keep public-to-source imports manual
unless your project has a separate review policy for accepting public changes.
For protected branches, required checks, bot-authored PRs, and token
permissions, see
[GitHub setup](https://github.com/rekursiv-ai/copybarista/blob/main/docs/github-setup.md).

## Security And Privacy Model

Copybarista is designed for the case where the source repository may contain
private code, names, paths, or workflows that must not appear in the public
repository. The main protections are:

- Explicit file selection: only configured paths under `source_root` are
  exported, and source-only paths such as private docs, local config, caches,
  build outputs, and release tooling can be excluded.
- Deterministic transforms: private blocks, imports, package names, and
  generated Python formatting are handled from checked-in config rather than ad
  hoc scripts.
- Leak checks: optional config rules reject forbidden paths and forbidden text
  in the transformed export tree before folder or Git destinations are mutated.
- Release-tree checks: public CI can reject private directories, source-only
  config files, generated caches, bytecode, build artifacts, nested VCS
  metadata, and unstripped private README markers before release.
- PR-only sync: generated branches are reviewed through GitHub pull requests;
  workflows should not push directly to protected default branches.
- Token isolation: public-to-source validation runs without write tokens, and
  token-bearing PR creation should run trusted workflow code captured before
  imported public changes can modify source files.
- Reverse verification: imports must reproduce the public base before applying
  changes and re-export to the public head afterward. Ambiguous or unsafe
  reverse mappings are rejected.

Copybarista does not replace normal review. Treat `copy.barista.toml`, GitHub
workflow files, and release-tree policy as part of the security boundary, and
review them whenever the source/public sync scope changes.

## Compatibility

Copybarista's native config format is TOML. The preferred filename is
`copy.barista.toml`. It can also accept a supported `copy.bara.sky` workflow:
the CLI translates the supported subset internally, validates the generated
Copybarista config, and then runs the same export engine.

| Feature | Native TOML | `copy.bara.sky` import | Limits |
| --- | --- | --- | --- |
| Workflow mode | `mode = "squash"` | `mode = "SQUASH"` | Change/history modes fail |
| Source checkout | CLI `SOURCE_REF` | `folder.origin()` | Remote origins fail |
| Root move | `source_root` | `core.move(ROOT, "")` | Non-root destinations fail |
| Extra source files | `[[files.copy]]` | Not imported | Native TOML only |
| Generated files | `[[files.write]]` | Not imported | Native TOML only |
| File globs | `include` / `exclude` | `glob(..., exclude=...)` | Unsupported glob constructs fail |
| Folder export | `[destination.folder]` | `folder.destination()` | Needs `--force` for existing folders |
| Git export | `[destination.git]` | `git.destination(...)` | Single-commit export |
| Literal replace | `type = "replace"` | `core.replace(...)` | Regex/options fail |
| Staged move | `type = "move"` | Not imported | Native TOML only |
| Ruff formatting | `type = "ruff_format"` | Not imported | Runs `ruff check --fix --no-cache` and `ruff format --no-cache` |
| Strip block | `type = "strip_block"` | Empty multiline replace | Marker form only |
| Arbitrary logic | Not supported | Rejected | No Starlark execution |
| Copybara review flows | Not supported | Rejected | Use local `import-change` |

The import path is static translation, not full Starlark interpretation. When
Copybarista sees an unsupported origin, destination, mode, transform, option,
or expression, it reports a config error instead of silently changing behavior.

Supported `copy.bara.sky` forms include `authoring.pass_thru(default=...)`,
positional `git.destination("url", push="main")`, omitted `origin_files` and
`destination_files`, `core.transform([...])`, explicit replace reversal, and
`core.reverse([...])` for literal replacements.

## Detailed Comparison

| Need | Copybarista | Copybara | Git subtree / `git-filter-repo` |
| --- | --- | --- | --- |
| Python packaging ecosystem | :white_check_mark: Yes: Python package, installable with `uv`, `pipx`, or `pip` | :x: No: Java-based toolchain | :warning: Partial: Git-first workflow with optional Python `git-filter-repo` install |
| Python project ergonomics | :white_check_mark: Yes: TOML config, pytest-friendly helper scripts, Python CI fit | :warning: Partial: powerful, but configured through Starlark and a separate runtime | :warning: Partial: easy to shell out, but project-specific behavior becomes custom scripts |
| GitHub ecosystem fit | :white_check_mark: Yes: example workflows open export/import PRs for review | :warning: Possible, but requires workflow glue | :x: No built-in PR workflow |
| Select a subtree and publish it as a standalone repository | :white_check_mark: Yes: selected files land at repository root | :white_check_mark: Yes: broader repository migration model | :white_check_mark: Yes: this is the core Git subtree / filtering use case |
| Assemble a public tree from selected files and directories | :white_check_mark: Yes: include/exclude globs select the exported tree | :white_check_mark: Yes: broader file-selection model | :warning: Partial: possible with path filters, but awkward for multiple locations |
| Keep full source history | :x: No: squash-style export is the focus | :white_check_mark: Yes | :white_check_mark: Yes: this is where Git subtree and `git-filter-repo` fit best |
| Rewrite absolute Python imports for the public package | :white_check_mark: Yes: supported literal replacements | :white_check_mark: Yes: broader transform model | :x: No: Git leaves file contents unchanged |
| Normalize generated Python after rewrites | :white_check_mark: Yes: optional Ruff transform | :warning: Partial: use custom workflow commands | :x: No: Git leaves file contents unchanged |
| Strip private README sections, generated blocks, or internal names | :white_check_mark: Yes: block stripping and release-tree checks | :white_check_mark: Yes: broader transform model | :x: No: requires custom scripts and leak checks |
| Leave private files out of the public repo | :white_check_mark: Yes: explicit include/exclude globs plus export validation | :white_check_mark: Yes: broader file-selection model | :warning: Partial: path filtering helps, but deeper cleanup is custom |
| Import public fixes back into the source checkout | :white_check_mark: Yes: reverse import verifies by re-exporting | :white_check_mark: Yes: supports bidirectional repository movement | :warning: Partial: subtree can move history, but not verify semantic rewrites |
| Fail loudly on unsupported rewrites | :white_check_mark: Yes: unsupported config is rejected | :white_check_mark: Yes: full config parser and migration engine | :x: No transform model to validate |
| Full migration engine | :x: No: intentionally scoped to package sync | :white_check_mark: Yes | :x: No |

## Intentional Scope

Copybarista is designed for GitHub-oriented package sync. The supported model is:

- A private or internal checkout is the source of truth.
- A selected subtree is exported as an exact standalone repository tree.
- GitHub Actions can open or update PRs in either direction.
- Public PR imports are accepted only when paths and transforms can be mapped
  back safely and verified by re-exporting.

The following are intentional non-goals until a real workflow needs them:

- Running arbitrary Starlark.
- Supporting a full origin and destination plugin model.
- Preserving per-commit history or iterative migration modes.
- Destination-file scoped partial cleanup instead of exact-tree replacement.
- Regex-template transforms beyond the literal replacement subset.
- A general transform plugin API.
- External implementation details such as cache directory layout.
## Documentation

- [Tutorial](https://github.com/rekursiv-ai/copybarista/blob/main/docs/tutorial.md):
  build and run a minimal folder export.
- [Examples](https://github.com/rekursiv-ai/copybarista/tree/main/examples):
  set up a full source-to-public and
  public-to-source GitHub PR workflow.
- [Config reference](https://github.com/rekursiv-ai/copybarista/blob/main/docs/config-reference.md):
  TOML fields, transforms, CLI behavior, and manifest shape.
- [Architecture](https://github.com/rekursiv-ai/copybarista/blob/main/docs/architecture.md):
  implementation boundaries and internal APIs.
- [GitHub setup](https://github.com/rekursiv-ai/copybarista/blob/main/docs/github-setup.md):
  recommended repository rules, branch protection, and release publishing
  defaults.

## Development

```bash
python -B scripts/check_release_tree.py . --allow-root-git
uv sync --all-groups
uv run --all-groups pre-commit install
uv run --all-groups pre-commit run --all-files
uv run --all-groups ruff check --no-fix --no-cache .
uv run --all-groups ruff format --check --no-cache .
uv run --all-groups codespell .
uv run --all-groups ty check
uv run --all-groups basedpyright copybarista tests scripts
uv run --all-groups pytest
uv build --out-dir /tmp/copybarista-dist-check
```

Clean generated local artifacts:

```bash
uv run python scripts/clean.py
uv run python scripts/clean.py --venv
```

Run the local benchmark helper when changing file selection, glob matching, or
copy logic:

```bash
uv run python scripts/bench.py \
  examples/python-package/source-repo/copy.barista.toml \
  examples/python-package/source-repo \
  --runs 5 \
  --json
```

Unit tests live next to the modules they cover as `*_test.py`. The top-level
`tests/` directory is reserved for integration tests plus fixtures.

## Contributing

Copybarista is intentionally conservative. When adding behavior, document the
config surface, add focused tests, keep exports deterministic, and reject
unsupported config instead of guessing.

## Acknowledgements

Copybarista's repository-sync model is inspired by
[Copybara](https://github.com/google/copybara), Google's open-source tool for
transforming and moving code between repositories. Copybara is licensed under
the Apache License 2.0.

Copybarista is an independent Python implementation focused on
package-oriented GitHub PR workflows. It does not vendor or copy Copybara
source code, documentation, logos, or test data, and it is not affiliated with
or endorsed by Google or the Copybara project.
