from .models import (
    GovernedMemoryCandidate,
    Policy,
    RiskLevel,
)

# NOTE on HIGH_RISK_TYPES: Architecture Spec §2 names five categories --
# "policy", "credential", "identity", "architecture", "security" -- but
# only "policy" is a valid MemoryType value (see models.py's MemoryType
# Literal). The other four are not valid memory_type values anywhere in
# this codebase; they appear inherited unedited from the v1 sketch's risk
# taxonomy and were never reconciled against the final MemoryType enum.
# Flagged as an open spec inconsistency, not silently resolved: this uses
# the only defensible literal reading (the single real overlap) rather
# than guessing at unlisted enum values. If those four concepts need
# governing, the fix belongs in models.py's MemoryType enum or a separate
# content-classification field -- a developer decision, not this module's.
HIGH_RISK_TYPES = frozenset({"policy"})

# Stub thresholds -- Phase 2 replaces these with a live read from the
# active row in the policy_versions table (Architecture §1.2).
REVIEW_THRESHOLD = 0.65
AUTO_PROMOTE_THRESHOLD = 0.85


def compute_risk_level(candidate: GovernedMemoryCandidate) -> RiskLevel:
    if candidate.memory_type in HIGH_RISK_TYPES:
        return "high"
    # Static approximation of "sharing CHANGE is high risk" (Architecture
    # §2) -- diffing against a prior shared_with requires storage (Phase
    # 2). At this phase: any non-project_local candidate is elevated.
    if candidate.scope.visibility != "project_local":
        return "medium"
    if candidate.confidence.score < 0.5:
        return "medium"
    return "low"


def check_provenance(candidate: GovernedMemoryCandidate) -> bool:
    """
    Validates required provenance fields are present and non-empty. Does
    NOT authenticate writer_identity -- that happens in provenance.py
    (Phase 3) before a candidate ever reaches decide_path. Guards only
    against an empty-string bypass Pydantic's type system doesn't forbid.
    """
    p = candidate.provenance
    return bool(p.source_type and p.source_ref and p.writer_identity and p.policy_version)


def check_scope(candidate: GovernedMemoryCandidate) -> bool:
    """
    Enforces the visibility/shared_with consistency defined in Scope Doc
    §6: project_local -> empty shared_with; shared_all -> exactly ["all"];
    shared_specific -> a real, non-empty, explicit project list.
    """
    scope = candidate.scope
    if scope.visibility == "project_local":
        return scope.shared_with == []
    if scope.visibility == "shared_all":
        return scope.shared_with == ["all"]
    if scope.visibility == "shared_specific":
        return len(scope.shared_with) > 0 and scope.shared_with != ["all"]
    return False  # unreachable given the Literal type; defensive only


def decide_path(candidate: GovernedMemoryCandidate) -> Policy:
    """
    The deterministic decision engine (Architecture §2, revised for the
    two-outcome model -- §2.2). Returns a Policy describing what SHOULD
    happen -- does NOT mutate the candidate or its status.
    state_machine.py is the sole authority for status changes; the caller
    applies policy.decision_path via apply_decision() (Phase 5).

    TWO-OUTCOME MODEL (Scope Doc §3, Principle 2, revised): this function's
    own automatic output is now limited to exactly two decision_path
    values -- "auto_promote" or "needs_review". It never returns "reject"
    or "quarantine" as its own decision -- those remain legal DecisionPath
    values elsewhere (e.g. a developer's own resolution of a review item
    via apply_decision()), but the engine itself no longer closes a
    candidate out unilaterally. Where the prior version auto-quarantined
    or auto-rejected, this version returns needs_review, carrying a
    `classification` tag and a `recommended_action` -- a suggestion
    attached to an open item, not a decision already made.
    """
    risk = compute_risk_level(candidate)

    if not check_provenance(candidate):
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="provenance_failure",
                      recommended_action="reject",
                      allowed_actions=["promote", "quarantine", "reject"])

    if not check_scope(candidate):
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="scope_invalid",
                      recommended_action="quarantine",
                      allowed_actions=["promote", "quarantine", "reject"])

    contradiction = candidate.contradiction
    if contradiction and contradiction.detected:
        # Corrected from the v1 defect: applies to EVERY memory_type, not
        # gated to HIGH_RISK_TYPES (Architecture §2 / §10 test #2).
        if (contradiction.cross_project
                or not contradiction.resolution
                or contradiction.resolution == "manual_review"):
            return Policy(risk_level=risk, decision_path="needs_review",
                          requires_human_review=True,
                          classification="unresolved_contradiction",
                          recommended_action=None,
                          allowed_actions=["promote", "quarantine", "reject"])
        # A genuinely RESOLVED contradiction (e.g. resolution ==
        # "closed_by_new_fact", a deliberate supersession -- Scope Doc §12)
        # does NOT block further evaluation here.

    if risk == "high":
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="high_risk_type",
                      recommended_action=None,
                      allowed_actions=["promote", "quarantine", "reject"])

    if candidate.confidence.score < REVIEW_THRESHOLD:
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="low_confidence",
                      recommended_action=None,
                      allowed_actions=["promote", "quarantine", "reject"])

    if risk == "medium" and candidate.confidence.score < AUTO_PROMOTE_THRESHOLD:
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="medium_risk_moderate_confidence",
                      recommended_action=None,
                      allowed_actions=["promote", "quarantine", "reject"])

    return Policy(risk_level=risk, decision_path="auto_promote",
                  requires_human_review=False, allowed_actions=["promote"])
