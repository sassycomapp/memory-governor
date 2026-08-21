from typing import Optional, Tuple
from .models import Status, MemoryDecision, GovernedMemoryCandidate, Audit

# Governs transitions between REAL Status values only, exactly as specified
# in Architecture §2. Creation (the virtual "none -> proposed" transition)
# is handled separately by initialize_candidate() below, not modeled here.
ALLOWED_TRANSITIONS = {
    "proposed":     {"under_review", "promoted", "quarantined", "rejected", "superseded"},
    "under_review": {"promoted", "quarantined", "rejected", "superseded"},
    "promoted":     {"superseded", "expired", "quarantined"},
    "quarantined":  {"promoted", "rejected", "expired"},
    "rejected":     set(),
    "superseded":   {"expired"},
    "expired":      set(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, set())


def initialize_candidate(candidate: GovernedMemoryCandidate) -> GovernedMemoryCandidate:
    """
    The SOLE entry point for creating a new candidate's status. This is the
    "none -> proposed" virtual transition (Architecture §2). It forces
    status to "proposed" unconditionally -- it does NOT trust or preserve
    whatever status value the incoming candidate object may already carry.

    This is what closes the v1 defect where submit_candidate() wrote
    status directly to storage on the initial-write path, bypassing
    transition validation entirely. Any caller building a candidate from
    an incoming payload MUST route it through this function before it is
    ever persisted -- never construct-and-save directly.

    Note: this alone does not detect whether memory_id already exists in
    storage (that requires the persistence layer, Phase 2). It guarantees
    only that status, once this function runs, is "proposed" and nothing
    else -- a caller cannot smuggle in "promoted" via a crafted payload.
    """
    candidate.status = "proposed"
    return candidate


def apply_decision(
    candidate: GovernedMemoryCandidate,
    new_status: Status,
    reason: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Tuple[GovernedMemoryCandidate, MemoryDecision]:
    """
    The SOLE function permitted to change the status of an EXISTING
    candidate (i.e. one that has already been created via
    initialize_candidate). Every non-creation write path -- review,
    supersede, quarantine, expire -- MUST call this. There is no other
    legitimate way to mutate status after creation anywhere in this
    codebase (enforced by test_status_field_never_assigned_outside_state_machine
    in the test suite).

    Raises ValueError, without mutating candidate.status, if the requested
    transition is not permitted from the candidate's current status.
    """
    previous_status = candidate.status

    if not can_transition(previous_status, new_status):
        raise ValueError(
            f"Invalid transition from '{previous_status}' to '{new_status}' "
            f"(memory_id={candidate.memory_id})"
        )

    candidate.status = new_status

    if candidate.audit is None:
        candidate.audit = Audit()
    candidate.audit.decision_by = reviewer or "governor"
    candidate.audit.decision_reason = reason or ""
    if reviewer:
        candidate.audit.reviewer = reviewer

    decision = MemoryDecision(
        memory_id=candidate.memory_id,
        previous_status=previous_status,
        new_status=new_status,
        reason=reason,
        reviewer=reviewer,
    )
    return candidate, decision
