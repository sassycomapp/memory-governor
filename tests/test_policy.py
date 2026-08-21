from datetime import datetime, timezone

import pytest

from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance, Timestamps,
    Confidence, Policy, Contradiction,
)
from app.policy import (
    decide_path, compute_risk_level, check_provenance, check_scope,
    HIGH_RISK_TYPES, REVIEW_THRESHOLD, AUTO_PROMOTE_THRESHOLD,
)


def _now():
    return datetime.now(timezone.utc)


def _candidate(**overrides) -> GovernedMemoryCandidate:
    base = dict(
        memory_id="mem-pol-001",
        owning_project="mb-3-cs-code",
        scope=overrides.pop("scope", Scope(
            visibility="project_local", retention="long_term",
            owning_project="mb-3-cs-code",
        )),
        memory_type=overrides.pop("memory_type", "fact"),
        content=Content(text="test content"),
        provenance=overrides.pop("provenance", Provenance(
            source_type="user", source_ref="/tmp/x.md",
            writer_identity="w1", policy_version="v1",
        )),
        timestamps=Timestamps(created_at=_now(), updated_at=_now()),
        confidence=overrides.pop("confidence", Confidence(score=0.9, method="human_verified")),
        policy=Policy(risk_level="low", decision_path="auto_promote"),
        status="proposed",
        contradiction=overrides.pop("contradiction", None),
    )
    base.update(overrides)
    return GovernedMemoryCandidate(**base)


# ---------------------------------------------------------------------------
# HIGH_RISK_TYPES — lock in the documented interpretation
# ---------------------------------------------------------------------------

class TestHighRiskTypesInterpretation:
    def test_only_policy_is_high_risk(self):
        assert HIGH_RISK_TYPES == frozenset({"policy"})

    def test_policy_type_is_high_risk(self):
        c = _candidate(memory_type="policy")
        assert compute_risk_level(c) == "high"

    @pytest.mark.parametrize("mtype", [
        "fact", "preference", "episode", "trace",
        "task_state", "relationship", "summary", "procedural",
    ])
    def test_no_other_memory_type_is_high_risk_by_type_alone(self, mtype):
        c = _candidate(memory_type=mtype, confidence=Confidence(score=0.95, method="human_verified"))
        assert compute_risk_level(c) != "high"


# ---------------------------------------------------------------------------
# compute_risk_level — sharing elevation + confidence elevation
# ---------------------------------------------------------------------------

class TestComputeRiskLevel:
    def test_project_local_high_confidence_is_low_risk(self):
        c = _candidate(confidence=Confidence(score=0.95, method="human_verified"))
        assert compute_risk_level(c) == "low"

    def test_shared_all_is_elevated_to_medium(self):
        c = _candidate(
            scope=Scope(visibility="shared_all", retention="permanent",
                        owning_project="mb-3-cs-code", shared_with=["all"]),
            confidence=Confidence(score=0.95, method="human_verified"),
        )
        assert compute_risk_level(c) == "medium"

    def test_shared_specific_is_elevated_to_medium(self):
        c = _candidate(
            scope=Scope(visibility="shared_specific", retention="long_term",
                        owning_project="mb-3-cs-code", shared_with=["mb4ecom-code"]),
            confidence=Confidence(score=0.95, method="human_verified"),
        )
        assert compute_risk_level(c) == "medium"

    def test_low_confidence_elevates_to_medium(self):
        c = _candidate(confidence=Confidence(score=0.3, method="model_based"))
        assert compute_risk_level(c) == "medium"

    def test_confidence_boundary_exactly_half_is_not_elevated(self):
        c = _candidate(confidence=Confidence(score=0.5, method="model_based"))
        assert compute_risk_level(c) == "low"


# ---------------------------------------------------------------------------
# check_provenance
# ---------------------------------------------------------------------------

class TestCheckProvenance:
    def test_complete_provenance_passes(self):
        c = _candidate()
        assert check_provenance(c) is True

    def test_empty_writer_identity_fails(self):
        # Pydantic's constr allows an empty string; this is exactly the
        # bypass check_provenance exists to catch.
        c = _candidate(provenance=Provenance(
            source_type="user", source_ref="/tmp/x.md",
            writer_identity="", policy_version="v1",
        ))
        assert check_provenance(c) is False

    def test_empty_policy_version_fails(self):
        c = _candidate(provenance=Provenance(
            source_type="user", source_ref="/tmp/x.md",
            writer_identity="w1", policy_version="",
        ))
        assert check_provenance(c) is False


# ---------------------------------------------------------------------------
# check_scope — Scope Doc §6 consistency rules
# ---------------------------------------------------------------------------

class TestCheckScope:
    def test_project_local_with_empty_shared_with_passes(self):
        c = _candidate(scope=Scope(visibility="project_local", retention="long_term",
                                    owning_project="p1", shared_with=[]))
        assert check_scope(c) is True

    def test_project_local_with_nonempty_shared_with_fails(self):
        c = _candidate(scope=Scope(visibility="project_local", retention="long_term",
                                    owning_project="p1", shared_with=["p2"]))
        assert check_scope(c) is False

    def test_shared_all_with_exact_all_list_passes(self):
        c = _candidate(scope=Scope(visibility="shared_all", retention="permanent",
                                    owning_project="p1", shared_with=["all"]))
        assert check_scope(c) is True

    def test_shared_all_with_empty_list_fails(self):
        c = _candidate(scope=Scope(visibility="shared_all", retention="permanent",
                                    owning_project="p1", shared_with=[]))
        assert check_scope(c) is False

    def test_shared_specific_with_real_list_passes(self):
        c = _candidate(scope=Scope(visibility="shared_specific", retention="long_term",
                                    owning_project="p1", shared_with=["p2", "p3"]))
        assert check_scope(c) is True

    def test_shared_specific_with_empty_list_fails(self):
        c = _candidate(scope=Scope(visibility="shared_specific", retention="long_term",
                                    owning_project="p1", shared_with=[]))
        assert check_scope(c) is False

    def test_shared_specific_with_all_literal_fails(self):
        # "all" belongs to shared_all's contract, not shared_specific's.
        c = _candidate(scope=Scope(visibility="shared_specific", retention="long_term",
                                    owning_project="p1", shared_with=["all"]))
        assert check_scope(c) is False


