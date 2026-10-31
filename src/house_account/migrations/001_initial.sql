CREATE TABLE receipts (
    id TEXT PRIMARY KEY,
    image_name TEXT NOT NULL,
    ocr_image_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE ocr_runs (
    id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'no_candidates', 'ocr_failed',
        'extraction_failed', 'timeout', 'interrupted'
    )),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    raw_json TEXT,
    candidates_json TEXT,
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE receipt_items (
    id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    source_run_id TEXT REFERENCES ocr_runs(id) ON DELETE SET NULL,
    source_candidate_id TEXT
);

CREATE TABLE item_tags (
    item_id TEXT NOT NULL REFERENCES receipt_items(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE RESTRICT,
    PRIMARY KEY (item_id, tag_id)
);

CREATE INDEX ocr_runs_queue ON ocr_runs(status, created_at);
CREATE INDEX ocr_runs_receipt ON ocr_runs(receipt_id, created_at DESC);
CREATE INDEX receipt_items_order ON receipt_items(receipt_id, position);
