"""Tests for public-to-source sync helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import hashlib
import re
import subprocess

import pytest

from copybarista.scripts import sync_import_change
from copybarista.scripts.sync_import_change import (
    ImportBaseError,
    ImportRequest,
    _commit_author,
    _gh_pr_exists,
    _run_import_change,
    _string_bool,
    _validate_target,
    import_branch_name,
    import_change_pr_body,
    import_commit_subject,
    import_commit_subject_prefix,
    last_synced_public_sha,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


# What ``git hash-object -t tree /dev/null`` prints: the id of the empty tree.
_EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class FakeGit:
    """Repositories in memory that answer the git commands the baseline walk runs.

    Each answer takes the shape ``git log`` documents and git 2.50 prints: a
    ``--format`` record ends in a newline and a ``-z`` record in a NUL, ``%s`` is the
    message's first paragraph joined by spaces and ``%B`` the message as stored, and a
    ref git cannot resolve exits 128. A command it does not model fails the test rather
    than answering. A manual parity script, ``sync_import_change_git_parity.py`` in the
    source repository, builds the same histories with real git and compares the
    answers byte for byte.
    """

    def __init__(self) -> None:
        self.repos: dict[Path, _Repo] = {}
        self._made = 0

    def commit(
        self,
        root: Path,
        *,
        message: str,
        files: Mapping[str, str],
        email: str = "t@example.com",
    ) -> str:
        """Write ``files`` and commit them on ``main`` as ``email``; return the SHA."""
        repo = self.repos.setdefault(root.resolve(), _Repo())
        repo.files.update(files)
        self._made += 1
        made = _Commit(
            sha=_digest(f"commit {self._made}"),
            tree=_digest(f"tree {sorted(repo.files.items())}"),
            message=_stored(message),
            email=email,
        )
        repo.refs["refs/heads/main"] = (made, *repo.refs.get("refs/heads/main", ()))
        return made.sha

    def amend(self, root: Path, *, message: str) -> None:
        """Reword the tip of ``main``, as ``git commit --amend -m`` does."""
        repo = self.repos[root.resolve()]
        tip, *rest = repo.refs["refs/heads/main"]
        self._made += 1
        made = _Commit(
            sha=_digest(f"commit {self._made}"),
            tree=tip.tree,
            message=_stored(message),
            email=tip.email,
        )
        repo.refs["refs/heads/main"] = (made, *rest)

    def branch(self, root: Path, name: str, *, at: str) -> None:
        """Point ``refs/heads/<name>`` at the commit ``at`` of ``main``."""
        repo = self.repos[root.resolve()]
        history = repo.refs["refs/heads/main"]
        start = next(index for index, each in enumerate(history) if each.sha == at)
        repo.refs[f"refs/heads/{name}"] = history[start:]

    def delete_branch(self, root: Path, name: str) -> None:
        """Delete ``refs/heads/<name>``, as ``git branch -D`` does."""
        del self.repos[root.resolve()].refs[f"refs/heads/{name}"]

    def clone(self, source: Path, dest: Path, *, branch: str) -> None:
        """Clone ``branch`` of ``source`` alone, as ``git clone --single-branch``."""
        origin = self.repos[source.resolve()]
        history = origin.refs[f"refs/heads/{branch}"]
        self.repos[dest.resolve()] = _Repo(
            refs={
                f"refs/heads/{branch}": history,
                f"refs/remotes/origin/{branch}": history,
            },
            files=dict(origin.files),
            origin=origin,
        )

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = False,
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        """Answer one ``_run`` of git as ``_run`` returns it.

        It exits when ``check`` is set and git fails. Output comes back only when
        ``capture`` asks for it: uncaptured, ``_run`` leaves it to the job's log.
        """
        del options
        assert cwd is not None
        done = self._answer_in(argv, cwd=cwd)
        if check and done.returncode:
            raise SystemExit(done.returncode)
        return done if capture else subprocess.CompletedProcess(argv, done.returncode)

    # ``init``, ``add`` and ``commit`` change it.
    #
    # Only ``cwd`` itself is a repository: git's search of the parent directories is not
    # modeled.
    def _answer_in(
        self,
        argv: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Answer ``argv`` run in ``cwd``."""
        # Outside a repository git refuses any command needing one, so only the
        # program is checked there; a misspelled one must not read as "not a repo".
        if argv[:1] != ["git"]:
            raise AssertionError(f"FakeGit runs only git, not {argv}")
        root = cwd.resolve()
        if argv == ["git", "init", "--quiet", "--initial-branch=main"]:
            (cwd / ".git").mkdir()
            self.repos[root] = _Repo()
            return subprocess.CompletedProcess(argv, 0, "", "")
        repo = self.repos.get(root)
        if repo is None:
            return subprocess.CompletedProcess(
                argv,
                128,
                "",
                "fatal: not a git repository (or any of the parent directories): .git\n",
            )
        match argv:
            case ["git", "add", "--all"]:
                repo.files = {
                    str(path.relative_to(cwd)): path.read_text(encoding="utf-8")
                    for path in cwd.rglob("*")
                    if path.is_file() and ".git" not in path.relative_to(cwd).parts
                }
                return subprocess.CompletedProcess(argv, 0, "", "")
            case [
                "git",
                "-c",
                name,
                "-c",
                email,
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                message,
            ] if name.startswith("user.name=") and email.startswith("user.email="):
                _ = self.commit(
                    cwd,
                    message=message,
                    files={},
                    email=email.removeprefix("user.email="),
                )
                return subprocess.CompletedProcess(argv, 0, "", "")
            case ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, f"{root}\n", "")
            case _:
                return _answer(argv, repo)


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    """Route every ``_run`` to a fresh ``FakeGit``."""
    fake = FakeGit()
    monkeypatch.setattr(sync_import_change, "_run", fake.run)
    return fake


def _git_repo_with_commits(git: FakeGit, *, root: Path, subjects: list[str]) -> None:
    """Commit each subject in order on ``main`` at root."""
    for i, subject in enumerate(subjects):
        _ = git.commit(root, message=subject, files={"f.txt": f"{i}\n"})


# Bodies matter here: a landed export commit is identified by the ``<label> export
# branch: <branch>`` line the squash-merge writes into the body, not by its subject. The
# returned SHAs are in commit order (oldest first) so a test can assert which one the
# resolver selects.
def _public_repo_with_messages(
    git: FakeGit,
    *,
    root: Path,
    messages: list[str],
) -> list[str]:
    """Commit each full message (subject + body) in order; return the SHAs."""
    _git_repo_with_commits(git, root=root, subjects=["Initial commit"])
    return [
        git.commit(root, message=message, files={"g.txt": f"{i}\n"})
        for i, message in enumerate(messages)
    ]


