import hashlib
import os
from typing import List, Optional

from .models import GovernedMemoryCandidate, Provenance, SourceType
from .storage import get_candidate

# Maps known environment variable names (provisioned in Step 0.3) to the
# writer_identity label that credential resolves to. Values are read from
# os.environ at call time, never hardcoded -- this module holds no secret
# material itself.
_CREDENTIAL_ENV_MAP = {
    "MEMORY_GOVERNOR_WRITER_GBRAIN_DREAMING": "gbrain_dreaming",
    "MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION": "manual_submission",
    "MEMORY_GOVERNOR_WRITER_SELF_HEALING": "self_healing",
}


def _load_credential_registry() -> dict:
    """
    Builds {credential_value: writer_identity_label} fresh from the current
    environment on every call -- deliberately not cached at import time, so
    a credential rotation takes effect without a process restart.
    """
    registry = {}
    for env_var, identity_label in _CREDENTIAL_ENV_MAP.items():
        value = os.environ.get(env_var)
        if value:
            registry[value] = identity_label
    return registry


def resolve_writer_identity(presented_credential: str) -> str:
    """
    The ONLY function permitted to turn a presented credential into a
    writer_identity. Raises PermissionError for anything not in the
    registry -- an empty string, a garbage value, or a credential that
    was valid once but has since been rotated out of the environment.
    """
    registry = _load_credential_registry()
    identity = registry.get(presented_credential)
    if identity is None:
        raise PermissionError("Presented credential does not resolve to a known writer identity")
    return identity


def build_provenance(
    source_type: SourceType,
    source_ref: str,
    presented_credential: str,
    policy_version: str,
    derived_from: Optional[List[str]] = None,
) -> Provenance:
    """
    The SOLE sanctioned constructor for a Provenance object backing a real
    submission. Deliberately does NOT accept writer_identity as a
    parameter -- there is no argument through which a caller can pass one
    in, spoofed or otherwise. writer_identity is always derived here, via
    resolve_writer_identity(), from the credential that actually
    authenticated the request.

    lineage_hash is computed automatically (see compute_lineage_hash) --
    also not a caller-supplied value.
    """
    writer_identity = resolve_writer_identity(presented_credential)
    lineage_hash = compute_lineage_hash(source_ref, source_type)
    return Provenance(
        source_type=source_type,
        source_ref=source_ref,
        writer_identity=writer_identity,
        derived_from=derived_from,
        policy_version=policy_version,
        lineage_hash=lineage_hash,
    )


def compute_lineage_hash(source_ref: str, source_type: str) -> str:
    """
    DESIGN CHOICE, not a settled spec requirement -- neither Scope Doc nor
    Architecture Spec defines what lineage_hash should hash or how. This
    implementation hashes source_type + source_ref (canonically ordered,
    UTF-8 encoded) via SHA-256. Revisit if a different lineage-tracking
    need emerges (e.g. hashing content itself to detect silent edits to
    the same source_ref over time).
    """
    canonical = f"{source_type}:{source_ref}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reconstruct_lineage(memory_id: str) -> List[GovernedMemoryCandidate]:
    """
    Walks the supersedes_id chain backward from the given candidate,
    returning the full lineage oldest-first. Raises ValueError if
    memory_id itself does not exist. A candidate with no supersedes_id
    returns a single-element list (itself, with no ancestors).

    Guards against a cyclic supersedes_id chain (which should never exist
    given the state machine's transition rules, but this function does
    not assume that guarantee blindly) by capping the walk and raising if
    exceeded, rather than looping forever.
    """
    chain: List[GovernedMemoryCandidate] = []
    current_id: Optional[str] = memory_id
    seen = set()
    max_depth = 1000

    while current_id is not None:
        if current_id in seen:
            raise ValueError(f"Cyclic supersedes_id chain detected at memory_id '{current_id}'")
        if len(chain) >= max_depth:
            raise ValueError(f"Lineage chain exceeds max depth ({max_depth}); possible corruption")

        candidate = get_candidate(current_id)
        if candidate is None:
            if not chain:
                raise ValueError(f"memory_id '{memory_id}' does not exist")
            break  # a broken chain link partway through -- stop, don't fail the whole walk

        chain.append(candidate)
        seen.add(current_id)
        current_id = candidate.supersedes_id

    chain.reverse()  # oldest first
    return chain
