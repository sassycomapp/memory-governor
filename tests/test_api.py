import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import router
from app.storage import get_connection, insert_project


@pytest.fixture(scope="module")
def api_client():
    """
    A real, in-process TestClient -- NOT the manually-run background
    uvicorn server on port 8321. Fully independent of whether that process
    happens to be running or freshly restarted; this fixture always
    exercises the current, actually-imported code, closing the exact class
    of stale-process confusion encountered live during Step 5.3.
    """
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(scope="module")
def real_credential_headers():
    return {"X-Governor-Credential": os.environ["MEMORY_GOVERNOR_WRITER_MANUAL_SUBMISSION"]}



class TestGlobalNulByteRejection:
    """
    Permanent regression coverage for the Step 5.3 finding: raw NUL bytes
    in path/query parameters reaching an unvalidated psycopg query produce
    a bare 500 (psycopg.DataError). Fixed via a single router-wide
    dependency (reject_nul_bytes_in_path_and_query) rather than patching
    each endpoint individually, after two individual patches in a row
    each missed a different call site.
    """

    def test_path_parameter_nul_byte_rejected_get_memory(self, api_client, real_credential_headers):
        resp = api_client.get("/memory/test-nul-%00-id", headers=real_credential_headers)
        assert resp.status_code == 400
        assert "NUL bytes" in resp.json()["detail"]

    def test_path_parameter_nul_byte_rejected_provenance(self, api_client, real_credential_headers):
        resp = api_client.get("/memory/test-nul-%00-id/provenance", headers=real_credential_headers)
        assert resp.status_code == 400

    def test_path_parameter_nul_byte_rejected_project_patch(self, api_client, real_credential_headers):
        resp = api_client.patch(
            "/memory/projects/test-nul-%00-id",
            headers=real_credential_headers,
            json={"dormant": True},
        )
        assert resp.status_code == 400

    def test_query_parameter_nul_byte_rejected_list_memory(self, api_client, real_credential_headers):
        resp = api_client.get(
            "/memory?owning_project=test-nul-%00-project", headers=real_credential_headers
        )
        assert resp.status_code == 400
        assert "NUL bytes" in resp.json()["detail"]

    def test_legitimate_request_without_nul_bytes_still_works(self, api_client, real_credential_headers):
        """
        The dependency must not be overly aggressive -- confirms a normal,
        valid request is unaffected by this guard.
        """
        resp = api_client.get(
            "/memory?owning_project=mb-3-cs-code", headers=real_credential_headers
        )
        assert resp.status_code == 200

    def test_request_body_nul_byte_rejected_via_shared_pydantic_validator(self, api_client, real_credential_headers):
        """
        SUPERSEDES the original version of this test. The router-wide
        dependency covers path/query parameters only, by design -- but the
        gap this originally documented (request BODY fields unguarded) was
        later found to be a REAL, exploitable defect (Schemathesis caught
        a NUL byte in POST /memory/policy's "reason" field producing a bare
        500), not an acceptable permanent boundary. Fixed with
        NoNulBytesModel, a shared Pydantic base class every request model
        in api.py now inherits, recursively rejecting NUL bytes anywhere
        in the body -- including nested structures a per-field check would
        miss. This now correctly returns 422 (a standard Pydantic
        validation failure), not a bespoke 400 -- consistent with how
        every other Pydantic validation error in this API already behaves.
        """
        resp = api_client.post(
            "/memory/projects",
            headers=real_credential_headers,
            json={"project_id": "test-nul-\u0000-project", "display_name": "test"},
        )
        assert resp.status_code == 422
        assert "NUL bytes" in resp.text

    def test_nested_request_body_nul_byte_also_rejected(self, api_client, real_credential_headers):
        """
        The specific case a naive top-level-only body check would miss --
        a NUL byte buried inside a nested object (proposed_thresholds),
        not a top-level string field. Confirms NoNulBytesModel'''s recursive
        check genuinely reaches this depth over real HTTP, not just at the
        unit level (already proven separately during Step 5.3's live
        verification).
        """
        resp = api_client.post(
            "/memory/policy",
            headers=real_credential_headers,
            json={
                "owning_project": "mb-3-cs-code",
                "proposed_policy_version": "v2",
                "proposed_thresholds": {"nested_key": "bad\u0000value"},
                "reason": "clean reason",
                "current_policy_version": "v1",
            },
        )
        assert resp.status_code == 422
        assert "NUL bytes" in resp.text


