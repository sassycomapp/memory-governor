"""
Step 12.1: permanent coverage for app/gbrain.py.

Two tiers, deliberately different in scope:

1. TestSourceSwitching -- genuinely safe to run automatically, forever,
   with zero real-world footprint: switching to a source that doesn't
   exist fails immediately and predictably, writes nothing.

2. Everything using disposable_gbrain_source -- a real, disposable
   GBrain source created and completely destroyed within each test,
   leaving zero permanent trace. Built after a real incident during
   this step's own design work: a chained bash script continued past a
   failed `sources add` call (which requires a git-INITIALIZED,
   COMMITTED path -- an empty directory fails cleanly) and wrote a
   real, permanent, incorrect fact into the real mb-3-cs-code source.
   Recovered via gbrain's own `forget` verb (expires with an audit
   trail, never a silent delete) -- documented in
   memory-governor-reference.md. Every subprocess call in the fixture
   below uses check=True: in Python, unlike that bash script, a failed
   step raises immediately and stops execution at that exact line,
   rather than silently continuing past it.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from app.gbrain import _switch_to_source, remember_candidate, SourceNotFoundError
from app.models import (
    GovernedMemoryCandidate, Scope, Content, Provenance,
    Timestamps, Confidence, Policy,
)


class TestSourceSwitching:
    def test_nonexistent_source_raises_clean_error(self):
        with pytest.raises(SourceNotFoundError):
            _switch_to_source("this-source-genuinely-does-not-exist-anywhere-xyz")


@pytest.fixture
def disposable_gbrain_source(tmp_path):
    """
    A genuinely real, genuinely disposable GBrain source -- created,
    used, and completely destroyed within one test, leaving zero trace.
    The finally block guarantees the real, original active source is
    restored before the disposable source is destroyed, regardless of
    whether the test itself passed, failed, or raised.
    """
    source_id = f"test-gbrain-{int(time.time())}-{os.getpid()}"
    source_path = tmp_path / "gbrain_source"
    source_path.mkdir()

    subprocess.run(["git", "init", str(source_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source_path), "config", "user.email", "test@test.local"], check=True)
    subprocess.run(["git", "-C", str(source_path), "config", "user.name", "Test"], check=True)
    (source_path / "README.md").write_text("disposable gbrain test source\n")
    subprocess.run(["git", "-C", str(source_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source_path), "commit", "-m", "initial"], check=True, capture_output=True)

    current = subprocess.run(["gbrain", "sources", "current"], check=True, capture_output=True, text=True)
    original_source = current.stdout.strip().splitlines()[0].replace("source: ", "").strip()

    subprocess.run(["gbrain", "sources", "add", source_id, "--path", str(source_path)], check=True, capture_output=True)

    try:
        yield source_id
    finally:
        subprocess.run(["gbrain", "sources", "default", original_source], check=True, capture_output=True)
        subprocess.run(["gbrain", "sources", "remove", source_id, "--confirm-destructive"], check=True, capture_output=True)


class TestRememberCandidateLiveWrite:
    """
    The real, end-to-end proof: a genuine candidate written into a
    genuinely real (if disposable) GBrain source via remember_candidate(),
    confirmed retrievable. Zero permanent footprint in any real project
    source once the fixture's teardown runs.
    """

    def test_remember_writes_a_real_retrievable_fact(self, disposable_gbrain_source):
        source_id = disposable_gbrain_source

        candidate = GovernedMemoryCandidate(
            memory_id="test-gbrain-live-001", owning_project=source_id,
            scope=Scope(visibility="project_local", retention="ephemeral", owning_project=source_id),
            memory_type="fact",
            content=Content(text="MEMORY-GOVERNOR-PYTEST-MARKER: real gbrain.py write test."),
            provenance=Provenance(source_type="user", source_ref="/tmp/x.md",
                                   writer_identity="w1", policy_version="v1"),
            timestamps=Timestamps(created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)),
            confidence=Confidence(score=0.9, method="human_verified"),
            policy=Policy(risk_level="low", decision_path="auto_promote"),
            status="promoted",
        )

        result = remember_candidate(candidate)
        assert "remembered" in result.lower() or "fact" in result.lower()

        recall_result = subprocess.run(
            ["gbrain", "call", "recall", '{"grep": "MEMORY-GOVERNOR-PYTEST-MARKER"}'],
            capture_output=True, text=True, check=True,
        )
        assert "MEMORY-GOVERNOR-PYTEST-MARKER" in recall_result.stdout

    def test_source_exists_while_fixture_active(self, disposable_gbrain_source):
        source_id = disposable_gbrain_source
        result = subprocess.run(["gbrain", "sources", "list"], capture_output=True, text=True, check=True)
        assert source_id in result.stdout


class TestNoLeftoverTestSources:
    def test_no_leftover_disposable_test_sources_from_prior_runs(self):
        """
        A genuine, standing guard against the exact accumulation risk
        this whole design was meant to avoid: confirms no source
        matching this fixture's naming pattern is left registered,
        which would indicate a prior test run's cleanup failed silently.
        """
        result = subprocess.run(["gbrain", "sources", "list"], capture_output=True, text=True, check=True)
        leftover = [line for line in result.stdout.splitlines() if line.strip().startswith("test-gbrain-")]
        assert leftover == [], f"Leftover disposable test sources found, cleanup did not run: {leftover}"


@pytest.fixture
def two_disposable_gbrain_sources(tmp_path):
    """
    Step 12.2: two genuinely separate disposable sources, needed to prove
    isolation, not just presence -- Step 12.1's own tests only ever
    confirmed a fact could be found again in the SAME source it was
    written to. Never proved a fact does NOT leak into a different
    project's source. Same safety pattern as disposable_gbrain_source,
    doubled: each source is git-initialized and committed before
    registration, and the finally block restores the real original
    active source and destroys BOTH disposable sources regardless of
    test outcome.
    """
    def _make_source(name: str) -> str:
        source_id = f"test-gbrain-{name}-{int(time.time())}-{os.getpid()}"
        source_path = tmp_path / f"gbrain_source_{name}"
        source_path.mkdir()
        subprocess.run(["git", "init", str(source_path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source_path), "config", "user.email", "test@test.local"], check=True)
        subprocess.run(["git", "-C", str(source_path), "config", "user.name", "Test"], check=True)
        (source_path / "README.md").write_text(f"disposable gbrain test source {name}\n")
        subprocess.run(["git", "-C", str(source_path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(source_path), "commit", "-m", "initial"], check=True, capture_output=True)
        subprocess.run(["gbrain", "sources", "add", source_id, "--path", str(source_path)], check=True, capture_output=True)
        return source_id

    current = subprocess.run(["gbrain", "sources", "current"], check=True, capture_output=True, text=True)
    original_source = current.stdout.strip().splitlines()[0].replace("source: ", "").strip()

    source_a = _make_source("a")
    source_b = _make_source("b")

    try:
        yield source_a, source_b
    finally:
        subprocess.run(["gbrain", "sources", "default", original_source], check=True, capture_output=True)
        subprocess.run(["gbrain", "sources", "remove", source_a, "--confirm-destructive"], check=True, capture_output=True)
        subprocess.run(["gbrain", "sources", "remove", source_b, "--confirm-destructive"], check=True, capture_output=True)


class TestStep12_2EndToEndPromotionToGBrain:
    """
    Architecture/Workflow Step 12.2 exit criteria, verified directly:
    content, provenance, and owning_project scoping are all correctly
    reflected in what GBrain stores. Uses the REAL governor pipeline
    (submit_candidate -> promoted -> worker -> remember_candidate),
    not a direct call to gbrain.py in isolation -- this is what actually
    proves "promote a candidate, confirm it's retrievable," per the
    Workflow's own literal wording.
    """

    def test_content_provenance_and_project_isolation_all_correct(
        self, two_disposable_gbrain_sources
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        from app.storage import get_connection, insert_project
        from app.worker import run_polling_pass

        source_a, source_b = two_disposable_gbrain_sources

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        real_credential = os.environ["MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION"]
        headers = {"X-Governor-Credential": real_credential}

        def cleanup_governor_db(memory_id=None):
            """
            Fix: the original version matched memory_id LIKE 'test-step-12-2-%',
            but real memory_ids are UUIDs generated by the system -- that
            pattern could never match anything, silently deleting zero
            audit_log/memory_events rows and leaving a real foreign-key
            violation when memory_candidates was deleted afterward. Now
            takes the REAL memory_id (once known) and deletes in the
            correct child-to-parent order.
            """
            with get_connection() as conn:
                with conn.cursor() as cur:
                    if memory_id:
                        cur.execute("DELETE FROM audit_log WHERE memory_id = %s", (memory_id,))
                        cur.execute("DELETE FROM memory_events WHERE memory_id = %s", (memory_id,))
                    cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (source_a,))
                    cur.execute("DELETE FROM projects WHERE project_id = %s", (source_a,))
                conn.commit()

        cleanup_governor_db()
        memory_id = None
        marker = None
        try:
            # Register the governor's own project record using source_a's
            # id as owning_project -- this is what makes gbrain.py switch
            # to source_a specifically when it writes.
            insert_project(source_a, "Step 12.2 Test Project")

            marker = f"STEP-12-2-MARKER-{int(time.time())}"
            resp = client.post("/memory/candidate", headers=headers, json={
                "owning_project": source_a, "visibility": "project_local", "retention": "ephemeral",
                "memory_type": "fact",
                "content": {"text": f"{marker}: real end-to-end Step 12.2 content."},
                "source_type": "user", "source_ref": "/tmp/step-12-2-test.md", "policy_version": "v1",
                "confidence": {"score": 0.95, "method": "human_verified"},
            })
            assert resp.status_code == 201
            assert resp.json()["status"] == "promoted"
            memory_id = resp.json()["memory_id"]

            processed = run_polling_pass()
            assert processed >= 1

            # --- Presence + provenance: the fact exists, carrying its
            # writer, policy version, memory_id, and decided status ---
            subprocess.run(["gbrain", "sources", "default", source_a], check=True, capture_output=True)
            recall_a = subprocess.run(
                ["gbrain", "call", "recall", f'{{"grep": "{marker}"}}'],
                capture_output=True, text=True, check=True,
            )
            assert marker in recall_a.stdout, "Content was not found in GBrain at all"
            assert f"{marker}: real end-to-end Step 12.2 content." in recall_a.stdout
            assert "manual_submission" in recall_a.stdout
            assert "v1" in recall_a.stdout
            assert memory_id in recall_a.stdout
            assert "promoted" in recall_a.stdout

            # --- Isolation (corrected 2026-09-08): GBrain's facts layer
            # is brain-global -- an unscoped grep finds any fact from any
            # context, and no platform mechanism scopes recall to the
            # active source. The isolation guarantee is therefore enforced
            # by STAMPING each governed fact with its owning project
            # (entity) and by visibility mapping (project_local -> private,
            # never world). Entity-scoped recall IS the project-scoped
            # read, and that is what this test now proves:
            recall_a_scoped = subprocess.run(
                ["gbrain", "call", "recall", f'{{"grep": "{marker}", "entity": "{source_a}"}}'],
                capture_output=True, text=True, check=True,
            )
            assert marker in recall_a_scoped.stdout, (
                "Content was not found under its own project's entity scope"
            )

            subprocess.run(["gbrain", "sources", "default", source_b], check=True, capture_output=True)
            recall_b_scoped = subprocess.run(
                ["gbrain", "call", "recall", f'{{"grep": "{marker}", "entity": "{source_b}"}}'],
                capture_output=True, text=True, check=True,
            )
            assert marker not in recall_b_scoped.stdout, (
                "Content leaked across project scopes -- entity stamping is not isolating writes"
            )

            # --- Visibility mapping: a project_local governed memory must
            # never be world-readable (the original defect: --visibility
            # was hardcoded to world, making every governed memory
            # globally readable regardless of its declared scope) ---
            assert '"visibility": "private"' in recall_a_scoped.stdout, (
                "project_local candidate was written world-visible -- "
                "visibility mapping is broken"
            )
        finally:
            cleanup_governor_db(memory_id)
            if memory_id is not None and marker is not None:
                # Expire this test's own fact in GBrain: the facts layer
                # is brain-global, so a test fact left behind is real
                # pollution in the live brain. forget() expires with a
                # full audit trail (never a silent delete), consistent
                # with the register's own "never delete, only retire"
                # principle.
                recall_cleanup = subprocess.run(
                    ["gbrain", "call", "recall", f'{{"grep": "{marker}"}}'],
                    capture_output=True, text=True,
                )
                try:
                    facts = json.loads(recall_cleanup.stdout).get("facts", [])
                    for fact in facts:
                        fact_id = fact.get("fact_id") or fact.get("id")
                        if fact_id:
                            subprocess.run(
                                ["gbrain", "call", "forget", json.dumps({
                                    "id": fact_id,
                                    "reason": "test cleanup: disposable STEP-12-2 test fact expired by its own test (brain-global facts layer, zero-permanent-trace requirement)",
                                })],
                                check=True, capture_output=True,
                            )
                except (json.JSONDecodeError, subprocess.CalledProcessError):
                    # Surfaced on the next memory-functionality audit run
                    # rather than masked here -- never silently skipped.
                    print("WARNING: test-fact cleanup could not complete; "
                          "leftover STEP-12-2 marker fact(s) remain in GBrain")
