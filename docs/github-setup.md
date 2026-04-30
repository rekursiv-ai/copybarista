# GitHub Repository Setup

Copybarista works best when the exported repository is protected like a normal
open-source project: changes land through pull requests, checks run before
merge, and releases publish from a protected default branch.

This guide provides a reusable starting point. Adapt check names, reviewers, and
release environments to your project.

## Complete Workflow Example

Copybarista ships a minimal two-way GitHub setup under
`examples/python-package/`:

- `examples/python-package/source-repo`
- `examples/python-package/github/source-to-public.yml`
- `examples/python-package/github/public-to-source.yml`
- `examples/python-package/github/protect-main-ruleset.json`

Start with `examples/README.md` when setting up a new source/public repository
pair. The example workflows install Copybarista from Python packaging, export a
standalone tree, open a public PR, validate public changes, and open a source PR
when public changes are merged.

The example workflows use generic variable names such as
`COPYBARISTA_SOURCE_REPO`. Copybarista's own repository workflows may use
project-specific names, but new repositories should start from `examples/`.
In the example workflows, `COPYBARISTA_SOURCE_PROJECT_PATH` is the directory
that contains `copy.barista.toml` and the `source_ref` passed to Copybarista.
Use `.` when the config is at the source repository root.

## Repository Setup Checklist

1. Create the destination repository as private.
2. Copy the source-to-public workflow into the source repository.
3. Copy the public-to-source workflow into the destination repository.
4. Add repository secrets and variables in both repositories.
5. Export the first Copybarista tree into a pull request.
6. Confirm CI passes on the exported tree.
7. Protect the destination default branch.
8. Merge the first export with squash merge.
9. Test reverse sync with a small public pull request.
10. Make the repository public when the public tree and sync loop are clean.
11. Configure package publishing.

Keeping the repository private until the first verified export avoids exposing
temporary setup commits, private path names, or incomplete release metadata.

## Action Triggers

The source repository owns source-to-public export. The example
`source-to-public.yml` runs on `workflow_dispatch` so maintainers can choose a
public-safe PR title, description, and optional branch name for each export. The
workflow checks out the source repository and public repository, runs
`copybarista export`, replaces the public checkout while preserving `.github/`,
validates it, and opens or updates a public pull request.

By default, the example uses one stable generated branch per source branch, or
the repository variable `COPYBARISTA_EXPORT_BRANCH` when set. Rerunning the
workflow with the same branch updates the same public PR instead of creating a
new active export PR. Generated export branches are replaced with
`git push --force-with-lease`.

To export automatically after source changes, add `push`, `schedule`, or path
filters to the source workflow and replace the manual PR title/body inputs with
generated public-safe text.

The public repository owns public-to-source import. The example
`public-to-source.yml` runs in three situations:

- `pull_request` to `main`: validate that a trusted same-repository public PR
  can be imported, but do not open a source PR yet.
- `push` to `main`: after a public PR is merged, import the merged public
  change and open or update a source repository PR. The first push to a new
  repository is skipped because GitHub reports an all-zero `before` SHA and
  there is no public base tree to compare.
- `workflow_dispatch`: manually import selected public refs and open or update
  a source repository PR.

Generated export branches named `copybarista/export/*` are skipped by the
pull-request validation path because their source of truth is the source
repository export. Direct public edits and manually dispatched imports still
flow back through `copybarista import-change`.

Merged generated export PRs should also be skipped on public `main` pushes.
The example workflow detects them by sync author email or a
`copybarista/export/` marker in the merge commit message, so use the same
`COPYBARISTA_SYNC_USER_EMAIL` in both repositories or keep that marker in the
generated export PR squash-merge title/body.

## Pull Request Text

The source-to-public workflow requires a manual `pr_title` and `pr_body` by
default. Treat those fields as public release text:

- describe the public change, not the private source repository;
- avoid private repository names, internal team names, private paths, and
  internal issue links;
- keep generated sync metadata in the PR body only when it is safe to expose;
- review the generated public PR before merging it.

For automatic exports, replace the manual inputs with a project-specific script
that generates public-safe titles and bodies. Keep that script in the source
repository so it can enforce the private-name policy your project needs.

Generated export PR bodies should say which `copybarista/export/*` branch they
come from and that maintainers should not push manual commits to that branch.
Manual changes belong in the source repository followed by another export run.
The example workflows use `git push --force-with-lease` for generated branches
so reruns can replace generated commits but fail if someone changed the remote
branch unexpectedly.

