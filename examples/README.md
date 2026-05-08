# Examples :coffee:

This directory contains one complete example: a tiny source repository plus the
GitHub workflows that publish it to a standalone repository and import public
changes back to source. The local commands and GitHub workflows use the same
`copy.barista.toml`, so the example can be tested before any repositories or
tokens exist.

## Files

- [`python-package/source-repo`](python-package/source-repo): source
  repository with a runnable Python package, export config, import rewriting,
  and private README block stripping.
- [`python-package/github/source-to-public.yml`](python-package/github/source-to-public.yml):
  source repository workflow that exports the package and opens a public
  repository PR.
- [`python-package/github/public-to-source.yml`](python-package/github/public-to-source.yml):
  public repository workflow that validates or imports public changes back to
  source.
- [`python-package/github/protect-main-ruleset.json`](python-package/github/protect-main-ruleset.json):
  example branch protection ruleset for the public repository.

## Repository Layout

The examples assume a source repository shaped like this:

```text
source-repo/
  copy.barista.toml
  packages/widget/
    README.md
    pyproject.toml
    tests/test_widget.py
    widget/__init__.py
github/
  source-to-public.yml
  public-to-source.yml
  protect-main-ruleset.json
```

The public repository receives the contents of `packages/widget` at its root.

## Try It Locally

From a Copybarista checkout, run the source-side tests:

```bash
PYTHONPATH=examples/python-package/source-repo \
  uv run --with pytest pytest examples/python-package/source-repo/packages/widget/tests
```

Then export the package:

```bash
uv run copybarista export \
  examples/python-package/source-repo/copy.barista.toml \
  examples/python-package/source-repo \
  --folder-dir /tmp/widget-public \
  --force
```

The exported tree is a standalone package:

```text
/tmp/widget-public/
  README.md
  pyproject.toml
  tests/test_widget.py
  widget/__init__.py
```

The export removes the private README block, rewrites the test imports from
the source repository package path to the public package path, and runs a leak
check that rejects leftover `packages.widget` references. Run the exported tests
with:

```bash
PYTHONPATH=/tmp/widget-public uv run --with pytest pytest /tmp/widget-public/tests
```

The GitHub workflow example uses the same source shape. In a real source
repository, copy the contents of `python-package/source-repo` to the directory
that should own the export config. Use the workflow files under
`python-package/github` to connect that source repository to a standalone
public repository.

## Full GitHub Sync Walkthrough

Use two repositories:

- Source repository: private or internal source of truth.
- Public repository: standalone package repository.

The walkthrough below assumes the example source files live at the source
repository root, so `COPYBARISTA_SOURCE_PROJECT_PATH` is `.`. If you keep the
Copybarista project in a subdirectory, put that directory in
`COPYBARISTA_SOURCE_PROJECT_PATH`; `copy.barista.toml` and `source_root` are
then interpreted relative to that directory.

### 1. Prepare The Source Repository

Copy the example source tree into the source repository:

```text
copy.barista.toml
packages/widget/
  README.md
  pyproject.toml
  tests/test_widget.py
  widget/__init__.py
```

Copy the source-to-public workflow:

```text
.github/workflows/source-to-public.yml
```

Use the contents of
`examples/python-package/github/source-to-public.yml`.

Expected result: the source repository has the source package, the
Copybarista config, and a manually dispatched `Export public repository`
workflow.

### 2. Prepare The Public Repository

Create the standalone public repository. Keeping it private until the first
verified export is safest.

Copy the public-to-source workflow into the public repository:

```text
.github/workflows/public-to-source.yml
```

Use the contents of
`examples/python-package/github/public-to-source.yml`.

Expected result: the public repository has an `Import public changes` workflow,
even before it has package source files.
The first push that creates this workflow has no previous public commit to
compare against, so the import job is skipped.

### 3. Configure Source Repository Settings

Add this source repository secret:

```text
COPYBARISTA_SYNC_TOKEN
```

The token needs access to the public repository with `Contents: read and write`
and `Pull requests: read and write`.

Add these source repository variables:

```text
COPYBARISTA_PUBLIC_REPO=OWNER/PUBLIC_REPO
COPYBARISTA_SOURCE_PROJECT_PATH=.
COPYBARISTA_EXPORT_BRANCH=copybarista/export/main
COPYBARISTA_SYNC_USER_NAME=copybarista
COPYBARISTA_SYNC_USER_EMAIL=copybarista@example.com
```

