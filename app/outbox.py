from typing import Optional

from .models import GovernedMemoryCandidate
from .storage import get_connection, _serialize

NOTIFY_CHANNEL = "memory_governor_events"

VALID_EVENT_TYPES = frozenset({
    "candidate_created", "contradiction_detected", "review_requested",
    "review_resolved", "candidate_superseded", "expiry_due",
    "scheduled_consolidation", "register_write", "gbrain_write",
})


def _validate_event_type(event_type: str) -> None:
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"Unknown event_type '{event_type}'. Must be one of: {sorted(VALID_EVENT_TYPES)}"
        )


def emit_event(cur, event_type: str, memory_id: Optional[str], payload: dict) -> int:
    """
    Low-level, composable primitive. Takes an ALREADY-OPEN cursor (does
    NOT open its own connection, does NOT commit) so the caller can
    include this insert inside a larger transaction. Issues NOTIFY via
    pg_notify() (transactional -- Postgres queues it and only delivers
    after the enclosing transaction commits, so a rolled-back transaction
    never notifies anyone of an event that didn't actually happen).

    Returns the new event_id. Caller is responsible for the eventual
    conn.commit().
    """
    _validate_event_type(event_type)
    from .storage import _json_or_null
    cur.execute(
        """
        INSERT INTO memory_events (event_type, memory_id, payload)
        VALUES (%s, %s, %s)
        RETURNING event_id
        """,
        (event_type, memory_id, _json_or_null(payload)),
    )
    event_id = cur.fetchone()["event_id"]
    cur.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, str(event_id)))
    return event_id


def insert_candidate_and_emit_event(
    candidate: GovernedMemoryCandidate,
    payload: Optional[dict] = None,
) -> int:
    """
    Atomic: inserts a NEW candidate and a 'candidate_created' event in ONE
    transaction, one commit. Either both happen or neither does -- this is
    what proves Architecture §4's "same transaction" requirement, which
    calling storage.insert_candidate() followed by a separate emit_event()
    call cannot guarantee (see design note above).
    """
    data = _serialize(candidate)
    columns = ", ".join(data.keys())
    placeholders = ", ".join(f"%({k})s" for k in data.keys())
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO memory_candidates ({columns}) VALUES ({placeholders})",
                data,
            )
            event_id = emit_event(
                cur, "candidate_created", candidate.memory_id,
                payload or {"memory_id": candidate.memory_id, "owning_project": candidate.owning_project},
            )
        conn.commit()
    return event_id


def update_candidate_and_emit_event(
    candidate: GovernedMemoryCandidate,
    event_type: str,
    payload: Optional[dict] = None,
) -> int:
    """
    Atomic: updates an EXISTING candidate (post state_machine.apply_decision())
    and emits an event in ONE transaction, one commit. Raises ValueError,
    with the transaction rolled back and NO event row created, if
    memory_id does not already exist -- mirroring storage.update_candidate()'s
    own existence guard, but atomically paired with the event this time.
    """
    data = _serialize(candidate)
    set_clause = ", ".join(f"{k} = %({k})s" for k in data.keys() if k != "memory_id")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE memory_candidates SET {set_clause} WHERE memory_id = %(memory_id)s",
                data,
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise ValueError(f"Cannot update: memory_id '{candidate.memory_id}' does not exist")
            event_id = emit_event(
                cur, event_type, candidate.memory_id,
                payload or {"memory_id": candidate.memory_id, "status": candidate.status},
            )
        conn.commit()
    return event_id


def supersede_candidates_and_emit_event(
    old_candidate: GovernedMemoryCandidate,
    new_candidate: GovernedMemoryCandidate,
    payload: Optional[dict] = None,
) -> int:
    """
    Atomic: updates BOTH the old candidate (already transitioned to
    'superseded' via state_machine.apply_decision(), with valid_until/
    valid_until_reason set by the caller) AND the new candidate (with
    supersedes_id set) AND emits ONE 'candidate_superseded' event -- all
    in a single transaction, single commit. Prevents the two-write
    non-atomicity gap flagged in this step's design note: either both
    candidates update together, or neither does.

    Both candidates must already exist (raises psycopg's normal errors
    if either update affects 0 rows -- mirrors update_candidate_and_emit_event's
    existence guard, applied to both rows here).
    """
    old_data = _serialize(old_candidate)
    new_data = _serialize(new_candidate)

    old_set = ", ".join(f"{k} = %(old_{k})s" for k in old_data.keys() if k != "memory_id")
    new_set = ", ".join(f"{k} = %(new_{k})s" for k in new_data.keys() if k != "memory_id")

    params = {f"old_{k}": v for k, v in old_data.items()}
    params.update({f"new_{k}": v for k, v in new_data.items()})

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE memory_candidates SET {old_set} WHERE memory_id = %(old_memory_id)s",
                params,
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise ValueError(f"Cannot supersede: old memory_id '{old_candidate.memory_id}' does not exist")

            cur.execute(
                f"UPDATE memory_candidates SET {new_set} WHERE memory_id = %(new_memory_id)s",
                params,
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise ValueError(f"Cannot supersede: new memory_id '{new_candidate.memory_id}' does not exist")

            event_id = emit_event(
                cur, "candidate_superseded", old_candidate.memory_id,
                payload or {
                    "old_memory_id": old_candidate.memory_id,
                    "new_memory_id": new_candidate.memory_id,
                },
            )
        conn.commit()
    return event_id
