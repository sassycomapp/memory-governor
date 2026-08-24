"""
Scope Doc §13A, Workflow Step 13.2: permanent coverage for the governed
PDLF step-completion write path. Proven live using real Step 16 artifact
shapes (CONTEXT.md glossary terms, ADRs) drawn from the actual PDLF
step files; this file makes that proof permanent.

Deliberately does NOT test GBrain querying, reading, or confidence/
contradiction assessment -- that is the submitting OpenCode agent's own
job (Scope Doc §11A), out of scope for this module and this repository,
the same boundary mb-submit-memory (Phase 14) already sits behind.
"""
from app.storage import get_connection, insert_project, get_candidate
from app.pdlf_writer import submit_pdlf_artifact
from app.models import Contradiction


TEST_PROJECT = "test-13-2-pytest"


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


class TestSubmitPdlfArtifact:
    def test_writer_identity_is_pdlf_step_sync(self):
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Step 13.2 Pytest")
            result = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="Glossary term: test definition.",
                memory_type="fact",
                source_ref="CONTEXT.md",
                confidence_score=0.95,
                confidence_method="human_verified",
            )
            candidate = get_candidate(result["memory_id"])
            assert candidate.provenance.writer_identity == "pdlf_step_sync"
        finally:
            _cleanup()

    def test_retention_is_permanent_and_source_type_is_agent_trace(self):
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Step 13.2 Pytest")
            result = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="Glossary term: test definition.",
                memory_type="fact",
                source_ref="CONTEXT.md",
                confidence_score=0.95,
                confidence_method="human_verified",
            )
            candidate = get_candidate(result["memory_id"])
            assert candidate.scope.retention == "permanent"
            assert candidate.provenance.source_type == "agent_trace"
        finally:
            _cleanup()

    def test_two_step16_style_artifacts_are_distinct_and_independently_decided(self):
        """
        Step 13.2's actual exit criteria, using Step 16's real, concrete
        shape: CONTEXT.md plus an ADR, submitted separately -- never
        batched into one candidate.
        """
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Step 13.2 Pytest")

            result_context = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="Glossary term: 'domain-locked' gate definition.",
                memory_type="fact",
                source_ref="CONTEXT.md",
                confidence_score=0.95,
                confidence_method="human_verified",
            )
            result_adr = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="ADR: client-instance-architecture decision and reasoning.",
                memory_type="fact",
                source_ref="adr-local/0001-client-instance-architecture.md",
                confidence_score=0.9,
                confidence_method="human_verified",
            )

            assert result_context["memory_id"] != result_adr["memory_id"]
            assert result_context["event_id"] != result_adr["event_id"]

            c1 = get_candidate(result_context["memory_id"])
            c2 = get_candidate(result_adr["memory_id"])
            assert c1.status == "promoted"
            assert c2.status == "promoted"
        finally:
            _cleanup()

    def test_contradiction_is_passed_through_not_computed(self):
        """
        Confirms this module genuinely never invents a contradiction
        value -- an unresolved, cross-project contradiction supplied by
        the (simulated) calling agent correctly blocks at needs_review,
        proving the value passed straight through decide_path()
        unmodified rather than being silently dropped or defaulted.
        """
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Step 13.2 Pytest")
            result = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="A glossary term that conflicts with an existing one.",
                memory_type="fact",
                source_ref="CONTEXT.md",
                confidence_score=0.95,
                confidence_method="human_verified",
                contradiction=Contradiction(detected=True, cross_project=True, resolution=None),
            )
            assert result["status"] == "under_review"
            assert result["decision_path"] == "needs_review"
        finally:
            _cleanup()

    def test_no_contradiction_supplied_auto_promotes_normally(self):
        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Step 13.2 Pytest")
            result = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="A clean, uncontested glossary term.",
                memory_type="fact",
                source_ref="CONTEXT.md",
                confidence_score=0.95,
                confidence_method="human_verified",
            )
            assert result["status"] == "promoted"
        finally:
            _cleanup()


class TestStepContinuationPrimitive:
    """
    Workflow Step 13.3: no PDLF step begins while any review item is open
    anywhere (design decision made during this project's own planning
    conversation, not this file's own invention). Confirms
    GET /memory/review-queue -- already built and proven in Phase 10 --
    is a genuinely sufficient, already-correct primitive for this exact
    purpose, specifically for pdlf_step_sync submissions. No new
    governor-side code was needed for this step; this test makes that
    finding permanent rather than a one-off live check.
    """

    def test_review_queue_correctly_reflects_pdlf_step_sync_before_during_and_after(self):
        import os
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        from app.models import Contradiction

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        headers = {"X-Governor-Credential": os.environ["MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION"]}

        _cleanup()
        try:
            insert_project(TEST_PROJECT, "Step 13.3 Pytest")

            before = client.get("/memory/review-queue", headers=headers).json()
            assert [c for c in before if c["owning_project"] == TEST_PROJECT] == []

            result = submit_pdlf_artifact(
                owning_project=TEST_PROJECT,
                content_text="A PDLF artifact requiring developer review.",
                memory_type="fact",
                source_ref="CONTEXT.md",
                confidence_score=0.9,
                confidence_method="human_verified",
                contradiction=Contradiction(detected=True, cross_project=True, resolution=None),
            )
            assert result["status"] == "under_review"
            memory_id = result["memory_id"]

            during = client.get("/memory/review-queue", headers=headers).json()
            matching = [c for c in during if c["owning_project"] == TEST_PROJECT]
            assert len(matching) == 1
            assert matching[0]["memory_id"] == memory_id

            resolve_resp = client.post(
                f"/memory/{memory_id}/review", headers=headers,
                json={"decision": "promote", "reason": "test", "reviewer": "dave"},
            )
            assert resolve_resp.status_code == 200

            after = client.get("/memory/review-queue", headers=headers).json()
            assert [c for c in after if c["owning_project"] == TEST_PROJECT] == []
        finally:
            _cleanup()