def test_last_synced_public_sha_ignores_forged_export_commit_message(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """Unauthenticated commit text must not advance the import baseline.

    The live incident: months of source->public EXPORTS advanced public main
    while no import landed, so the target's import ledger stayed pinned months
    back. The three-way merge then used that stale ledger SHA as the common
    ancestor and re-presented already-exported work as 39 file conflicts.

    The fix lets an export commit advance the baseline, recognized by the
    ``<label> export branch: <branch>`` line its squash-merge body carries. That
    text is forgeable, so here no such branch holds the commit's tree: the
    resolver must ignore it and keep the ledger SHA.
    """
    target = tmp_path / "target"
    public = tmp_path / "public"
    ledger_sha = "a" * 40
    _git_repo_with_commits(
        git,
        root=target,
        subjects=[f"Import Sagent public changes {ledger_sha}"],
    )
    # Public history: the ledger's imported commit, then a later export.
    public_shas = _public_repo_with_messages(
        git,
        root=public,
        messages=[
            # The public commit the ledger recorded (its SHA is what the target
            # walk yields). Its own body is irrelevant to this arm.
            "Import Sagent public changes (ledger head)",
            "Unrelated public change",
            "Publish export\n\nSagent export branch: copybarista/export/main",
        ],
    )
    # Pin the ledger SHA onto the matching public commit so the target walk and
    # the public walk agree on identity: the resolver's ledger arm matches this
    # commit, its export arm matches the newer one, and export wins by recency.
    git.amend(target, message=f"Import Sagent public changes {public_shas[0]}")
    ledger_public_sha = public_shas[0]
    export_public_sha = public_shas[2]

    result = last_synced_public_sha(
        target_dir=target,
        sync_label="Sagent",
        base_branch="main",
        public_dir=public,
    )

    assert result == ledger_public_sha, (
        f"forged export text advanced the baseline to {export_public_sha[:12]}"
    )


def test_last_synced_public_sha_fetches_export_provenance_from_single_ref_clone(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """A main-only Actions checkout must fetch the export branch before trusting it."""
    public_source = tmp_path / "public-source"
    public_shas = _public_repo_with_messages(
        git,
        root=public_source,
        messages=[
            "Import Sagent public changes (ledger head)",
            "Publish export\n\nSagent export branch: sagent/export/main",
        ],
    )
    git.branch(public_source, "sagent/export/main", at=public_shas[1])
    public = tmp_path / "public"
    # Actions' single-ref checkout: main alone, the export branch left on origin.
    git.clone(public_source, public, branch="main")
    assert "refs/remotes/origin/sagent/export/main" not in git.repos[public].refs
    target = tmp_path / "target"
    _git_repo_with_commits(
        git,
        root=target,
        subjects=[f"Import Sagent public changes {public_shas[0]}"],
    )

    result = last_synced_public_sha(
        target_dir=target,
        sync_label="Sagent",
        base_branch="main",
        public_dir=public,
    )

    assert result == public_shas[1]


def test_last_synced_public_sha_prefers_the_ledger_when_it_is_newer(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """When the newest sync IS an import, the ledger SHA wins.

    Symmetric to the export case: the resolver returns whichever recognized
    commit is newest in public history, so a recent import that post-dates
    every export must not be overridden by an older export commit.
    """
    target = tmp_path / "target"
    public = tmp_path / "public"
    public_shas = _public_repo_with_messages(
        git,
        root=public,
        messages=[
            "Publish export\n\nSagent export branch: copybarista/export/main",
            "Unrelated public change",
            "Import Sagent public changes (this is the ledger head)",
        ],
    )
    import_public_sha = public_shas[2]
    git.branch(public, "copybarista/export/main", at=public_shas[0])
    _git_repo_with_commits(
        git,
        root=target,
        subjects=[f"Import Sagent public changes {import_public_sha}"],
    )

    result = last_synced_public_sha(
        target_dir=target,
        sync_label="Sagent",
        base_branch="main",
        public_dir=public,
    )

    assert result == import_public_sha


def test_last_synced_public_sha_export_arm_scopes_to_sync_label(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """A different label's export commit must not be read as this label's sync.

    The public repo is per-package (one label), but the marker match must be
    label-scoped anyway: a mislabeled match would silently pick a baseline
    from an unrelated export and reintroduce conflicts.
    """
    target = tmp_path / "target"
    public = tmp_path / "public"
    public_shas = _public_repo_with_messages(
        git,
        root=public,
        messages=[
            "Import Sagent public changes (ledger head)",
            "Publish export\n\nConfiggle export branch: copybarista/export/main",
        ],
    )
    _ = git.commit(
        target,
        message=f"Import Sagent public changes {public_shas[0]}",
        files={"f.txt": "x\n"},
    )

    result = last_synced_public_sha(
        target_dir=target,
        sync_label="Sagent",
        base_branch="main",
        public_dir=public,
    )

    # The Configgle export is ignored; only the Sagent ledger commit matches.
    assert result == public_shas[0]


def test_last_synced_public_sha_without_public_dir_walks_only_the_ledger(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """Omitting the public checkout preserves the original ledger-only behavior.

    The PR/open-pr path resolves the baseline from target history alone and
    passes no public dir; that call must be unchanged.
    """
    sha = "d" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[f"Import Sagent public changes {sha}"],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == sha
    )


# For tests that only care about target_dir / project routing.
def _import_request(*, target_dir: Path) -> ImportRequest:
    """Build an ImportRequest with placeholder fields."""
    return ImportRequest(
        public_base=target_dir / "public-base",
        public_head=target_dir / "public-head",
        target_dir=target_dir,
        target_repo="rekursiv-ai/source",
        project_path=Path("package"),
        base_branch="main",
        public_repo="rekursiv-ai/public",
        public_sha="abcdef1234567890abcdef1234567890abcdef12",
        public_base_ref="base",
        public_head_ref="head",
        branch="copybarista/import/sha-abcdef1234567890abcdef1234567890abcdef12",
        sync_label="Package",
        sync_user_name="copybarista",
        sync_user_email="copybarista@example.com",
        report=target_dir / "report.json",
        open_pr=False,
        open_pr_only=False,
        runner_temp=target_dir,
        validation_commands=(),
        refresh_public_lockfile=False,
    )


def test_main_accepts_generic_project_validation_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
        [
            "--project-path",
            "packages/configgle",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
            "--validation-command",
            "uv sync --all-groups",
            "--validation-command",
            "uv run pytest",
        ],
    )

    assert captured[0].project_path == Path("packages/configgle")
    assert captured[0].validation_commands == ("uv sync --all-groups", "uv run pytest")
    assert not captured[0].refresh_public_lockfile


def test_export_copybarista_requirements_uses_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The copybarista-group export must not re-resolve the monorepo lock.

    Run in the monorepo root without ``--frozen``, ``uv export`` is free to
    re-resolve the whole ``loop`` project whenever the runner's checked-out
    ``uv.lock`` looks stale, dragging in monorepo-only ``loop:dev`` deps (e.g.
    the linux-only ``pycairo``, which needs a system cairo the import runner
    lacks). ``--frozen`` pins it to the committed lock so the bootstrap only
    reads the ``copybarista`` group and never builds monorepo dev deps.
    """
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    sync_import_change._export_copybarista_requirements(
        target_dir=tmp_path,
        runner_temp=tmp_path,
    )

    assert captured, "expected an uv export invocation"
    argv = captured[0]
    assert "export" in argv
    assert "--frozen" in argv, f"uv export must be frozen; got {argv}"


def test_main_resolves_filesystem_inputs_to_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Copybarista subprocesses run with cwd=target_dir, so relative path inputs
    # must be made absolute or they double (target/target/...).
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
        [
            "--project-path",
            "pkg",
            "--target-dir",
            "target",
            "--public-base",
            "pb",
            "--public-head",
            "ph",
            "--report",
            "r.json",
            "--runner-temp",
            "rt",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
        ],
    )

    req = captured[0]
    for value in (
        req.target_dir,
        req.public_base,
        req.public_head,
        req.report,
        req.runner_temp,
    ):
        assert value.is_absolute(), value


def test_old_push_workflow_defaults_to_recording_red_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
        [
            "--project-path",
            "packages/configgle",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
        ],
    )

    assert captured[0].record_on_validation_failure


def test_main_accepts_refresh_public_lockfile_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ImportRequest] = []

    def fake_run_import_sync(request: ImportRequest) -> None:
        captured.append(request)

    monkeypatch.setattr(sync_import_change, "run_import_sync", fake_run_import_sync)

    sync_import_change.run(
        [
            "--project-path",
            "packages/configgle",
            "--public-base-ref",
            "base",
            "--public-head-ref",
            "head",
            "--refresh-public-lockfile",
        ],
    )

    assert captured[0].refresh_public_lockfile


def test_import_change_ignores_generated_public_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    public_base = tmp_path / "public-base"
    public_head = tmp_path / "public-head"
    target = tmp_path / "target"
    project = target / "package"
    for root in (public_base, public_head, project, target / "tools" / "copybarista"):
        root.mkdir(parents=True)
    (public_base / "module.py").write_text("base\n", encoding="utf-8")
    (public_base / "uv.lock").write_text("base lock\n", encoding="utf-8")
    (public_head / "module.py").write_text("head\n", encoding="utf-8")
    (public_head / "uv.lock").write_text("head lock\n", encoding="utf-8")
    for public in (public_base, public_head):
        (public / "examples/widget").mkdir(parents=True)
        (public / "examples/widget/uv.lock").write_text(
            "nested lock\n",
            encoding="utf-8",
        )
    (project / "copy.barista.toml").write_text("[workflow]\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        sanitized_base = Path(argv[argv.index("--public-base") + 1])
        sanitized_head = Path(argv[argv.index("--public-head") + 1])
        assert sanitized_base != public_base
        assert sanitized_head != public_head
        assert (sanitized_base / "module.py").read_text(encoding="utf-8") == "base\n"
        assert (sanitized_head / "module.py").read_text(encoding="utf-8") == "head\n"
        assert not (sanitized_base / "uv.lock").exists()
        assert not (sanitized_head / "uv.lock").exists()
        assert (
            sanitized_base / "examples/widget/uv.lock"
        ).read_text() == "nested lock\n"
        assert (
            sanitized_head / "examples/widget/uv.lock"
        ).read_text() == "nested lock\n"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    _run_import_change(
        request=ImportRequest(
            public_base=public_base,
            public_head=public_head,
            target_dir=target,
            target_repo="rekursiv-ai/source",
            project_path=Path("package"),
            base_branch="main",
            public_repo="rekursiv-ai/public",
            public_sha="abcdef1234567890abcdef1234567890abcdef12",
            public_base_ref="base",
            public_head_ref="head",
            branch="copybarista/import/sha-abcdef1234567890abcdef1234567890abcdef12",
            sync_label="Package",
            sync_user_name="copybarista",
            sync_user_email="copybarista@example.com",
            report=tmp_path / "report.json",
            open_pr=False,
            open_pr_only=False,
            runner_temp=runner_temp,
            validation_commands=(),
            refresh_public_lockfile=True,
        ),
        project=project,
        requirements=tmp_path / "copybarista-requirements.txt",
    )

    assert len(calls) == 1


def test_run_import_sync_imports_then_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    target = tmp_path / "target"

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_export_requirements(*, target_dir: Path, runner_temp: Path) -> Path:
        del target_dir  # Signature must match for monkeypatch; unused here.
        return runner_temp / "copybarista-requirements.txt"

    def fake_import_change(
        *,
        request: ImportRequest,
        project: Path,
        requirements: Path,
    ) -> None:
        calls.append(
            ["import", str(project), str(request.target_dir), str(requirements)],
        )

    # Signature must match _validate_target for monkeypatch; some args unused here.
    def fake_validate_target(
        *,
        request: ImportRequest,
        project: Path,
        runner_temp: Path,
        requirements: Path,
    ) -> None:
        del request  # Unused here; present to match the patched signature.
        calls.append(
            ["validate", str(project), str(runner_temp), str(requirements)],
        )

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(
        sync_import_change,
        "_export_copybarista_requirements",
        fake_export_requirements,
    )
    monkeypatch.setattr(
        sync_import_change,
        "_run_import_change",
        fake_import_change,
    )
    monkeypatch.setattr(
        sync_import_change,
        "_validate_target",
        fake_validate_target,
    )

    sync_import_change.run_import_sync(
        ImportRequest(
            public_base=tmp_path / "public-base",
            public_head=tmp_path / "public-head",
            target_dir=target,
            target_repo="rekursiv-ai/source",
            project_path=Path("package"),
            base_branch="main",
            public_repo="rekursiv-ai/public",
            public_sha="abcdef1234567890abcdef1234567890abcdef12",
            public_base_ref="base",
            public_head_ref="head",
            branch="copybarista/import/sha-abcdef1234567890abcdef1234567890abcdef12",
            sync_label="Package",
            sync_user_name="copybarista",
            sync_user_email="copybarista@example.com",
            report=tmp_path / "report.json",
            open_pr=False,
            open_pr_only=False,
            runner_temp=tmp_path,
            validation_commands=("uv run pytest",),
            refresh_public_lockfile=False,
        ),
    )

    # Requirements are exported (stubbed), then import, then validate -- both
    # receive the same pinned requirements path.
    reqs = str(tmp_path / "copybarista-requirements.txt")
    assert calls[0] == ["import", str(target / "package"), str(target), reqs]
    assert calls[1] == [
        "validate",
        str(target / "package"),
        str(tmp_path),
        reqs,
    ]


def test_push_import_records_ledger_despite_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A public-main push is authoritative even when validation is red."""
    git_calls: list[list[str]] = []
    github_calls: list[list[str]] = []

    def fake_export_requirements(*, target_dir: Path, runner_temp: Path) -> Path:
        del target_dir
        return runner_temp / "copybarista-requirements.txt"

    def fake_import(**_: object) -> None:
        return None

    def validation_failure(**_: object) -> None:
        raise SystemExit(1)

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        git_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="a" * 40 + "\n")

    def fake_run_gh(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        github_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def fake_pr_exists(*, branch: str, repo: str, cwd: Path) -> bool:
        del branch, repo, cwd
        return False

    def fake_has_changes(*, path: Path, rel: Path) -> bool:
        del path, rel
        return True

    monkeypatch.setattr(
        sync_import_change,
        "_export_copybarista_requirements",
        fake_export_requirements,
    )
    monkeypatch.setattr(sync_import_change, "_run_import_change", fake_import)
    monkeypatch.setattr(sync_import_change, "_validate_target", validation_failure)
    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_run_gh", fake_run_gh)
    monkeypatch.setattr(sync_import_change, "_gh_pr_exists", fake_pr_exists)
    monkeypatch.setattr(sync_import_change, "_git_has_changes", fake_has_changes)
    request = replace(
        _import_request(target_dir=tmp_path),
        open_pr=True,
        record_on_validation_failure=True,
    )

    sync_import_change.run_import_sync(request)

    commits = [call for call in git_calls if call[:2] == ["git", "commit"]]
    assert commits, "validation must not leave the import ledger missing"
    assert any(call[:3] == ["gh", "pr", "create"] for call in github_calls)
    merges = [call for call in github_calls if call[:3] == ["gh", "pr", "merge"]]
    assert len(merges) == 1
    assert "--admin" in merges[0], "red checks must not leave the import pending"
    assert request.validation_failure_marker.exists()
    err = capsys.readouterr().err
    assert "::error::" in err
    assert "will still be recorded" in err


def test_failed_import_annotates_the_stalled_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing import must say that it just stalled the export.

    The import exiting nonzero is visible, but nothing connected that failure
    to its consequence: no ledger marker lands, so the export guard blocks
    every subsequent run. Six projects sat un-exported for days because the
    two halves were never linked in the logs.
    """

    def fake_export_requirements(*, target_dir: Path, runner_temp: Path) -> Path:
        del target_dir
        return runner_temp / "copybarista-requirements.txt"

    def boom(**_: object) -> None:
        raise SystemExit(1)

    def unused_run(*_: object) -> None:
        return None

    monkeypatch.setattr(sync_import_change, "_run", unused_run)
    monkeypatch.setattr(
        sync_import_change,
        "_export_copybarista_requirements",
        fake_export_requirements,
    )
    monkeypatch.setattr(sync_import_change, "_run_import_change", boom)

    with pytest.raises(SystemExit):
        sync_import_change.run_import_sync(_import_request(target_dir=tmp_path))

    # _log writes to stderr; stdout is the machine channel (--print-synced-base).
    err = capsys.readouterr().err
    assert "::error::" in err, "a failed import must annotate the run"
    assert "export" in err.casefold(), "the annotation must name the consequence"


def test_import_change_pr_body_contains_review_context():
    body = import_change_pr_body(
        public_repo="rekursiv-ai/copybarista",
        public_sha="abcdef1234567890",
        public_base_ref="base",
        public_head_ref="head",
        source_base_ref="source",
        sync_label="Copybarista",
    )

    assert "Imports Copybarista public repository changes into the source" in body
    assert "- Public repository: `rekursiv-ai/copybarista`" in body
    assert "- Public SHA: `abcdef1234567890`" in body
    assert "- Public base: `base`" in body
    assert "- Public head: `head`" in body
    assert "- Source base: `source`" in body
    assert "`copybarista import-change`" in body
    assert "Regenerate this PR before merging if source `main` changes." in body


def test_import_change_pr_body_accepts_custom_sync_label():
    body = import_change_pr_body(
        public_repo="rekursiv-ai/configgle",
        public_sha="abcdef1234567890",
        public_base_ref="base",
        public_head_ref="head",
        source_base_ref="source",
        sync_label="Configgle",
    )

    assert "Imports Configgle public repository changes into the source" in body


def test_string_bool_accepts_action_boolean_values():
    assert _string_bool("true")
    assert _string_bool("1")
    assert _string_bool("yes")
    assert not _string_bool("false")
    assert not _string_bool("")


def test_import_branch_name_uses_public_sha():
    assert (
        import_branch_name(
            explicit="",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )
        == "copybarista/import/sha-abcdef123456"
    )


def test_import_branch_name_allows_explicit_branch():
    assert (
        import_branch_name(
            explicit="copybarista/import/custom",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )
        == "copybarista/import/custom"
    )


def test_import_branch_name_rejects_non_generated_explicit_branch():
    with pytest.raises(SystemExit) as error:
        import_branch_name(
            explicit="main",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )

    assert error.value.code == 2


def test_import_branch_name_rejects_malformed_explicit_branch():
    with pytest.raises(SystemExit) as error:
        import_branch_name(
            explicit="copybarista/import/../main",
            public_sha="abcdef1234567890",
            prefix="copybarista/import/",
        )

    assert error.value.code == 2


def test_commit_author_uses_sync_identity():
    assert (
        _commit_author("copybarista", "copybarista@example.com")
        == "copybarista <copybarista@example.com>"
    )


def test_gh_pr_exists_only_counts_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0:4] == ["gh", "pr", "list", "--repo"]
        assert "--state" in argv
        assert "open" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="[]")

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    assert not _gh_pr_exists(
        branch="copybarista/import/sha-abcdef1234567890abcdef1234567890abcdef12",
        repo="rekursiv-ai/source",
        cwd=Path.cwd(),
    )


def test_gh_pr_exists_retries_transient_github_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="HTTP 504: try resubmitting your request",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    def no_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr("time.sleep", no_sleep)

    assert not _gh_pr_exists(
        branch="copybarista/import/sha-abcdef1234567890abcdef1234567890abcdef12",
        repo="rekursiv-ai/source",
        cwd=Path.cwd(),
    )
    assert calls == 2


def test_gh_pr_exists_fails_loudly_after_retry_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="HTTP 504: try resubmitting your request",
        )

    def no_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr("time.sleep", no_sleep)

    with pytest.raises(SystemExit) as error:
        _gh_pr_exists(
            branch="copybarista/import/sha-abcdef1234567890abcdef1234567890abcdef12",
            repo="rekursiv-ai/source",
            cwd=Path.cwd(),
        )

    assert error.value.code == 1
    assert calls == sync_import_change.GITHUB_RETRY_ATTEMPTS
    assert "HTTP 504" in capsys.readouterr().err


def test_validate_target_runs_checks_against_exported_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path | None]] = []
    tree = Path("/sentinel/validation-tree")

    def fake_run(
        argv: list[str],
        *,
        cwd: Path | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0)

    # Signature must match _export_public_tree for monkeypatch; args unused.
    def fake_export(
        *,
        request: ImportRequest,
        project: Path,
        runner_temp: Path,
        requirements: Path,
    ) -> Path:
        del request, project, runner_temp, requirements
        return tree

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_export_public_tree", fake_export)

    _validate_target(
        request=replace(
            _import_request(target_dir=Path("/repo/target")),
            validation_commands=("uv sync", "uv run pytest"),
        ),
        project=Path("/repo/pkg"),
        runner_temp=Path("/sentinel/runner"),
        requirements=Path("/sentinel/copybarista-requirements.txt"),
    )

    # One script, run as a GitHub ``run:`` step runs it.
    assert calls == [
        (
            [
                "bash",
                "--noprofile",
                "--norc",
                "-eo",
                "pipefail",
                "-c",
                "uv sync\nuv run pytest",
            ],
            tree,
        ),
    ]


def test_export_public_tree_runs_copybarista_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    target = tmp_path / "target"
    project = target / "package"
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    requirements = tmp_path / "copybarista-requirements.txt"
    tree = sync_import_change._export_public_tree(
        request=_import_request(target_dir=target),
        project=project,
        runner_temp=runner_temp,
        requirements=requirements,
    )

    assert tree == runner_temp / "copybarista-validation-tree"
    export = calls[0]
    # Dependency-free copybarista export of the post-import tree to a folder,
    # using the pinned requirements file.
    assert "--no-project" in export
    assert str(requirements) in export
    assert export[-6:] == [
        "export",
        str(project / "copy.barista.toml"),
        str(target),
        "--folder-dir",
        str(tree),
        "--force",
    ]


def test_export_public_tree_relocks_branch_dependencies_before_git_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validated tree must track sibling branches, like the export does.

    A seed lock pins a sibling (configgle) at whatever SHA was current when it
    was last locked. Validating with ``uv sync
    --frozen`` against that pin tested a stale sibling, so an import failed on
    a bug the sibling had already fixed on its branch.
    """
    runner_temp = tmp_path / "runner"
    tree = runner_temp / "copybarista-validation-tree"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-3:-1] == ["--folder-dir", str(tree)]:
            tree.mkdir(parents=True)
            (tree / "pyproject.toml").write_text(
                "[tool.uv.sources]\n"
                'configgle = { git = "https://example.invalid/c", branch = "main" }\n'
                'pinned = { git = "https://example.invalid/p", rev = "abc123" }\n',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    request = replace(
        _import_request(target_dir=tmp_path / "target"),
        refresh_public_lockfile=True,
    )

    sync_import_change._export_public_tree(
        request=request,
        project=tmp_path / "target" / "package",
        runner_temp=runner_temp,
        requirements=tmp_path / "copybarista-requirements.txt",
    )

    lock = ["uv", "lock", "--upgrade-package", "configgle"]
    assert lock in calls
    assert calls.index(lock) < calls.index(["git", "add", "--all"])


def test_export_copybarista_requirements_exports_group_from_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="pyyaml==6.0.3\nruff==0.15.17\n",
        )

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    target = tmp_path / "target"
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    requirements = sync_import_change._export_copybarista_requirements(
        target_dir=target,
        runner_temp=runner_temp,
    )

    assert requirements == (runner_temp / "copybarista-requirements.txt").resolve()
    assert requirements.read_text() == "pyyaml==6.0.3\nruff==0.15.17\n"
    # Pins come from the lock via the copybarista group, not a maintained file.
    assert calls[0] == [
        "uv",
        "--quiet",
        "export",
        "--frozen",
        "--only-group",
        "copybarista",
        "--no-hashes",
        "--no-emit-project",
        "--format",
        "requirements.txt",
    ]


def test_import_commit_subject_round_trips_through_baseline_walk(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    # The SHA written into the import commit subject must be the exact SHA the
    # next run's baseline walk recovers. This is the contract OA-1 broke at the
    # workflow level (recording the run SHA instead of the imported head SHA).
    sha = "3" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[import_commit_subject_prefix("Sagent") + sha],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == sha
    )


def test_import_commit_subject_refuses_an_abbreviated_sha() -> None:
    """The writer rejects what the baseline walk could not read.

    An abbreviated SHA parses as no marker at all, so the walk returns an older
    baseline (or none) and the export guard concludes the public head was never
    imported -- which stops the project exporting while every run stays green.
    Refusing at the write side is what keeps that unrepresentable.
    """
    with pytest.raises(ImportBaseError, match="unreadable import ledger"):
        import_commit_subject("Wesearch", "7f2f091d115a")


def test_import_commit_subject_refuses_a_reworded_prefix() -> None:
    """A subject missing the exact prefix is refused, not silently written."""
    with pytest.raises(ImportBaseError, match="unreadable import ledger"):
        import_commit_subject("Wesearch", "not-a-sha")


def test_import_commit_subject_round_trips_a_full_sha(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """What the writer accepts, the baseline walk recovers unchanged."""
    sha = "7" * 40
    subject = import_commit_subject("Wesearch", sha)
    _git_repo_with_commits(git, root=tmp_path, subjects=[subject])

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Wesearch",
            base_branch="main",
        )
        == sha
    )


def test_last_synced_public_sha_returns_newest_imported_sha(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    older = "a" * 40
    newer = "b" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {older}",
            "Add cross-experiment metric query support",
            f"Import Sagent public changes {newer}",
            "Later unrelated work",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == newer
    )


def test_last_synced_public_sha_reads_a_squash_merged_subject(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """A squash-merged import still records the baseline.

    GitHub appends ``(#N)`` to the subject when a PR is squash-merged, which
    is how these imports land. An end-anchored match skips those commits and
    silently returns an OLDER baseline -- the export's data-loss guard then
    compares against stale state, and the import's three-way merge gets a
    wrong common ancestor, manufacturing conflicts that are not real.
    """
    older = "a" * 40
    newer = "b" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {older}",
            f"Import Sagent public changes {newer} (#77)",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == newer
    )


def test_last_synced_public_sha_ignores_trailing_prose(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """Only the PR-number suffix is tolerated, not arbitrary trailing text."""
    good = "a" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {good}",
            f"Import Sagent public changes {'b' * 40} but reverted later",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == good
    )


def test_last_synced_public_sha_scopes_to_sync_label(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    # A different label's imports must not be mistaken for this label's baseline.
    sagent = "c" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {sagent}",
            f"Import Configgle public changes {'d' * 40}",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == sagent
    )


def test_last_synced_public_sha_raises_without_prior_import(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    _git_repo_with_commits(git, root=tmp_path, subjects=["Initial commit"])

    with pytest.raises(
        ImportBaseError,
        match="No landed 'Sagent' import or export commit",
    ):
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )


def test_last_synced_public_sha_returns_fallback_without_prior_import(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    # First import: no landed history yet, so the caller-supplied baseline
    # (the pushed commit's parent) stands in for the merge ancestor.
    parent = "f" * 40
    _git_repo_with_commits(git, root=tmp_path, subjects=["Initial commit"])

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
            fallback=parent,
        )
        == parent
    )


