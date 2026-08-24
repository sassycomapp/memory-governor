from typing import Optional

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


def get_active_thresholds() -> dict:
    """
    Step 11.3: reads REVIEW_THRESHOLD/AUTO_PROMOTE_THRESHOLD from the
    currently active policy_versions row -- closing a real gap flagged
    since Phase 1 (the module constants above were never actually
    consulted; policy_versions has sat empty and unread since the table
    was created in Step 2.1). Falls back to the hardcoded defaults above
    when no active row exists yet -- the correct bootstrap behavior for
    a fresh install that has never had a policy change approved.
    """
    from .storage import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT thresholds FROM policy_versions WHERE active = TRUE LIMIT 1")
            row = cur.fetchone()

    if row is None:
        return {"REVIEW_THRESHOLD": REVIEW_THRESHOLD, "AUTO_PROMOTE_THRESHOLD": AUTO_PROMOTE_THRESHOLD}
    return row["thresholds"]


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


def can_project_view(candidate: GovernedMemoryCandidate, requesting_project: Optional[str]) -> bool:
    """
    Step 9.1 (Scope Doc §6): read-time visibility resolution -- distinct
    from check_scope() below, which only validates that a candidate's OWN
    shared_with is internally consistent with its declared visibility at
    WRITE time. This function answers a different question: given a
    specific requesting project, can it actually see this candidate.

    Backward-compatible by construction: if requesting_project is None
    (no caller identity supplied), every candidate is visible -- this is
    the exact behavior every existing caller and test already relies on,
    unchanged. Visibility narrows only once a caller identifies itself.

    A candidate is visible to a requesting project when it is the
    project's own, OR shared_all, OR shared_specific with that project
    explicitly listed. No other case grants visibility.
    """
    if requesting_project is None:
        return True
    if candidate.owning_project == requesting_project:
        return True
    if candidate.scope.visibility == "shared_all":
        return True
    if candidate.scope.visibility == "shared_specific":
        return requesting_project in candidate.scope.shared_with
    return False


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

    active_thresholds = get_active_thresholds()
    if candidate.confidence.score < active_thresholds["REVIEW_THRESHOLD"]:
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="low_confidence",
                      recommended_action=None,
                      allowed_actions=["promote", "quarantine", "reject"])

    if risk == "medium" and candidate.confidence.score < active_thresholds["AUTO_PROMOTE_THRESHOLD"]:
        return Policy(risk_level=risk, decision_path="needs_review",
                      requires_human_review=True,
                      classification="medium_risk_moderate_confidence",
                      recommended_action=None,
                      allowed_actions=["promote", "quarantine", "reject"])

    return Policy(risk_level=risk, decision_path="auto_promote",
                  requires_human_review=False, allowed_actions=["promote"])


def apply_approved_policy_change(candidate, reviewer: str) -> bool:
    """
    Step 11.3 (Architecture §7 point 4): the approved-change flow. Called
    from review_memory() (api.py) immediately after a policy-type
    candidate is approved and promoted. Runs SYNCHRONOUSLY, in the same
    request -- deliberately NOT the async outbox pattern used for
    register_write/gbrain_write. Those are async because they talk to
    external systems (git, GBrain) with real latency and real failure
    modes worth retrying. This is a plain, local Postgres write with no
    external dependency -- if it failed silently and async, a developer
    would believe they'd approved a real change that never actually took
    effect, which is worse than a loud, immediate error here.

    Only acts if the candidate genuinely carries a structured proposal
    (content.structured has BOTH proposed_policy_version and
    proposed_thresholds) -- this is what distinguishes a real,
    human-originated proposal (propose_policy(), Step 5.1) from a
    self-healing observation (Step 11.2), which is deliberately
    narrative-only and must never auto-apply a specific threshold change
    on its own. Returns False (no-op) if the structured fields aren't
    present -- not an error, just "nothing to apply here."

    The register mirror (Architecture §7 point 4's other requirement) is
    NOT duplicated here -- the candidate's own promotion already fires
    register_write (Phase 7), which captures this candidate's full
    content.structured (the real proposed thresholds) in its JSON
    payload automatically. No second, separate register file needed.
    """
    structured = candidate.content.structured or {}
    proposed_version = structured.get("proposed_policy_version")
    proposed_thresholds = structured.get("proposed_thresholds")

    if not proposed_version or not proposed_thresholds:
        return False

    from .storage import get_connection
    import json as _json

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE policy_versions SET active = FALSE WHERE active = TRUE")
            cur.execute(
                "INSERT INTO policy_versions (policy_version, thresholds, active, approved_by) "
                "VALUES (%s, %s, TRUE, %s)",
                (proposed_version, _json.dumps(proposed_thresholds), reviewer),
            )
        conn.commit()
    return True