Generated import PR titles and bodies should include the public base SHA,
public head SHA, and source base SHA. If source `main` changes after an import
PR is generated, rerun the import workflow before merging it so the branch is
rebuilt on current source.

## Optional Auto-Merge

Auto-merge is reasonable for source-to-public export PRs when all of these are
true:

- the export branch is generated by the source workflow;
- the public repository checks cover package tests, release-tree policy, and
  private-name leak checks;
- `.github/` remains public-repo-owned and is preserved by export;
- the generated PR title and body are public-safe;
- branch protection requires the relevant checks before merge.

The example source-to-public workflow exposes this as the `auto_merge` manual
input. Leave it disabled for the first export, then enable it once required
checks and branch protection are installed.

Keep public-to-source imports manual by default. Public changes are proposals
that can carry semantic decisions, so source maintainers should review the
generated import PR before accepting it.

## Sync Identity

Use a stable sync identity for generated commits, such as:

```text
COPYBARISTA_SYNC_USER_NAME=copybarista
COPYBARISTA_SYNC_USER_EMAIL=copybarista@example.com
```

Set the same name and email in both repositories. The public-to-source workflow
uses that email, plus the `copybarista/export/` branch marker, to distinguish
generated export merges from public-authored changes that should be imported
back to source.

The identity can be a machine user, a GitHub App installation, or a fine-grained
token owner. The important property is stability: changing the email without
updating both repositories can make generated export merges look like public
changes.

## Multiple Exports From One Monorepo

One source repository can export multiple projects to separate public
repositories. Treat each exported project as its own sync pair:

- one `copy.barista.toml` per exported project;
- one public repository per exported project;
- one source-to-public workflow per exported project, or one matrix workflow
  with an explicit project key;
- one public-to-source workflow in each public repository;
- distinct export branches and public-safe PR titles for each project.

The example workflow serializes one project's exports with:

```yaml
concurrency:
  group: copybarista-export
  cancel-in-progress: false
```

If several projects share one source repository, give each project a distinct
concurrency group so unrelated exports can run at the same time:

```yaml
concurrency:
  group: copybarista-export-widget
  cancel-in-progress: false
```

For a reusable workflow, include a project key or source project path in the
group:

```yaml
concurrency:
  group: copybarista-export-${{ inputs.project || vars.COPYBARISTA_SOURCE_PROJECT_PATH }}
  cancel-in-progress: false
```

Use project-specific variables when one source repository exports more than one
package:

```text
COPYBARISTA_PUBLIC_REPO=OWNER/WIDGET_PUBLIC_REPO
COPYBARISTA_SOURCE_PROJECT_PATH=packages/widget
COPYBARISTA_EXPORT_BRANCH=copybarista/export/widget
```

The reverse-sync workflow should also serialize per public repository or per
project, not globally across all projects. That keeps a long-running import for
one package from blocking an unrelated package.

## Required Secrets And Variables

Set these in the source repository:

| Name | Kind | Purpose |
| --- | --- | --- |
| `COPYBARISTA_SYNC_TOKEN` | Secret | Push export branches and open PRs in the public repository. |
| `COPYBARISTA_PUBLIC_REPO` | Variable | Public repository in `owner/name` form. |
| `COPYBARISTA_SOURCE_PROJECT_PATH` | Variable | Source checkout directory that contains `copy.barista.toml`. Use `.` at repository root. |
| `COPYBARISTA_EXPORT_BRANCH` | Variable | Optional stable generated export branch, for example `copybarista/export/widget`. |
| `COPYBARISTA_SYNC_USER_NAME` | Variable | Optional sync commit author name. |
| `COPYBARISTA_SYNC_USER_EMAIL` | Variable | Optional sync commit author email. Use the same value in both repositories if reverse sync skips generated export merges by author. |

Set these in the public repository:

| Name | Kind | Purpose |
| --- | --- | --- |
| `COPYBARISTA_IMPORT_TOKEN` | Secret | Push import branches and open PRs in the source repository. |
| `COPYBARISTA_SOURCE_REPO` | Variable | Source repository in `owner/name` form. |
| `COPYBARISTA_SOURCE_PROJECT_PATH` | Variable | Source checkout directory that contains `copy.barista.toml`. |
| `COPYBARISTA_SYNC_USER_NAME` | Variable | Optional sync commit author name. |
| `COPYBARISTA_SYNC_USER_EMAIL` | Variable | Optional sync commit author email. Use the same value in both repositories if reverse sync skips generated export merges by author. |

