CREATE TABLE consolidation_proposals (
  id TEXT PRIMARY KEY NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  input_raw_ids TEXT NOT NULL,
  input_fingerprints TEXT NOT NULL,
  proposals TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  decision_reason TEXT,
  created_at INTEGER NOT NULL,
  decided_at INTEGER
);
CREATE INDEX consolidation_proposals_status_created_idx
  ON consolidation_proposals(status, created_at);
