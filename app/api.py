import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, model_validator

from .models import (
    GovernedMemoryCandidate, Scope, Subject, Content, Timestamps,
    Confidence, Contradiction, Policy, SourceType, MemoryType,
    Visibility, Retention, Authority, Method,
)
import os
from pathlib import Path

from .provenance import build_provenance

# Step 6.1: explicit allowlist of directories a source_ref path submission
# may read from. Resolved via Path.resolve() before comparison (below) to
# close path-traversal bypasses (../../etc/passwd-style tricks) -- a plain
# string-prefix check would NOT be sufficient on its own.
ALLOWED_SOURCE_ROOTS = [
    Path("/mnt/c/dev").resolve(),
    Path("/home/dev-p/memory-governor").resolve(),
]


def _is_path_allowed(path: Path) -> bool:
    resolved = path.resolve()
    return any(
        resolved == root or root in resolved.parents
        for root in ALLOWED_SOURCE_ROOTS
    )


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _read_text_file(path: Path) -> Optional[str]:
    """
    Returns decoded UTF-8 text, or None if the file can't be decoded as
    text (Step 6.1 design: binary/unreadable files are skipped with a
    per-file error, not a fatal failure for the whole batch).
    """
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def resolve_source_ref_to_files(source_ref: str) -> List[Path]:
    """
    Architecture §5: when content.text is absent, source_ref is read as a
    filesystem path. A single file yields one path; a directory yields
    every readable text file within it (recursive, sorted alphabetically
    for reproducibility, hidden files/dirs skipped -- e.g. .git, .venv).

    Raises FileNotFoundError if source_ref doesn't resolve to anything on
    disk, and PermissionError if it resolves outside ALLOWED_SOURCE_ROOTS
    (Step 6.1 design note #3) -- both are caught and mapped to HTTP status
    codes by the caller, not handled here.
    """
    path = Path(source_ref)
    # Allowlist check FIRST, before existence -- prevents an "existence
    # oracle" where an out-of-bounds path's response code (403 vs 404)
    # would otherwise leak whether that path exists on the server's
    # filesystem. Every out-of-bounds path now uniformly returns 403,
    # found and fixed during Step 6.1 live testing.
    if not _is_path_allowed(path):
        raise PermissionError(f"source_ref '{source_ref}' is outside the allowed directories")
    if not path.exists():
        raise FileNotFoundError(f"source_ref '{source_ref}' does not exist")

    if path.is_file():
        return [path]

    files = sorted(
        p for p in path.rglob("*")
        if p.is_file() and not _is_hidden(p.relative_to(path))
    )
    return files
from .state_machine import initialize_candidate, apply_decision
from .policy import decide_path
from .outbox import insert_candidate_and_emit_event
from .audit import append_audit_entry

def reject_nul_bytes_in_path_and_query(request: Request) -> None:
    """
    Global guard applied to EVERY route on this router (see APIRouter(...)
    below). PostgreSQL text columns cannot store NUL bytes at the
    wire-protocol level -- any raw string reaching a query via psycopg
    raises an uncaught psycopg.DataError, surfacing as a bare 500.

    Found via Schemathesis fuzzing (Step 5.3): patching individual
    endpoints one at a time (create_project, then get_memory, then
    list_memory...) kept missing call sites. This checks path parameters
    and query parameters -- the two places raw string input reaches an
    endpoint before Pydantic request-body validation even runs -- for
    every route at once, rather than requiring every future endpoint to
    remember this defensively on its own.

    Does NOT check the JSON request body -- Pydantic string fields in
    request models still need their own validation if they reach a raw
    SQL query unvalidated (as create_project's fields did). This guard
    covers the URL itself; body fields remain each endpoint's own
    responsibility, same as the explicit check already added to
    create_project.
    """
    for value in request.path_params.values():
        if isinstance(value, str) and "\x00" in value:
            raise HTTPException(status_code=400, detail="Path parameters cannot contain NUL bytes")
    for value in request.query_params.values():
        if "\x00" in value:
            raise HTTPException(status_code=400, detail="Query parameters cannot contain NUL bytes")


router = APIRouter(dependencies=[Depends(reject_nul_bytes_in_path_and_query)])

