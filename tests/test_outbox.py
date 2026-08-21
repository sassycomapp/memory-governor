from datetime import datetime, timezone

import psycopg
import pytest

from app.outbox import (
    emit_event, insert_candidate_and_emit_event, update_candidate_and_emit_event,
    VALID_EVENT_TYPES, NOTIFY_CHANNEL, _validate_event_type,
)
from app.storage import get_connection, insert_project, insert_candidate, get_candidate
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)

TEST_PROJECT_ID = "test-project-outbox"


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
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 't-outbox-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()
    insert_project(TEST_PROJECT_ID, "Outbox Test Project")
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 't-outbox-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()


class TestValidateEventType:
    @pytest.mark.parametrize("event_type", sorted(VALID_EVENT_TYPES))
    def test_every_documented_event_type_is_accepted(self, event_type):
        _validate_event_type(event_type)  # must not raise

    def test_unknown_event_type_rejected(self):
        with pytest.raises(ValueError):
            _validate_event_type("not_a_real_event_type")


class TestInsertCandidateAndEmitEvent:
    def test_both_candidate_and_event_persist_together(self):
        c = _candidate("t-outbox-001")
        event_id = insert_candidate_and_emit_event(c)

        fetched = get_candidate("t-outbox-001")
        assert fetched is not None

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT event_type, memory_id FROM memory_events WHERE event_id = %s", (event_id,))
                row = cur.fetchone()
        assert row["event_type"] == "candidate_created"
        assert row["memory_id"] == "t-outbox-001"

    def test_failed_insert_leaves_no_candidate_and_no_event(self):
        """
        The core atomicity proof (Step 4.1 design note): a candidate whose
        owning_project does not exist fails the FK constraint on the
        candidate INSERT -- and because both writes share one transaction,
        NEITHER the candidate NOR any event row survives.
        """
        bad = _candidate("t-outbox-002-should-not-exist", owning_project="nonexistent-project")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            insert_candidate_and_emit_event(bad)

        assert get_candidate("t-outbox-002-should-not-exist") is None
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS c FROM memory_events WHERE memory_id = %s",
                    ("t-outbox-002-should-not-exist",),
                )
                count = cur.fetchone()["c"]
        assert count == 0

    def test_duplicate_memory_id_leaves_no_event_either(self):
        """
        Mirrors storage.py's own UniqueViolation guarantee (Architecture
        §10 test #1's storage half) -- but confirms it ALSO holds for the
        outbox path specifically, since this is a separate code path from
        storage.insert_candidate() and could theoretically have drifted.
        """
        c = _candidate("t-outbox-003")
        insert_candidate_and_emit_event(c)  # first insert succeeds

        duplicate = _candidate("t-outbox-003", status="promoted")
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_candidate_and_emit_event(duplicate)

        # Original candidate unchanged; only ONE event exists (from the first, successful call)
        fetched = get_candidate("t-outbox-003")
        assert fetched.status == "proposed"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS c FROM memory_events WHERE memory_id = %s",
                    ("t-outbox-003",),
                )
                count = cur.fetchone()["c"]
        assert count == 1

    def test_custom_payload_is_stored_correctly(self):
        c = _candidate("t-outbox-004")
        event_id = insert_candidate_and_emit_event(c, payload={"custom": "value", "n": 42})
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM memory_events WHERE event_id = %s", (event_id,))
                row = cur.fetchone()
        assert row["payload"] == {"custom": "value", "n": 42}


class TestUpdateCandidateAndEmitEvent:
    def test_update_and_event_persist_together(self):
        c = _candidate("t-outbox-005")
        insert_candidate(c)  # plain insert, no event -- simulating a pre-existing row

        c.status = "promoted"
        event_id = update_candidate_and_emit_event(c, "review_requested")

        fetched = get_candidate("t-outbox-005")
        assert fetched.status == "promoted"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT event_type FROM memory_events WHERE event_id = %s", (event_id,))
                row = cur.fetchone()
        assert row["event_type"] == "review_requested"

    def test_update_nonexistent_candidate_raises_and_emits_no_event(self):
        c = _candidate("t-outbox-006-never-inserted")
        with pytest.raises(ValueError):
            update_candidate_and_emit_event(c, "review_requested")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS c FROM memory_events WHERE memory_id = %s",
                    ("t-outbox-006-never-inserted",),
                )
                count = cur.fetchone()["c"]
        assert count == 0

    def test_invalid_event_type_rejected_before_any_write(self):
        c = _candidate("t-outbox-007")
        insert_candidate(c)
        with pytest.raises(ValueError):
            update_candidate_and_emit_event(c, "not_a_real_event_type")
        # Candidate row untouched by the rejected call
        fetched = get_candidate("t-outbox-007")
        assert fetched.status == "proposed"


class TestEmitEventNotify:
    def test_notify_channel_constant_is_correct(self):
        assert NOTIFY_CHANNEL == "memory_governor_events"

    def test_low_level_emit_event_requires_open_cursor_shares_caller_transaction(self):
        """
        Confirms emit_event() itself does not open a connection or commit --
        calling it directly inside a transaction the CALLER controls, then
        rolling back, must leave no trace, proving it truly composes into
        an existing transaction rather than being secretly autonomous.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                emit_event(cur, "review_requested", None, {"test": "rollback-should-erase-this"})
            conn.rollback()  # deliberately do NOT commit

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS c FROM memory_events WHERE payload->>'test' = %s",
                    ("rollback-should-erase-this",),
                )
                count = cur.fetchone()["c"]
        assert count == 0
