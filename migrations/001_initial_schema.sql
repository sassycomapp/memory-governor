CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    dormant BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memory_candidates (
    memory_id TEXT PRIMARY KEY,
    owning_project TEXT NOT NULL REFERENCES projects(project_id),
    agent_id TEXT,
    session_id TEXT,
    scope JSONB NOT NULL,
    memory_type TEXT NOT NULL,
    subject JSONB,
    content JSONB NOT NULL,
    provenance JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    valid_until_reason TEXT,
    confidence JSONB NOT NULL,
    policy JSONB NOT NULL,
    status TEXT NOT NULL,
    supersedes_id TEXT REFERENCES memory_candidates(memory_id),
    contradiction JSONB,
    review JSONB,
    audit JSONB
);
CREATE INDEX idx_mc_project ON memory_candidates(owning_project);
CREATE INDEX idx_mc_status ON memory_candidates(status);

CREATE TABLE policy_versions (
    policy_version TEXT PRIMARY KEY,
    thresholds JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by TEXT NOT NULL
);

CREATE TABLE memory_events (
    event_id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    memory_id TEXT REFERENCES memory_candidates(memory_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    processed_by TEXT,
    attempt_count INT NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX idx_events_unprocessed ON memory_events(created_at) WHERE processed_at IS NULL;
