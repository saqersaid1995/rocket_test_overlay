PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organisations (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED','DISABLED')),
    created_at TEXT NOT NULL,
    UNIQUE (organisation_id, email)
);

CREATE TABLE IF NOT EXISTS role_assignments (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role_code TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    granted_by TEXT NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS programmes (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    accountable_manager_id TEXT REFERENCES users(id),
    lifecycle_state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE (organisation_id, code)
);

CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    programme_id TEXT NOT NULL REFERENCES programmes(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    mission_type TEXT NOT NULL,
    objective TEXT NOT NULL,
    success_criteria TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    launch_site TEXT,
    window_open TEXT,
    window_close TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organisation_id, code)
);

CREATE TABLE IF NOT EXISTS configuration_items (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    item_number TEXT NOT NULL,
    name TEXT NOT NULL,
    item_type TEXT NOT NULL,
    parent_item_id TEXT REFERENCES configuration_items(id),
    created_at TEXT NOT NULL,
    UNIQUE (organisation_id, item_number)
);

CREATE TABLE IF NOT EXISTS configuration_revisions (
    id TEXT PRIMARY KEY,
    configuration_item_id TEXT NOT NULL REFERENCES configuration_items(id),
    revision TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    content_hash TEXT,
    released_by TEXT REFERENCES users(id),
    released_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (configuration_item_id, revision)
);

CREATE TABLE IF NOT EXISTS serialised_assets (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    configuration_item_id TEXT NOT NULL REFERENCES configuration_items(id),
    serial_number TEXT NOT NULL,
    lot_number TEXT,
    acceptance_state TEXT NOT NULL,
    location TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (organisation_id, serial_number)
);

CREATE TABLE IF NOT EXISTS test_campaigns (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    programme_id TEXT NOT NULL REFERENCES programmes(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    test_type TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    test_director_id TEXT REFERENCES users(id),
    planned_start TEXT,
    planned_end TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organisation_id, code)
);

CREATE TABLE IF NOT EXISTS test_runs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES test_campaigns(id),
    run_number INTEGER NOT NULL,
    test_article_revision_id TEXT NOT NULL REFERENCES configuration_revisions(id),
    lifecycle_state TEXT NOT NULL,
    scheduled_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result TEXT CHECK (result IS NULL OR result IN ('PASS','FAIL','PARTIAL','INVALID')),
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (campaign_id, run_number)
);

CREATE TABLE IF NOT EXISTS launch_campaigns (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    mission_id TEXT NOT NULL REFERENCES missions(id),
    flight_configuration_revision_id TEXT REFERENCES configuration_revisions(id),
    lifecycle_state TEXT NOT NULL,
    launch_director_id TEXT REFERENCES users(id),
    rso_id TEXT REFERENCES users(id),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hazards (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    programme_id TEXT REFERENCES programmes(id),
    mission_id TEXT REFERENCES missions(id),
    hazard_number TEXT NOT NULL,
    title TEXT NOT NULL,
    cause TEXT NOT NULL,
    consequence TEXT NOT NULL,
    initial_severity INTEGER NOT NULL,
    initial_likelihood INTEGER NOT NULL,
    controls TEXT NOT NULL,
    residual_severity INTEGER NOT NULL,
    residual_likelihood INTEGER NOT NULL,
    lifecycle_state TEXT NOT NULL,
    owner_id TEXT REFERENCES users(id),
    accepted_by TEXT REFERENCES users(id),
    accepted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (organisation_id, hazard_number)
);

CREATE TABLE IF NOT EXISTS controlled_documents (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    document_number TEXT NOT NULL,
    title TEXT NOT NULL,
    document_type TEXT NOT NULL,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (organisation_id, document_number)
);

CREATE TABLE IF NOT EXISTS document_revisions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES controlled_documents(id),
    revision TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    object_key TEXT,
    sha256 TEXT NOT NULL,
    authored_by TEXT NOT NULL REFERENCES users(id),
    approved_by TEXT REFERENCES users(id),
    approved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, revision)
);

CREATE TABLE IF NOT EXISTS readiness_gates (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    gate_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    result TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    gate_id TEXT REFERENCES readiness_gates(id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_revision TEXT NOT NULL,
    authority_role TEXT NOT NULL,
    approver_id TEXT NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVE','REJECT','ABSTAIN')),
    conditions TEXT,
    meaning TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    owner_id TEXT REFERENCES users(id),
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    due_at TEXT,
    closed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS evidence_objects (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    captured_at TEXT,
    uploaded_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    actor_id TEXT REFERENCES users(id),
    acting_role TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    previous_version INTEGER,
    new_version INTEGER,
    before_hash TEXT,
    after_hash TEXT,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_missions_programme ON missions(programme_id, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_tests_programme ON test_campaigns(programme_id, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_assets_configuration ON serialised_assets(configuration_item_id, acceptance_state);
CREATE INDEX IF NOT EXISTS idx_hazards_mission ON hazards(mission_id, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, sequence);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id, sequence);

