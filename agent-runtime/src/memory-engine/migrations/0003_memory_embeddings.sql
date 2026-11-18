CREATE TABLE memory_embeddings (
  memory_id TEXT PRIMARY KEY NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  vector TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