CREDENTIAL_HEADER_NAME = "X-Governor-Credential"


def _contains_nul_byte(value) -> bool:
    """
    Recursively checks strings, dict keys/values, and list/tuple items for
    a NUL byte. Used by NoNulBytesModel below to close the gap the
    router-wide path/query dependency explicitly does NOT cover (request
    JSON bodies) -- found via Schemathesis fuzzing two separate times in
    Step 5.3 (POST /memory/policy's "reason" field, and a nested field in
    a stateful POST /memory/candidate test case).
    """
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul_byte(k) or _contains_nul_byte(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_nul_byte(item) for item in value)
    return False


class NoNulBytesModel(BaseModel):
    """
    Shared base for every request body model in this file. Recursively
    rejects NUL bytes anywhere in the payload, including nested objects
    (e.g. content.structured, proposed_thresholds) that a per-field string
    check would miss. Raises a standard Pydantic ValidationError -- FastAPI
    turns this into a 422, a code every endpoint already documents, so
    this closes the gap with zero new response-code declarations needed.
    """
    @model_validator(mode="after")
    def _reject_nul_bytes(self):
        if _contains_nul_byte(self.model_dump()):
            raise ValueError("Request fields cannot contain NUL bytes")
        return self

DECISION_PATH_TO_STATUS = {
    "auto_promote": "promoted",
    "quarantine": "quarantined",
    "reject": "rejected",
    "needs_review": "under_review",
}


def get_presented_credential(
    x_governor_credential: str = Header(..., alias=CREDENTIAL_HEADER_NAME)
) -> str:
    """
    Extracts the raw credential from the request header. Deliberately does
    NOT resolve it to a writer_identity here -- that happens exactly once,
    inside build_provenance() (Step 3.1), so credential resolution logic
    lives in one place only.
    """
    return x_governor_credential


class CandidateSubmissionRequest(NoNulBytesModel):
    """
    API-layer request shape for POST /memory/candidate. Deliberately
    NARROWER than GovernedMemoryCandidate (Architecture §1.1) -- fields
    the server must compute (writer_identity, lineage_hash, timestamps,
    policy, status) have NO field here for a caller to populate, mirroring
    build_provenance()'s own structural guarantee (Step 3.1) at this layer.
    """
    memory_id: Optional[str] = None
    owning_project: str
    visibility: Visibility
    retention: Retention
    authority: Optional[Authority] = None
    shared_with: List[str] = []
    memory_type: MemoryType
    subject: Optional[Subject] = None
    content: Optional[Content] = None
    source_type: SourceType
    source_ref: str
    policy_version: str
    derived_from: Optional[List[str]] = None
    confidence: Confidence
    contradiction: Optional[Contradiction] = None


def _build_candidate_from_request(
    req: CandidateSubmissionRequest, presented_credential: str
) -> GovernedMemoryCandidate:
    memory_id = req.memory_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    provenance = build_provenance(
        source_type=req.source_type,
        source_ref=req.source_ref,
        presented_credential=presented_credential,
        policy_version=req.policy_version,
        derived_from=req.derived_from,
    )

    scope = Scope(
        visibility=req.visibility,
        retention=req.retention,
        authority=req.authority,
        owning_project=req.owning_project,
        shared_with=req.shared_with,
    )

    return GovernedMemoryCandidate(
        memory_id=memory_id,
        owning_project=req.owning_project,
        scope=scope,
        memory_type=req.memory_type,
        subject=req.subject,
        content=req.content,
        provenance=provenance,
        timestamps=Timestamps(created_at=now, updated_at=now),
        confidence=req.confidence,
        policy=Policy(risk_level="low", decision_path="auto_promote"),  # placeholder; decide_path() overwrites
        status="proposed",  # placeholder; initialize_candidate() forces this regardless
        contradiction=req.contradiction,
    )


