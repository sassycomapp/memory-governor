"""
Architecture §5A / §12: the governed write into GBrain. Step 12.1.

Uses gbrain's `remember` verb (MEMORY_VERBS, Step 0.2), NEVER `gbrain
sync` -- confirmed in extended design discussion earlier this project:
sync ingests whatever raw file sits in a registered source folder
regardless of governor approval, which would silently defeat governance
for anything saved inside a synced folder. remember writes a distinct,
provenance-carrying entry instead.

Known limitation, deliberately not solved here: `remember` takes one
plain-text fact -- content.structured (e.g. a policy proposal's real
threshold numbers) has no natural home in a single remember call and is
NOT captured in GBrain by this module. The full structured payload
remains fully captured in registers/candidates/<id>.json (Phase 7) --
this is a known, accepted gap in GBrain's own copy specifically, not a
silent data-loss risk overall.
"""
import subprocess

from .models import GovernedMemoryCandidate


class SourceNotFoundError(Exception):
    """Raised when a candidate's owning_project has no matching GBrain source."""


def _switch_to_source(owning_project: str) -> None:
    """
    Confirmed live (Step 12.1 investigation): `gbrain sources default
    <id>` fails LOUDLY with a real, non-zero exit and a clear message
    ('Source "..." not found.') when the target source doesn't exist --
    never a silent no-op. This is what makes it safe to call before
    every single write: a bad owning_project can never silently write
    into whatever source happened to be active a moment before.
    """
    result = subprocess.run(
        ["gbrain", "sources", "default", owning_project],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SourceNotFoundError(
            f"GBrain source '{owning_project}' not found or could not be activated: {result.stderr.strip()}"
        )


def remember_candidate(candidate: GovernedMemoryCandidate) -> str:
    """
    Switches to the candidate's owning_project as the active GBrain
    source, then calls `gbrain remember` with the candidate's real
    content and provenance. Returns gbrain's raw stdout for logging.

    Raises SourceNotFoundError or subprocess.CalledProcessError on any
    real failure -- both are genuine errors the caller's retry/backoff
    wrapper (with_retry(), Phase 4.3) is designed to catch, exactly the
    same pattern already proven for register_write's git operations.
    Never silently swallowed here.
    """
    _switch_to_source(candidate.owning_project)

    provenance_text = (
        f"memory_governor: memory_id={candidate.memory_id}, "
        f"writer={candidate.provenance.writer_identity}, "
        f"policy_version={candidate.provenance.policy_version}, "
        f"decided={candidate.status}"
    )

    result = subprocess.run(
        [
            "gbrain", "remember", candidate.content.text,
            "--provenance", provenance_text,
            "--visibility", "world",
        ],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()
