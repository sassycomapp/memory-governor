from datetime import datetime, timezone

import psycopg
import pytest

from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)
from app.storage import (
    get_connection, insert_project, insert_candidate, update_candidate,
    get_candidate, candidate_exists,
)

TEST_PROJECT_ID = "test-project-storage"


def _now():
    return datetime.now(timezone.utc)


def _candidate(memory_id: str, **overrides) -> GovernedMemoryCandidate:
    base = dict(
        memory_id=memory_id,
        owning_project=TEST_PROJECT_ID,
        scope=Scope(visibility="project_local", retention="long_term",
                     owning_project=TEST_PROJECT_ID),
        memory_type="fact",
        content=Content(text="test content"),
        provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                               writer_identity="w1", policy_version="v1"),
        timestamps=Timestamps(created_at=_now(), updated_at=_now()),
        confidence=Confidence(score=0.9, method="human_verified"),
        policy=Policy(risk_level="low", decision_path="auto_promote"),
        status="proposed",
    )
    base.update(overrides)
    return GovernedMemoryCandidate(**base)


@pytest.fixture(autouse=True)
def isolated_test_project():
    """
    Runs before AND after every test in this file. Ensures TEST_PROJECT_ID
    exists before the test, and wipes both its candidates and the project
    row itself afterward -- so this suite never leaks state between runs
    and never touches mb-3-cs-code or any other real project.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()

    insert_project(TEST_PROJECT_ID, "Storage Test Project")

    yield

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()


class TestInsertAndGet:
    def test_insert_then_get_round_trips_all_scalar_fields(self):
        c = _candidate("t-storage-001")
        insert_candidate(c)
        fetched = get_candidate("t-storage-001")
        assert fetched.memory_id == c.memory_id
        assert fetched.owning_project == c.owning_project
        assert fetched.memory_type == c.memory_type
        assert fetched.status == c.status

    def test_insert_then_get_round_trips_nested_json_fields(self):
        c = _candidate("t-storage-002",
                        content=Content(text="specific text", structured={"k": "v"}))
        insert_candidate(c)
        fetched = get_candidate("t-storage-002")
        assert fetched.content.text == "specific text"
        assert fetched.content.structured == {"k": "v"}
        assert fetched.scope.visibility == "project_local"
        assert fetched.confidence.score == 0.9

    def test_get_nonexistent_returns_none(self):
        assert get_candidate("t-storage-does-not-exist") is None

    def test_insert_duplicate_memory_id_raises(self):
        c = _candidate("t-storage-003")
        insert_candidate(c)
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_candidate(c)

    def test_insert_with_missing_project_fk_raises(self):
        c = _candidate("t-storage-004", owning_project="project-does-not-exist")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            insert_candidate(c)

    def test_null_optional_fields_stay_true_null_not_json_null(self):
        c = _candidate("t-storage-005")  # subject, contradiction, review, audit all None
        insert_candidate(c)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT subject, contradiction, review, audit FROM memory_candidates WHERE memory_id = %s",
                    ("t-storage-005",),
                )
                row = cur.fetchone()
        assert row["subject"] is None
        assert row["contradiction"] is None
        assert row["review"] is None
        assert row["audit"] is None


class TestCandidateExists:
    def test_exists_true_after_insert(self):
        insert_candidate(_candidate("t-storage-006"))
        assert candidate_exists("t-storage-006") is True

    def test_exists_false_before_insert(self):
        assert candidate_exists("t-storage-007-never-inserted") is False


class TestUpdateCandidate:
    def test_update_existing_candidate_persists_change(self):
        c = _candidate("t-storage-008", status="proposed")
        insert_candidate(c)
        c.status = "promoted"  # direct field set is fine here -- state_machine
                                 # enforcement is a separate concern from storage's job
        update_candidate(c)
        fetched = get_candidate("t-storage-008")
        assert fetched.status == "promoted"

    def test_update_nonexistent_candidate_raises_value_error(self):
        c = _candidate("t-storage-009-never-inserted")
        with pytest.raises(ValueError):
            update_candidate(c)

    def test_update_does_not_create_a_row(self):
        c = _candidate("t-storage-010-never-inserted")
        try:
            update_candidate(c)
        except ValueError:
            pass
        assert candidate_exists("t-storage-010-never-inserted") is False


class TestProjectFixtureIsolation:
    def test_mb_3_cs_code_untouched_by_this_suite(self):
        """
        Confirms this test file's fixture teardown scope is exactly
        TEST_PROJECT_ID and never reaches the real mb-3-cs-code project.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM projects WHERE project_id = %s", ("mb-3-cs-code",))
                assert cur.fetchone() is not None, "mb-3-cs-code was unexpectedly removed"


class TestStateMachineStorageIntegration:
    """
    Step 2.3's real proof point: a candidate that has gone through
    state_machine.py's validated transitions round-trips through real
    Postgres with zero corruption -- status, audit, and decision fields
    all survive serialize -> insert -> get -> deserialize exactly.

    This does NOT replace test_state_machine.py's own suite (which
    correctly stays storage-independent per Architecture §9) -- it proves
    the two modules compose correctly together, which neither file alone
    can prove on its own.
    """

    def test_initialized_and_promoted_candidate_round_trips_correctly(self):
        from app.state_machine import initialize_candidate, apply_decision

        c = _candidate("t-storage-sm-001", status="promoted")  # deliberately WRONG on construction
        c = initialize_candidate(c)  # must force back to "proposed"
        assert c.status == "proposed"

        insert_candidate(c)

        fetched_before_decision = get_candidate("t-storage-sm-001")
        assert fetched_before_decision.status == "proposed"

        updated, decision = apply_decision(fetched_before_decision, "promoted",
                                            reason="test promotion", reviewer="dave")
        assert updated.status == "promoted"
        update_candidate(updated)

        fetched_after = get_candidate("t-storage-sm-001")
        assert fetched_after.status == "promoted"
        assert fetched_after.audit is not None
        assert fetched_after.audit.decision_by == "dave"
        assert fetched_after.audit.decision_reason == "test promotion"
        assert fetched_after.audit.reviewer == "dave"

    def test_invalid_transition_is_rejected_before_any_storage_write(self):
        from app.state_machine import apply_decision

        c = _candidate("t-storage-sm-002", status="rejected")  # terminal state
        insert_candidate(c)

        with pytest.raises(ValueError):
            apply_decision(c, "promoted")  # rejected -> promoted is not allowed

        # Confirm the rejected attempt never touched the database --
        # storage still shows the original "rejected" status, unchanged.
        fetched = get_candidate("t-storage-sm-002")
        assert fetched.status == "rejected"

    def test_resubmitting_existing_memory_id_via_insert_still_blocked_at_storage_level(self):
        """
        This is the storage-layer half of Architecture §10 test #1, which
        Step 1.2 explicitly could not prove on its own (it required
        storage to exist). Proving it now: the memory_id PRIMARY KEY
        constraint means insert_candidate() can never silently overwrite
        an existing promoted candidate -- the only path to change an
        existing candidate is update_candidate(), after apply_decision().
        """
        c = _candidate("t-storage-sm-003", status="proposed")
        insert_candidate(c)

        forged = _candidate("t-storage-sm-003", status="promoted")  # attacker-style resubmission
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_candidate(forged)

        # Confirm the original, legitimate row was NOT overwritten
        fetched = get_candidate("t-storage-sm-003")
        assert fetched.status == "proposed"
