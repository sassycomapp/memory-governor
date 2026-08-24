"""
Architecture §7: the Self-Healing / Iterative Learning Mechanism.

Step 11.1 scope, per Workflow: read-only pattern-detection ONLY. This
module never writes anything -- no candidate submission, no
policy_versions row. Step 11.2 (procedural candidate generation) and
Step 11.3 (the approved-change write path) are deliberately separate,
later steps, consuming what this module detects.

"The governor is permitted to notice things about itself. It is never
permitted to act on that noticing without the same human gate every
other high-risk candidate goes through." -- Architecture §7.
"""
from typing import List
import os

from .storage import get_connection
from .api import submit_candidate, CandidateSubmissionRequest
from .models import Content, Confidence

# Conservative by design (Architecture §7's own wording: "a separately,
# conservatively set noteworthiness threshold"). Two independent guards,
# not one: a rate threshold alone would flag noise from tiny sample
# sizes (1 out of 1 rejected is not a pattern), and a sample-size
# threshold alone would flag large groups with an unremarkable rate.
# Both must be crossed together.
MIN_SAMPLE_SIZE = 5
NOTEWORTHY_RATE_THRESHOLD = 0.5
OBSERVATION_WINDOW_DAYS = 30


def detect_review_outcome_patterns(
    window_days: int = OBSERVATION_WINDOW_DAYS,
    min_sample_size: int = MIN_SAMPLE_SIZE,
    rate_threshold: float = NOTEWORTHY_RATE_THRESHOLD,
) -> List[dict]:
    """
    Read-only pass over recent decision history (Architecture §7, point 1).
    Groups audit_log's developer-recorded review decisions by
    (memory_type, owning_project, classification) and flags any group
    where a "reject" or "quarantined" outcome crosses BOTH the sample-size
    and rate thresholds together.

    Deliberately looks at "reject"/"quarantined" as developer-CHOSEN
    dispositions (Scope Doc §3, Principle 2, the two-outcome model) --
    these are never automatic since Step 6.5, so a high rate here reflects
    a genuine, real pattern in the developer's own review decisions for
    that context, not noise from an old auto-quarantine/auto-reject path
    that no longer exists.

    Returns a list of pattern dicts, each with memory_type, owning_project,
    classification, total_reviewed, unfavorable_count, and rate. Returns
    an empty list if nothing crosses the threshold. Never writes anything.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    mc.memory_type,
                    mc.owning_project,
                    mc.policy->>'classification' AS classification,
                    COUNT(*) AS total_reviewed,
                    COUNT(*) FILTER (WHERE al.new_status IN ('rejected', 'quarantined')) AS unfavorable_count
                FROM audit_log al
                JOIN memory_candidates mc ON mc.memory_id = al.memory_id
                WHERE al.recorded_at >= now() - (%(window_days)s || ' days')::interval
                  AND al.reviewer IS NOT NULL
                GROUP BY mc.memory_type, mc.owning_project, mc.policy->>'classification'
                """,
                {"window_days": window_days},
            )
            rows = cur.fetchall()

    patterns = []
    for row in rows:
        total = row["total_reviewed"]
        unfavorable = row["unfavorable_count"]
        if total < min_sample_size:
            continue
        rate = unfavorable / total
        if rate < rate_threshold:
            continue
        patterns.append({
            "memory_type": row["memory_type"],
            "owning_project": row["owning_project"],
            "classification": row["classification"],
            "total_reviewed": total,
            "unfavorable_count": unfavorable,
            "rate": rate,
        })
    return patterns


def _describe_pattern(pattern: dict) -> str:
    """
    Architecture §7 point 2: the candidate content must describe both
    the observation AND a suggested policy adjustment -- not just raw
    numbers with no interpretation.
    """
    return (
        f"Self-healing observation: {pattern['unfavorable_count']} of "
        f"{pattern['total_reviewed']} {pattern['memory_type']}-type candidates "
        f"in project '{pattern['owning_project']}' with classification "
        f"'{pattern['classification']}' were rejected or quarantined by the "
        f"developer over the last {OBSERVATION_WINDOW_DAYS} days "
        f"({pattern['rate']:.0%}). This may indicate the current policy "
        f"thresholds for this context are miscalibrated -- worth reviewing "
        f"whether REVIEW_THRESHOLD or AUTO_PROMOTE_THRESHOLD should be "
        f"adjusted for '{pattern['classification']}' candidates in this "
        f"project, or whether this classification's underlying check "
        f"itself needs revisiting."
    )


def submit_detected_pattern(pattern: dict) -> dict:
    """
    Step 11.2 (Architecture §7 points 2-3): submits ONE detected pattern
    as a real candidate through submit_candidate() -- the exact same
    function the HTTP endpoint POST /memory/candidate calls, not a
    reimplementation or a special-cased write path (Workflow's explicit
    requirement).

    memory_type is "policy", NOT "procedural" as Architecture §7's own
    literal wording states -- a deliberate, confirmed deviation. Only
    "policy" is in HIGH_RISK_TYPES (app/policy.py); "procedural" is not.
    Submitting as "procedural" would silently violate point 3's own
    stated guarantee ("guaranteed needs_review") for ordinary confidence/
    contradiction values, since nothing else in decide_path() would
    reliably force review for this content. "A suggested policy
    adjustment" (the spec's own description of the content) is, on
    reflection, a more honest label as "policy" than "procedural" in any
    case -- this mirrors how POST /memory/policy already treats a
    proposed-but-not-yet-adopted policy as ordinary policy-type content
    pending review, not a special category of its own.

    Uses the dedicated self_healing writer credential (Step 11.2) --
    distinct from gbrain_dreaming, manual_submission, and the future
    pdlf_step_sync -- so the governor can always tell a self-generated
    observation apart from every other writer.
    """
    credential = os.environ["MEMORY_GOVERNOR_WRITER_SELF_HEALING"]

    req = CandidateSubmissionRequest(
        owning_project=pattern["owning_project"],
        visibility="project_local",
        retention="long_term",
        memory_type="policy",
        content=Content(text=_describe_pattern(pattern)),
        source_type="agent_trace",
        source_ref="self_healing.detect_review_outcome_patterns",
        policy_version="v1",
        confidence=Confidence(score=1.0, method="rule_based"),
    )

    result = submit_candidate(req, credential)
    return result