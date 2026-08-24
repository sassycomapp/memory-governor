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