def test_last_synced_public_sha_matches_label_literally(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    # A label with regex metacharacters must match by literal text, not as a
    # git --grep regex that could match unintended subjects.
    sha = "1" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[f"Import C++.NET public changes {sha}"],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="C++.NET",
            base_branch="main",
        )
        == sha
    )


def test_last_synced_public_sha_ignores_incidental_sha_in_subject(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    # A 40-hex token elsewhere in an unrelated subject must not be mistaken for
    # a landed import; only the import-template position counts.
    incidental = "9" * 40
    landed = "2" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[
            f"Import Sagent public changes {landed}",
            f"Revert commit {incidental} for reasons",
        ],
    )

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == landed
    )


def test_main_print_synced_base_prints_and_skips_import(
    git: FakeGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha = "e" * 40
    _git_repo_with_commits(
        git,
        root=tmp_path,
        subjects=[f"Import Sagent public changes {sha}"],
    )

    def fail_run_import_sync(_: ImportRequest) -> None:
        raise AssertionError("import must not run in print-synced-base mode")

    monkeypatch.setattr(sync_import_change, "run_import_sync", fail_run_import_sync)

    sync_import_change.run(
        [
            "--print-synced-base",
            "--target-dir",
            str(tmp_path),
            "--sync-label",
            "Sagent",
            "--base-branch",
            "main",
        ],
    )

    assert capsys.readouterr().out.strip() == sha


def test_main_print_synced_base_emits_fallback_without_history(
    git: FakeGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent = "7" * 40
    _git_repo_with_commits(git, root=tmp_path, subjects=["Initial commit"])

    def fail_run_import_sync(_: ImportRequest) -> None:
        raise AssertionError("import must not run in print-synced-base mode")

    monkeypatch.setattr(sync_import_change, "run_import_sync", fail_run_import_sync)

    sync_import_change.run(
        [
            "--print-synced-base",
            "--target-dir",
            str(tmp_path),
            "--sync-label",
            "Sagent",
            "--base-branch",
            "main",
            "--fallback-sha",
            parent,
        ],
    )

    assert capsys.readouterr().out.strip() == parent


def test_main_print_synced_base_prefers_export_from_public_dir(
    git: FakeGit,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: --public-dir makes a landed export the printed baseline.

    Drives the real CLI path the workflow uses, so a stale ledger plus a later
    public export prints the export SHA -- the fix's whole point.
    """
    target = tmp_path / "target"
    public = tmp_path / "public"
    public_shas = _public_repo_with_messages(
        git,
        root=public,
        messages=[
            "Import Sagent public changes (ledger head)",
            "Publish export\n\nSagent export branch: copybarista/export/main",
        ],
    )
    git.branch(public, "copybarista/export/main", at=public_shas[1])
    _git_repo_with_commits(
        git,
        root=target,
        subjects=[f"Import Sagent public changes {public_shas[0]}"],
    )

    def fail_run_import_sync(_: ImportRequest) -> None:
        raise AssertionError("import must not run in print-synced-base mode")

    monkeypatch.setattr(sync_import_change, "run_import_sync", fail_run_import_sync)

    sync_import_change.run(
        [
            "--print-synced-base",
            "--target-dir",
            str(target),
            "--sync-label",
            "Sagent",
            "--base-branch",
            "main",
            "--public-dir",
            str(public),
        ],
    )

    assert capsys.readouterr().out.strip() == public_shas[1]


_MERGE_ARGV = [
    "gh",
    "pr",
    "merge",
    "pkg/import/sha-abc",
    "--repo",
    "rekursiv-ai/source",
    "--squash",
    "--subject",
    "Import Package public changes abc",
    "--body",
    "Package import branch: pkg/import/sha-abc",
]
_ADMIN_FAILED = "Admin merge unavailable; queueing auto-merge behind source checks.\n"
_AUTO_FAILED = (
    "Auto-merge unavailable (no branch protection / pending checks); "
    "merging the import PR directly.\n"
)


# ``results`` maps a mode flag (``--admin``, ``--auto``, or ``""`` for the direct merge)
# to that call's returncode, stdout, and stderr.
def _merge_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    results: dict[str, tuple[int, str, str]],
) -> list[tuple[list[str], dict[str, object]]]:
    """Run ``_merge_import_pr`` against a fake ``gh``; return every call made."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run_gh(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        mode = next((flag for flag in ("--admin", "--auto") if flag in argv), "")
        code, out, err = results.get(mode, (0, "", ""))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    monkeypatch.setattr(sync_import_change, "_run_gh", fake_run_gh)
    sync_import_change._merge_import_pr(
        branch="pkg/import/sha-abc",
        target_repo="rekursiv-ai/source",
        title="Import Package public changes abc",
        sync_label="Package",
        cwd=tmp_path,
    )
    return calls


def test_import_pr_auto_merges_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean import merges itself; a human adds nothing to that decision.

    The public change is already reviewed and already live -- the source is
    the render authority, not a second approval gate. Waiting on a human is
    also what stalls the export, since an unmerged import blocks it. A red
    source CI must not hold it either, so the first attempt is ``--admin``.
    """
    calls = _merge_with(monkeypatch, tmp_path, results={})

    assert calls == [
        ([*_MERGE_ARGV, "--admin"], {"cwd": tmp_path, "capture": True, "check": False}),
    ]
    assert capsys.readouterr().err == ""


def test_import_pr_falls_back_to_auto_merge_without_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A token that cannot bypass branch rules still queues the merge."""
    calls = _merge_with(
        monkeypatch,
        tmp_path,
        results={"--admin": (1, "", "Repository rule violations found.")},
    )

    kwargs = {"cwd": tmp_path, "capture": True, "check": False}
    assert calls == [
        ([*_MERGE_ARGV, "--admin"], kwargs),
        ([*_MERGE_ARGV, "--auto"], kwargs),
    ]
    assert capsys.readouterr().err == _ADMIN_FAILED


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("", "Protected branch rules not configured."),
        ("GraphQL: enablePullRequestAutoMerge is not allowed", ""),
    ],
)
def test_import_pr_merges_directly_when_auto_merge_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stdout: str,
    stderr: str,
) -> None:
    """Without deferrable merges, ``--auto`` fails; merge immediately instead.

    Either stream may carry the refusal, in any case.
    """
    calls = _merge_with(
        monkeypatch,
        tmp_path,
        results={"--admin": (1, "", "denied"), "--auto": (1, stdout, stderr)},
    )

    kwargs = {"cwd": tmp_path, "capture": True, "check": False}
    assert calls == [
        ([*_MERGE_ARGV, "--admin"], kwargs),
        ([*_MERGE_ARGV, "--auto"], kwargs),
        (_MERGE_ARGV, {"cwd": tmp_path}),
    ]
    assert capsys.readouterr().err == _ADMIN_FAILED + _AUTO_FAILED


