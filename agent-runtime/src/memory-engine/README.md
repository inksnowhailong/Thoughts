# Memory Engine

The SQLite-backed Memory Engine starts by default with the Agent Runtime and
can also run independently with `npm run build` followed by
`npm run memory-engine`. Its default database is
`data/agent-runtime/memory-engine.sqlite`; it does not import or modify legacy
memory databases. The standalone API binds to `127.0.0.1:18802`.
The Main Agent records each user turn and recalls relevant memories. Worker
tasks record their task specification and merge recalled summaries into the
provided memory packet, up to its eight-summary limit.

The only required AI setting is `SILICONFLOW_API_KEY`. Local storage, FTS,
contexts, routes, scheduler, and worker work without it; when present, model
tasks and embedding generation use SiliconFlow. Raw conversation text is sent
to SiliconFlow for extraction, and extracted memories are promoted
automatically after exact evidence validation.

## Database and search

SQLite uses WAL mode, foreign-key enforcement, a migration ledger, and FTS5
at initialization. Recall combines FTS5, lexical ranking, and stored
embedding vectors. Embeddings are generated automatically for new/updated
memories and backfilled on startup. SiliconFlow embedding models are attempted
in order: `Qwen/Qwen3-Embedding-8B`, then `BAAI/bge-m3`.

The evaluator uses the SiliconFlow OpenAI-compatible endpoint by default.
Importance/classification tries `XingChenAGI/XingChenASR-V3.2-Ultra`, then
`Qwen/Qwen3-8B`; consolidation tries `XingChenAGI/Xing4.0-29B`, then
`THUDM/GLM-Z1-9B-0414`. Each candidate is verified by an actual model request;
the first usable model is recorded with its proposal. The 12-hour scheduler,
20-item batches, and single-job worker start automatically.

## API

Agent Runtime routes require its bearer token. The standalone Memory API binds
to loopback and does not add a separate user/tenant authentication layer.

- `POST /api/v1/contexts`, `GET /api/v1/contexts/:contextId`,
  `POST /api/v1/contexts/:contextId/close`
- `POST /api/v1/memories/raw`
- `POST /api/v1/memories`
- `PATCH /api/v1/memories/:id` (requires `expectedVersion`; saves a version)
- `POST /api/v1/recall`

Recall combines FTS5, lexical, and vector retrieval using reciprocal-rank fusion, then
applies importance, recency, accessibility, scene relevance, and confidence.
Context loads are transactionally deduplicated. A loaded Brief can be upgraded
to Full, while Full memories are not returned again unless `refresh` is
explicitly requested. The token budget is shared across each Context; usage is
estimated conservatively (four ASCII characters or one non-ASCII code point
per token) plus a fixed per-memory cost.

`POST /api/v1/recall/expand` retrieves full memory content, provenance,
relations, and conflict evidence. `POST /api/v1/recall/deep` broadens candidate
search to low-accessibility memories, can expand a supplied time range, seeds
search from related memory IDs, and searches matching raw events after canonical
recall. Raw-event results are also deduplicated and charged to the Context
budget.

## Evaluation, lifecycle, and consolidation

- `POST /internal/evaluator/{importance|relations|conflicts|duplicates|retention}` evaluates
  and records a validated proposal. Duplicate, relation, and conflict decisions are
  automatically applied after target, evidence, and version checks. Evaluator
  proposals retain the memory and candidate versions plus the selected model.
- `GET /internal/evaluator/proposals` lists proposals;
  `POST /internal/evaluator/proposals/:id/decision` accepts or rejects one.
  Acceptance rechecks versions and evidence IDs before committing changes.
- `POST /api/v1/lifecycle/run` applies deterministic accessibility decay and
  reports forget/archive candidates. It does not automatically delete or
  archive them. Explicit `/archive`, `/restore`, and confirmed `DELETE` routes
  preserve version or deletion audit records.
- `POST /api/v1/consolidation/trigger` queues bounded, idempotent jobs;
  `/jobs`, `/jobs/:id`, and `/jobs/:id/retry` expose status and recovery.
- `GET /internal/consolidation/proposals` lists extraction proposals;
  `POST /internal/consolidation/proposals/:id/decision` remains available for
  compatibility; normal consolidation accepts extracted memories automatically
  after source fingerprints and exact evidence quotes are rechecked. The worker
  indexes raw events, extracts canonical memories, runs evaluator tasks, and applies
  lifecycle decay. Job retries are bounded and checkpoint completed memory IDs.

Extraction uses a bounded batch: source IDs must belong to the batch, and
evidence quotes must match the original event before the backend promotes
derived memories with source links and version history. Exact duplicate
memories reuse the existing record. The `duplicates` evaluator task can identify and
automatically merge semantically equivalent canonical memories. It is included
in `all` jobs when an evaluator is configured and can also be queued directly.
The primary memory's content is retained, missing source evidence is copied,
both sides are versioned, and duplicates are marked `superseded`; no record is
physically deleted. Relation decisions are also automatically applied.
Conflict evaluation compares one candidate at a time: when evidence supports a
winner, the losing memory is marked `superseded`; otherwise both memories stay
active with a `contradicts` relation. Candidate discovery uses shared
tags/entities plus available embeddings. Raw-memory retention cleanup is not
implemented yet.
