"""
Architecture §10, test #10: reconstructing a candidate from
registers/candidates/<memory_id>.json alone (no Postgres) reproduces every
field of the original.

Proven live during Step 7.5's drill against the real, running system.
This file makes that proof permanent -- a future change to models.py or
registers.py that silently breaks JSON round-tripping should fail here,
not be discovered for the first time during a real disaster, which is
the worst possible moment to learn the recovery path doesn't work.

Register/payload paths are sandboxed via monkeypatch (Step 8.1 fix --
see commit history for the real incident this closes: the original
version of this file wrote real rows into the real, committed
registers/register-memory-governor.md on every test run, with no way
to clean them up since registers.py deliberately has no delete-a-row
function -- discovered when `git status` kept coming back dirty after
clean test runs). The Postgres connection stays completely real --
only the register/payload FILE paths are redirected, matching
test_registers.py's already-proven pattern.
"""
import json
from datetime import datetime, timezone

import pytest

import app.registers as registers_module
from app.storage import get_connection, insert_project, insert_candidate, get_candidate
from app.registers import register_candidate
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy, Subject, Contradiction,
)


def _now():
    return datetime.now(timezone.utc)


TEST_PROJECT = "test-project-disaster-recovery-pytest"


@pytest.fixture
def dr_project(tmp_path, monkeypatch):
    sandbox_dir = tmp_path / "registers"
    monkeypatch.setattr(registers_module, "REGISTERS_DIR", sandbox_dir)
    monkeypatch.setattr(registers_module, "CANDIDATES_DIR", sandbox_dir / "candidates")
    monkeypatch.setattr(registers_module, "REGISTER_FILE", sandbox_dir / "register-memory-governor.md")

    def cleanup():
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE memory_id LIKE 'mem-drt-%'")
                cur.execute("DELETE FROM memory_events WHERE memory_id LIKE 'mem-drt-%'")
                cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT,))
                cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT,))
            conn.commit()

    cleanup()
    insert_project(TEST_PROJECT, "Disaster Recovery Pytest Fixture")
    yield
    cleanup()


def _full_candidate(memory_id: str) -> GovernedMemoryCandidate:
    """
    A candidate with real, non-default values in every optional field --
    a reconstruction test that only exercises required fields would not
    actually prove the payload captures everything the register's own
    nine columns cannot (Architecture §8.2's original motivation).
    """
    return GovernedMemoryCandidate(
        memory_id=memory_id, owning_project=TEST_PROJECT,
        agent_id="agent-42", session_id="session-99",
        scope=Scope(visibility="shared_specific", retention="permanent",
                     owning_project=TEST_PROJECT, shared_with=["other-project"],
                     authority="human_reviewed"),
        memory_type="relationship",
        subject=Subject(entity="vault_service", predicate="encrypts_via", object="encryption_service"),
        content=Content(text="Disaster recovery pytest content.", structured={"key": "value", "n": 42}),
        provenance=Provenance(source_type="user", source_ref="/tmp/dr-test.md",
                               writer_identity="w1", policy_version="v1",
                               lineage_hash="abc123"),
        timestamps=Timestamps(created_at=_now(), updated_at=_now()),
        confidence=Confidence(score=0.87, method="human_verified"),
        policy=Policy(risk_level="medium", decision_path="needs_review",
                       classification="scope_invalid", recommended_action="quarantine"),
        status="promoted",
        derived_from=["mem-source-001", "mem-source-002"],
        contradiction=Contradiction(detected=True, cross_project=False, resolution="closed_by_new_fact"),
    )


class TestDisasterRecovery:
    def test_reconstruction_from_json_matches_original_in_every_field(self, dr_project):
        original = _full_candidate("mem-drt-001")
        insert_candidate(original)
        register_candidate(original)

        payload_path = registers_module.CANDIDATES_DIR / "mem-drt-001.json"
        payload_data = json.loads(payload_path.read_text())
        reconstructed = GovernedMemoryCandidate(**payload_data)

        assert reconstructed.model_dump() == original.model_dump()

    def test_full_recovery_loop_delete_reconstruct_restore(self, dr_project):
        """
        The complete drill: genuinely delete from Postgres, reconstruct
        purely from the JSON payload with zero Postgres involvement,
        restore, and confirm the restored row is identical to the
        original -- not just that reconstruction is theoretically possible.
        """
        original = _full_candidate("mem-drt-002")
        insert_candidate(original)
        register_candidate(original)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_candidates WHERE memory_id = 'mem-drt-002'")
            conn.commit()

        assert get_candidate("mem-drt-002") is None, "setup error: candidate not actually deleted"

        payload_path = registers_module.CANDIDATES_DIR / "mem-drt-002.json"
        reconstructed = GovernedMemoryCandidate(**json.loads(payload_path.read_text()))

        insert_candidate(reconstructed)
        restored = get_candidate("mem-drt-002")

        assert restored is not None
        assert restored.model_dump() == original.model_dump()

    def test_reconstruction_preserves_nested_structured_content(self, dr_project):
        """
        Narrower, targeted check: content.structured is exactly the kind
        of nested field the register's own nine flat columns cannot
        represent at all -- this is Architecture §8.2's original defect
        this whole module exists to fix. Checked in isolation, not just
        as part of the full model_dump() comparison above.
        """
        original = _full_candidate("mem-drt-003")
        register_candidate(original)

        payload_path = registers_module.CANDIDATES_DIR / "mem-drt-003.json"
        reconstructed = GovernedMemoryCandidate(**json.loads(payload_path.read_text()))

        assert reconstructed.content.structured == {"key": "value", "n": 42}