def test_import_pr_unrecognized_auto_merge_failure_exits_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ``--auto`` failure that is not a missing-protection refusal is fatal."""
    with pytest.raises(SystemExit) as raised:
        _merge_with(
            monkeypatch,
            tmp_path,
            results={"--admin": (1, "", "denied"), "--auto": (4, "", "HTTP 403")},
        )

    assert raised.value.code == 4


def test_pr_title_sha_is_readable_by_the_ledger(git: FakeGit, tmp_path: Path) -> None:
    """The title an import writes must parse as the marker it later reads.

    ``_open_or_update_target_pr`` wrote an abbreviated SHA while
    ``last_synced_public_sha`` required the full 40 characters, so a landed
    import recorded nothing the ledger could see -- the export's guard then
    reported the just-imported commit as unimported and skipped forever.
    """
    full = "0e8b8406cdd2" + "0" * 28
    title = f"Import Sagent public changes {sync_import_change._pr_title_sha(full)}"
    _git_repo_with_commits(git, root=tmp_path, subjects=[title])

    assert (
        last_synced_public_sha(
            target_dir=tmp_path,
            sync_label="Sagent",
            base_branch="main",
        )
        == full
    )


def test_import_title_feeds_the_export_guard(git: FakeGit, tmp_path: Path) -> None:
    """The import's own title must satisfy the export's data-loss guard.

    Every component here passed its own test while the loop stayed broken:
    the import wrote a subject, the ledger parsed a subject, and the guard
    read the ledger -- but nobody drove the writer's real output into the
    reader, so a 12-vs-40-character mismatch survived and every export
    skipped while reporting success.

    Drive the actual title the import writes into the actual ledger the
    guard consults, and require the guard to see the commit as imported.
    """
    public_sha = "0e8b8406cdd2cd79218a711687228111298afcaa"
    title = (
        f"Import Sagent public changes {sync_import_change._pr_title_sha(public_sha)}"
    )
    source = tmp_path / "source"
    _git_repo_with_commits(git, root=source, subjects=[title])

    # The ledger the guard reads.
    assert (
        last_synced_public_sha(
            target_dir=source,
            sync_label="Sagent",
            base_branch="main",
        )
        == public_sha
    ), "the import's title must parse as the marker the guard reads"


def test_squash_merged_import_title_feeds_the_export_guard(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """The same holds once GitHub appends its squash-merge suffix."""
    public_sha = "1" * 40
    title = (
        f"Import Sagent public changes "
        f"{sync_import_change._pr_title_sha(public_sha)} (#123)"
    )
    source = tmp_path / "source"
    _git_repo_with_commits(git, root=source, subjects=[title])

    assert (
        last_synced_public_sha(
            target_dir=source,
            sync_label="Sagent",
            base_branch="main",
        )
        == public_sha
    )


def test_export_public_tree_initializes_git_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exported tree must be a git repo before validation runs in it.

    The validation script is shared with the public repo's
    ``package-validation.yml`` and the export gate, and every package's script
    runs ``pre-commit run --all-files``. pre-commit resolves ``--all-files``
    through git and aborts with "git failed. Is it installed, and are you in a
    Git repository directory?" outside a repo. The other two consumers run in a
    real checkout; only this path exports to a bare directory, so it must
    supply the repo itself.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    target = tmp_path / "target"
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    sync_import_change._export_public_tree(
        request=_import_request(target_dir=target),
        project=target / "package",
        runner_temp=runner_temp,
        requirements=tmp_path / "copybarista-requirements.txt",
    )

    git_calls = [c for c in calls if c[:1] == ["git"]]
    assert [c[1] for c in git_calls[:2]] == ["init", "add"], calls
    assert git_calls[2][-5:-2] == ["commit", "--quiet", "--allow-empty"], calls
    assert len(git_calls) == 3, calls
    # Ordering matters: the repo must exist before any validation command runs,
    # and `_validate_target` runs them immediately after this returns.
    assert calls.index(git_calls[0]) > 0, "export must precede git init"


def test_export_public_tree_gives_validation_a_head_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validated test that records its checkout's commit must find one.

    The public repo validates in a real checkout, so ``git rev-parse HEAD``
    works there; a staged but uncommitted tree has no HEAD, which failed the
    craftax ghosts tests only on import.
    """
    runner_temp = tmp_path / "runner"
    tree = runner_temp / "copybarista-validation-tree"
    git = FakeGit()

    def fake_run(
        argv: list[str],
        **kwargs: Path | None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:1] == ["git"]:
            return git.run(argv, cwd=kwargs.get("cwd"))
        tree.mkdir(parents=True)
        (tree / "README.md").write_text("Exported.\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    sync_import_change._export_public_tree(
        request=_import_request(target_dir=tmp_path / "target"),
        project=tmp_path / "target" / "package",
        runner_temp=runner_temp,
        requirements=tmp_path / "copybarista-requirements.txt",
    )

    head = git.run(["git", "rev-parse", "HEAD"], cwd=tree, check=False)
    assert head.returncode == 0, head.stderr
    (commit,) = git.repos[tree.resolve()].refs["refs/heads/main"]
    assert commit.email == "copybarista@example.com"
    assert git.repos[tree.resolve()].files == {"README.md": "Exported.\n"}


