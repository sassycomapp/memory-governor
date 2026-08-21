from typing import List, Optional

from .models import MemoryDecision
from .storage import get_connection


def append_audit_entry(decision: MemoryDecision, policy_version: Optional[str] = None) -> int:
    """
    Writes ONE immutable row to audit_log for a decision that has already
    been validated and applied by state_machine.apply_decision(). This
    function does NOT call apply_decision() itself and does NOT touch
    memory_candidates -- it is purely an append-only historian, matching
    Architecture §9's description exactly ("append immutable decision
    records"). The caller is responsible for calling apply_decision()
    first, then this, together -- see Phase 5's api.py for where that
    pairing is actually enforced at the request level.

    Returns the new row's audit_id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log
                    (memory_id, previous_status, new_status, decision_by,
                     decision_reason, reviewer, policy_version)
                VALUES (%(memory_id)s, %(previous_status)s, %(new_status)s,
                        %(decision_by)s, %(decision_reason)s, %(reviewer)s,
                        %(policy_version)s)
                RETURNING audit_id
                """,
                {
                    "memory_id": decision.memory_id,
                    "previous_status": decision.previous_status,
                    "new_status": decision.new_status,
                    "decision_by": decision.reviewer or "governor",
                    "decision_reason": decision.reason,
                    "reviewer": decision.reviewer,
                    "policy_version": policy_version,
                },
            )
            audit_id = cur.fetchone()["audit_id"]
        conn.commit()
    return audit_id


def get_audit_history(memory_id: str) -> List[dict]:
    """
    Returns the FULL decision history for a candidate, oldest first --
    this is what Architecture §3.2's GET /memory/{memory_id}/provenance
    endpoint (Phase 5) will actually serve. Unlike memory_candidates.audit
    (a single JSONB column that only ever holds the MOST RECENT decision),
    this reflects every decision ever made, in order.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, memory_id, previous_status, new_status,
                       decision_by, decision_reason, reviewer, policy_version,
                       recorded_at
                FROM audit_log
                WHERE memory_id = %s
                ORDER BY recorded_at ASC, audit_id ASC
                """,
                (memory_id,),
            )
            return cur.fetchall()
