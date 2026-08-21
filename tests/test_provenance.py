import inspect
import time

import pytest

from app.provenance import (
    resolve_writer_identity, build_provenance, compute_lineage_hash,
    reconstruct_lineage, _CREDENTIAL_ENV_MAP,
)
from app.storage import get_connection, insert_project, insert_candidate
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)
from datetime import datetime, timezone

TEST_PROJECT_ID = "test-project-provenance"
FAKE_CRED = "fake-credential-does-not-exist-anywhere"


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
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()
    insert_project(TEST_PROJECT_ID, "Provenance Test Project")
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (TEST_PROJECT_ID,))
            cur.execute("DELETE FROM projects WHERE project_id = %s", (TEST_PROJECT_ID,))
        conn.commit()


class TestBuildProvenanceHasNoWriterIdentityParameter:
    """
    This is the structural half of Scope Doc §11's guarantee -- proven at
    the signature level, not just by behavior, so a future edit that
    accidentally adds a writer_identity parameter to build_provenance()
    fails this test immediately, before anyone even calls it wrong.
    """
    def test_writer_identity_is_not_an_accepted_parameter(self):
        sig = inspect.signature(build_provenance)
        assert "writer_identity" not in sig.parameters

    def test_exact_expected_parameter_set(self):
        sig = inspect.signature(build_provenance)
        assert set(sig.parameters.keys()) == {
            "source_type", "source_ref", "presented_credential",
            "policy_version", "derived_from",
        }


class TestResolveWriterIdentity:
    def test_real_credential_env_vars_are_registered(self):
        assert "MEMORY_GOVERNOR_WRITER_GBRAIN_DREAMING" in _CREDENTIAL_ENV_MAP
        assert "MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION" in _CREDENTIAL_ENV_MAP

    def test_fake_credential_raises_permission_error(self):
        with pytest.raises(PermissionError):
            resolve_writer_identity(FAKE_CRED)

    def test_empty_string_credential_raises_permission_error(self):
        with pytest.raises(PermissionError):
            resolve_writer_identity("")

    def test_a_real_writer_identity_string_used_AS_a_credential_is_rejected(self):
        """
        Architecture §10 test #3's exact scenario: someone tries to pass
        the writer_identity LABEL itself ("manual_submission") where a
        credential is expected, hoping it gets accepted as-is. It must
        not -- only the actual secret credential value resolves.
        """
        with pytest.raises(PermissionError):
            resolve_writer_identity("manual_submission")


class TestBuildProvenanceIgnoresPayloadWriterIdentity:
    """
    The actual proof of Architecture §10 test #3: build_provenance()
    cannot be tricked into accepting an attacker-supplied writer_identity,
    because there is no parameter for one to flow through in the first
    place -- confirmed here by attempting the call **kwargs-style with an
    injected writer_identity and confirming it is rejected outright by
    Python itself (TypeError), not silently absorbed.
    """
    def test_injecting_writer_identity_via_kwargs_raises_type_error(self, monkeypatch):
        monkeypatch.setenv("MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION", "real-cred-value")
        forged_kwargs = {
            "source_type": "user",
            "source_ref": "/tmp/x.md",
            "presented_credential": "real-cred-value",
            "policy_version": "v1",
            "writer_identity": "attacker-controlled-identity",  # the injection attempt
        }
        with pytest.raises(TypeError):
            build_provenance(**forged_kwargs)

    def test_legitimate_call_resolves_writer_identity_from_credential_only(self, monkeypatch):
        monkeypatch.setenv("MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION", "real-cred-value")
        prov = build_provenance(
            source_type="user",
            source_ref="/tmp/x.md",
            presented_credential="real-cred-value",
            policy_version="v1",
        )
        assert prov.writer_identity == "manual_submission"


class TestComputeLineageHash:
    def test_same_inputs_produce_same_hash(self):
        h1 = compute_lineage_hash("/tmp/x.md", "user")
        h2 = compute_lineage_hash("/tmp/x.md", "user")
        assert h1 == h2

    def test_different_source_ref_produces_different_hash(self):
        h1 = compute_lineage_hash("/tmp/x.md", "user")
        h2 = compute_lineage_hash("/tmp/y.md", "user")
        assert h1 != h2

    def test_different_source_type_produces_different_hash(self):
        h1 = compute_lineage_hash("/tmp/x.md", "user")
        h2 = compute_lineage_hash("/tmp/x.md", "file")
        assert h1 != h2

    def test_hash_is_valid_sha256_hex_digest(self):
        h = compute_lineage_hash("/tmp/x.md", "user")
        assert len(h) == 64
        int(h, 16)  # raises ValueError if not valid hex


class TestReconstructLineage:
    def test_candidate_with_no_supersedes_returns_single_element(self):
        c = _candidate("t-prov-001")
        insert_candidate(c)
        chain = reconstruct_lineage("t-prov-001")
        assert len(chain) == 1
        assert chain[0].memory_id == "t-prov-001"

    def test_two_link_chain_returns_oldest_first(self):
        original = _candidate("t-prov-002")
        insert_candidate(original)
        successor = _candidate("t-prov-003", supersedes_id="t-prov-002")
        insert_candidate(successor)

        chain = reconstruct_lineage("t-prov-003")
        assert len(chain) == 2
        assert chain[0].memory_id == "t-prov-002"  # oldest first
        assert chain[1].memory_id == "t-prov-003"

    def test_three_link_chain_full_order(self):
        insert_candidate(_candidate("t-prov-004"))
        insert_candidate(_candidate("t-prov-005", supersedes_id="t-prov-004"))
        insert_candidate(_candidate("t-prov-006", supersedes_id="t-prov-005"))

        chain = reconstruct_lineage("t-prov-006")
        assert [c.memory_id for c in chain] == ["t-prov-004", "t-prov-005", "t-prov-006"]

    def test_nonexistent_memory_id_raises_value_error(self):
        with pytest.raises(ValueError):
            reconstruct_lineage("t-prov-does-not-exist")
