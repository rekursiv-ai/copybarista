---
name: copybarista
description: ALWAYS invoke this skill when editing a copy.barista.toml, copybarista.sync.toml, transforms, fences, leak-check text, or [[files.copy]] entries -- anything changing WHAT ships. Do not edit export config blind -- read the whole toml first.
---

# Copybarista Export Mechanics

Copybarista publishes monorepo subtrees as standalone public packages. This
skill covers what ships, how it is rewritten, and the traps in changing
either.

## Does this file export?

Discover configs and package metadata rather than trusting a fixed list
or a shallow glob:

```bash
fdfind --hidden pyproject.toml
fdfind --hidden copy.barista.toml
fdfind --hidden copybarista.sync.toml
```

preferring builtin glob tool if available.

Inspect each package's sync config and workflows to determine which exports
are active. A config can exist without an active publishing workflow; keep
inactive configs in step when changing shared export conventions.

Check `source_root`, includes, and excludes to determine what ships. A file
OUTSIDE a package root can still ship if its config copies it -- for example,
type stubs or shared utility modules.

Editing an exported file means three extra obligations:

1. Public users have no monorepo. Read the package's own leak-check rules:
   patterns may cover selected import namespaces, slash-form paths, or
   checkout paths rather than every possible monorepo reference.
2. Read `requires-python` in the public metadata; the public floor may
   differ from the monorepo's interpreter.
3. Test behavior must survive the export -- a fixture or conftest the
   monorepo provides is not automatically there.

## Depending on a sibling package

Inspect public dependencies and copy rules to distinguish sibling imports
from vendored code.

The monorepo hides the dependency: both sides resolve from the worktree. The
export does not. For a consumer using git-backed sibling dependencies and
PyPI releases, check FOUR coupled locations (paths below are examples):

| File | Edit |
|---|---|
| `.export/pyproject.toml` | floor in `project.dependencies` + git source in `[tool.uv.sources]` |
| `.export/uv.lock` | `uv lock --upgrade-package <sibling>` |
| `copybarista.sync.toml` | wheel smoke gets `--with "<sibling> @ git+https://github.com/example-org/<sibling>"` |
| `.export/.github/workflows/publish-pypi.yml` | a step resolving the wheel against PyPI ALONE |

Why the split. `tool.uv.sources` makes a uv install FROM GITHUB resolve the
sibling from the configured git revision, so a consumer can use an unreleased symbol
without cutting a release first. It never enters wheel metadata --
`Requires-Dist` still names the floor -- so PyPI is unaffected. That asymmetry
is the point, and it forces the two gates apart:

- If exports must support unreleased sibling changes, install the sibling from git.
  Left resolving from PyPI, a packaging smoke test silently doubles as "every
  dep is already released" and blocks routine pushes.
- The publish gate runs once, before an irreversible upload, so it must resolve
  from PyPI alone. Without it a consumer importing an unpublished symbol ships
  broken.

Traps:

- **`validation_commands` feeds generated workflows.** After editing it, run
  `write-public-workflows <sync.toml>` AND
  `write-export-workflow <sync.toml> --output .github/workflows/export-<pkg>.yml`.
  Inspect the sync config for output paths and run the repository's workflow
  parity checks after regenerating both.
- **`uv lock` will not advance an already-locked branch source.** It DOES adopt
  a newly-added one. `--upgrade-package` is correct in both cases.
- **Plain `pip` ignores `tool.uv.sources` entirely.** pip users always get the
  PyPI sibling, from git or not. A PEP 508 direct reference in
  `project.dependencies` would reach pip but permanently blocks PyPI upload
  (`Can't have direct dependency`, warehouse `forklift/metadata.py`), extras
  included.

For release ordering, inspect the project's release procedure; publish the
required sibling version before a consumer that depends on it.

## Core rule: read the whole config first

**Read `copy.barista.toml` end to end before editing it.** Not a grep, not
a `sed -n` window. Configs contain ordered rules; a keyword search shows
you a match and can hide rules that already cover your case.

Failure this prevents: grepping for `ruff_format`, concluding a repo-wide
`strip_block` is missing, and adding a duplicate of an existing rule.
The duplicate can pass local checks because it is a no-op.

Before adding ANY rule, state which existing rule is closest and why it
does not cover the case. If you cannot name one, you have not read enough.

## Pipeline order

`workflow.py:91-144` runs exactly this sequence. Transforms see the staged
tree, never the source checkout:

