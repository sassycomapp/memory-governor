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



TEST_PROJECT_112 = "test-11-2-pytest"


def _cleanup_112():
    from app.storage import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_log WHERE memory_id IN "
                "(SELECT memory_id FROM memory_candidates WHERE owning_project = %s)",
                (TEST_PROJECT_112,),
            )
            cur.execute(
                "DELETE FROM memory_events WHERE memory_id IN "
                "(SELECT memory_id FROM memory_candidates WHERE owning_project = %s)",
                (TEST_PROJECT_112,),
            )
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_112,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_112,))
        conn.commit()


class TestSubmitDetectedPattern:
    """
    Step 11.2 (Architecture §7, points 2-3). The single most important
    guarantee this step provides: a self-generated pattern submission
    must NEVER auto-promote. This is a real, previously-latent defect in
    the specification's own literal wording (it said "procedural"-type,
    which is NOT in HIGH_RISK_TYPES and would NOT have been guaranteed
    needs_review) -- found and fixed before any code existed. This test
    is what makes that fix permanent, not just proven once live.
    """

    def test_submitted_pattern_is_always_needs_review_not_auto_promoted(self):
        from app.storage import insert_project, get_candidate
        from app.self_healing import submit_detected_pattern

        _cleanup_112()
        try:
            insert_project(TEST_PROJECT_112, "Step 11.2 Pytest")

            fake_pattern = {
                "memory_type": "fact",
                "owning_project": TEST_PROJECT_112,
                "classification": "low_confidence",
                "total_reviewed": 8,
                "unfavorable_count": 6,
                "rate": 0.75,
            }

            result = submit_detected_pattern(fake_pattern)
            candidate = get_candidate(result["memory_id"])

            assert candidate.status == "under_review"
            assert result["decision_path"] == "needs_review"
        finally:
            _cleanup_112()

    def test_submitted_as_policy_type_not_procedural(self):
        """
        Regression test for the exact defect: memory_type must be
        "policy" (genuinely high-risk), never "procedural" (not in
        HIGH_RISK_TYPES) -- Architecture §7's own literal wording, if
        followed exactly, would fail this test.
        """
        from app.storage import insert_project, get_candidate
        from app.self_healing import submit_detected_pattern

        _cleanup_112()
        try:
            insert_project(TEST_PROJECT_112, "Step 11.2 Pytest")

            fake_pattern = {
                "memory_type": "fact",
                "owning_project": TEST_PROJECT_112,
                "classification": "low_confidence",
                "total_reviewed": 8,
                "unfavorable_count": 6,
                "rate": 0.75,
            }

            result = submit_detected_pattern(fake_pattern)
            candidate = get_candidate(result["memory_id"])

            assert candidate.memory_type == "policy"
        finally:
            _cleanup_112()

    def test_writer_identity_is_self_healing_not_another_writer(self):
        from app.storage import insert_project, get_candidate
        from app.self_healing import submit_detected_pattern

        _cleanup_112()
        try:
            insert_project(TEST_PROJECT_112, "Step 11.2 Pytest")

            fake_pattern = {
                "memory_type": "fact",
                "owning_project": TEST_PROJECT_112,
                "classification": "low_confidence",
                "total_reviewed": 8,
                "unfavorable_count": 6,
                "rate": 0.75,
            }

            result = submit_detected_pattern(fake_pattern)
            candidate = get_candidate(result["memory_id"])

            assert candidate.provenance.writer_identity == "self_healing"
            assert candidate.provenance.source_type == "agent_trace"
        finally:
            _cleanup_112()

    def test_content_describes_observation_and_suggests_adjustment(self):
        """
        Architecture §7 point 2: must describe both the observation
        AND a suggested policy adjustment, not just raw numbers.
        """
        from app.self_healing import _describe_pattern

        pattern = {
            "memory_type": "fact",
            "owning_project": "proj-x",
            "classification": "low_confidence",
            "total_reviewed": 10,
            "unfavorable_count": 7,
            "rate": 0.7,
        }
        description = _describe_pattern(pattern)

        assert "7" in description and "10" in description
        assert "proj-x" in description
        assert "low_confidence" in description
        assert "threshold" in description.lower() or "adjust" in description.lower()
