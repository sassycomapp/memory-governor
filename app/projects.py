"""
Architecture §3.2 / Scope Doc §7-8: project onboarding and the dormancy
toggle. Extracted from api.py in Step 8.1 to match this codebase's own
established pattern -- every other domain (state transitions, policy
decisions, provenance, registers) already lives in its own module, with
api.py staying a thin HTTP layer. Projects were the one exception.
"""
from fastapi import HTTPException

from .models import NoNulBytesModel
from .storage import get_project, insert_project, set_project_dormant


class ProjectCreateRequest(NoNulBytesModel):
    project_id: str
    display_name: str


class ProjectDormantRequest(NoNulBytesModel):
    dormant: bool


def create_project(req: ProjectCreateRequest) -> dict:
    """
    The new-project onboarding mechanism. Deliberately thin --
    storage.insert_project() already does exactly what's needed (Step
    2.2); no additional business logic, matching Scope Doc §7's stated
    intent that onboarding costs "no schema migration, no redeploy."

    NUL-byte protection happens automatically via NoNulBytesModel
    (inherited by ProjectCreateRequest) before this function ever runs --
    Pydantic validation rejects the request with a 422 at the FastAPI
    layer, so no explicit check is needed here. (Historical note: an
    earlier version of this logic, when it lived in api.py, had a
    hand-written NUL-byte check at this exact point, made redundant once
    the shared validator existed -- see Step 5.3/8.1.)
    """
    if get_project(req.project_id) is not None:
        raise HTTPException(status_code=409, detail=f"Project '{req.project_id}' already exists")
    insert_project(req.project_id, req.display_name)
    return {"project_id": req.project_id, "display_name": req.display_name, "dormant": False}


def update_project_dormant(project_id: str, req: ProjectDormantRequest) -> dict:
    """The dormancy toggle."""
    try:
        set_project_dormant(project_id, req.dormant)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    project = get_project(project_id)
    return {"project_id": project["project_id"], "dormant": project["dormant"]}
