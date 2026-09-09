import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from app.models import (
    Scope, Subject, Content, Provenance, Timestamps, Confidence,
    Policy, Contradiction, ReviewPresentation, Audit,
    GovernedMemoryCandidate, MemoryDecision,
)


def _now():
    return datetime.now(timezone.utc)


def _minimal_candidate(**overrides) -> dict:
    """Baseline valid payload; overrides let a test mutate one field."""
    base = dict(
        memory_id="mem-001",
        owning_project="mb-3-cs-code",
        scope=Scope(
            visibility="project_local",
            retention="long_term",
            owning_project="mb-3-cs-code",
        ),
        memory_type="fact",
        content=Content(text="The vault encrypts secrets at rest."),
        provenance=Provenance(
            source_type="user",
            source_ref="/mnt/c/dev/example.md",
            writer_identity="manual-submission",
            policy_version="v1",
        ),
        timestamps=Timestamps(created_at=_now(), updated_at=_now()),
        confidence=Confidence(score=0.9, method="human_verified"),
        policy=Policy(risk_level="low", decision_path="auto_promote"),
        status="proposed",
    )
    base.update(overrides)
    return base


class TestScope:
    def test_construct_valid_visibility_project_local(self):
        s = Scope(visibility="project_local", retention="long_term", owning_project="p1")
        assert s.visibility == "project_local"
        assert s.shared_with == []  # default

    def test_construct_shared_all(self):
        s = Scope(visibility="shared_all", retention="permanent", owning_project="p1", shared_with=["all"])
        assert s.shared_with == ["all"]

    def test_invalid_visibility_rejected(self):
        with pytest.raises(ValidationError):
            Scope(visibility="not_a_real_value", retention="long_term", owning_project="p1")

    def test_invalid_retention_rejected(self):
        with pytest.raises(ValidationError):
            Scope(visibility="project_local", retention="not_a_real_value", owning_project="p1")

    def test_owning_project_max_length_enforced(self):
        with pytest.raises(ValidationError):
            Scope(visibility="project_local", retention="long_term", owning_project="x" * 65)


class TestSubject:
    def test_construct_valid_spo_triple(self):
        s = Subject(entity="encrypt()", predicate="defined_in", object="encryption_service.py")
        assert s.object == "encryption_service.py"

    def test_object_accepts_bool(self):
        s = Subject(entity="feature_x", predicate="enabled", object=True)
        assert s.object is True

    def test_entity_max_length_enforced(self):
        with pytest.raises(ValidationError):
            Subject(entity="x" * 257, predicate="p")


class TestContent:
    def test_construct_text_only(self):
        c = Content(text="hello")
        assert c.structured is None

    def test_construct_with_structured_dict(self):
        c = Content(text="hello", structured={"key": "value"})
        assert c.structured == {"key": "value"}

    def test_construct_with_structured_list(self):
        c = Content(text="hello", structured=[1, 2, 3])
        assert c.structured == [1, 2, 3]


class TestProvenance:
    def test_construct_valid(self):
        p = Provenance(source_type="user", source_ref="/tmp/x.md",
                        writer_identity="w1", policy_version="v1")
        assert p.derived_from is None

    def test_invalid_source_type_rejected(self):
        with pytest.raises(ValidationError):
            Provenance(source_type="not_real", source_ref="/tmp/x.md",
                       writer_identity="w1", policy_version="v1")

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            Provenance(source_type="user", source_ref="/tmp/x.md")  # missing writer_identity, policy_version


class TestTimestamps:
    def test_construct_minimal(self):
        t = Timestamps(created_at=_now(), updated_at=_now())
        assert t.valid_until is None
        assert t.valid_until_reason is None

    def test_construct_with_valid_until_reason(self):
        t = Timestamps(created_at=_now(), updated_at=_now(),
                        valid_until=_now(), valid_until_reason="superseded")
        assert t.valid_until_reason == "superseded"

    def test_invalid_valid_until_reason_rejected(self):
        with pytest.raises(ValidationError):
            Timestamps(created_at=_now(), updated_at=_now(), valid_until_reason="not_real")