def test_no_op_import_still_writes_the_ledger_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge that changes nothing must still record the public SHA.

    The export guard answers "has the source absorbed this public commit?" by
    searching target history for the import commit's subject. A clean merge
    that produced no diff means the source ALREADY holds that content -- the
    strongest possible yes -- yet writing no commit made it read as no. That
    left the export blocked on a SHA it had, clearable only by hand-forging
    the commit this function should have written.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="")

    def fake_gh_pr_exists(**_: object) -> bool:
        return False

    def fake_git_has_changes(**_: object) -> bool:
        # No porcelain output => no file changes: the no-op import path.
        return False

    monkeypatch.setattr(sync_import_change, "_run", fake_run)
    monkeypatch.setattr(sync_import_change, "_run_gh", fake_run)
    monkeypatch.setattr(sync_import_change, "_gh_pr_exists", fake_gh_pr_exists)
    monkeypatch.setattr(
        sync_import_change,
        "_git_has_changes",
        fake_git_has_changes,
    )

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    request = _import_request(target_dir=tmp_path)

    sync_import_change._open_or_update_target_pr(request=request)

    commits = [c for c in calls if c[:2] == ["git", "commit"]]
    assert commits, "no-op import wrote no commit, so the ledger has no record"
    subject = commits[0][commits[0].index("-m") + 1]
    expected = import_commit_subject_prefix(request.sync_label) + request.public_sha
    assert subject == expected, subject
    assert "--allow-empty" in commits[0], commits[0]


