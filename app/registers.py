import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .models import GovernedMemoryCandidate

# Registers live inside this application's own repo (Architecture §8.1) --
# not a separately managed external location.
REGISTERS_DIR = Path(__file__).resolve().parent.parent / "registers"
CANDIDATES_DIR = REGISTERS_DIR / "candidates"
REGISTER_FILE = REGISTERS_DIR / "register-memory-governor.md"

REGISTER_HEADER = (
    "| doc-id | filename | folder | type | state | date-registered | "
    "date-state-changed | superseded-by | notes |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_payload(candidate: GovernedMemoryCandidate) -> Path:
    """
    Writes the full JSON payload for a candidate -- every field, not just
    the register's own nine columns (Architecture §8.2, the fix for the
    register-alone-is-insufficient defect: content.text has no column in
    the generic schema). Overwrites if it already exists, since a
    candidate's fields (status, valid_until, etc.) can legitimately change
    after first being registered.
    """
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = CANDIDATES_DIR / f"{candidate.memory_id}.json"
    payload_path.write_text(candidate.model_dump_json(indent=2))
    return payload_path


def _read_register_rows() -> List[List[str]]:
    """Returns existing register rows as lists of 9 cell strings, header/separator excluded."""
    if not REGISTER_FILE.exists():
        return []
    rows = []
    for line in REGISTER_FILE.read_text().splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "doc-id":  # header row
            continue
        if len(cells) == 9:
            rows.append(cells)
    return rows


def _write_register_rows(rows: List[List[str]]) -> None:
    REGISTERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REGISTER_FILE, "w") as f:
        f.write(REGISTER_HEADER)
        for row in rows:
            f.write("| " + " | ".join(row) + " |\n")


def append_or_update_register_row(candidate: GovernedMemoryCandidate) -> None:
    """
    Adds a new row for this candidate, or updates its existing row in
    place if one already exists (e.g. a later status change on an
    already-registered candidate). date-registered is preserved across
    updates; date-state-changed only advances when something other than
    the timestamp itself has genuinely changed (Step 7.3 idempotency fix
    -- see below). The existing
    superseded-by value (if any) is preserved -- this function never
    clears it; only update_supersession_pointer() below ever sets it.
    """
    rows = _read_register_rows()
    existing_idx = next((i for i, r in enumerate(rows) if r[0] == candidate.memory_id), None)

    now = _now_iso()
    notes = f"project={candidate.owning_project}; writer={candidate.provenance.writer_identity}"

    if existing_idx is not None:
        existing = rows[existing_idx]
        # Idempotency fix (Step 7.3): only advance date-state-changed if
        # something OTHER than the timestamp itself has genuinely changed.
        # Without this, a retried call (e.g. after a failed git push)
        # rewrites the row with a fresh timestamp every attempt, making
        # `git status` see a "real" change on every retry and committing
        # near-duplicate commits for a single logical write.
        unchanged = (
            existing[3] == candidate.memory_type
            and existing[4] == candidate.status
            and existing[8] == notes
        )
        date_registered = existing[5]
        date_state_changed = existing[6] if unchanged else now
        superseded_by = existing[7]

        if unchanged:
            return  # genuinely nothing to write -- do not touch the file at all

        new_row = [
            candidate.memory_id,
            f"candidates/{candidate.memory_id}.json",
            "registers/candidates/",
            candidate.memory_type,
            candidate.status,
            date_registered,
            date_state_changed,
            superseded_by,
            notes,
        ]
        rows[existing_idx] = new_row
    else:
        new_row = [
            candidate.memory_id,
            f"candidates/{candidate.memory_id}.json",
            "registers/candidates/",
            candidate.memory_type,
            candidate.status,
            now,
            now,
            "",
            notes,
        ]
        rows.append(new_row)

    _write_register_rows(rows)


def update_supersession_pointer(old_memory_id: str, new_memory_id: str) -> None:
    """
    Fills in the OLD candidate's superseded-by column -- the row is never
    deleted, only annotated (this project's standing "never delete, only
    retire" rule). No-ops silently if the old row doesn't exist yet.
    """
    rows = _read_register_rows()
    for row in rows:
        if row[0] == old_memory_id:
            row[7] = new_memory_id
            break
    _write_register_rows(rows)


def register_candidate(candidate: GovernedMemoryCandidate) -> Path:
    """
    Single entry point (Architecture §8.2): writes the JSON payload and the
    register row/update together for one candidate. If this candidate
    supersedes another, also updates the old candidate's superseded-by
    pointer. Does NOT commit or push -- that's the worker's job (Step 7.2).
    """
    payload_path = write_payload(candidate)
    append_or_update_register_row(candidate)
    if candidate.supersedes_id:
        update_supersession_pointer(candidate.supersedes_id, candidate.memory_id)
    return payload_path


def commit_and_push_register_changes(memory_id: str) -> bool:
    """
    Commits and pushes ONLY the registers/ directory -- deliberately
    scoped, NEVER `git add -A` or `git commit -a`. This repo's own working
    tree has, in practice, held large amounts of unrelated, unfinished
    code sitting uncommitted at the same time real register writes need
    to happen (confirmed directly earlier this session) -- an unscoped
    commit here would silently sweep that unrelated work in. Scoping to
    registers/ is not a stylistic preference, it's a correctness
    requirement for this specific repo's real working pattern.

    Returns True if a commit+push happened, False if there was genuinely
    nothing to commit (e.g. re-registering a candidate with no actual
    field change) -- not an error, a legitimate no-op.

    Raises subprocess.CalledProcessError on any real git failure (network
    unreachable, remote rejected, etc.) -- with_retry() (Step 4.3) is what
    catches this, applies backoff, and eventually surfaces a permanent
    failure after 5 attempts, exactly as Architecture §8.3 specifies.
    Never catches and swallows a git error here.
    """
    repo_root = REGISTERS_DIR.parent

    status = subprocess.run(
        ["git", "status", "--porcelain", "registers/"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    if not status.stdout.strip():
        return False  # genuinely nothing changed -- not a failure

    subprocess.run(["git", "add", "registers/"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"chore(register): {memory_id}"],
        cwd=repo_root, check=True,
    )
    subprocess.run(["git", "push"], cwd=repo_root, check=True)
    return True