class TestConfidence:
    def test_construct_valid_boundary_zero(self):
        c = Confidence(score=0.0, method="rule_based")
        assert c.score == 0.0

    def test_construct_valid_boundary_one(self):
        c = Confidence(score=1.0, method="model_based")
        assert c.score == 1.0

    def test_score_above_one_rejected(self):
        with pytest.raises(ValidationError):
            Confidence(score=1.1, method="rule_based")

    def test_score_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            Confidence(score=-0.1, method="rule_based")

    def test_invalid_method_rejected(self):
        with pytest.raises(ValidationError):
            Confidence(score=0.5, method="not_real")


class TestPolicy:
    def test_construct_minimal(self):
        p = Policy(risk_level="low", decision_path="auto_promote")
        assert p.requires_human_review is None

    def test_invalid_risk_level_rejected(self):
        with pytest.raises(ValidationError):
            Policy(risk_level="not_real", decision_path="auto_promote")

    def test_invalid_decision_path_rejected(self):
        with pytest.raises(ValidationError):
            Policy(risk_level="low", decision_path="not_real")

    def test_invalid_allowed_action_rejected(self):
        with pytest.raises(ValidationError):
            Policy(risk_level="low", decision_path="auto_promote", allowed_actions=["not_real"])


class TestContradiction:
    def test_default_not_detected(self):
        c = Contradiction()
        assert c.detected is False
        assert c.cross_project is False

    def test_cross_project_flag(self):
        c = Contradiction(detected=True, cross_project=True, resolution="manual_review")
        assert c.cross_project is True

    def test_invalid_resolution_rejected(self):
        with pytest.raises(ValidationError):
            Contradiction(detected=True, resolution="not_real")


class TestReviewPresentation:
    def test_construct_requires_all_three_fields(self):
        r = ReviewPresentation(recommendation="promote", validation="passed", ramifications="none")
        assert r.recommendation == "promote"

    def test_missing_field_rejected(self):
        with pytest.raises(ValidationError):
            ReviewPresentation(recommendation="promote", validation="passed")  # missing ramifications


class TestAudit:
    def test_construct_empty(self):
        a = Audit()
        assert a.decision_by is None

    def test_construct_full(self):
        a = Audit(decision_by="governor", decision_at=_now(),
                   decision_reason="auto", reviewer=None)
        assert a.decision_by == "governor"


class TestGovernedMemoryCandidate:
    def test_construct_minimal_valid(self):
        c = GovernedMemoryCandidate(**_minimal_candidate())
        assert c.status == "proposed"
        assert c.contradiction is None
        assert c.review is None

    def test_procedural_memory_type_accepted(self):
        c = GovernedMemoryCandidate(**_minimal_candidate(memory_type="procedural"))
        assert c.memory_type == "procedural"

    @pytest.mark.parametrize("mtype", [
        "credential", "identity", "architecture", "security",
    ])
    def test_high_risk_memory_types_accepted(self, mtype):
        """Added 2026-09-08: resolves the HIGH_RISK_TYPES spec
        inconsistency -- Architecture Spec §2's five high-risk categories
        are now all representable in the MemoryType enum."""
        c = GovernedMemoryCandidate(**_minimal_candidate(memory_type=mtype))
        assert c.memory_type == mtype

    def test_invalid_memory_type_rejected(self):
        with pytest.raises(ValidationError):
            GovernedMemoryCandidate(**_minimal_candidate(memory_type="not_real"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            GovernedMemoryCandidate(**_minimal_candidate(status="not_real"))

    def test_memory_id_max_length_enforced(self):
        with pytest.raises(ValidationError):
            GovernedMemoryCandidate(**_minimal_candidate(memory_id="x" * 129))

    def test_construct_with_full_review_block(self):
        payload = _minimal_candidate(status="under_review")
        payload["review"] = ReviewPresentation(
            recommendation="promote", validation="provenance ok", ramifications="none"
        )
        c = GovernedMemoryCandidate(**payload)
        assert c.review.recommendation == "promote"

    def test_construct_with_cross_project_contradiction(self):
        payload = _minimal_candidate(status="under_review")
        payload["contradiction"] = Contradiction(detected=True, cross_project=True)
        c = GovernedMemoryCandidate(**payload)
        assert c.contradiction.cross_project is True


class TestMemoryDecision:
    def test_construct_valid(self):
        d = MemoryDecision(memory_id="mem-001", previous_status="proposed",
                            new_status="promoted", reason="auto", reviewer=None)
        assert d.new_status == "promoted"

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            MemoryDecision(memory_id="mem-001", previous_status="not_real", new_status="promoted")
