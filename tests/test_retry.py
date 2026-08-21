import time
from datetime import datetime, timezone

import pytest

from app.retry import with_retry, compute_backoff_seconds, MAX_ATTEMPTS, BASE_BACKOFF_SECONDS
from app.worker import HANDLER_REGISTRY, process_event, run_polling_pass
from app.outbox import insert_candidate_and_emit_event
from app.storage import get_connection, insert_project
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)

TEST_PROJECT_ID = "test-project-retry"


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
            cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 't-retry-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()
    insert_project(TEST_PROJECT_ID, "Retry Test Project")
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 't-retry-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()


class TestComputeBackoffSeconds:
    def test_exact_documented_sequence(self):
        assert [compute_backoff_seconds(n) for n in range(1, MAX_ATTEMPTS + 1)] == [2, 4, 8, 16, 32]


class TestWithRetrySuccess:
    def test_successful_handler_returns_true_and_does_not_touch_retry_columns(self):
        c = _candidate("t-retry-001")
        event_id = insert_candidate_and_emit_event(c)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_events WHERE event_id = %s", (event_id,))
                event = cur.fetchone()

        succeeded = with_retry(event, lambda e: None)  # handler that succeeds
        assert succeeded is True

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT attempt_count, last_error, permanently_failed FROM memory_events WHERE event_id = %s",
                    (event_id,),
                )
                row = cur.fetchone()
        assert row["attempt_count"] == 0  # unchanged -- with_retry only touches these on FAILURE
        assert row["last_error"] is None
        assert row["permanently_failed"] is False


class TestWithRetryFailure:
    def test_single_failure_increments_attempt_count_and_schedules_backoff(self):
        c = _candidate("t-retry-002")
        event_id = insert_candidate_and_emit_event(c)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_events WHERE event_id = %s", (event_id,))
                event = cur.fetchone()

        def always_fails(e):
            raise RuntimeError("test failure")

        before = time.monotonic()
        succeeded = with_retry(event, always_fails)
        assert succeeded is False

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT attempt_count, last_error, permanently_failed, next_retry_after FROM memory_events WHERE event_id = %s",
                    (event_id,),
                )
                row = cur.fetchone()
        assert row["attempt_count"] == 1
        assert "RuntimeError: test failure" in row["last_error"]
        assert row["permanently_failed"] is False
        assert row["next_retry_after"] is not None

    def test_never_raises_back_to_caller(self):
        """
        The core guarantee this module exists for: a handler exception
        must never propagate out of with_retry(), so a batch-processing
        caller (run_polling_pass) can always continue.
        """
        c = _candidate("t-retry-003")
        event_id = insert_candidate_and_emit_event(c)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_events WHERE event_id = %s", (event_id,))
                event = cur.fetchone()

        def always_fails(e):
            raise RuntimeError("should not escape with_retry")

        try:
            with_retry(event, always_fails)  # must not raise
        except Exception:
            pytest.fail("with_retry() raised an exception -- it must always catch and never propagate")

    def test_permanently_failed_after_max_attempts_and_next_retry_after_cleared(self):
        """
        Automates the live liveness check's core proof, including the
        fix applied this step: once permanently_failed is set,
        next_retry_after is explicitly NULL, not a stale leftover value.
        """
        c = _candidate("t-retry-004")
        event_id = insert_candidate_and_emit_event(c)

        def always_fails(e):
            raise RuntimeError("permanent failure test")

        for _ in range(MAX_ATTEMPTS):
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM memory_events WHERE event_id = %s", (event_id,))
                    event = cur.fetchone()
            with_retry(event, always_fails)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT attempt_count, permanently_failed, next_retry_after, processed_at FROM memory_events WHERE event_id = %s",
                    (event_id,),
                )
                final = cur.fetchone()
        assert final["attempt_count"] == MAX_ATTEMPTS
        assert final["permanently_failed"] is True
        assert final["next_retry_after"] is None  # the fix applied this step
        assert final["processed_at"] is None


class TestPollingPassEligibilityFilter:
    def test_freshly_failed_event_not_immediately_repolled(self):
        c = _candidate("t-retry-005")
        original_handler = HANDLER_REGISTRY["candidate_created"]

        def always_fails(e):
            raise RuntimeError("eligibility filter test")

        HANDLER_REGISTRY["candidate_created"] = always_fails
        try:
            event_id = insert_candidate_and_emit_event(c)
            first_pass = run_polling_pass()
            assert first_pass == 1  # attempted once, failed, scheduled backoff

            second_pass = run_polling_pass()
            assert second_pass == 0  # NOT eligible yet -- backoff hasn't elapsed
        finally:
            HANDLER_REGISTRY["candidate_created"] = original_handler

    def test_permanently_failed_event_never_repolled_even_after_time_passes(self):
        c = _candidate("t-retry-006")
        original_handler = HANDLER_REGISTRY["candidate_created"]

        def always_fails(e):
            raise RuntimeError("permanent failure eligibility test")

        HANDLER_REGISTRY["candidate_created"] = always_fails
        try:
            event_id = insert_candidate_and_emit_event(c)
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Manually exhaust attempts without waiting for real backoff time
                    for _ in range(MAX_ATTEMPTS):
                        cur.execute("SELECT * FROM memory_events WHERE event_id = %s", (event_id,))
                        event = cur.fetchone()
                        with_retry(event, always_fails)

            # Manually clear next_retry_after's time-gate by checking straight away --
            # permanently_failed alone must exclude it regardless of timing
            count = run_polling_pass()
            assert count == 0
        finally:
            HANDLER_REGISTRY["candidate_created"] = original_handler
