"""
Architecture §6, Step 10.2: permanent coverage for the Q&A sheet
rendering logic. Proven live against a real HTTP response in Step 10.2's
own verification; this file makes that proof permanent.
"""
from app.rendering import render_review_entry, render_review_queue


REAL_ENTRY = {
    "memory_id": "04d0a13d-89af-4876-8b2e-3d50af2befbe",
    "memory_type": "policy",
    "owning_project": "test-10-1-review-queue",
    "risk_level": "high",
    "classification": "high_risk_type",
    "recommended_action": None,
    "review": {
        "recommendation": "Policy engine flagged this policy candidate for human review (risk=high). No automatic action taken.",
        "validation": "risk_level=high; confidence=0.95",
        "ramifications": "No supersession or cross-project sharing effects",
    },
}


class TestRenderReviewEntry:
    def test_matches_architecture_6_template_exactly(self):
        expected = (
            "## Memory Review Required\n"
            "- Candidate: 04d0a13d-89af-4876-8b2e-3d50af2befbe, type: policy, project: test-10-1-review-queue, risk: high\n"
            "- Anomaly / classification: high_risk_type\n"
            "- Recommendation: Policy engine flagged this policy candidate for human review (risk=high). No automatic action taken. (suggested action: none)\n"
            "- Validation: risk_level=high; confidence=0.95\n"
            "- Ramifications: No supersession or cross-project sharing effects\n"
            "- Decision needed: promote / quarantine / reject"
        )
        assert render_review_entry(REAL_ENTRY) == expected

    def test_populated_recommended_action_renders_correctly(self):
        entry = dict(REAL_ENTRY, recommended_action="reject")
        rendered = render_review_entry(entry)
        assert "(suggested action: reject)" in rendered

    def test_missing_review_block_does_not_crash(self):
        entry = dict(REAL_ENTRY, review=None)
        rendered = render_review_entry(entry)
        assert "## Memory Review Required" in rendered


class TestRenderReviewQueue:
    def test_empty_queue_renders_unambiguous_message(self):
        assert render_review_queue([]) == "No candidates currently require review."

    def test_multiple_entries_separated_by_blank_line(self):
        entry2 = dict(REAL_ENTRY, memory_id="second-id")
        rendered = render_review_queue([REAL_ENTRY, entry2])
        assert rendered.count("## Memory Review Required") == 2
        assert "\n\n## Memory Review Required" in rendered