Use fine-grained tokens with the narrowest repository access that works:

- Source-to-public token: public repository `Contents: read and write` and
  `Pull requests: read and write`.
- Public-to-source token: source repository `Contents: read and write` and
  `Pull requests: read and write`.
- Add `Workflows: read and write` only when the exported tree intentionally
  creates or updates workflow files under `.github/workflows`.
- Add public repository `Contents: read` to the public-to-source token while
  the public repository is still private.

The example source-to-public workflow preserves the public repository's
`.github/` directory, so the reverse-sync workflow remains public-repo-owned.
If your exported tree owns workflow files instead, configure `Workflows: read
and write` before the first export. GitHub rejects pushes that create or update
`.github/workflows/*` without that permission, even when `Contents: read and
write` is present.

## Merge Settings

Use squash merge as the default for generated export PRs. It keeps public
history concise while preserving review history in GitHub.

Recommended repository settings:

- Enable squash merge.
- Disable merge commits.
- Disable rebase merge unless your project intentionally wants it.
- Keep generated branches after merge when branch names are used as sync
  history.

Squash merge keeps generated public history concise. Keeping generated branches
is optional, but it can make sync audits easier because branch names encode the
export project or imported public SHA. Generated branch updates should use
`git push --force-with-lease`; protected default branches should not allow force
pushes.

Check current settings:

```bash
OWNER=your-org
REPO=your-repo
gh repo view "$OWNER/$REPO" \
  --json mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed,deleteBranchOnMerge
```

## Main Branch Ruleset

Copybarista ships an example GitHub repository ruleset at
`examples/python-package/github/protect-main-ruleset.json`.

Install it with:

```bash
OWNER=your-org
REPO=your-repo
gh api \
  --method POST \
  "repos/$OWNER/$REPO/rulesets" \
  --input examples/python-package/github/protect-main-ruleset.json
```

Update an existing ruleset:

```bash
OWNER=your-org
REPO=your-repo
RULESET_ID="$(gh api "repos/$OWNER/$REPO/rulesets" \
  --jq '.[] | select(.name == "Protect main") | .id')"
gh api \
  --method PUT \
  "repos/$OWNER/$REPO/rulesets/$RULESET_ID" \
  --input examples/python-package/github/protect-main-ruleset.json
```

Verify the active rules:

```bash
gh api "repos/$OWNER/$REPO/rules/branches/main" \
  --jq '.[] | {type, parameters}'
```

The example ruleset requires:

- Pull requests for `main`.
- Squash merge only.
- One approval.
- Last-pusher approval by someone else.
- Resolved review threads.
- Fresh required checks.
- Passing `Python 3.12` and `Python 3.13`.
- Linear history.
- No force pushes to `main`.
- No `main` deletion.

Organization admins are allowed to bypass the rules. That keeps initial setup
and emergency repair possible while maintainers, contributors, and bots remain
subject to normal PR rules unless granted a separate bypass.

If your CI check names differ, edit
`rules[].parameters.required_status_checks[].context` before installing the
ruleset.

## PyPI Trusted Publishing

For Python packages, prefer PyPI Trusted Publishing over long-lived PyPI API
tokens.

The GitHub workflow needs:

- `permissions.id-token: write`
- `environment: pypi`
- `pypa/gh-action-pypi-publish@release/v1`

Create or update the GitHub release environment:

```bash
OWNER=your-org
REPO=your-repo
gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "/repos/$OWNER/$REPO/environments/pypi" \
  -F wait_timer=0 \
  -F prevent_self_review=true \
  -F deployment_branch_policy[protected_branches]=true \
  -F deployment_branch_policy[custom_branch_policies]=false
```

Then create a pending Trusted Publisher in PyPI with the exact GitHub owner,
repository, workflow filename, and environment. The PyPI project appears only
after the first successful publish.

## Release Checks

Before publishing or making a repository public, run the exported tree checks
from the repository root:

```bash
python scripts/check_release_tree.py . --allow-root-git
uv sync --all-groups
uv run --all-groups ruff check .
uv run --all-groups ruff format --check .
uv run --all-groups basedpyright copybarista scripts tests
uv run --all-groups pytest
uv build --out-dir /tmp/copybarista-dist-check
```

Run `scripts/check_release_tree.py` before dependency tools create local
artifacts such as `.venv` or package metadata.
