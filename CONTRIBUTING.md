# Contributing

Copybarista is a small Python project for publishing clean standalone
repositories from source trees. Keep the implementation Pythonic and scoped to
specific sync problems rather than broad migration-engine compatibility.

## Development Setup

Use `uv` from the repository root:

```bash
uv sync --all-groups
```

## Validation

Run these before submitting changes:

```bash
uv run --all-groups ruff check .
uv run --all-groups ruff format --check .
uv run --all-groups basedpyright copybarista scripts tests
uv run --all-groups pytest
uv build --out-dir /tmp/copybarista-dist-check
```

`pytest` enforces at least 90% line coverage. `basedpyright` must pass with
zero errors and zero warnings.

Unit tests live adjacent to source modules as `*_test.py`. Keep only
non-unit integration tests under `tests/`.
