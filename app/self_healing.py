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

from .storage import get_connection

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
