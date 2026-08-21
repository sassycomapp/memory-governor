from datetime import datetime, timezone

import pytest

from app.audit import append_audit_entry, get_audit_history
from app.storage import get_connection, insert_project, insert_candidate, update_candidate, get_candidate
from app.state_machine import initialize_candidate, apply_decision
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy, MemoryDecision,
)

TEST_PROJECT_ID = "test-project-audit"


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
            cur.execute("DELETE FROM audit_log WHERE memory_id LIKE 't-audit-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()
    insert_project(TEST_PROJECT_ID, "Audit Test Project")
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE memory_id LIKE 't-audit-%'")
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()


class TestAppendAuditEntry:
    def test_append_returns_a_real_audit_id(self):
        insert_candidate(_candidate("t-audit-001"))
        decision = MemoryDecision(memory_id="t-audit-001", previous_status="proposed",
                                    new_status="promoted", reason="test", reviewer="dave")
        audit_id = append_audit_entry(decision, policy_version="v1")
        assert isinstance(audit_id, int)
        assert audit_id > 0

    def test_append_does_not_modify_memory_candidates_table(self):
        c = _candidate("t-audit-002")
        insert_candidate(c)
        decision = MemoryDecision(memory_id="t-audit-002", previous_status="proposed",
                                    new_status="promoted", reason="test", reviewer="dave")
        append_audit_entry(decision, policy_version="v1")
        fetched = get_candidate("t-audit-002")
        assert fetched.status == "proposed"

    def test_decision_by_defaults_to_governor_when_no_reviewer(self):
        insert_candidate(_candidate("t-audit-003"))
        decision = MemoryDecision(memory_id="t-audit-003", previous_status="proposed",
                                    new_status="quarantined", reason="low confidence", reviewer=None)
        append_audit_entry(decision, policy_version="v1")
        history = get_audit_history("t-audit-003")
        assert history[0]["decision_by"] == "governor"
        assert history[0]["reviewer"] is None


class TestGetAuditHistory:
    def test_empty_history_for_candidate_with_no_decisions(self):
        insert_candidate(_candidate("t-audit-004"))
        history = get_audit_history("t-audit-004")
        assert history == []

    def test_multiple_decisions_accumulate_not_overwrite(self):
        c = _candidate("t-audit-005")
        c = initialize_candidate(c)
        insert_candidate(c)

        fetched = get_candidate("t-audit-005")
        updated1, decision1 = apply_decision(fetched, "promoted", reason="first decision", reviewer="dave")
        update_candidate(updated1)
        append_audit_entry(decision1, policy_version="v1")

        fetched2 = get_candidate("t-audit-005")
        updated2, decision2 = apply_decision(fetched2, "superseded", reason="second decision", reviewer="dave")
        update_candidate(updated2)
        append_audit_entry(decision2, policy_version="v1")

        history = get_audit_history("t-audit-005")
        assert len(history) == 2

    def test_history_returned_oldest_first(self):
        c = _candidate("t-audit-006")
        c = initialize_candidate(c)
        insert_candidate(c)

        fetched = get_candidate("t-audit-006")
        updated1, decision1 = apply_decision(fetched, "under_review", reason="first", reviewer="dave")
        update_candidate(updated1)
        append_audit_entry(decision1, policy_version="v1")

        fetched2 = get_candidate("t-audit-006")
        updated2, decision2 = apply_decision(fetched2, "promoted", reason="second", reviewer="dave")
        update_candidate(updated2)
        append_audit_entry(decision2, policy_version="v1")

        history = get_audit_history("t-audit-006")
        assert history[0]["decision_reason"] == "first"
        assert history[1]["decision_reason"] == "second"

    def test_history_is_scoped_to_the_correct_memory_id_only(self):
        insert_candidate(_candidate("t-audit-007"))
        insert_candidate(_candidate("t-audit-008"))

        decision_a = MemoryDecision(memory_id="t-audit-007", previous_status="proposed",
                                      new_status="promoted", reason="for 007", reviewer="dave")
        decision_b = MemoryDecision(memory_id="t-audit-008", previous_status="proposed",
                                      new_status="rejected", reason="for 008", reviewer="dave")
        append_audit_entry(decision_a, policy_version="v1")
        append_audit_entry(decision_b, policy_version="v1")

        history_007 = get_audit_history("t-audit-007")
        history_008 = get_audit_history("t-audit-008")
        assert len(history_007) == 1
        assert len(history_008) == 1
        assert history_007[0]["decision_reason"] == "for 007"
        assert history_008[0]["decision_reason"] == "for 008"

    def test_current_candidate_jsonb_audit_shows_only_most_recent(self):
        c = _candidate("t-audit-009")
        c = initialize_candidate(c)
        insert_candidate(c)

        fetched = get_candidate("t-audit-009")
        updated1, decision1 = apply_decision(fetched, "promoted", reason="original reason", reviewer="dave")
        update_candidate(updated1)
        append_audit_entry(decision1, policy_version="v1")

        fetched2 = get_candidate("t-audit-009")
        updated2, decision2 = apply_decision(fetched2, "superseded", reason="latest reason", reviewer="dave")
        update_candidate(updated2)
        append_audit_entry(decision2, policy_version="v1")

        final = get_candidate("t-audit-009")
        assert final.audit.decision_reason == "latest reason"

        history = get_audit_history("t-audit-009")
        reasons = [row["decision_reason"] for row in history]
        assert reasons == ["original reason", "latest reason"]