1. `files.include` / `files.exclude` select from `source_root`
2. `files.moves` relocate within the tree
3. `files.copy` pull in paths from OUTSIDE `source_root` (repo-relative)
4. `files.write` emit generated files
5. `transforms` apply **in config order** (`transforms.py:63-71`)
6. `leak_check` runs LAST, on the transformed tree (`workflow.py:140`)

This is PHASE order, not file order. Every `[[files.copy]]` runs before every
transform (`workflow.py:105-119` vs `:126`) no matter where it sits in the
TOML -- so copies can appear after `ruff_format` without running after it.

Two consequences that bite:

- A PYTHON text transform placed after the `ruff_format` entry ships its own
  mess, because only transforms preceding it get formatted. This does NOT
  generalize: later transforms may act on non-Python text or files ruff
  never touches. Inspect their paths before changing their order.
- `forbidden_text` is checked post-transform, so a forbidden reference
  removed by a configured fence is fine; one left in a shipped comment is not.

## Config surface the examples omit

Inspect these four settings; they are not inferable from a rule you copy:

- **`globstar = "zero_or_more"`** (`config.py:263,320`) -- the default
  `one_or_more` makes `**/*.py` MISS root-level modules, so a package whose
  modules sit flat at the export root needs it for that pattern. Governs
  whether your new transform's `path` matches anything.
- **`use_default_python_excludes = true`** (`config.py:40-65`) -- needed by any
  directory `[[files.copy]]` that does NOT already constrain itself with an
  `include` allowlist. Without either, a developer's `__pycache__` ships and a
  `.export/.venv` symlink pointing outside the source root hard-fails the
  export (`workflow.py:366-370`).
- **`leak_check.forbidden_path`** (`config.py:566-588`) -- a `paths` glob list,
  not a regex. Use it to block a source-only FILE from shipping.
  `forbidden_text` cannot express this.
- **`reversible = false`** (`config.py:804`) -- a `regex_groups` replace must
  compile in BOTH directions, since import replays it swapped. A one-way rule
  raises `replace is not reversible: ... Set reversible = false`
  (`config.py:810-817`). A literal replace may instead give explicit
  `reverse_before`/`reverse_after` (`config.py:783-790`).

## The `.export/` staging convention

A package can keep public repo metadata (`pyproject.toml`, `uv.lock`,
`.github/workflows/`, `SECURITY.md`) in `<source_root>/.export/`.
Inspect the config to confirm that transforms leave it verbatim.
Two coupled entries stage it at the export root:

```toml
files.exclude = [".export/**"]   # keep it out of the normal sweep

[[files.copy]]
source = "packages/widget/.export"  # then place it at the export root
destination = "."
use_default_python_excludes = true
```

Drop the exclude and the sweep ALSO stages it under the package prefix, so the
tree double-ships. Check for a `leak_check.forbidden_path` on
`"**/.export/**"`; without it, the duplication can pass silently.
When this convention is used, sibling-dependency edits target
`.export/pyproject.toml`, not a transform.

## Transform types

All in `transforms.py:93-132`. Every one takes `path`, `id`, and `required`.

| type | Does | Extra fields |
|---|---|---|
| `replace` | Literal, `regex_groups`, or `module` substitution | `before`, `after`, `regex_groups`, `module`, `reversible`, `reverse_before`, `reverse_after` |
| `strip_block` | Delete `start`..`end` regions | `start`, `end`, `inclusive`, `else` |
| `internal_lines` | Delete every line carrying `start` | `start` |
| `uncomment` | Uncomment marked lines/blocks (public-only code) | `start`, `end` |
| `move` | Relocate a staged path | `destination` |
| `ruff_format` | `ruff check --fix` then `ruff format` | -- |

Two traps in those shared fields:

- **`path` is a glob only for the text transforms.** `move` and `ruff_format`
  reject glob syntax outright -- `move path must be an exact file or directory`
  (`config.py:836-837`, `config.py:855-856`).
- **`required = true` fails on no CHANGE, not no MATCH.** A transform whose
  glob matched files but rewrote nothing still raises
  `made no changes: found files, but no replacement text`
  (`transforms.py:203-212`). Set `required = false` when a no-op is expected.

**Renaming a vendored module: use `module = true`, never a literal twin
pair.** Python spells one module two ways -- the dotted token
(`lazy_import("acme.shared.paths")`, `acme.shared.paths.data_dir()`) and
`from acme.shared import paths` -- and a literal rule on the dotted token
misses the second. Hand-written twins per leaf are easy to miss, leaving
a monorepo import in the export. `module = true` with dotted
`before`/`after` derives every spelling (including the leaf inside a comma list
or parenthesised block) from one rule and reverses the same way
(`template.compile_module_replace`).