`COPYBARISTA_SYNC_USER_NAME` and `COPYBARISTA_SYNC_USER_EMAIL` are optional,
but using the same values in both repositories lets reverse sync skip generated
export merges reliably.

Expected result: manually running `Export public repository` has enough
information to check out the public repository, push an export branch, and open
a PR.

### 4. Configure Public Repository Settings

Add this public repository secret:

```text
COPYBARISTA_IMPORT_TOKEN
```

The token needs access to the source repository with `Contents: read and write`
and `Pull requests: read and write`. While the public repository is private,
the token also needs public repository `Contents: read`.

Add these public repository variables:

```text
COPYBARISTA_SOURCE_REPO=OWNER/SOURCE_REPO
COPYBARISTA_SOURCE_PROJECT_PATH=.
COPYBARISTA_SYNC_USER_NAME=copybarista
COPYBARISTA_SYNC_USER_EMAIL=copybarista@example.com
```

Expected result: `Import public changes` can check out the source repository,
run `copybarista import-change`, push an import branch, and open a source PR.

### 5. Run The First Export

In the source repository, run `Export public repository` manually.

Use a public-safe title and body, for example:

```text
Title: Publish initial widget package
Body: Exports the initial standalone widget package.
```

Leave `branch` blank for the default
`COPYBARISTA_EXPORT_BRANCH` value, or `copybarista/export/<source-branch>`
when that variable is unset. Rerun the workflow with the same branch to update
the same public PR.

Leave `auto_merge` disabled for the first export. After branch protection and
checks are working, enabling it asks GitHub to squash-merge the generated PR
when all required checks pass.

Expected result:

- The workflow exports `packages/widget` into a temporary tree.
- The public checkout is replaced except for `.git` and `.github`.
- Public tests run if `tests/` exists.
- A PR opens in the public repository from a `copybarista/export/...` branch.
- Rerunning with the same generated branch replaces the branch with
  `git push --force-with-lease` and updates the same PR.
- The PR title and body are the manual public-safe values you entered.
- The PR body includes the generated branch name and says not to push manual
  commits to that branch.

If the workflow prints `No public changes to sync.`, the public repository
already matches the exported source tree.

### 6. Merge The First Export

Review the public PR diff before merging. It should contain the standalone
package root:

```text
README.md
pyproject.toml
tests/test_widget.py
widget/__init__.py
```

The README private block should be absent, and test imports should use
`widget`, not `packages.widget`.

Merge the PR with squash merge.

Expected result: the public `main` branch now matches the exported source
tree. The public repository `push` workflow should skip opening a reverse-sync
PR for this generated export merge when the sync author email or
`copybarista/export/` marker matches.

### 7. Protect The Public Main Branch

After the first export is clean, install the optional ruleset:

```bash
OWNER=your-org
REPO=your-public-repo
gh api \
  --method POST \
  "repos/$OWNER/$REPO/rulesets" \
  --input examples/python-package/github/protect-main-ruleset.json
```

Edit required check names in the JSON first if your CI does not emit
`Lint, type-check, test, and build`.

Expected result: future public changes go through pull requests with the checks
and review rules your repository requires.

### 8. Test Reverse Sync

Open a normal public PR that edits a reversible file, such as
`widget/__init__.py` or `tests/test_widget.py`.

Do not use `README.md` for the first reverse-sync test. The example strips a
private README block, so README paths are intentionally treated as
export-only.

Expected result on the public PR:

- `Import public changes` checks out the public base and head.
- It runs `copybarista import-change`.
- It validates that the imported source re-exports to the public PR head.
- It does not open a source PR yet.

Merge the public PR after checks pass.

Expected result after merge:

- The public `push` trigger reruns `Import public changes`.
- The workflow imports the public change into the source checkout.
- A PR opens in the source repository from
  `copybarista/import/sha-<public-sha>`.
- The source PR only changes files under `COPYBARISTA_SOURCE_PROJECT_PATH`.

Merge the source PR after source-side review and checks.

### 9. Continue Sync

For source-owned changes, run `Export public repository` again and merge the
generated public PR.

For public-owned fixes, merge the public PR first, then merge the generated
source PR. The import PR title and body include the public base SHA, public
head SHA, and source base SHA. If source `main` changes after the import PR was
generated, rerun the public-to-source workflow before merging so the PR is
rebuilt on current source.