@router.post("/memory/candidate", status_code=201, responses={
    400: {"description": "Malformed request body"},
    401: {"description": "Invalid or unrecognized credential"},
    404: {"description": "owning_project or source_ref does not exist"},
})
def submit_candidate(
    req: CandidateSubmissionRequest,
    presented_credential: str = Depends(get_presented_credential),
):
    """
    Step 6.1: if req.content is omitted, source_ref is resolved as a
    filesystem path (Architecture §5). A single file submits exactly as
    before (same single-object response shape, no change for existing
    callers). A directory submits ONE CANDIDATE PER FILE -- a genuine,
    visible response-shape change for this case only: returns
    {"submitted": [...]}, one result entry per file, each independently
    routed through the full decide_path() pipeline. A file that fails to
    decode as text is skipped with its own error entry, not a fatal
    failure for the whole batch.
    """
    if req.content is None:
        try:
            files = resolve_source_ref_to_files(req.source_ref)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

        if len(files) == 1 and Path(req.source_ref).is_file():
            text = _read_text_file(files[0])
            if text is None:
                raise HTTPException(status_code=400, detail=f"'{req.source_ref}' could not be decoded as text")
            single_req = req.model_copy(deep=True)
            single_req.content = Content(text=text)
            return _submit_single_candidate(single_req, presented_credential)

        results = []
        for file_path in files:
            text = _read_text_file(file_path)
            file_req = req.model_copy(deep=True)
            file_req.source_ref = str(file_path)
            if text is None:
                results.append({"source_ref": str(file_path), "error": "could not be decoded as text"})
                continue
            file_req.content = Content(text=text)
            try:
                result = _submit_single_candidate(file_req, presented_credential)
                result["source_ref"] = str(file_path)
                results.append(result)
            except HTTPException as exc:
                results.append({"source_ref": str(file_path), "error": exc.detail, "status_code": exc.status_code})
            except Exception as exc:
                # Widened deliberately: a single file's unexpected failure
                # (not just an HTTPException) must not abort the rest of a
                # folder batch -- the whole point of per-file isolation
                # (Step 6.1 design). Logged with the real exception type so
                # this is diagnosable, never silently swallowed.
                results.append({
                    "source_ref": str(file_path),
                    "error": f"{type(exc).__name__}: {exc}",
                    "status_code": 500,
                })
        return {"submitted": results}

    return _submit_single_candidate(req, presented_credential)


def _submit_single_candidate(
    req: CandidateSubmissionRequest,
    presented_credential: str,
) -> dict:
    """
    Architecture §3.2's core submission endpoint. Pipeline:
    1. Resolve credential -> writer_identity (build_provenance, Step 3.1)
    2. Construct the full candidate
    3. initialize_candidate() -- forces status to "proposed" (Step 1.2)
    4. decide_path() -- the deterministic policy engine (Step 1.3)
    5. apply_decision() -- validated transition to the decided status (Step 1.2)
    6. insert_candidate_and_emit_event() -- atomic persist + event (Step 4.1)
    7. append_audit_entry() -- permanent decision history (Step 3.2)
    """
    from .storage import get_project

    if not get_project(req.owning_project):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{req.owning_project}' does not exist -- register it via POST /memory/projects first",

        )

    try:
        candidate = _build_candidate_from_request(req, presented_credential)
    except PermissionError:
        raise HTTPException(status_code=401, detail="Invalid or unrecognized credential")

    candidate = initialize_candidate(candidate)
    computed_policy = decide_path(candidate)
    candidate.policy = computed_policy

    if computed_policy.decision_path == "needs_review":
        from .models import ReviewPresentation
        validation_parts = [f"risk_level={computed_policy.risk_level}",
                             f"confidence={candidate.confidence.score}"]
        if candidate.contradiction and candidate.contradiction.detected:
            validation_parts.append(
                f"contradiction_detected=true cross_project={candidate.contradiction.cross_project}"
            )
        ramifications = (
            f"Supersedes {candidate.supersedes_id}" if candidate.supersedes_id
            else "No supersession or cross-project sharing effects"
        )
        candidate.review = ReviewPresentation(
            recommendation=(
                f"Policy engine flagged this {candidate.memory_type} candidate for "
                f"human review (risk={computed_policy.risk_level}). No automatic action taken."
            ),
            validation="; ".join(validation_parts),
            ramifications=ramifications,
        )

    target_status = DECISION_PATH_TO_STATUS[computed_policy.decision_path]
    updated, decision = apply_decision(
        candidate, target_status,
        reason=f"policy engine: {computed_policy.decision_path}",
        reviewer=None,
    )

    event_id = insert_candidate_and_emit_event(
        updated,
        payload={
            "memory_id": updated.memory_id,
            "owning_project": updated.owning_project,
            "status": updated.status,
        },
    )
    append_audit_entry(decision, policy_version=req.policy_version)

    return {
        "memory_id": updated.memory_id,
        "status": updated.status,
        "decision_path": computed_policy.decision_path,
        "risk_level": computed_policy.risk_level,
        "event_id": event_id,
    }