`inclusive = true` also collapses the blank-line gap the cut leaves
(`transforms.py:736-739`), so a stripped block does not strand blank
lines. `else` turns strip into "keep the commented else-branch instead"
(`transforms.py:511-558`) -- useful when public needs a DIFFERENT body,
not just less.

## Shipping a file the export needs

A file outside `source_root` (type stubs or shared utility modules) ships
via `[[files.copy]]`:

```toml
[[files.copy]]
source = "shared/paths.py"          # repo-relative
destination = "widget/lib/paths.py" # export-relative
```

If most of the file is monorepo-only, fence it rather than forking it --
one source of truth beats two that drift:

```python
# copybarista:internal:start
from acme.distributed.testing import WorkerPool
# copybarista:internal:end

import os  # keeps shipping
```

Two marker families; configs can wire these four forms to selected paths:

| Marker | Effect |
|---|---|
| `# copybarista:internal:start` / `:end` | delete the block (`strip_block`) |
| `# copybarista:internal` (line suffix) | delete that line (`internal_lines`) |
| `# copybarista:external:start` / `:end` | UNcomment the block -- public-only code |
| `# copybarista:external` (line suffix) | uncomment that line |

`external` is how public gets code the monorepo must not run. `strip_block`
with `else` is the third option when public needs a DIFFERENT body.

**Coverage is per package** -- check YOUR config before relying on a marker.
Inspect both marker forms and path globs: a rule covering one file type or
specific file does not enable that marker everywhere.

## Verify by running the real export

Never hand-simulate the strip. Discover the CLI entrypoint in the package's
`pyproject.toml` (`[project.scripts]`) and use it below. Run from the repository
root; replace the example config path with the discovered one:

```bash
cli=copybarista  # Replace with the discovered entrypoint.
exp="$(mktemp -d)/export"
uv --quiet run --frozen "$cli" export \
  packages/widget/copy.barista.toml . --folder-dir "$exp"
```

Then inspect what actually shipped. The export already ran `leak_check`, so a
clean exit IS the leak evidence -- do not re-grep with a hand-written pattern
that is neither the package's rule nor a valid universal one:

```bash
cat "$exp"/<file>                                  # exported body
uv --quiet run --frozen ruff check "$exp"/<file>    # export must lint clean
```

A hand-written Python loop that mimics the marker walk is not evidence:
it will not reproduce `inclusive` gap collapse, `else` branches, marker
tokenization (`line_has_marker_token`, `transforms.py:604`), or the
`ruff_format` pass.

## Reverse import

`import_request.py` re-inserts source-only regions when public changes flow
back. Primary path is an exact offset splice (`import_request.py:860`); when a
public edit rewrote the surrounding context the splice lands stale and it falls
back to anchoring each region against the kept lines that bracket it, requiring
both to align AND to be adjacent in public
(`import_request.py:1377-1385`). It REJECTS rather than guesses.

Practical constraint: put a fence boundary next to stable context, not inside a
region a public contributor is likely to reformat.

`strip_block` with `else` never uses either path -- it has no verbatim removed
region (`transforms.py:651-655`) and is instead reversed by whole-block
substitution (`import_request.py:854-859`). Reversible, different mechanism.

## Stop conditions

- The change needs a file to exist publicly with a genuinely different
  body: use `strip_block` with `else`, or `files.write`, not a fork.
- The leak check fails on text you believe is safe: fix the text, never
  loosen `forbidden_text`. That list is a gate.
- A transform must run for a package but not its siblings: it belongs in
  that package's `copy.barista.toml`, not the shared one.

## Common mistakes

| Mistake | Correct behavior |
|---|---|
| Grep the toml, then edit | Read it whole; name the closest existing rule |
| Put a Python text transform after `ruff_format` | Place it before -- other types may follow |
| Hand-simulate the strip in Python | Run the real `copybarista export` |
| Add a path-specific rule | Check for an existing repo-wide one first |
| Fork the file for the export | Fence it with `copybarista:internal` |
| Assume blank-line residue ships | `inclusive` + `ruff_format` handle it -- verify |
| Glob in a `move`/`ruff_format` path | Exact file or directory only |
| Leave `required = true` on a may-be-noop rule | It fails on no CHANGE, not no match |
| Shallow glob to enumerate configs | Discover nested configs with `fdfind --hidden copy.barista.toml` |
| Edit `validation_commands`, stop there | Regenerate BOTH workflow sets |
| Add a git source, skip the publish gate | PyPI-only check must exist somewhere |