class TestPathBasedIngestion:
    """
    Permanent regression coverage for Step 6.1 (Architecture §5). Two
    real defects were found and fixed during this step's own live
    testing, both covered explicitly below: an existence-oracle leak in
    the allowlist check ordering, and a folder-batch abort on a
    non-HTTPException failure.
    """

    TEST_DIR = "/home/dev-p/memory-governor/tests-fixtures-path-ingestion"

    @pytest.fixture(autouse=True)
    def _setup_test_tree(self, request):
        import shutil
        os.makedirs(f"{self.TEST_DIR}/subdir", exist_ok=True)
        with open(f"{self.TEST_DIR}/doc1.md", "w") as f:
            f.write("First document.")
        with open(f"{self.TEST_DIR}/doc2.md", "w") as f:
            f.write("Second document.")
        with open(f"{self.TEST_DIR}/subdir/doc3.md", "w") as f:
            f.write("Nested document.")
        with open(f"{self.TEST_DIR}/binary.dat", "wb") as f:
            f.write(b"\x00\x01\x02\xff\xfe")
        yield
        shutil.rmtree(self.TEST_DIR, ignore_errors=True)

    @pytest.fixture(autouse=True)
    def _project(self):
        pid = "test-project-path-ingest"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (pid,))
                cur.execute("DELETE FROM projects WHERE project_id = %s", (pid,))
            conn.commit()
        insert_project(pid, "Path Ingestion Test")
        yield pid
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE memory_id IN (SELECT memory_id FROM memory_candidates WHERE owning_project = %s)", (pid,))
                cur.execute("DELETE FROM memory_events WHERE memory_id IN (SELECT memory_id FROM memory_candidates WHERE owning_project = %s)", (pid,))
                cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (pid,))
                cur.execute("DELETE FROM projects WHERE project_id = %s", (pid,))
            conn.commit()

    def _payload(self, project_id, source_ref):
        return {
            "owning_project": project_id, "visibility": "project_local", "retention": "long_term",
            "memory_type": "fact", "source_type": "file", "policy_version": "v1",
            "confidence": {"score": 0.95, "method": "human_verified"},
            "source_ref": source_ref,
        }

    def test_single_file_submission_preserves_original_response_shape(
        self, api_client, real_credential_headers, _project
    ):
        resp = api_client.post(
            "/memory/candidate", headers=real_credential_headers,
            json=self._payload(_project, f"{self.TEST_DIR}/doc1.md"),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "memory_id" in body
        assert "submitted" not in body

    def test_folder_submission_returns_one_result_per_file(
        self, api_client, real_credential_headers, _project
    ):
        resp = api_client.post(
            "/memory/candidate", headers=real_credential_headers,
            json=self._payload(_project, self.TEST_DIR),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["submitted"]) == 4

    def test_binary_file_isolated_not_fatal_to_batch(
        self, api_client, real_credential_headers, _project
    ):
        resp = api_client.post(
            "/memory/candidate", headers=real_credential_headers,
            json=self._payload(_project, self.TEST_DIR),
        )
        body = resp.json()
        binary_result = next(r for r in body["submitted"] if "binary.dat" in r["source_ref"])
        assert "error" in binary_result
        good = [r for r in body["submitted"] if "error" not in r]
        assert len(good) == 3

    def test_existence_oracle_closed_nonexistent_outside_allowlist(
        self, api_client, real_credential_headers, _project
    ):
        """
        Regression test for the real defect found in Step 6.1: a path
        outside the allowlist must return 403 regardless of whether it
        exists -- NOT 404 for "doesn't exist" vs 403 for "exists but
        forbidden", which would leak filesystem information.
        """
        resp = api_client.post(
            "/memory/candidate", headers=real_credential_headers,
            json=self._payload(_project, "/tmp/definitely-does-not-exist-anywhere.md"),
        )
        assert resp.status_code == 403

    def test_existence_oracle_closed_existing_outside_allowlist(
        self, api_client, real_credential_headers, _project, tmp_path
    ):
        outside_file = tmp_path / "exists-outside-allowlist.md"
        outside_file.write_text("real content")
        resp = api_client.post(
            "/memory/candidate", headers=real_credential_headers,
            json=self._payload(_project, str(outside_file)),
        )
        assert resp.status_code == 403

    def test_nonexistent_path_inside_allowlist_returns_404(
        self, api_client, real_credential_headers, _project
    ):
        resp = api_client.post(
            "/memory/candidate", headers=real_credential_headers,
            json=self._payload(_project, f"{self.TEST_DIR}/does-not-exist.md"),
        )
        assert resp.status_code == 404

    def test_non_httpexception_failure_isolated_within_batch(
        self, api_client, real_credential_headers, _project
    ):
        """
        Regression test for the real defect found in Step 6.1: a folder
        batch must survive a genuinely unexpected exception (not just an
        HTTPException) on one file, isolating it and continuing with the
        rest -- this is the whole point of per-file error isolation.
        """
        import app.api as api_module
        original_submit = api_module._submit_single_candidate
        call_count = {"n": 0}

        def flaky_submit(req, cred):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise KeyError("simulated unexpected failure")
            return original_submit(req, cred)

        api_module._submit_single_candidate = flaky_submit
        try:
            resp = api_client.post(
                "/memory/candidate", headers=real_credential_headers,
                json=self._payload(_project, self.TEST_DIR),
            )
            assert resp.status_code == 201
            body = resp.json()
            errored = [r for r in body["submitted"] if r.get("status_code") == 500]
            assert len(errored) == 1
            assert "KeyError" in errored[0]["error"]
            succeeded = [r for r in body["submitted"] if "error" not in r]
            assert len(succeeded) == 2
        finally:
            api_module._submit_single_candidate = original_submit


class TestDormantProjectSubmissionGate:
    """
    Step 8.2 (Scope Doc §8): a dormant project rejects new candidate
    submissions with a clean 403, but existing state is never touched
    and reactivation requires no special handling -- submissions simply
    resume, since dormancy only ever gated new writes, never event
    processing (see Step 8.2 design discussion for why event-processing
    exclusion would have corrupted the outbox pattern instead).
    """

    def test_dormant_project_rejects_new_submission(self, api_client, real_credential_headers):
        from app.storage import insert_project, set_project_dormant, get_connection

        project_id = "test-dormant-gate-pytest"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
            conn.commit()
        insert_project(project_id, "Dormant Gate Pytest")
        set_project_dormant(project_id, True)

        try:
            resp = api_client.post("/memory/candidate", headers=real_credential_headers, json={
                "owning_project": project_id, "visibility": "project_local", "retention": "long_term",
                "memory_type": "fact", "content": {"text": "should be rejected"},
                "source_type": "user", "source_ref": "/tmp/x.md", "policy_version": "v1",
                "confidence": {"score": 0.95, "method": "human_verified"},
            })
            assert resp.status_code == 403
            assert "dormant" in resp.json()["detail"].lower()
        finally:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
                conn.commit()

    def test_reactivated_project_accepts_submission_with_no_special_handling(self, api_client, real_credential_headers):
        from app.storage import insert_project, set_project_dormant, get_connection

        project_id = "test-dormant-reactivate-pytest"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
            conn.commit()
        insert_project(project_id, "Dormant Reactivate Pytest")
        set_project_dormant(project_id, True)
        set_project_dormant(project_id, False)

        try:
            resp = api_client.post("/memory/candidate", headers=real_credential_headers, json={
                "owning_project": project_id, "visibility": "project_local", "retention": "long_term",
                "memory_type": "fact", "content": {"text": "should succeed after reactivation"},
                "source_type": "user", "source_ref": "/tmp/x.md", "policy_version": "v1",
                "confidence": {"score": 0.95, "method": "human_verified"},
            })
            assert resp.status_code == 201
        finally:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM audit_log WHERE memory_id IN (SELECT memory_id FROM memory_candidates WHERE owning_project = %s)", (project_id,))
                    cur.execute("DELETE FROM memory_events WHERE memory_id IN (SELECT memory_id FROM memory_candidates WHERE owning_project = %s)", (project_id,))
                    cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (project_id,))
                    cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
                conn.commit()


