import threading
import time
from datetime import datetime, timezone

import pytest

from app.worker import process_event, run_polling_pass, listen_loop, HANDLER_REGISTRY
from app.outbox import insert_candidate_and_emit_event, VALID_EVENT_TYPES
from app.storage import get_connection, insert_project, insert_candidate
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)

TEST_PROJECT_ID = "test-project-worker"


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
            cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 't-worker-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()
    insert_project(TEST_PROJECT_ID, "Worker Test Project")
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 't-worker-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()


class TestHandlerRegistry:
    def test_every_valid_event_type_has_a_handler(self):
        assert set(HANDLER_REGISTRY.keys()) == VALID_EVENT_TYPES


class TestProcessEvent:
    def test_process_event_marks_event_as_processed(self):
        c = _candidate("t-worker-001")
        event_id = insert_candidate_and_emit_event(c)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_events WHERE event_id = %s", (event_id,))
                event = cur.fetchone()
        assert event["processed_at"] is None  # not yet processed

        process_event(event)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT processed_at, processed_by FROM memory_events WHERE event_id = %s", (event_id,))
                row = cur.fetchone()
        assert row["processed_at"] is not None
        assert row["processed_by"] == "worker"

    def test_unknown_event_type_raises(self):
        fake_event = {"event_id": 999999, "event_type": "not_a_real_type", "memory_id": None}
        with pytest.raises(ValueError):
            process_event(fake_event)


class TestRunPollingPass:
    def test_polling_pass_processes_all_pending_events(self):
        c1 = _candidate("t-worker-002")
        c2 = _candidate("t-worker-003")
        insert_candidate_and_emit_event(c1)
        insert_candidate_and_emit_event(c2)

        processed_count = run_polling_pass()
        assert processed_count == 2

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS c FROM memory_events WHERE memory_id IN ('t-worker-002', 't-worker-003') AND processed_at IS NULL"
                )
                unprocessed = cur.fetchone()["c"]
        assert unprocessed == 0

    def test_polling_pass_does_not_reprocess_already_processed_events(self):
        c = _candidate("t-worker-004")
        insert_candidate_and_emit_event(c)

        first_pass_count = run_polling_pass()
        assert first_pass_count == 1

        second_pass_count = run_polling_pass()
        assert second_pass_count == 0  # nothing left to process

    def test_polling_pass_processes_oldest_first(self):
        c1 = _candidate("t-worker-005")
        insert_candidate_and_emit_event(c1)
        c2 = _candidate("t-worker-006")
        insert_candidate_and_emit_event(c2)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id, memory_id FROM memory_events WHERE memory_id IN ('t-worker-005', 't-worker-006') ORDER BY created_at ASC"
                )
                expected_order = [row["memory_id"] for row in cur.fetchall()]

        run_polling_pass()

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT memory_id, processed_at FROM memory_events WHERE memory_id IN ('t-worker-005', 't-worker-006') ORDER BY processed_at ASC"
                )
                actual_order = [row["memory_id"] for row in cur.fetchall()]
        assert actual_order == expected_order


class TestListenLoopBoundedRun:
    def test_listen_loop_stops_after_specified_seconds(self):
        start = time.monotonic()
        listen_loop(stop_after_seconds=1, poll_interval_seconds=0.5)
        elapsed = time.monotonic() - start
        assert elapsed < 3, f"Loop ran for {elapsed:.2f}s, expected to stop near 1s"

    def test_listen_loop_stops_after_specified_iterations(self):
        iterations = listen_loop(stop_after_iterations=2, poll_interval_seconds=0.2)
        assert iterations == 2

    def test_listen_loop_processes_event_emitted_during_its_run(self):
        """
        The permanent regression test for this step's core proof: an event
        emitted mid-run, on a long poll_interval, gets processed almost
        immediately -- proving NOTIFY-driven wake, not reliance on the
        timeout. Mirrors the live verification run exactly, as a
        repeatable automated test.
        """
        c = _candidate("t-worker-007")

        result = {}
        def run_worker():
            result["iterations"] = listen_loop(stop_after_seconds=3, poll_interval_seconds=30)

        worker_thread = threading.Thread(target=run_worker)
        worker_thread.start()
        time.sleep(0.5)  # let the LISTEN establish before emitting

        emit_time = time.monotonic()
        event_id = insert_candidate_and_emit_event(c)

        processed_time = None
        for _ in range(50):
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT processed_at FROM memory_events WHERE event_id = %s", (event_id,))
                    row = cur.fetchone()
            if row["processed_at"] is not None:
                processed_time = time.monotonic()
                break
            time.sleep(0.1)

        worker_thread.join()

        assert processed_time is not None, "Event was never processed"
        elapsed = processed_time - emit_time
        assert elapsed < 10, f"Took {elapsed:.2f}s against a 30s poll_interval -- NOTIFY did not fire"