def test_ledger_subject_satisfies_the_export_guard(
    git: FakeGit,
    tmp_path: Path,
) -> None:
    """The subject this module writes is the one the export guard parses.

    Two modules agree on a string format by convention alone; an abbreviated
    SHA or a reworded prefix parses as no marker at all and silently reblocks
    every export. Assert the round trip rather than the format.
    """
    sha = "7e241ac9b43e60bd69f93251814a5dd6a1355097"
    subject = import_commit_subject_prefix("Priml") + sha
    root = tmp_path / "source"
    root.mkdir()
    _git_repo_with_commits(git, root=root, subjects=["init", subject, "later work"])

    assert (
        last_synced_public_sha(target_dir=root, sync_label="Priml", base_branch="main")
        == sha
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class _Commit:
    """A commit's id, its tree's id, and its message as git stores it."""

    sha: str
    tree: str
    message: str
    email: str


@dataclass(kw_only=True, slots=True)
class _Repo:
    """Each ref's history newest first, the worktree's files, and the origin."""

    refs: dict[str, tuple[_Commit, ...]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    origin: _Repo | None = None


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


def _stored(message: str) -> str:
    """Return ``message`` as ``git commit -m`` stores it, after its whitespace cleanup."""
    lines = "\n".join(line.rstrip() for line in message.splitlines())
    return re.sub(r"\n{3,}", "\n\n", lines).strip("\n") + "\n"


def _answer(argv: list[str], repo: _Repo) -> subprocess.CompletedProcess[str]:
    """Answer one git argv the way git 2.50 does, or fail on one it does not model."""
    match argv:
        case ["git", "log", ref, "--fixed-strings", grep, "--format=%s"] if (
            grep.startswith("--grep=")
        ):
            needle = grep.removeprefix("--grep=")
            return _log(
                argv,
                repo,
                ref,
                lambda each: (
                    f"{_subject(each.message)}\n"
                    if any(needle in line for line in each.message.splitlines())
                    else ""
                ),
            )
        case ["git", "log", ref, "-z", "--format=%H%x00%T%x00%B"]:
            return _log(
                argv,
                repo,
                ref,
                lambda each: f"{each.sha}\0{each.tree}\0{each.message}\0",
            )
        case ["git", "log", ref, "--format=%T"]:
            return _log(argv, repo, ref, lambda each: f"{each.tree}\n")
        case ["git", "log", span, "--format=%h%x00%ae%x00%s"] if ".." in span:
            return _log_span(argv, repo, span)
        case ["git", "rev-parse", "HEAD"]:
            history = repo.refs.get("refs/heads/main", ())
            if not history:
                return subprocess.CompletedProcess(
                    argv,
                    128,
                    "HEAD\n",
                    "fatal: ambiguous argument 'HEAD': unknown revision or path not in "
                    "the working tree.\n",
                )
            return subprocess.CompletedProcess(argv, 0, f"{history[0].sha}\n", "")
        case ["git", "hash-object", "-t", "tree", "/dev/null"]:
            return subprocess.CompletedProcess(argv, 0, f"{_EMPTY_TREE}\n", "")
        case ["git", "fetch", "--no-tags", "origin", refspec] if refspec.startswith(
            "+",
        ):
            return _fetch(argv, repo, refspec.removeprefix("+"))
        case _:
            raise AssertionError(f"FakeGit does not model {argv}")


def _log(
    argv: list[str],
    repo: _Repo,
    ref: str,
    record: Callable[[_Commit], str],
) -> subprocess.CompletedProcess[str]:
    """Print ``record`` of each commit of ``ref``, newest first, as ``git log`` does."""
    history = repo.refs.get(ref if ref.startswith("refs/") else f"refs/heads/{ref}")
    if history is None:
        return subprocess.CompletedProcess(
            argv,
            128,
            "",
            f"fatal: ambiguous argument '{ref}': unknown revision or path not in the "
            "working tree.\n",
        )
    return subprocess.CompletedProcess(argv, 0, "".join(map(record, history)), "")


# A ``base`` git cannot resolve fails the range; the empty tree excludes nothing.
def _log_span(
    argv: list[str],
    repo: _Repo,
    span: str,
) -> subprocess.CompletedProcess[str]:
    """Print each commit of ``base..head`` as ``%h%x00%ae%x00%s``, newest first."""
    base, _, head = span.partition("..")
    shown = _rev_history(repo, head)
    if shown is None:
        raise AssertionError(f"FakeGit does not model an unknown head: {argv}")
    hidden = () if base == _EMPTY_TREE else _rev_history(repo, base)
    if hidden is None:
        return subprocess.CompletedProcess(
            argv,
            128,
            "",
            f"fatal: Invalid revision range {span}\n",
        )
    return subprocess.CompletedProcess(
        argv,
        0,
        "".join(
            f"{each.sha[:7]}\0{each.email}\0{_subject(each.message)}\n"
            for each in shown
            if each not in hidden
        ),
        "",
    )


def _rev_history(repo: _Repo, rev: str) -> tuple[_Commit, ...] | None:
    """Return the history ``rev`` names (``HEAD``, a branch or a SHA), or None."""
    named = repo.refs.get("refs/heads/main" if rev == "HEAD" else f"refs/heads/{rev}")
    if named is not None:
        return named
    return next(
        (
            history[index:]
            for history in repo.refs.values()
            for index, each in enumerate(history)
            if each.sha == rev
        ),
        None,
    )


def _fetch(
    argv: list[str],
    repo: _Repo,
    refspec: str,
) -> subprocess.CompletedProcess[str]:
    """Copy ``origin``'s ref into a local one, as a forced ``git fetch`` does."""
    source, _, target = refspec.partition(":")
    if repo.origin is None:
        return subprocess.CompletedProcess(
            argv,
            128,
            "",
            "fatal: 'origin' does not appear to be a git repository\n",
        )
    history = repo.origin.refs.get(source)
    if history is None:
        return subprocess.CompletedProcess(
            argv,
            128,
            "",
            f"fatal: couldn't find remote ref {source}\n",
        )
    repo.refs[target] = history
    return subprocess.CompletedProcess(argv, 0, "", "")


def _subject(message: str) -> str:
    """Return ``%s``: the message's first paragraph, its lines joined by spaces."""
    return " ".join(message.split("\n\n", 1)[0].splitlines())


@dataclass(slots=True, kw_only=True)
class RecordingRun:
    """``subprocess.run`` in place: records each call, exits with ``returncode``."""

    returncode: int = 0
    calls: list[tuple[list[str], dict[str, object]]] = field(default_factory=list)

    def __call__(
        self,
        argv: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, options))
        return subprocess.CompletedProcess(argv, self.returncode, "out", "err")


def test_a_command_is_logged_and_run_without_the_callers_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ran = RecordingRun()
    monkeypatch.setattr(sync_import_change.subprocess, "run", ran)
    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere/.venv")
    monkeypatch.setenv("KEPT", "1")

    done = sync_import_change._run(["uv", "sync"], cwd=tmp_path, capture=True)

    assert done.stdout == "out"
    assert capsys.readouterr().err == "+ uv sync\n"
    ((argv, options),) = ran.calls
    env = options.pop("env")
    assert isinstance(env, dict)
    assert "VIRTUAL_ENV" not in env
    assert env["KEPT"] == "1"
    assert (argv, options) == (
        ["uv", "sync"],
        {
            "cwd": tmp_path,
            "check": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        },
    )


def test_a_failed_checked_command_exits_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_import_change.subprocess,
        "run",
        RecordingRun(returncode=3),
    )

    with pytest.raises(SystemExit) as exited:
        sync_import_change._run(["git", "push"])
    assert exited.value.code == 3
    assert sync_import_change._run(["git", "push"], check=False).returncode == 3


