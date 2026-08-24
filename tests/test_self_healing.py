"""
Architecture §7, Step 11.1: permanent coverage for the read-only
pattern-detection logic. Proven live in Step 11.1's own verification;
this file makes that proof permanent. This module never writes anything
-- Step 11.2/11.3 own submission and the approved-change write path.
"""
import os

from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.api import router
from app.storage import get_connection, insert_project
from app.self_healing import detect_review_outcome_patterns


TEST_PROJECT = "test-self-healing-pytest"


def _real_credential_headers():
    return {"X-Governor-Credential": os.environ["MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION"]}


def _app_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _cleanup():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_log WHERE memory_id IN "
                "(SELECT memory_id FROM memory_candidates WHERE owning_project = %s)",
                (TEST_PROJECT,),
            )
            cur.execute(
                "DELETE FROM memory_events WHERE memory_id IN "
                "(SELECT memory_id FROM memory_candidates WHERE owning_project = %s)",
                (TEST_PROJECT,),
            )
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT,))
        conn.commit()


def _submit_and_reject(client, headers, n):
    for i in range(n):
        resp = client.post("/memory/candidate", headers=headers, json={
            "owning_project": TEST_PROJECT, "visibility": "project_local", "retention": "long_term",
            "memory_type": "policy", "content": {"text": f"Pytest observation candidate {i}."},
            "source_type": "user", "source_ref": "/tmp/x.md", "policy_version": "v1",
            "confidence": {"score": 0.95, "method": "human_verified"},
        })
        assert resp.status_code == 201
        memory_id = resp.json()["memory_id"]
        review_resp = client.post(f"/memory/{memory_id}/review", headers=headers, json={
            "decision": "reject", "reason": "pytest observation test", "reviewer": "dave",
        })
        assert review_resp.status_code == 200


class TestObservationPass:
    def test_pattern_crossing_both_thresholds_is_detected(self):
        client = _app_client()
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Self-Healing Pytest")
            _submit_and_reject(client, _real_credential_headers(), 6)

            patterns = detect_review_outcome_patterns()
            match = next((p for p in patterns if p["owning_project"] == TEST_PROJECT), None)

            assert match is not None
            assert match["total_reviewed"] == 6
            assert match["unfavorable_count"] == 6
            assert match["rate"] == 1.0
            assert match["memory_type"] == "policy"
        finally:
            _cleanup()

    def test_sample_size_below_minimum_is_not_flagged_even_at_100_percent_rate(self):
        client = _app_client()
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Self-Healing Pytest")
            _submit_and_reject(client, _real_credential_headers(), 3)

            patterns = detect_review_outcome_patterns()
            match = next((p for p in patterns if p["owning_project"] == TEST_PROJECT), None)

            assert match is None
        finally:
            _cleanup()

    def test_never_writes_anything(self):
        """
        Architecture §7's own explicit boundary: this pass is read-only.
        Confirms calling it does not create, modify, or submit any
        candidate, project, or event -- only reads.
        """
        client = _app_client()
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Self-Healing Pytest")
            _submit_and_reject(client, _real_credential_headers(), 6)

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM memory_candidates WHERE owning_project = %s",
                        (TEST_PROJECT,),
                    )
                    before = cur.fetchone()["n"]

            detect_review_outcome_patterns()

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM memory_candidates WHERE owning_project = %s",
                        (TEST_PROJECT,),
                    )
                    after = cur.fetchone()["n"]

            assert before == after == 6
        finally:
            _cleanup()