If both sides changed the same exported file, import the public change into
source first when it should survive, then run a fresh export.

## How The Example Syncs

Source-to-public sync treats the source repository as canonical. The workflow
exports the configured subtree into a temporary directory, replaces the public
checkout contents except `.git` and `.github`, validates the result, and opens
or updates a public pull request. Merging that PR makes the public repository
match the latest exported source tree while leaving public-repository workflows
and settings files in place.

The example source-to-public workflow is manual-only because it requires a
reviewable public PR title and body. To run it automatically on source changes,
add a `push` or `schedule` trigger and replace the required manual inputs with
generated public-safe text.

For scaffolded package sync, prefer the generated workflow from
`copybarista write-export-workflow`. It uses `[pull_request]` defaults from
`copybarista.sync.toml` and can replay `Copybarista-PR-*` commit metadata into
the generated public PR title and body. Source attribution is represented in
the generated export commit author and `Co-authored-by` trailers. If the public
repository has a PR template, Copybarista fills its summary section and keeps
the template structure, except human checklist sections are omitted for
automated export PRs. When one commit touches multiple exported packages, use
one `Copybarista-PR-Scope: <package>` block per package so each public
repository receives the right text.
Ordinary commit subjects and bodies are not used as generated PR title/body;
only the explicit `Copybarista-PR-*` fields are treated as approved public PR
text. Without matching metadata, the generated workflow keeps the configured
generic title and body.

Public-to-source sync treats public changes as proposals. The workflow checks
out the public base and head trees, runs `copybarista import-change`, validates
the source checkout, and opens a source pull request only when the imported
source tree re-exports to the public head. That re-export check is what catches
unmapped files, excluded paths, non-reversible transforms, and semantic drift.

Pull requests in the public repository run import validation only. Source PRs
are opened after a public PR is merged to `main`, or when the workflow is run
manually. Fork PRs are skipped by default because the workflow needs a token
that can read and write the private/source repository.
The first push to a new public repository is skipped because GitHub reports an
all-zero `before` SHA and there is no public base tree to import from.

Generated export merges are skipped by author email or by a generated export
branch marker in the merge commit message. Auto-merge writes
`Copybarista export branch: ...` into the squash body; manual squash merges
should keep that marker or a `copybarista/export/` marker in the title/body.

Do not manually edit generated `copybarista/export/*` branches. Treat them as
workflow output. Change the source repository and rerun
`Export public repository` with the same branch instead. The workflow uses
`git push --force-with-lease` so it
can replace generated commits without overwriting unexpected manual updates.

If both repositories change the same exported file, merge order matters. Import
the public change into source first when it is meant to survive, then run a new
source-to-public export. If the source change should win, close or update the
public PR before merging the generated export PR.

## Source Repository Settings

Secrets:

- `COPYBARISTA_SYNC_TOKEN`: token used to push branches and open PRs in the
  public repository.

Variables:

- `COPYBARISTA_PUBLIC_REPO`: public repository in `owner/name` form.
- `COPYBARISTA_SOURCE_PROJECT_PATH`: source checkout directory that contains
  `copy.barista.toml`. Use `.` when the config is at the repository root.
- `COPYBARISTA_EXPORT_BRANCH`: optional stable export branch. Use one value
  per source project, such as `copybarista/export/widget`.
- `COPYBARISTA_SYNC_USER_NAME`: optional commit author name.
- `COPYBARISTA_SYNC_USER_EMAIL`: optional commit author email. Use the same
  value in both repositories so reverse sync can identify generated export
  merges.

Workflow inputs:

- `pr_title`: required public PR title.
- `pr_body`: required public PR description.
- `branch`: optional export branch. The default is `COPYBARISTA_EXPORT_BRANCH`
  when set, otherwise `copybarista/export/<source-branch>`.
- `auto_merge`: optional source-to-public auto-merge after required checks
  pass. Use this only for generated export PRs, not public-to-source imports.

## Public Repository Settings

Secrets:

- `COPYBARISTA_IMPORT_TOKEN`: token used to push branches and open PRs in the
  source repository.

Variables:

- `COPYBARISTA_SOURCE_REPO`: source repository in `owner/name` form.
- `COPYBARISTA_SOURCE_PROJECT_PATH`: source checkout directory that contains
  `copy.barista.toml`.
- `COPYBARISTA_SYNC_USER_NAME`: optional commit author name.
- `COPYBARISTA_SYNC_USER_EMAIL`: optional commit author email. Use the same
  value in both repositories so reverse sync can identify generated export
  merges.

