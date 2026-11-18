CREATE TABLE raw_memories (
  id TEXT PRIMARY KEY NOT NULL,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  session_id TEXT,
  occurred_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  content TEXT NOT NULL,
  media_refs TEXT NOT NULL,
  metadata TEXT NOT NULL,
  search_text TEXT NOT NULL DEFAULT '',
  processing_status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL
);
CREATE INDEX raw_memories_status_occurred_idx
  ON raw_memories(processing_status, occurred_at);

CREATE TABLE memories (
  id TEXT PRIMARY KEY NOT NULL,
  content TEXT NOT NULL,
  brief_content TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  tags TEXT NOT NULL,
  entities TEXT NOT NULL,
  importance REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  accessibility REAL NOT NULL DEFAULT 1 CHECK (accessibility BETWEEN 0 AND 1),
  accessibility_updated_at INTEGER NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0 CHECK (access_count >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_accessed_at INTEGER,
  last_confirmed_at INTEGER,
  valid_from INTEGER,
  valid_until INTEGER,
  decay_rate REAL NOT NULL DEFAULT 0.01 CHECK (decay_rate >= 0),
  temporal_type TEXT NOT NULL DEFAULT 'stable',
  origin TEXT NOT NULL DEFAULT 'explicit',
  status TEXT NOT NULL DEFAULT 'active',
  retention_policy TEXT NOT NULL DEFAULT 'normal',
  created_by TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
);
CREATE INDEX memories_status_importance_idx ON memories(status, importance);
CREATE INDEX memories_last_accessed_idx ON memories(last_accessed_at);
CREATE INDEX memories_type_status_idx ON memories(memory_type, status);
CREATE INDEX memories_valid_until_idx ON memories(valid_until);

CREATE TABLE memory_sources (
  id TEXT PRIMARY KEY NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  raw_memory_id TEXT REFERENCES raw_memories(id) ON DELETE SET NULL,
  evidence_type TEXT NOT NULL,
  evidence_text TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  created_at INTEGER NOT NULL
);
CREATE INDEX memory_sources_memory_idx ON memory_sources(memory_id);
CREATE INDEX memory_sources_raw_idx ON memory_sources(raw_memory_id);

CREATE TABLE memory_relations (
  id TEXT PRIMARY KEY NOT NULL,
  source_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  target_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  origin TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX memory_relations_source_idx ON memory_relations(source_memory_id);
CREATE INDEX memory_relations_target_idx ON memory_relations(target_memory_id);

CREATE TABLE memory_versions (
  id TEXT PRIMARY KEY NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  metadata TEXT NOT NULL,
  change_reason TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(memory_id, version)
);

CREATE TABLE memory_indexes (
  memory_id TEXT PRIMARY KEY NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  search_text TEXT NOT NULL,
  keywords TEXT NOT NULL,
  index_version INTEGER NOT NULL DEFAULT 1,
  indexed_at INTEGER NOT NULL
);

CREATE VIRTUAL TABLE memory_search_fts USING fts5(
  memory_id UNINDEXED,
  search_text,
  tokenize='unicode61'
);
CREATE VIRTUAL TABLE raw_memory_search_fts USING fts5(
  raw_memory_id UNINDEXED,
  search_text,
  tokenize='unicode61'
);

CREATE TABLE memory_contexts (
  id TEXT PRIMARY KEY NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT,
  scene TEXT,
  token_budget INTEGER NOT NULL DEFAULT 1200 CHECK (token_budget > 0),
  tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL,
  closed_at INTEGER
);

CREATE TABLE context_memory_loads (
  context_id TEXT NOT NULL REFERENCES memory_contexts(id) ON DELETE CASCADE,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  load_level TEXT NOT NULL,
  estimated_tokens INTEGER NOT NULL DEFAULT 0,
  loaded_at INTEGER NOT NULL,
  PRIMARY KEY(context_id, memory_id)
);
CREATE TABLE context_raw_memory_loads (
  context_id TEXT NOT NULL REFERENCES memory_contexts(id) ON DELETE CASCADE,
  raw_memory_id TEXT NOT NULL REFERENCES raw_memories(id) ON DELETE CASCADE,
  estimated_tokens INTEGER NOT NULL DEFAULT 0,
  loaded_at INTEGER NOT NULL,
  PRIMARY KEY(context_id, raw_memory_id)
);
CREATE TABLE context_relation_loads (
  context_id TEXT NOT NULL REFERENCES memory_contexts(id) ON DELETE CASCADE,
  relation_id TEXT NOT NULL REFERENCES memory_relations(id) ON DELETE CASCADE,
  estimated_tokens INTEGER NOT NULL DEFAULT 0,
  loaded_at INTEGER NOT NULL,
  PRIMARY KEY(context_id, relation_id)
);

CREATE TABLE consolidation_jobs (
  id TEXT PRIMARY KEY NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  job_type TEXT NOT NULL,
  input_ids TEXT NOT NULL,
  checkpoint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  retry_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX consolidation_jobs_status_created_idx
  ON consolidation_jobs(status, created_at);

CREATE TABLE memory_change_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id TEXT,
  change_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  processing_status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL
);
CREATE INDEX memory_change_logs_status_created_idx
  ON memory_change_logs(processing_status, created_at);

CREATE TABLE evaluator_proposals (
  id TEXT PRIMARY KEY NOT NULL,
  task_type TEXT NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  input_version INTEGER NOT NULL,
  proposal TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  decision_reason TEXT,
  created_at INTEGER NOT NULL,
  decided_at INTEGER
);
CREATE INDEX evaluator_proposals_status_created_idx
  ON evaluator_proposals(status, created_at);
CREATE INDEX evaluator_proposals_memory_idx ON evaluator_proposals(memory_id);
