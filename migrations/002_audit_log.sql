CREATE TABLE audit_log (
    audit_id          BIGSERIAL PRIMARY KEY,
    memory_id         TEXT NOT NULL REFERENCES memory_candidates(memory_id),
    previous_status   TEXT NOT NULL,
    new_status        TEXT NOT NULL,
    decision_by       TEXT NOT NULL,
    decision_reason   TEXT,
    reviewer          TEXT,
    policy_version    TEXT,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_log_memory_id ON audit_log(memory_id);
