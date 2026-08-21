from pydantic import BaseModel, Field, constr, confloat
from typing import Optional, Literal, List, Union
from datetime import datetime

Visibility = Literal["project_local", "shared_all", "shared_specific"]
Retention = Literal["ephemeral", "session", "project", "long_term", "permanent"]
Authority = Literal["self_observed", "user_asserted", "tool_observed", "policy_derived", "human_reviewed"]
MemoryType = Literal[
    "fact", "preference", "policy", "episode", "trace",
    "task_state", "relationship", "summary", "procedural"
]
SourceType = Literal["user", "agent_trace", "tool_result", "file", "ticket", "document", "human_review", "external"]
RiskLevel = Literal["low", "medium", "high", "critical"]
DecisionPath = Literal["auto_promote", "quarantine", "reject", "needs_review", "supersede"]
Status = Literal["proposed", "promoted", "quarantined", "rejected", "superseded", "expired", "under_review"]
Resolution = Literal["none", "closed_by_new_fact", "manual_review", "merged", "deprecated"]
Method = Literal["rule_based", "model_based", "human_verified", "hybrid"]
Action = Literal["promote", "quarantine", "reject", "supersede", "expire", "reconcile"]
ValidUntilReason = Literal["superseded", "ttl_expired", "manual"]


class Scope(BaseModel):
    visibility: Visibility
    retention: Retention
    authority: Optional[Authority] = None
    owning_project: constr(max_length=64)
    shared_with: List[constr(max_length=64)] = []


class Subject(BaseModel):
    entity: str = Field(max_length=256)
    predicate: str = Field(max_length=256)
    object: Optional[Union[str, int, float, bool]] = None


class Content(BaseModel):
    text: str
    structured: Optional[Union[dict, list]] = None


class Provenance(BaseModel):
    source_type: SourceType
    source_ref: constr(max_length=1024)
    writer_identity: constr(max_length=128)
    derived_from: Optional[List[constr(max_length=128)]] = None
    policy_version: constr(max_length=64)
    lineage_hash: Optional[constr(max_length=128)] = None


class Timestamps(BaseModel):
    created_at: datetime
    updated_at: datetime
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    valid_until_reason: Optional[ValidUntilReason] = None


class Confidence(BaseModel):
    score: confloat(ge=0, le=1)
    method: Method


class Policy(BaseModel):
    risk_level: RiskLevel
    decision_path: DecisionPath
    requires_human_review: Optional[bool] = None
    allowed_actions: Optional[List[Action]] = None
    classification: Optional[str] = None       # e.g. "low_confidence", "unresolved_contradiction" --
                                                 # machine-readable reason tag for a review item
    recommended_action: Optional[Action] = None  # a SUGGESTION shown on the review item,
                                                   # never a decision the system has made (Scope Doc §3, Principle 2)


class Contradiction(BaseModel):
    detected: bool = False
    conflicts_with: Optional[List[constr(max_length=128)]] = None
    resolution: Optional[Resolution] = None
    cross_project: bool = False


class ReviewPresentation(BaseModel):
    """Populated whenever decision_path == needs_review."""
    recommendation: str
    validation: str
    ramifications: str


class Audit(BaseModel):
    decision_by: Optional[constr(max_length=128)] = None
    decision_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    reviewer: Optional[constr(max_length=128)] = None


class GovernedMemoryCandidate(BaseModel):
    memory_id: constr(max_length=128)
    owning_project: constr(max_length=64)
    agent_id: Optional[constr(max_length=64)] = None
    session_id: Optional[constr(max_length=128)] = None
    scope: Scope
    memory_type: MemoryType
    subject: Optional[Subject] = None
    content: Content
    provenance: Provenance
    timestamps: Timestamps
    confidence: Confidence
    policy: Policy
    status: Status
    supersedes_id: Optional[constr(max_length=128)] = None
    contradiction: Optional[Contradiction] = None
    review: Optional[ReviewPresentation] = None
    audit: Optional[Audit] = None


class MemoryDecision(BaseModel):
    memory_id: str
    previous_status: Status
    new_status: Status
    reason: Optional[str] = None
    reviewer: Optional[str] = None
