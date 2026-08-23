"""
Architecture §6: renders GET /memory/review-queue entries into the exact
"## Memory Review Required" markdown convention already used throughout
this project's own work-package reports (docs-manager's report format).

Step 10.2: the actual formatting logic belongs here, tested, in this
repo -- independent of OpenCode. Only the act of an OpenCode session
displaying this text is genuinely outside this codebase; producing the
text itself is not.
"""
from typing import List


def render_review_entry(entry: dict) -> str:
    """
    Renders exactly one review-queue entry (the dict shape returned by
    GET /memory/review-queue) into Architecture §6's template.

    recommended_action is genuinely optional -- some decide_path()
    branches (e.g. low_confidence) have no single obvious suggestion, by
    design (Step 6.5.1) -- rendered as "none" rather than omitted, so the
    line's structure stays consistent across every entry.
    """
    review = entry.get("review") or {}
    recommended_action = entry.get("recommended_action") or "none"

    return (
        "## Memory Review Required\n"
        f"- Candidate: {entry['memory_id']}, type: {entry['memory_type']}, "
        f"project: {entry['owning_project']}, risk: {entry['risk_level']}\n"
        f"- Anomaly / classification: {entry.get('classification') or 'none'}\n"
        f"- Recommendation: {review.get('recommendation', '')} "
        f"(suggested action: {recommended_action})\n"
        f"- Validation: {review.get('validation', '')}\n"
        f"- Ramifications: {review.get('ramifications', '')}\n"
        "- Decision needed: promote / quarantine / reject"
    )


def render_review_queue(entries: List[dict]) -> str:
    """
    Renders the full queue -- one block per entry, separated by a blank
    line, matching how multiple findings are already separated in
    docs-manager's own report format. An empty queue renders a plain,
    unambiguous statement rather than nothing at all, since "nothing was
    printed" and "confirmed, nothing pending" are not the same claim.
    """
    if not entries:
        return "No candidates currently require review."
    return "\n\n".join(render_review_entry(e) for e in entries)