class TestProjectOnboardingEndToEnd:
    """
    Step 8.3: register a brand-new project and submit a candidate scoped
    to it, with NO code change -- the exit criterion this whole test
    exists to prove. Every other test in this codebase has done exactly
    this as a byproduct of testing something else, dozens of times since
    Step 2.2; this is the one dedicated test where onboarding itself,
    not something built on top of it, is the actual subject.
    """

    def test_freshly_registered_project_accepts_and_correctly_scopes_a_candidate(
        self, api_client, real_credential_headers
    ):
        from app.storage import get_connection

        project_id = "test-onboarding-e2e-pytest"

        def cleanup():
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM audit_log WHERE memory_id IN "
                        "(SELECT memory_id FROM memory_candidates WHERE owning_project = %s)",
                        (project_id,),
                    )
                    cur.execute(
                        "DELETE FROM memory_events WHERE memory_id IN "
                        "(SELECT memory_id FROM memory_candidates WHERE owning_project = %s)",
                        (project_id,),
                    )
                    cur.execute("DELETE FROM memory_candidates WHERE owning_project = %s", (project_id,))
                    cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
                conn.commit()

        cleanup()
        try:
            # Register a genuinely new project through the real endpoint --
            # not storage.insert_project() directly, since onboarding IS
            # the thing under test here.
            create_resp = api_client.post(
                "/memory/projects",
                headers=real_credential_headers,
                json={"project_id": project_id, "display_name": "Onboarding E2E Pytest"},
            )
            assert create_resp.status_code == 201
            assert create_resp.json()["dormant"] is False

            # Submit a candidate scoped to it, immediately, with no
            # intervening code change of any kind
            submit_resp = api_client.post(
                "/memory/candidate",
                headers=real_credential_headers,
                json={
                    "owning_project": project_id, "visibility": "project_local", "retention": "long_term",
                    "memory_type": "fact", "content": {"text": "Onboarding end-to-end test content."},
                    "source_type": "user", "source_ref": "/tmp/onboarding-test.md", "policy_version": "v1",
                    "confidence": {"score": 0.95, "method": "human_verified"},
                },
            )
            assert submit_resp.status_code == 201
            memory_id = submit_resp.json()["memory_id"]
            assert submit_resp.json()["status"] == "promoted"

            # Confirm the candidate is correctly, genuinely scoped to the
            # new project -- not just that submission returned 201
            get_resp = api_client.get(f"/memory/{memory_id}", headers=real_credential_headers)
            assert get_resp.status_code == 200
            assert get_resp.json()["owning_project"] == project_id

            # Confirm it shows up correctly when filtering the list by
            # this specific project -- proving real, queryable scoping,
            # not just a field sitting unused on the row
            list_resp = api_client.get(
                f"/memory?owning_project={project_id}", headers=real_credential_headers
            )
            assert list_resp.status_code == 200
            listed_ids = [c["memory_id"] for c in list_resp.json()]
            assert memory_id in listed_ids
        finally:
            cleanup()
