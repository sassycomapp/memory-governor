import os
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .models import GovernedMemoryCandidate


def get_connection() -> psycopg.Connection:
    """
    Opens a connection using MEMORY_GOVERNOR_DATABASE_URL from the
    environment. Request-scoped, opened/closed per call -- fine at this
    project's scale (Phase 4's worker introduces a persistent connection
    for the outbox listener specifically; this remains the simple path
    for everything else).
    """
    database_url = os.environ["MEMORY_GOVERNOR_DATABASE_URL"]
    return psycopg.connect(database_url, row_factory=dict_row)


def insert_project(project_id: str, display_name: str) -> None:
    """
    Minimal project registration, needed here because memory_candidates
    has a NOT NULL FK to projects. Phase 8's projects.py builds the real
    onboarding/dormancy API on top of this same low-level insert.

    Rejects NUL bytes in either field with a clean ValueError -- PostgreSQL
    text columns cannot store NUL bytes at the wire-protocol level
    (psycopg.DataError otherwise), a real defect found via Schemathesis
    fuzzing in Step 5.3.
    """
    if "\x00" in project_id or "\x00" in display_name:
        raise ValueError("project_id and display_name cannot contain NUL bytes")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (project_id, display_name) VALUES (%s, %s)",
                (project_id, display_name),
            )
        conn.commit()


def _json_or_null(value):
    """
    psycopg3 does NOT auto-adapt Python dict/list to JSONB on insert --
    unlike psycopg2, it requires an explicit Json() wrapper, or the insert
    raises ProgrammingError: cannot adapt type 'dict'. None must stay a
    genuine None (-> SQL NULL), never Json(None) (-> the JSON literal
    "null", which is a different, wrong value for a nullable column).
    """
    return Json(value) if value is not None else None


def _serialize(candidate: GovernedMemoryCandidate) -> dict:
    data = candidate.model_dump(mode="json")
    return {
        "memory_id": candidate.memory_id,
        "owning_project": candidate.owning_project,
        "agent_id": candidate.agent_id,
        "session_id": candidate.session_id,
        "scope": _json_or_null(data["scope"]),
        "memory_type": candidate.memory_type,
        "subject": _json_or_null(data["subject"]),
        "content": _json_or_null(data["content"]),
        "provenance": _json_or_null(data["provenance"]),
        "created_at": candidate.timestamps.created_at,
        "updated_at": candidate.timestamps.updated_at,
        "valid_from": candidate.timestamps.valid_from,
        "valid_until": candidate.timestamps.valid_until,
        "valid_until_reason": candidate.timestamps.valid_until_reason,
        "confidence": _json_or_null(data["confidence"]),
        "policy": _json_or_null(data["policy"]),
        "status": candidate.status,
        "supersedes_id": candidate.supersedes_id,
        "contradiction": _json_or_null(data["contradiction"]),
        "review": _json_or_null(data["review"]),
        "audit": _json_or_null(data["audit"]),
    }


def _deserialize(row: dict) -> GovernedMemoryCandidate:
    payload = {
        "memory_id": row["memory_id"],
        "owning_project": row["owning_project"],
        "agent_id": row["agent_id"],
        "session_id": row["session_id"],
        "scope": row["scope"],
        "memory_type": row["memory_type"],
        "subject": row["subject"],
        "content": row["content"],
        "provenance": row["provenance"],
        "timestamps": {
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "valid_until_reason": row["valid_until_reason"],
        },
        "confidence": row["confidence"],
        "policy": row["policy"],
        "status": row["status"],
        "supersedes_id": row["supersedes_id"],
        "contradiction": row["contradiction"],
        "review": row["review"],
        "audit": row["audit"],
    }
    return GovernedMemoryCandidate(**payload)


def insert_candidate(candidate: GovernedMemoryCandidate) -> None:
    """
    Persists a NEW candidate only. memory_id's PRIMARY KEY constraint makes
    this a genuine database-level guarantee against the v1 resubmission
    bypass (Architecture §10 test #1) -- attempting to insert an existing
    memory_id raises psycopg.errors.UniqueViolation. Callers must never
    catch-and-ignore this to force an overwrite; an existing candidate can
    only be changed via update_candidate(), after a validated
    state_machine.apply_decision() call.
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
        conn.commit()


def update_candidate(candidate: GovernedMemoryCandidate) -> None:
    """
    Persists changes to an EXISTING candidate. Raises ValueError if
    memory_id does not already exist -- an update can never silently
    create a row, the mirror guarantee to insert_candidate().
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
        conn.commit()


def get_candidate(memory_id: str) -> Optional[GovernedMemoryCandidate]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM memory_candidates WHERE memory_id = %s", (memory_id,))
            row = cur.fetchone()
    return _deserialize(row) if row is not None else None


def candidate_exists(memory_id: str) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM memory_candidates WHERE memory_id = %s", (memory_id,))
            return cur.fetchone() is not None


def list_candidates(
    owning_project: Optional[str] = None,
    status: Optional[str] = None,
    memory_type: Optional[str] = None,
) -> list:
    """
    Filterable listing, backing GET /memory (Architecture §3.2). All
    filters are optional and AND-combined. Returns GovernedMemoryCandidate
    objects, oldest-created first.
    """
    conditions = []
    params = {}
    if owning_project is not None:
        conditions.append("owning_project = %(owning_project)s")
        params["owning_project"] = owning_project
    if status is not None:
        conditions.append("status = %(status)s")
        params["status"] = status
    if memory_type is not None:
        conditions.append("memory_type = %(memory_type)s")
        params["memory_type"] = memory_type

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM memory_candidates {where_clause} ORDER BY created_at ASC",
                params,
            )
            rows = cur.fetchall()

    return [_deserialize(row) for row in rows]


def get_project(project_id: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE project_id = %s", (project_id,))
            return cur.fetchone()


def set_project_dormant(project_id: str, dormant: bool) -> None:
    """
    Toggles a project's dormant flag (Scope Doc §8). Raises ValueError if
    the project does not exist -- mirrors update_candidate()'s existence
    guard pattern used throughout this codebase.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE projects SET dormant = %s WHERE project_id = %s",
                (dormant, project_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise ValueError(f"Cannot update: project_id '{project_id}' does not exist")
        conn.commit()