from .storage import get_candidate, list_candidates
from .audit import get_audit_history


@router.get("/memory/review-queue")
def get_review_queue(
    presented_credential: str = Depends(get_presented_credential),
) -> List[dict]:
    """
    Architecture §3.2/§6: the data source for the OpenCode Q&A sheet.
    Returns STRUCTURED data -- rendering into the "## Memory Review
    Required" text format is OpenCode's job (Phase 10), not this
    endpoint's. Every entry here has status == under_review by
    construction (only decide_path's needs_review path sets that status),
    so review should always be non-null -- but this is read defensively
    in case a future code path ever reaches under_review without going
    through submit_candidate()'s review-population fix (Part 3 of this step).
    """
    candidates = list_candidates(status="under_review")
    return [
        {
            "memory_id": c.memory_id,
            "memory_type": c.memory_type,
            "owning_project": c.owning_project,
            "risk_level": c.policy.risk_level,
            "review": c.review.model_dump(mode="json") if c.review else None,
        }
        for c in candidates
    ]




@router.get("/memory/{memory_id}", responses={
    400: {"description": "Path parameters cannot contain NUL bytes"},
    404: {"description": "Memory not found"},
})
def get_memory(
    memory_id: str,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    candidate = get_candidate(memory_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return candidate.model_dump(mode="json")


@router.get("/memory", responses={
    400: {"description": "Query parameters cannot contain NUL bytes"},
})
def list_memory(
    owning_project: Optional[str] = None,
    status: Optional[str] = None,
    memory_type: Optional[str] = None,
    presented_credential: str = Depends(get_presented_credential),
) -> List[dict]:
    candidates = list_candidates(
        owning_project=owning_project, status=status, memory_type=memory_type
    )
    return [c.model_dump(mode="json") for c in candidates]


@router.get("/memory/{memory_id}/provenance", responses={
    400: {"description": "Path parameters cannot contain NUL bytes"},
    404: {"description": "Memory not found"},
})
def get_provenance(
    memory_id: str,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """
    Full decision history (Architecture §3.2), backed by audit_log
    (Step 3.2) -- NOT memory_candidates.audit, which only ever holds the
    most recent decision. 404s if the candidate itself doesn't exist,
    distinct from a candidate that exists but has an empty history
    (which returns 200 with an empty list -- get_audit_history()'s own
    documented behavior).
    """
    candidate = get_candidate(memory_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    history = get_audit_history(memory_id)
    return {
        "memory_id": memory_id,
        "current_status": candidate.status,
        "history": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
            for row in history
        ],
    }


from typing import Literal
from .outbox import update_candidate_and_emit_event

REVIEW_DECISION_TO_STATUS = {
    "promote": "promoted",
    "quarantine": "quarantined",
    "reject": "rejected",
}


class ReviewDecisionRequest(NoNulBytesModel):
    """
    Request body for POST /memory/{memory_id}/review. `reviewer` is
    REQUIRED (unlike state_machine.apply_decision()'s optional parameter)
    -- a review resolution without an identified human defeats the point
    of a hard-stop review. See this step's design note: reviewer is
    self-reported, NOT cryptographically verified like writer_identity.
    """
    decision: Literal["promote", "quarantine", "reject"]
    reason: Optional[str] = None
    reviewer: str


@router.post("/memory/{memory_id}/review", responses={
    400: {"description": "Malformed request body, path parameters with NUL bytes, or invalid state transition"},
    404: {"description": "Memory not found"},
})
def review_memory(
    memory_id: str,
    req: ReviewDecisionRequest,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """
    Architecture §3.2: the ONLY path that may resolve a needs_review
    candidate. Reuses the same atomic outbox path as submission
    (update_candidate_and_emit_event, Step 4.1) rather than plain
    storage.update_candidate(), so the status change and the
    review_resolved event (added this step, Part 3) commit together or
    not at all -- consistent with every other durable transition in this
    codebase.
    """
    candidate = get_candidate(memory_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    target_status = REVIEW_DECISION_TO_STATUS[req.decision]

    try:
        updated, decision = apply_decision(
            candidate, target_status, reason=req.reason, reviewer=req.reviewer
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    event_id = update_candidate_and_emit_event(
        updated, "review_resolved",
        payload={"memory_id": memory_id, "new_status": target_status, "reviewer": req.reviewer},
    )
    append_audit_entry(decision, policy_version=candidate.provenance.policy_version)

    return {
        "memory_id": memory_id,
        "previous_status": decision.previous_status,
        "new_status": decision.new_status,
        "event_id": event_id,
    }


from datetime import datetime, timezone as _timezone
from .outbox import supersede_candidates_and_emit_event


class SupersedeRequest(NoNulBytesModel):
    new_memory_id: str
    reason: Optional[str] = None
    reviewer: Optional[str] = None


@router.post("/memory/{memory_id}/supersede", responses={
    400: {"description": "Malformed request body, path parameters with NUL bytes, self-supersession, or invalid state transition"},
    404: {"description": "Old or new memory not found"},
})
def supersede_memory(
    memory_id: str,
    req: SupersedeRequest,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """
    Architecture §3.2. Closes the v1 defect named explicitly in
    Architecture §3.2's own endpoint description: valid_until/
    valid_until_reason were modeled in the schema (Step 1.1) but never
    actually SET by any operation until this endpoint. Uses
    supersede_candidates_and_emit_event() (this step) for genuine
    two-candidate atomicity -- see this step's design note.
    """
    old_candidate = get_candidate(memory_id)
    if old_candidate is None:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")

    new_candidate = get_candidate(req.new_memory_id)
    if new_candidate is None:
        raise HTTPException(status_code=404, detail=f"Successor memory '{req.new_memory_id}' not found")

    if old_candidate.memory_id == new_candidate.memory_id:
        raise HTTPException(status_code=400, detail="A candidate cannot supersede itself")

    try:
        updated_old, decision = apply_decision(
            old_candidate, "superseded",
            reason=req.reason or f"Superseded by {req.new_memory_id}",
            reviewer=req.reviewer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = datetime.now(_timezone.utc)
    updated_old.timestamps.valid_until = now
    updated_old.timestamps.valid_until_reason = "superseded"

    new_candidate.supersedes_id = old_candidate.memory_id

    event_id = supersede_candidates_and_emit_event(updated_old, new_candidate)
    append_audit_entry(decision, policy_version=old_candidate.provenance.policy_version)

    return {
        "old_memory_id": old_candidate.memory_id,
        "new_memory_id": new_candidate.memory_id,
        "old_status": updated_old.status,
        "event_id": event_id,
    }


from .storage import get_project, set_project_dormant, insert_project as storage_insert_project


class QuarantineRequest(NoNulBytesModel):
    memory_id: str
    reason: Optional[str] = None
    reviewer: Optional[str] = None


@router.post("/memory/quarantine", responses={
    400: {"description": "Malformed request body, or invalid state transition"},
    404: {"description": "Memory not found"},
})
def quarantine_memory(
    req: QuarantineRequest,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """
    Architecture §3.2: manual quarantine of an EXISTING candidate --
    distinct from the automatic quarantine decide_path() can already
    produce at submission time. Reuses the same validated
    apply_decision() + atomic outbox path as every other status change
    in this codebase; ALLOWED_TRANSITIONS (Step 1.2) is what actually
    determines which source statuses this is legal from, so this
    endpoint enforces no additional rule of its own.
    """
    candidate = get_candidate(req.memory_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    try:
        updated, decision = apply_decision(
            candidate, "quarantined", reason=req.reason, reviewer=req.reviewer
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    event_id = update_candidate_and_emit_event(
        updated, "review_requested",
        payload={"memory_id": req.memory_id, "reason": req.reason},
    )
    append_audit_entry(decision, policy_version=candidate.provenance.policy_version)

    return {"memory_id": req.memory_id, "status": updated.status, "event_id": event_id}


class ProjectCreateRequest(NoNulBytesModel):
    project_id: str
    display_name: str


@router.post("/memory/projects", status_code=201, responses={
    400: {"description": "Malformed request body"},
    409: {"description": "Project already exists"},
})
def create_project(
    req: ProjectCreateRequest,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """
    Architecture §3.2 / Scope Doc §7: the new-project onboarding
    mechanism. Deliberately thin -- storage.insert_project() already does
    exactly what's needed (Step 2.2); this endpoint is a pure HTTP wrapper,
    no additional business logic, matching Scope Doc §7's stated intent
    that onboarding costs "no schema migration, no redeploy."

    NUL-byte check happens FIRST, before ANY database call -- a Schemathesis
    finding (Step 5.3) showed the original fix only guarded
    storage.insert_project(), but get_project() (the duplicate check, called
    BEFORE insert_project() in this same function) hits Postgres with the
    identical unvalidated string one call earlier, reproducing the same 500
    via a different code path. Validating at the true entry point closes
    this for both calls at once, rather than chasing it function by function.
    """
    if get_project(req.project_id) is not None:
        raise HTTPException(status_code=409, detail=f"Project '{req.project_id}' already exists")
    storage_insert_project(req.project_id, req.display_name)
    return {"project_id": req.project_id, "display_name": req.display_name, "dormant": False}


class ProjectDormantRequest(NoNulBytesModel):
    dormant: bool


@router.patch("/memory/projects/{project_id}", responses={
    400: {"description": "Malformed request body, or path parameters with NUL bytes"},
    404: {"description": "Project does not exist"},
})
def update_project_dormant(
    project_id: str,
    req: ProjectDormantRequest,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """Architecture §3.2 / Scope Doc §8: the dormancy toggle."""
    try:
        set_project_dormant(project_id, req.dormant)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    project = get_project(project_id)
    return {"project_id": project["project_id"], "dormant": project["dormant"]}


class PolicyProposalRequest(NoNulBytesModel):
    """
    Request body for POST /memory/policy. Deliberately thin: this endpoint
    does NOT write to policy_versions (that's Phase 11's job, per
    Architecture §7 -- applying an approved policy change is part of the
    self-healing loop, not this step). This endpoint only gets a proposal
    through the standard gate: submit -> guaranteed needs_review (policy
    is a HIGH_RISK_TYPES memory_type) -> human resolves via the existing
    POST /memory/{memory_id}/review endpoint, same as any other candidate.
    """
    owning_project: str
    proposed_policy_version: str
    proposed_thresholds: dict
    reason: str
    current_policy_version: str


@router.post("/memory/policy", status_code=201, responses={
    400: {"description": "Malformed request body"},
    401: {"description": "Invalid or unrecognized credential"},
    404: {"description": "owning_project does not exist"},
})
def propose_policy(
    req: PolicyProposalRequest,
    presented_credential: str = Depends(get_presented_credential),
) -> dict:
    """
    Architecture §3.2/§7. Reshapes the proposal into a standard
    CandidateSubmissionRequest with memory_type="policy" and calls
    submit_candidate() DIRECTLY -- not a parallel path, the exact same
    pipeline every other candidate goes through. Because "policy" is in
    HIGH_RISK_TYPES (Step 1.3), decide_path() checks risk level before
    confidence and unconditionally routes this to needs_review regardless
    of the placeholder confidence value below -- confidence is
    structurally inert for this memory_type, not a real uncertainty
    measure (see this step's design note).
    """
    submission = CandidateSubmissionRequest(
        owning_project=req.owning_project,
        visibility="project_local",
        retention="permanent",
        memory_type="policy",
        content=Content(
            text=(
                f"Proposed policy version '{req.proposed_policy_version}': {req.reason}"
            ),
            structured={
                "proposed_policy_version": req.proposed_policy_version,
                "proposed_thresholds": req.proposed_thresholds,
            },
        ),
        source_type="user",
        source_ref="policy-proposal",
        policy_version=req.current_policy_version,
        confidence=Confidence(score=1.0, method="rule_based"),
    )
    return submit_candidate(submission, presented_credential)