def test_main_runs_with_the_process_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_import_change, "run", _seven)

    assert sync_import_change.main() == 7


@pytest.mark.parametrize(
    ("argv", "missing"),
    [
        (["--public-base-ref", "b", "--public-head-ref", "h"], "--project-path"),
        (["--project-path", "p", "--public-head-ref", "h"], "--public-base-ref"),
        (["--project-path", "p", "--public-base-ref", "b"], "--public-head-ref"),
    ],
)
def test_run_requires_each_import_input(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    missing: str,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        sync_import_change.run(argv)

    assert f"{missing} is required for an import" in capsys.readouterr().err


def test_open_pr_only_opens_the_pr_and_imports_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[ImportRequest] = []

    def refuse(**_: object) -> Path:
        raise AssertionError("an open-PR-only run exports nothing")

    monkeypatch.setattr(sync_import_change, "_export_copybarista_requirements", refuse)

    def open_pr(*, request: ImportRequest) -> None:
        opened.append(request)

    monkeypatch.setattr(sync_import_change, "_open_or_update_target_pr", open_pr)
    request = replace(_import_request(target_dir=tmp_path), open_pr_only=True)

    sync_import_change.run_import_sync(request)

    assert opened == [request]


def test_a_red_validation_off_main_records_no_import_and_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing: list[ImportRequest] = []

    def validation_failure(**_: object) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(
        sync_import_change,
        "_export_copybarista_requirements",
        _requirements_in_runner_temp,
    )
    monkeypatch.setattr(sync_import_change, "_run_import_change", _import_nothing)
    monkeypatch.setattr(sync_import_change, "_validate_target", validation_failure)
    monkeypatch.setattr(sync_import_change, "_log_missing_import", missing.append)
    request = _import_request(target_dir=tmp_path)

    with pytest.raises(SystemExit, match="1"):
        sync_import_change.run_import_sync(request)

    assert missing == [request]
    assert not request.validation_failure_marker.exists()


@pytest.mark.parametrize(
    ("branch", "valid"),
    [
        ("copybarista/import/sha-1", True),
        ("main", False),
        ("-x", False),
        ("/x", False),
        ("x/", False),
        ("x.", False),
        ("x.lock", False),
        ("x..y", False),
        # Git refuses a component that leads with ``.`` or ends ``.lock``.
        ("x/.y", False),
        ("x.lock/y", False),
    ],
)
def test_valid_git_branch_name_refuses_what_a_force_push_must_not_name(
    branch: str,
    valid: bool,
) -> None:
    assert sync_import_change.valid_git_branch_name(branch) is valid


def test_run_gh_replays_an_uncaptured_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "out\n", "err\n")

    monkeypatch.setattr(sync_import_change, "_run", run)

    sync_import_change._run_gh(["gh", "pr", "list"], cwd=Path("w"))

    assert capsys.readouterr()[:2] == ("out\n", "err\n")


