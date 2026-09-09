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
# HIGH_RISK_TYPES — RESOLVED 2026-09-08 (developer decision): the
# MemoryType enum was extended with the four missing spec categories, and
# all five of Architecture Spec §2's high-risk categories are governed
# here exactly as the architectural intent specifies.
# ---------------------------------------------------------------------------

class TestHighRiskTypesInterpretation:
    def test_high_risk_types_are_the_five_spec_categories(self):
        assert HIGH_RISK_TYPES == frozenset({
            "policy", "credential", "identity", "architecture", "security",
        })

    def test_policy_type_is_high_risk(self):
        c = _candidate(memory_type="policy")
        assert compute_risk_level(c) == "high"

    @pytest.mark.parametrize("mtype", [
        "credential", "identity", "architecture", "security",
    ])
    def test_each_new_spec_category_is_high_risk(self, mtype):
        c = _candidate(memory_type=mtype)
        assert compute_risk_level(c) == "high"

    @pytest.mark.parametrize("mtype", [
        "fact", "preference", "episode", "trace",
        "task_state", "relationship", "summary", "procedural",
    ])
    def test_no_other_memory_type_is_high_risk_by_type_alone(self, mtype):
        c = _candidate(memory_type=mtype, confidence=Confidence(score=0.95, method="human_verified"))
        assert compute_risk_level(c) != "high"


# ---------------------------------------------------------------------------
# compute_risk_level — unaffected by the two-outcome model
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
# check_provenance / check_scope — unaffected, called directly
# ---------------------------------------------------------------------------

class TestCheckProvenance:
    def test_complete_provenance_passes(self):
        c = _candidate()
        assert check_provenance(c) is True

    def test_empty_writer_identity_fails(self):
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
        c = _candidate(scope=Scope(visibility="shared_specific", retention="long_term",
                                    owning_project="p1", shared_with=["all"]))
        assert check_scope(c) is False


# ---------------------------------------------------------------------------
# decide_path — REWRITTEN for the two-outcome model (Step 6.5.1).
# Every case that used to auto-reject/auto-quarantine now asserts
# needs_review + the correct classification + the correct recommended_action.
# ---------------------------------------------------------------------------

