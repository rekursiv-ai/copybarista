# Config Reference :coffee:

Copybarista configs are TOML files, preferably named `copy.barista.toml`. They
describe one single-commit workflow: select files below a source root, apply
transforms, and write the transformed tree to a folder or Git destination.

Copybarista can also translate a supported `copy.bara.sky` workflow into this
TOML format:

```bash
copybarista translate copy.bara.sky --workflow export --output copy.barista.toml
```

Direct `copy.bara.sky` input is accepted by `validate`, `export`, and
`publish-git`. Copybarista auto-translates the supported workflow internally,
validates it as Copybarista config, and then runs the normal engine.
Unsupported `copy.bara.sky` features fail with explicit config errors.

## Minimal Config

```toml
[workflow]
name = "example"
mode = "squash"
source_root = "path/in/source"

[destination.folder]
path = "/tmp/example-oss"

[files]
include = ["**"]
exclude = []
```

Run it with:

```bash
copybarista export copy.barista.toml /path/to/source
```

`/path/to/source` is the source checkout root. `workflow.source_root` is a
relative path inside that checkout.

## `[workflow]`

`name`:

Human-readable workflow name. Defaults to `default`.

`mode`:

Only `squash` is supported. Copybarista writes the current transformed tree
rather than preserving source history.

`source_root`:

Required relative path to export from the source checkout. Absolute paths and
paths containing `..` are rejected.

Copybarista exports the contents of this directory at the destination root.
Additional `[[files.copy]]` entries may assemble specific files or directories
from elsewhere in the same source checkout.

## `[files]`

`include`:

List of source-root-relative glob patterns to export. Defaults to `["**"]`.

`exclude`:

List of source-root-relative glob patterns to omit. Defaults to `[]`.

Copybarista supports `*`, `**`, `?`, brace alternation, character classes, and
escaped literal characters. It rejects absolute paths and `..` path traversal.
Brace alternation must contain at least two non-empty choices, such as
`{main,test}`. A pattern segment like `**/` requires at least one directory, so
use both `*.pyc` and `**/*.pyc` when root-level and nested files should match.

Common excludes:

```toml
exclude = [
  ".pytest_cache/**",
  "**/.pytest_cache/**",
  ".ruff_cache/**",
  "**/.ruff_cache/**",
  ".venv/**",
  "**/__pycache__/**",
  "*.pyc",
  "**/*.pyc",
  "*.egg-info/**",
  "**/*.egg-info/**",
  "build/**",
  "dist/**",
]
```

`destination_prefix`:

Optional destination directory added to selected `source_root` files. Use
`destination_prefix_exclude` for root-owned files such as `README.md` or
`.github/**` that should stay at the public repository root.

`[[files.copy]]`:

Additional repo-relative files or directories to copy into the staged public
tree before transforms run. This is for packages that depend on selected shared
monorepo utilities but should not keep duplicate source files in the project
tree.

```toml
[[files.copy]]
source = "shared/json.py"
destination = "package/lib/json.py"

[[files.copy]]
source = "shared/web"
destination = "package/lib/web"
include = ["search.py", "useragents.txt", "__init__.py"]
exclude = []
```

`source` is relative to the source checkout root, not `workflow.source_root`.
`destination` is relative to the exported public tree. Directory copies preserve
paths below `source`; file copies write exactly to `destination`. Copybarista
fails if a copied destination collides with another exported file.

Import workflows map public changes under a copied destination back to the
configured `source`, so edits to `package/lib/json.py` can return to
`shared/json.py`.

## `[leak_check]`

Leak checks are read-only release gates over the transformed export tree. They
run after file selection, `[[files.copy]]`, and transforms, but before folder or
Git destinations are mutated. Use them to catch source-only paths, unrewritten
monorepo imports, private markers, internal hostnames, or other strings that
must never land in the public tree.

Copybarista reports the rule id, path, and line number. It does not print the
matched text, which keeps CI logs from echoing secrets.

`[[leak_check.forbidden_path]]`:

Reject matching exported paths:

```toml
[[leak_check.forbidden_path]]
id = "source-only-paths"
paths = ["private/**", "copy.barista.toml", ".github/workflows/pages.yml"]
message = "source-only path was exported"
```

`paths` uses the same supported glob syntax as `[files]`.

`[[leak_check.forbidden_text]]`:

Reject matching text in selected files:

```toml
[[leak_check.forbidden_text]]
id = "monorepo-imports"
pattern = "\\bloop\\."
paths = ["*.py", "**/*.py", "*.md", "**/*.md", "*.toml", "**/*.toml"]
exclude = ["tests/fixtures/**"]
message = "monorepo reference remained after export"
```

`pattern` is a Python regular expression compiled with multiline mode. `paths`
defaults to `["**"]`; `exclude` defaults to `[]`. `**/*.md` matches nested
Markdown files, so include `*.md` too when root-level files should be scanned.
Symlink contents are skipped, but symlink paths are still checked by
`forbidden_path` rules.

Run the same policy against an existing tree:

```bash
copybarista check-leaks copy.barista.toml /path/to/exported/tree
```

## `[[transform]]`

Transforms run after file selection and staging. Paths are relative to the
destination root, not the original monorepo root.

Each transform accepts:

`type`:

Required transform type. Supported values are `replace`, `ruff_format`,
`strip_block`, and `move`.

`path`:

Required destination-root-relative path. `replace` accepts a supported glob
pattern. `ruff_format`, `strip_block`, and `move` require one exact file or
directory path.

`id`:

Optional stable identifier for manifest output. If omitted, Copybarista derives
one from the transform position, type, and path.

`required`:

Boolean. Defaults to `true`. Required transforms fail when they do not change a
file.

### `replace`

Literal UTF-8 text replacement:

```toml
[[transform]]
type = "replace"
path = "tests/test_widget.py"
before = "from monorepo.packages.widget import"
after = "from widget import"
```

`before` and `after` are required strings, and `before` must be non-empty.
Replacement is literal and applied to the whole UTF-8 file; it is not regex
based. Unsupported transform options are rejected by config validation.

For change-request imports, Copybarista normally reverses a replacement by
swapping `after` back to `before`. If that automatic reversal would be unsafe,
set `reverse_before` and `reverse_after` together to define the public-to-source
replacement explicitly:

```toml
reverse_before = "from widget import"
reverse_after = "from monorepo.packages.widget import"
```

### `ruff_format`

Run Ruff fixes and formatting on the staged export tree:

```toml
[[transform]]
type = "ruff_format"
path = "."
```

Copybarista runs `ruff check --fix --no-cache <path>` and
`ruff format --no-cache <path>` after earlier transforms and before final
manifest hashing. Use it when import rewrites change Python import groups or
formatting. Public validation should then use read-only checks such as
`ruff check --no-fix --no-cache .` and
`ruff format --check --no-cache .`.

`ruff_format` is deterministic but not reversed during public change imports;
the final import verification re-exports the source tree and compares the
formatted result to the public head.

For this transform, `required = true` only requires the configured path to
exist. A no-op formatting pass succeeds.

### `move`

Relocate a staged file or directory after file selection and `[[files.copy]]`
assembly:

```toml
[[transform]]
type = "move"
path = "_stubs/ty_extensions"
destination = "ty_extensions"
```

`destination` is required and must be a relative destination-root path.
Copybarista records manifest entries at the moved destination, so manifests and
Git exports describe the actual public tree. Use this for small public layout
adjustments; use `source_root` and `[[files.copy]]` for primary selection and
multi-source assembly.

### `strip_block`

Remove a marked text block:

```toml
[[transform]]
type = "strip_block"
path = "README.md"
start = "<!-- copybarista:strip:start -->"
end = "<!-- copybarista:strip:end -->"
inclusive = true
```

`start` and `end` are required strings. `inclusive` defaults to `true`, which
removes both marker lines and the text between them.
`path` must be one exact file path because removed block contents cannot be
mapped back safely during public change imports.

Markers are exact config strings. Copybarista does not hardcode marker names or
automatically alias marker families. To strip `copybarista:*` markers, configure
those exact strings; to strip `copybara:*` markers, configure those exact
strings. If a file may contain either marker family, add two `strip_block`
transforms and set `required = false` when one family may be absent.

## `[destination.folder]`

`path`:

Default folder destination for `copybarista export`. The CLI `--folder-dir`
flag overrides this value.

Folder export replaces the destination contents after safety checks. Existing
destinations require `--force`.

## `[destination.git]`

```toml
[destination.git]
url = "file:///tmp/example-oss.git"
branch = "main"
committer_name = "Example Export"
committer_email = "opensource@example.com"
```

`url`:

Required for `copybarista publish-git`. Local paths and `file://` URLs are
supported. Local remotes must already be Git repositories or existing empty
directories that Copybarista can initialize as bare repositories.

`branch`:

Destination branch. Defaults to `main`.

`committer_name` and `committer_email`:

Optional Git identity for the generated commit. If omitted, Git must already
provide `user.name` and `user.email` through repository, global, or system
config.

Git export creates one commit only when the transformed tree differs from the
destination branch, then pushes `HEAD` to the configured branch. It updates a
cached bare mirror before creating the temporary checkout. Like folder export,
Git export replaces all non-`.git` worktree contents for the supported single-commit
workflow.

## CLI Reference

Validate a config:

```bash
copybarista validate CONFIG [--workflow NAME]
```

Translate supported `copy.bara.sky` syntax to Copybarista TOML:

```bash
copybarista translate COPY_BARA_SKY [--workflow NAME] [--output CONFIG]
```

For a typical file named `copy.bara.sky`:

```bash
copybarista translate copy.bara.sky --workflow export \
  --output copy.barista.toml
```

Export to a folder:

```bash
copybarista export CONFIG SOURCE_REF [--workflow NAME] \
  [--folder-dir DIR] [--force] [--json]
```

Export to Git:

```bash
copybarista publish-git CONFIG SOURCE_REF [--workflow NAME] [--json]
```

Import a public change request into a source-of-truth checkout:

```bash
copybarista import-change CONFIG \
  --public-base DIR \
  --public-head DIR \
  --source-base DIR \
  --destination DIR \
  [--workflow NAME] \
  [--no-verify] \
  [--json]
```

Write reusable package sync scaffolding:

```bash
copybarista init-sync ROOT \
  --package-name NAME \
  --source-root PATH \
  --public-repo OWNER/REPO \
  --source-repo OWNER/REPO \
  --copybarista-project-path PATH \
  --smoke-import MODULE \
  [--sync-label LABEL] \
  [--type-check-target PATH] \
  [--forbidden-pr-text TEXT] \
  [--validation-python-version VERSION] \
  [--validation-command COMMAND] \
  [--sync-user-name NAME] \
  [--sync-user-email EMAIL] \
  [--overwrite]
```

Validate generated package sync scaffolding:

```bash
copybarista check-sync-config ROOT
```

Render the source-repository export workflow from package sync metadata:

```bash
copybarista write-export-workflow copybarista.sync.toml [--output PATH]
```

`--json` prints the export manifest to stdout. Without `--json`, successful
commands are quiet.

`CONFIG` can be a native Copybarista TOML file or a supported `copy.bara.sky`
file. The `copy.bara.sky` import path supports the documented subset only;
arbitrary Starlark, unsupported workflow modes, review workflows, remote
origins, unsupported glob constructs, and unsupported transform options are
rejected.
`--workflow` selects a named workflow from `copy.bara.sky`; `publish-git`
defaults to `export_git`, and other commands default to `export`.

`--force` is required when a folder export destination already exists.
Destination replacement still follows Copybarista's safety checks.

`import-change` expects local checkouts. It computes the public diff between
`--public-base` and `--public-head`, maps changed paths back under
`workflow.source_root`, reverses supported literal replacements, writes the
mapped changes into `--destination`, and re-exports that destination to confirm
it reproduces `--public-head`. It ignores VCS and `.copybarista` metadata while
diffing, refuses writes that target metadata paths, and rejects excluded paths,
non-reversible transforms such as `strip_block`, escaping symlinks, and public
base mismatches. If any step fails, touched destination paths are restored.

`--no-verify` skips the public-base and final re-export checks and prints a
warning to stderr. It is intended only for local diagnosis because it removes
the main protection against stale bases and incomplete reverse mappings.

## Manifest

The JSON manifest has this shape:

```json
{
  "files": [
    {
      "destination": "README.md",
      "sha256": "...",
      "size": 128,
      "source": "packages/widget/README.md"
    }
  ],
  "transforms": [
    {
      "changed": 1,
      "count": 1,
      "files": [
        {
          "count": 1,
          "destination": "README.md",
          "source": "packages/widget/README.md"
        }
      ],
      "id": "1:strip_block:README.md",
      "path": "README.md",
      "type": "strip_block"
    }
  ]
}
```

File order and JSON keys are deterministic.
For symlinks, `size` and `sha256` describe the link target text rather than the
target file contents.
