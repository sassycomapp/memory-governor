import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.registers as registers_module
from app.registers import (
    write_payload, append_or_update_register_row, update_supersession_pointer,
    register_candidate, commit_and_push_register_changes, _read_register_rows,
)
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)


def _now():
    return datetime.now(timezone.utc)


def _candidate(memory_id, **overrides):
    base = dict(
        memory_id=memory_id, owning_project="p1",
        scope=Scope(visibility="project_local", retention="long_term", owning_project="p1"),
        memory_type="fact", content=Content(text="test content"),
        provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                               writer_identity="w1", policy_version="v1"),
        timestamps=Timestamps(created_at=_now(), updated_at=_now()),
        confidence=Confidence(score=0.9, method="human_verified"),
        policy=Policy(risk_level="low", decision_path="auto_promote"),
        status="promoted",
    )
    base.update(overrides)
    return GovernedMemoryCandidate(**base)


@pytest.fixture
def sandbox_registers(tmp_path, monkeypatch):
    """
    A genuinely real, disposable git repository -- NOT the actual
    memory-governor repo -- so this test file can exercise real git commit
    and push behavior without ever touching real project history. A
    second bare repo acts as the 'remote' so push has somewhere real to
    go, and we can inspect what actually landed there afterward.

    registers.py needs no code change for this: its functions resolve
    REGISTERS_DIR/CANDIDATES_DIR/REGISTER_FILE as module-level names at
    call time, so monkeypatching them here redirects all file and git
    operations into the sandbox for the duration of each test.
    """
    work_repo = tmp_path / "work_repo"
    bare_remote = tmp_path / "bare_remote.git"

    work_repo.mkdir()
    bare_remote.mkdir()

    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)
    # Fix (Step 7.4): a fresh bare repo's HEAD defaults to refs/heads/master
    # regardless of what branch gets pushed to it later. Everything in this
    # fixture pushes to "main", so without this, any command that resolves
    # HEAD implicitly (git log, git show with no branch given) fails with
    # exit 128 even though the push itself succeeded and "main" genuinely
    # exists with the real commit on it.
    subprocess.run(
        ["git", "--git-dir", str(bare_remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True, capture_output=True,
    )
    # -b main: sets the LOCAL branch name explicitly, so it genuinely
    # matches the real repo's branch name -- git init otherwise defaults
    # to "master" on this system, which caused a real fixture bug: pushing
    # "HEAD:main" to a remote while the local branch stays "master" leaves
    # the upstream tracking ref mismatched with the local branch's own
    # name, and a later plain `git push` (exactly how the real code always
    # calls it) fails with "upstream branch does not match." Not a defect
    # in commit_and_push_register_changes() -- confirmed by the local
    # commit succeeding correctly every time this failed.
    subprocess.run(["git", "init", "-b", "main", str(work_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work_repo), "config", "user.email", "test@test.local"], check=True)
    subprocess.run(["git", "-C", str(work_repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(work_repo), "remote", "add", "origin", str(bare_remote)], check=True)

    (work_repo / "README.md").write_text("sandbox\n")
    subprocess.run(["git", "-C", str(work_repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(work_repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work_repo), "push", "-u", "origin", "main"], check=True, capture_output=True)

    sandbox_registers_dir = work_repo / "registers"
    monkeypatch.setattr(registers_module, "REGISTERS_DIR", sandbox_registers_dir)
    monkeypatch.setattr(registers_module, "CANDIDATES_DIR", sandbox_registers_dir / "candidates")
    monkeypatch.setattr(registers_module, "REGISTER_FILE", sandbox_registers_dir / "register-memory-governor.md")

    yield work_repo, bare_remote


class TestWritePayload:
    def test_writes_real_json_with_correct_content(self, sandbox_registers):
        c = _candidate("mem-t-001")
        path = write_payload(c)
        assert path.exists()
        assert "mem-t-001" in path.read_text()
        assert "test content" in path.read_text()


class TestAppendOrUpdateRegisterRow:
    def test_new_candidate_creates_a_row(self, sandbox_registers):
        c = _candidate("mem-t-002")
        append_or_update_register_row(c)
        rows = _read_register_rows()
        row = next(r for r in rows if r[0] == "mem-t-002")
        assert row[3] == "fact"
        assert row[4] == "promoted"

    def test_updating_existing_candidate_preserves_date_registered(self, sandbox_registers):
        c = _candidate("mem-t-003")
        append_or_update_register_row(c)
        rows_before = _read_register_rows()
        original_date = next(r for r in rows_before if r[0] == "mem-t-003")[5]

        c2 = _candidate("mem-t-003", status="superseded")
        append_or_update_register_row(c2)
        rows_after = _read_register_rows()
        row_after = next(r for r in rows_after if r[0] == "mem-t-003")
        assert row_after[5] == original_date
        assert row_after[4] == "superseded"

    def test_identical_call_is_idempotent(self, sandbox_registers):
        """
        Regression test for the Step 7.3 defect: calling this twice with
        an UNCHANGED candidate must not rewrite the file at all.
        """
        c = _candidate("mem-t-004")
        append_or_update_register_row(c)
        mtime_1 = registers_module.REGISTER_FILE.stat().st_mtime_ns

        append_or_update_register_row(c)
        mtime_2 = registers_module.REGISTER_FILE.stat().st_mtime_ns
        assert mtime_1 == mtime_2, "file was rewritten despite no genuine change"


class TestUpdateSupersessionPointer:
    def test_fills_in_old_row_without_deleting_it(self, sandbox_registers):
        append_or_update_register_row(_candidate("mem-t-005"))
        update_supersession_pointer("mem-t-005", "mem-t-006")
        rows = _read_register_rows()
        old_row = next(r for r in rows if r[0] == "mem-t-005")
        assert old_row[7] == "mem-t-006"
        assert len(rows) == 1


class TestRegisterCandidate:
    def test_writes_payload_and_row_together(self, sandbox_registers):
        c = _candidate("mem-t-007")
        register_candidate(c)
        assert (registers_module.CANDIDATES_DIR / "mem-t-007.json").exists()
        rows = _read_register_rows()
        assert any(r[0] == "mem-t-007" for r in rows)

    def test_supersession_updates_old_row_pointer(self, sandbox_registers):
        register_candidate(_candidate("mem-t-008"))
        register_candidate(_candidate("mem-t-009", supersedes_id="mem-t-008"))
        rows = _read_register_rows()
        old_row = next(r for r in rows if r[0] == "mem-t-008")
        assert old_row[7] == "mem-t-009"


class TestCommitAndPushRegisterChanges:
    def test_normal_path_commits_and_pushes_real_changes(self, sandbox_registers):
        """
        Step 7.4's actual proof: a real candidate, registered, produces a
        real commit that genuinely reaches the (sandbox) remote -- checked
        by inspecting the bare remote's own log directly, the same
        discipline used against the real repo throughout this project.
        """
        work_repo, bare_remote = sandbox_registers
        c = _candidate("mem-t-010")
        register_candidate(c)

        result = commit_and_push_register_changes("mem-t-010")
        assert result is True

        remote_log = subprocess.run(
            ["git", "--git-dir", str(bare_remote), "log", "--oneline", "-1"],
            capture_output=True, text=True, check=True,
        )
        assert "mem-t-010" in remote_log.stdout

    def test_no_op_when_nothing_changed_returns_false(self, sandbox_registers):
        c = _candidate("mem-t-011")
        register_candidate(c)
        commit_and_push_register_changes("mem-t-011")

        register_candidate(c)
        result = commit_and_push_register_changes("mem-t-011")
        assert result is False

    def test_scoped_to_registers_directory_only(self, sandbox_registers):
        """
        Confirms the commit never sweeps in unrelated files sitting
        elsewhere in the working tree -- the exact real defect risk
        flagged (and observed live) during Step 7.2/7.3.
        """
        work_repo, bare_remote = sandbox_registers
        (work_repo / "unrelated_file.txt").write_text("should never be committed by this")

        c = _candidate("mem-t-012")
        register_candidate(c)
        commit_and_push_register_changes("mem-t-012")

        remote_show = subprocess.run(
            ["git", "--git-dir", str(bare_remote), "show", "--stat", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        assert "unrelated_file.txt" not in remote_show.stdout
        assert "registers/" in remote_show.stdout
