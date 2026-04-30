# Tutorial

This tutorial creates a tiny source tree, exports one subdirectory to a clean
folder, and inspects the manifest. It does not require Git.

Copybarista's native config format is TOML, preferably in a file named
`copy.barista.toml`. If you already have a supported `copy.bara.sky` workflow,
Copybarista can accept that file directly and auto-translate it internally. You
can also materialize the translated TOML:

```bash
copybarista translate copy.bara.sky --workflow export \
  --output copy.barista.toml
```

The `copy.bara.sky` import path covers the documented subset only. Unsupported
constructs fail with explicit config errors.

## 1. Create a Source Tree

```bash
mkdir -p /tmp/copybarista-demo/source/packages/widget/tests
cd /tmp/copybarista-demo/source
```

Create a README with an internal block:

```bash
cat > packages/widget/README.md <<'EOF'
# Widget

Public package docs.

<!-- copybarista:strip:start -->
Internal release notes.
<!-- copybarista:strip:end -->
EOF
```

Create one Python file and one test file:

```bash
cat > packages/widget/widget.py <<'EOF'
def name() -> str:
    return "widget"
EOF

cat > packages/widget/tests/test_widget.py <<'EOF'
from monorepo.packages.widget.widget import name


def test_name() -> None:
    assert name() == "widget"
EOF
```

## 2. Write a Config

Create `/tmp/copybarista-demo/copy.barista.toml`:

```toml
[workflow]
name = "widget"
mode = "squash"
source_root = "packages/widget"

[destination.folder]
path = "/tmp/copybarista-demo/out"

[files]
include = ["**"]
exclude = [
  "**/__pycache__/**",
  "*.pyc",
  "**/*.pyc",
]

[[transform]]
type = "replace"
path = "tests/test_widget.py"
before = "from monorepo.packages.widget.widget import"
after = "from widget import"

[[transform]]
type = "strip_block"
path = "README.md"
start = "<!-- copybarista:strip:start -->"
end = "<!-- copybarista:strip:end -->"
inclusive = true
```

The source checkout is `/tmp/copybarista-demo/source`. The configured
`source_root` is relative to that checkout, so `packages/widget` is exported to
the destination root.

## 3. Validate and Export

From the Copybarista package directory:

```bash
uv run copybarista validate /tmp/copybarista-demo/copy.barista.toml
uv run copybarista export /tmp/copybarista-demo/copy.barista.toml \
  /tmp/copybarista-demo/source \
  --force
```

The exported folder now contains:

```text
/tmp/copybarista-demo/out/
  README.md
  tests/test_widget.py
  widget.py
```

`README.md` no longer contains the internal block, and
`tests/test_widget.py` imports from the standalone package path.

## 4. Inspect the Manifest

Run with `--json` to print a deterministic export report:

```bash
uv run copybarista export /tmp/copybarista-demo/copy.barista.toml \
  /tmp/copybarista-demo/source \
  --force \
  --json
```

The manifest includes:

- `files`: source path, destination path, size, and SHA-256 for each exported
  file.
- `transforms`: transform id, type, path, and changed-file count.

## 5. Override the Folder Destination

Use `--folder-dir` to keep the same config but write elsewhere:

```bash
uv run copybarista export /tmp/copybarista-demo/copy.barista.toml \
  /tmp/copybarista-demo/source \
  --folder-dir /tmp/copybarista-demo/out-alt \
  --force
```

This is the usual pattern for CI jobs that export to a temporary directory
before running package checks.

## 6. Use a Supported `copy.bara.sky` Directly

For supported `copy.bara.sky` workflows, pass the config file in
place of `copy.barista.toml`:

```bash
uv run copybarista export /tmp/copybarista-demo/copy.bara.sky \
  /tmp/copybarista-demo/source \
  --folder-dir /tmp/copybarista-demo/out \
  --force
```

Copybarista translates the workflow internally before export. Use the
`translate` command when you want to inspect, edit, or check in the generated
TOML.
