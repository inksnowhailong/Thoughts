# Mission Control / Observability

The local dashboard is served at `http://127.0.0.1:18800/mission-control`.
It shows voice recognition, Main Agent and Worker runs, model/tool calls,
Memory reads and writes, consolidation/extraction, node health, and a
read-only Memory Explorer.

Trace, span, and event projections are stored in
`data/agent-runtime/observability.sqlite`; artifact bodies are stored in the
adjacent `artifacts` directory. Python voice instrumentation uses a bounded,
durable SQLite outbox and retries delivery to the local Agent Runtime. Event
IDs make redelivery idempotent. The dashboard snapshot and SSE stream use a
persisted hub offset, and the stream requests a resnapshot if its replay window
has expired.

Historical traces support paging, pinning, event-by-event playback, and
artifact retrieval in bounded byte ranges. Artifact bodies can be searched,
copied, or exported from the inspector. A daily cleanup removes completed,
unpinned traces and their artifacts after 30 days; the System view exposes
storage statistics and manual cleanup.

The page bootstraps a same-site, HttpOnly cookie scoped to
`/api/observability`. The runtime remains bound to loopback. Trace content can
include full prompts, user messages, model output, and tool arguments; treat
the database, artifact directory, and dashboard as private local data.

Provider and HTTP transport payloads are not available through the current
model SDK instrumentation. The UI labels these fields as not collected rather
than presenting logical model requests as transport data.