# ---------------------------------------------------------------------------
# decide_path — full decision engine, ordered per Architecture §2
# ---------------------------------------------------------------------------

class TestDecidePathOrdering:
    def test_missing_provenance_rejects_before_anything_else(self):
        c = _candidate(
            provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                                   writer_identity="", policy_version="v1"),
            confidence=Confidence(score=0.99, method="human_verified"),  # would otherwise auto-promote
        )
        result = decide_path(c)
        assert result.decision_path == "reject"
        assert result.allowed_actions == ["reject"]

    def test_bad_scope_quarantines_before_confidence_checked(self):
        c = _candidate(
            scope=Scope(visibility="project_local", retention="long_term",
                        owning_project="p1", shared_with=["p2"]),  # inconsistent
            confidence=Confidence(score=0.99, method="human_verified"),
        )
        result = decide_path(c)
        assert result.decision_path == "quarantine"

    def test_high_risk_type_always_needs_review_even_with_perfect_confidence(self):
        c = _candidate(memory_type="policy", confidence=Confidence(score=1.0, method="human_verified"))
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.requires_human_review is True

    def test_below_review_threshold_quarantines(self):
        c = _candidate(confidence=Confidence(score=REVIEW_THRESHOLD - 0.01, method="model_based"))
        result = decide_path(c)
        assert result.decision_path == "quarantine"

    def test_at_review_threshold_boundary_does_not_quarantine_on_confidence_alone(self):
        c = _candidate(confidence=Confidence(score=REVIEW_THRESHOLD, method="model_based"))
        result = decide_path(c)
        assert result.decision_path != "quarantine"

    def test_medium_risk_below_auto_promote_threshold_needs_review(self):
        c = _candidate(
            scope=Scope(visibility="shared_all", retention="permanent",
                        owning_project="p1", shared_with=["all"]),
            confidence=Confidence(score=AUTO_PROMOTE_THRESHOLD - 0.01, method="model_based"),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"

    def test_low_risk_high_confidence_auto_promotes(self):
        c = _candidate(confidence=Confidence(score=0.95, method="human_verified"))
        result = decide_path(c)
        assert result.decision_path == "auto_promote"
        assert result.allowed_actions == ["promote"]


class TestDecidePathContradictionUniformity:
    """
    This is what actually proves Architecture §10 test #2 -- the v1 defect
    where contradiction-blocking was incorrectly gated to HIGH_RISK_TYPES
    only. Every test below uses memory_type='fact' (deliberately NOT in
    HIGH_RISK_TYPES) with otherwise-perfect confidence, to isolate that the
    contradiction check alone is what blocks auto-promotion.
    """

    def test_unresolved_contradiction_on_low_risk_fact_blocks_promotion(self):
        c = _candidate(
            memory_type="fact",  # NOT high-risk
            confidence=Confidence(score=0.99, method="human_verified"),  # would otherwise auto-promote
            contradiction=Contradiction(detected=True, resolution=None),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"

    def test_manual_review_resolution_on_low_risk_fact_still_blocks(self):
        c = _candidate(
            memory_type="preference",  # NOT high-risk
            confidence=Confidence(score=0.99, method="human_verified"),
            contradiction=Contradiction(detected=True, resolution="manual_review"),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"

    def test_cross_project_contradiction_on_low_risk_fact_always_blocks(self):
        c = _candidate(
            memory_type="fact",
            confidence=Confidence(score=0.99, method="human_verified"),
            contradiction=Contradiction(detected=True, cross_project=True,
                                        resolution="closed_by_new_fact"),  # even a real resolution
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"

    def test_genuinely_resolved_non_cross_project_contradiction_does_not_block(self):
        c = _candidate(
            memory_type="fact",
            confidence=Confidence(score=0.95, method="human_verified"),
            contradiction=Contradiction(detected=True, cross_project=False,
                                        resolution="closed_by_new_fact"),
        )
        result = decide_path(c)
        assert result.decision_path == "auto_promote"

    def test_contradiction_not_detected_does_not_block(self):
        c = _candidate(
            memory_type="fact",
            confidence=Confidence(score=0.95, method="human_verified"),
            contradiction=Contradiction(detected=False),
        )
        result = decide_path(c)
        assert result.decision_path == "auto_promote"

    def test_no_contradiction_object_at_all_does_not_block(self):
        c = _candidate(
            memory_type="fact",
            confidence=Confidence(score=0.95, method="human_verified"),
            contradiction=None,
        )
        result = decide_path(c)
        assert result.decision_path == "auto_promote"


class TestDecidePathDoesNotMutate:
    def test_decide_path_never_touches_status(self):
        c = _candidate(confidence=Confidence(score=0.95, method="human_verified"))
        original_status = c.status
        decide_path(c)
        assert c.status == original_status

    def test_decide_path_returns_new_policy_object_not_mutating_existing(self):
        c = _candidate(confidence=Confidence(score=0.95, method="human_verified"))
        original_policy = c.policy
        result = decide_path(c)
        assert result is not original_policy