def test_run_gh_returns_an_unchecked_failure_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "not found\n")

    monkeypatch.setattr(sync_import_change, "_run", run)

    result = sync_import_change._run_gh(
        ["gh", "pr", "view"],
        cwd=Path("w"),
        check=False,
    )

    assert (result.returncode, len(calls)) == (1, 1)


def test_public_tree_for_import_without_a_relock_is_the_source(tmp_path: Path) -> None:
    assert (
        sync_import_change._public_tree_for_import(
            source=tmp_path / "public",
            destination=tmp_path / "out",
            refresh_public_lockfile=False,
        )
        == tmp_path / "public"
    )


def test_export_public_tree_replaces_a_stale_tree_and_seeds_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_temp = tmp_path / "runner"
    tree = runner_temp / "copybarista-validation-tree"
    (tree / "stale").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / "uv.lock").write_text("seed lock\n", encoding="utf-8")
    seeded: list[str] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[-3:-1] == ["--folder-dir", str(tree)]:
            assert not tree.exists()
            tree.mkdir(parents=True)
            (tree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        if argv[:2] == ["uv", "lock"]:
            seeded.append((tree / "uv.lock").read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sync_import_change, "_run", fake_run)

    sync_import_change._export_public_tree(
        request=replace(
            _import_request(target_dir=target),
            refresh_public_lockfile=True,
        ),
        project=target / "package",
        runner_temp=runner_temp,
        requirements=tmp_path / "requirements.txt",
    )

    assert seeded == ["seed lock\n"]
    assert not (tree / "stale").exists()


def test_an_open_import_pr_is_edited_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="a" * 40 + "\n")

    def run_gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        github_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sync_import_change, "_run", run)
    monkeypatch.setattr(sync_import_change, "_run_gh", run_gh)
    monkeypatch.setattr(sync_import_change, "_gh_pr_exists", _pr_exists)
    request = replace(_import_request(target_dir=tmp_path), auto_merge=False)

    sync_import_change._open_or_update_target_pr(request=request)

    assert [argv[:4] for argv in github_calls] == [["gh", "pr", "edit", request.branch]]


@pytest.mark.parametrize(
    ("porcelain", "changed"),
    [(" M package/a.py\n", True), ("", False)],
)
def test_git_has_changes_reads_git_status_under_the_path(
    monkeypatch: pytest.MonkeyPatch,
    porcelain: str,
    changed: bool,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, stdout=porcelain)

    monkeypatch.setattr(sync_import_change, "_run", run)

    assert (
        sync_import_change._git_has_changes(path=Path("t"), rel=Path("package"))
        is changed
    )
    assert calls == [
        (
            ["git", "status", "--porcelain", "package"],
            {"cwd": Path("t"), "check": False, "capture": True},
        ),
    ]


def test_newest_synced_public_sha_is_empty_when_no_commit_is_reflected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = FakeGit()
    git.commit(tmp_path, message="Public change", files={"f.txt": "x\n"})
    monkeypatch.setattr(sync_import_change, "_run", git.run)

    assert (
        sync_import_change._newest_synced_public_sha(
            public_dir=tmp_path,
            base_branch="main",
            sync_label="Package",
            ledger_sha="b" * 40,
        )
        == ""
    )


@pytest.mark.parametrize(
    "message",
    [
        "Publish\n\nPackage export branch: main\n",
        "Publish\n\nPackage export branch: pkg/export/..\n",
        "Publish\n",
    ],
)
def test_export_branch_from_message_refuses_a_malformed_claim(message: str) -> None:
    assert (
        sync_import_change._export_branch_from_message(
            message=message,
            sync_label="Package",
        )
        == ""
    )


def _seven() -> int:
    return 7


def _requirements_in_runner_temp(*, target_dir: Path, runner_temp: Path) -> Path:
    del target_dir
    return runner_temp / "requirements.txt"


def _import_nothing(**_: object) -> None:
    return None


def _pr_exists(**_: object) -> bool:
    return True


if __name__ == "__main__":
    from copybarista.lib.testing.runner import test_main

    test_main(__file__)