class TestDecidePathOrdering:
    def test_missing_provenance_routes_to_review_with_reject_recommendation(self):
        c = _candidate(
            provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                                   writer_identity="", policy_version="v1"),
            confidence=Confidence(score=0.99, method="human_verified"),  # would otherwise auto-promote
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.classification == "provenance_failure"
        assert result.recommended_action == "reject"
        assert result.allowed_actions == ["promote", "quarantine", "reject"]

    def test_bad_scope_routes_to_review_with_quarantine_recommendation(self):
        c = _candidate(
            scope=Scope(visibility="project_local", retention="long_term",
                        owning_project="p1", shared_with=["p2"]),  # inconsistent
            confidence=Confidence(score=0.99, method="human_verified"),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.classification == "scope_invalid"
        assert result.recommended_action == "quarantine"

    def test_high_risk_type_always_needs_review_even_with_perfect_confidence(self):
        c = _candidate(memory_type="policy", confidence=Confidence(score=1.0, method="human_verified"))
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.classification == "high_risk_type"
        assert result.requires_human_review is True

    def test_below_review_threshold_routes_to_review_with_no_recommendation(self):
        c = _candidate(confidence=Confidence(score=REVIEW_THRESHOLD - 0.01, method="model_based"))
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.classification == "low_confidence"
        assert result.recommended_action is None  # no single obvious call to make here, by design

    def test_at_review_threshold_boundary_does_not_trigger_low_confidence_review(self):
        c = _candidate(confidence=Confidence(score=REVIEW_THRESHOLD, method="model_based"))
        result = decide_path(c)
        assert result.classification != "low_confidence"

    def test_medium_risk_below_auto_promote_threshold_needs_review(self):
        c = _candidate(
            scope=Scope(visibility="shared_all", retention="permanent",
                        owning_project="p1", shared_with=["all"]),
            confidence=Confidence(score=AUTO_PROMOTE_THRESHOLD - 0.01, method="model_based"),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.classification == "medium_risk_moderate_confidence"

    def test_low_risk_high_confidence_auto_promotes(self):
        c = _candidate(confidence=Confidence(score=0.95, method="human_verified"))
        result = decide_path(c)
        assert result.decision_path == "auto_promote"
        assert result.allowed_actions == ["promote"]
        assert result.classification is None
        assert result.recommended_action is None


class TestDecidePathContradictionUniformity:
    """
    Unaffected by the two-outcome rewrite -- contradiction handling already
    only ever returned needs_review/auto_promote, never reject/quarantine.
    Kept as the permanent proof of Architecture §10 test #2.
    """

    def test_unresolved_contradiction_on_low_risk_fact_blocks_promotion(self):
        c = _candidate(
            memory_type="fact",
            confidence=Confidence(score=0.99, method="human_verified"),
            contradiction=Contradiction(detected=True, resolution=None),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"
        assert result.classification == "unresolved_contradiction"

    def test_manual_review_resolution_on_low_risk_fact_still_blocks(self):
        c = _candidate(
            memory_type="preference",
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
                                        resolution="closed_by_new_fact"),
        )
        result = decide_path(c)
        assert result.decision_path == "needs_review"

    def test_genuinely_resolved_non_cross_project_contradiction_does_not_block(self):
        """
        The blue-to-yellow case: a deliberate supersession, correctly
        resolved, must NOT be blocked -- it promotes cleanly.
        """
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


class TestTwoOutcomeModel:
    """
    New for Step 6.5.2 (Architecture §2.2, Scope Doc §3 Principle 2).
    Directly proves the governing guarantee itself: across every scenario
    that would have triggered an automatic reject or quarantine under the
    old design, decide_path()'s own decision_path is NEVER anything other
    than "auto_promote" or "needs_review". This is the test-level
    counterpart to Step 6.5.1's source-inspection check -- proving the
    behavior, not just the absence of the old literal strings in the code.
    """

    SCENARIOS = [
        ("missing_provenance", dict(
            provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                                   writer_identity="", policy_version="v1"),
        )),
        ("bad_scope", dict(
            scope=Scope(visibility="project_local", retention="long_term",
                        owning_project="p1", shared_with=["p2"]),
        )),
        ("high_risk_type", dict(memory_type="policy")),
        ("low_confidence", dict(confidence=Confidence(score=0.1, method="model_based"))),
        ("unresolved_contradiction", dict(
            contradiction=Contradiction(detected=True, resolution=None),
        )),
        ("cross_project_contradiction", dict(
            contradiction=Contradiction(detected=True, cross_project=True,
                                        resolution="closed_by_new_fact"),
        )),
        ("medium_risk_moderate_confidence", dict(
            scope=Scope(visibility="shared_all", retention="permanent",
                        owning_project="p1", shared_with=["all"]),
            confidence=Confidence(score=0.7, method="model_based"),
        )),
        ("clean_candidate", dict(confidence=Confidence(score=0.95, method="human_verified"))),
    ]

    @pytest.mark.parametrize("label,overrides", SCENARIOS)
    def test_decision_path_is_always_one_of_the_two_allowed_values(self, label, overrides):
        c = _candidate(**overrides)
        result = decide_path(c)
        assert result.decision_path in ("auto_promote", "needs_review"), (
            f"Scenario '{label}' produced decision_path='{result.decision_path}' -- "
            f"decide_path() must NEVER return 'reject' or 'quarantine' automatically"
        )

    def test_recommended_action_is_never_treated_as_final_by_construction(self):
        """
        Documents the actual guarantee in code: recommended_action is a
        plain Optional[Action] field on Policy, carrying only a suggestion.
        Nothing in decide_path() ever sets `status` -- confirmed separately
        by TestDecidePathDoesNotMutate -- so a recommendation can never
        become a final disposition without passing through apply_decision()
        (state_machine.py), which requires an explicit developer-driven call.
        """
        c = _candidate(
            provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                                   writer_identity="", policy_version="v1"),
        )
        result = decide_path(c)
        assert result.recommended_action == "reject"
        assert c.status == "proposed"  # unchanged -- the suggestion was never applied
