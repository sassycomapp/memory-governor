import re
from pathlib import Path
from datetime import datetime, timezone

import pytest

from app.models import GovernedMemoryCandidate, Scope, Content, Provenance, Timestamps, Confidence, Policy
from app.state_machine import (
    ALLOWED_TRANSITIONS, can_transition, initialize_candidate, apply_decision,
)


def _now():
    return datetime.now(timezone.utc)


def _candidate(status="proposed", **overrides) -> GovernedMemoryCandidate:
    base = dict(
        memory_id="mem-sm-001",
        owning_project="mb-3-cs-code",
        scope=Scope(visibility="project_local", retention="long_term", owning_project="mb-3-cs-code"),
        memory_type="fact",
        content=Content(text="test content"),
        provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                               writer_identity="w1", policy_version="v1"),
        timestamps=Timestamps(created_at=_now(), updated_at=_now()),
        confidence=Confidence(score=0.9, method="human_verified"),
        policy=Policy(risk_level="low", decision_path="auto_promote"),
        status=status,
    )
    base.update(overrides)
    return GovernedMemoryCandidate(**base)


# ---------------------------------------------------------------------------
# Transition table correctness — every ALLOWED pair and every DISALLOWED pair
# ---------------------------------------------------------------------------

ALL_STATUSES = ["proposed", "under_review", "promoted", "quarantined",
                 "rejected", "superseded", "expired"]

ALLOWED_PAIRS = [
    (frm, to) for frm, tos in ALLOWED_TRANSITIONS.items() for to in tos
]
DISALLOWED_PAIRS = [
    (frm, to) for frm in ALL_STATUSES for to in ALL_STATUSES
    if to not in ALLOWED_TRANSITIONS.get(frm, set())
]


class TestTransitionTable:
    @pytest.mark.parametrize("frm,to", ALLOWED_PAIRS)
    def test_allowed_transition_succeeds(self, frm, to):
        c = _candidate(status=frm)
        updated, decision = apply_decision(c, to, reason="test", reviewer="tester")
        assert updated.status == to
        assert decision.previous_status == frm
        assert decision.new_status == to

    @pytest.mark.parametrize("frm,to", DISALLOWED_PAIRS)
    def test_disallowed_transition_raises_and_does_not_mutate(self, frm, to):
        c = _candidate(status=frm)
        with pytest.raises(ValueError):
            apply_decision(c, to)
        # Critical: status must be UNCHANGED after a rejected transition
        assert c.status == frm

    def test_rejected_is_a_true_terminal_state(self):
        assert ALLOWED_TRANSITIONS["rejected"] == set()

    def test_expired_is_a_true_terminal_state(self):
        assert ALLOWED_TRANSITIONS["expired"] == set()

    def test_can_transition_matches_table_directly(self):
        for frm in ALL_STATUSES:
            for to in ALL_STATUSES:
                expected = to in ALLOWED_TRANSITIONS.get(frm, set())
                assert can_transition(frm, to) == expected

    def test_can_transition_unknown_from_status_returns_false(self):
        assert can_transition("not_a_real_status", "proposed") is False


# ---------------------------------------------------------------------------
# initialize_candidate() — the creation path, must never trust incoming status
# ---------------------------------------------------------------------------

class TestInitializeCandidate:
    def test_forces_proposed_regardless_of_input(self):
        # Deliberately construct a candidate CLAIMING to already be "promoted" --
        # this is exactly the v1 bypass scenario (a crafted/resubmitted payload).
        c = _candidate(status="promoted")
        result = initialize_candidate(c)
        assert result.status == "proposed"

    def test_forces_proposed_even_when_claiming_terminal_state(self):
        c = _candidate(status="rejected")
        result = initialize_candidate(c)
        assert result.status == "proposed"

    def test_returns_same_object_mutated_in_place(self):
        c = _candidate(status="quarantined")
        result = initialize_candidate(c)
        assert result is c
        assert c.status == "proposed"


# ---------------------------------------------------------------------------
# apply_decision() — audit trail side effects
# ---------------------------------------------------------------------------

class TestApplyDecisionAudit:
    def test_creates_audit_block_if_absent(self):
        c = _candidate(status="proposed")
        assert c.audit is None
        updated, _ = apply_decision(c, "promoted", reason="looks good", reviewer="dave")
        assert updated.audit is not None
        assert updated.audit.decision_by == "dave"
        assert updated.audit.decision_reason == "looks good"
        assert updated.audit.reviewer == "dave"

    def test_defaults_decision_by_to_governor_when_no_reviewer(self):
        c = _candidate(status="proposed")
        updated, _ = apply_decision(c, "quarantined")
        assert updated.audit.decision_by == "governor"
        assert updated.audit.reviewer is None

    def test_decision_object_fields_match_transition(self):
        c = _candidate(status="under_review")
        _, decision = apply_decision(c, "rejected", reason="bad provenance", reviewer="dave")
        assert decision.memory_id == "mem-sm-001"
        assert decision.previous_status == "under_review"
        assert decision.new_status == "rejected"
        assert decision.reason == "bad provenance"
        assert decision.reviewer == "dave"


# ---------------------------------------------------------------------------
# Codebase-wide enforcement: status may ONLY be assigned inside this module.
# This is what actually proves Architecture §10 test #1's intent at this
# phase -- no other file, now or added later, may bypass the state machine
# by directly setting `.status =` on a candidate.
# ---------------------------------------------------------------------------

class TestNoDirectStatusAssignmentOutsideStateMachine:
    def test_status_field_never_assigned_outside_state_machine(self):
        app_dir = Path(__file__).parent.parent / "app"
        assignment_pattern = re.compile(r"\.status\s*=(?!=)")  # "=" but not "=="
        violations = []
        for py_file in app_dir.glob("*.py"):
            if py_file.name == "state_machine.py":
                continue
            text = py_file.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if assignment_pattern.search(line):
                    violations.append(f"{py_file.name}:{lineno}: {line.strip()}")
        assert violations == [], (
            "Direct '.status =' assignment found outside state_machine.py "
            f"(bypasses transition validation): {violations}"
        )

    def test_state_machine_itself_is_the_only_file_assigning_status(self):
        app_dir = Path(__file__).parent.parent / "app"
        assignment_pattern = re.compile(r"\.status\s*=(?!=)")
        sm_file = app_dir / "state_machine.py"
        text = sm_file.read_text()
        matches = [
            f"{lineno}: {line.strip()}"
            for lineno, line in enumerate(text.splitlines(), start=1)
            if assignment_pattern.search(line)
        ]
        # We expect exactly the two intentional assignments:
        # initialize_candidate()'s forced "proposed", and apply_decision()'s new_status.
        assert len(matches) == 2, f"Expected exactly 2 status assignments in state_machine.py, found: {matches}"
