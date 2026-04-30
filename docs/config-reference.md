# Config Reference

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

## `[[transform]]`

Transforms run after file selection and staging. Paths are relative to the
destination root, not the original monorepo root.

Each transform accepts:

`type`:

Required transform type. Supported values are `replace` and `strip_block`.

`path`:

Required destination-root-relative path. `replace` accepts a supported glob
pattern; `strip_block` requires one exact file path.

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

`--json` prints the export manifest to stdout. Without `--json`, successful
commands are quiet.

`CONFIG` can be a native Copybarista TOML file or a supported `copy.bara.sky`
file. The `copy.bara.sky` import path supports the documented subset only; arbitrary
Starlark, unsupported workflow modes, review workflows, remote origins,
unsupported glob constructs, and unsupported transform options are rejected.
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