## Branch Names

Generated export branches default to one stable branch per source branch, or
to `COPYBARISTA_EXPORT_BRANCH` when set. That keeps one active export PR per
project branch. To update the PR, rerun the workflow with the same branch.
Generated import branches default to `copybarista/import/sha-<public-sha>`.
Rerunning the import workflow for the same public SHA also uses
`git push --force-with-lease` to refresh the generated source PR branch.

Explicit branch inputs must be valid, non-protected Git branch names and should
not be your default branch.

The example workflows assume both repositories use `main` as the default
branch. If either repository uses another default branch, update checkout refs,
workflow branch filters, PR bases, and the ruleset JSON together.

## Token Permissions

Use the narrowest token that can perform the sync:

- Source-to-public token: public repository `Contents: read and write` and
  `Pull requests: read and write`.
- Public-to-source token: source repository `Contents: read and write` and
  `Pull requests: read and write`.
- Add `Workflows: read and write` only if your exported tree intentionally
  creates or updates files under `.github/workflows`. The example export
  preserves the public repository's `.github/` directory instead.
- Add public repository `Contents: read` to the public-to-source token while
  the public repository is still private.

GitHub documents fine-grained token repository permissions at
<https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens>.

## Common Failures

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| `Set COPYBARISTA_SYNC_TOKEN.` | Source repository secret is missing. | Add `COPYBARISTA_SYNC_TOKEN` to the source repository, not the public repository. |
| `Set COPYBARISTA_PUBLIC_REPO.` | Source repository variable is missing. | Set `COPYBARISTA_PUBLIC_REPO` to `OWNER/PUBLIC_REPO`. |
| `Set COPYBARISTA_IMPORT_TOKEN.` | Public repository secret is missing. | Add `COPYBARISTA_IMPORT_TOKEN` to the public repository, not the source repository. |
| `Set COPYBARISTA_SOURCE_REPO.` | Public repository variable is missing. | Set `COPYBARISTA_SOURCE_REPO` to `OWNER/SOURCE_REPO`. |
| `Cannot read config` or `test -f "$PROJECT_PATH/copy.barista.toml"` fails. | `COPYBARISTA_SOURCE_PROJECT_PATH` points at the wrong directory. | Use `.` when `copy.barista.toml` is at the source repository root; otherwise use the directory that contains it. |
| Public PR opens with private files or wrong imports. | `copy.barista.toml` include/exclude or transforms are incomplete. | Run the local export first, inspect `/tmp/widget-public`, and update the config before running the workflow. |
| Initial public push does not open a source PR. | GitHub reports an all-zero `before` SHA for the first push. | This is expected; merge or push a real public change after the first export. |
| `Cannot resolve public base ref.` appears on initial public setup. | The public repository is using an older workflow that did not skip all-zero first-push events. | Update `public-to-source.yml` from this example. |
| Public `push` opens a source PR after merging a generated export PR. | Generated export merge was not recognized. | Keep `Copybarista export branch: ...` or `copybarista/export/` in the squash-merge title/body. |
| Public PR validation fails with an unmapped or non-reversible path. | The public PR changed a path that Copybarista cannot safely map back. | Change a reversible file, add explicit reverse transforms, or keep that path source-owned. |
| `auto_merge` fails or leaves the PR waiting. | The public repository has not enabled auto-merge, required checks are missing, or branch protection does not require the expected checks. | Leave `auto_merge` disabled until branch protection and required checks are installed. |
| Ruleset install succeeds but PRs are blocked forever. | Required check names in the ruleset do not match your CI. | Edit `required_status_checks` in `protect-main-ruleset.json` before installing or updating it. |
| Workflow cannot update files under `.github/workflows`. | The exported tree intentionally owns workflow files, but the token lacks workflow permission. | Add `Workflows: read and write` to the relevant fine-grained token. |

## Validation Hooks

The example workflows include placeholder validation steps. Replace them with
the checks your repositories already require, and make sure any protected
branch ruleset requires check names that your CI actually emits, such as:

```bash
uv sync --all-groups
uv run --all-groups ruff check --no-fix --no-cache .
uv run --all-groups ruff format --check --no-cache .
uv run --all-groups pytest
```

Keep validation in normal project files when it grows beyond a few shell
commands. The workflow should check out repositories, pass credentials, and call
small scripts or standard commands.
