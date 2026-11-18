# Mission Control / Observability

The local dashboard is served at `http://127.0.0.1:18800/mission-control`.
It shows real chat-completion traces, model requests and responses, Memory
Engine calls, tool calls, live span events, and the current Memory Engine
records. The Memory view is read-only and pages directly against the Memory
Engine database.

The Agent Runtime persists trace, span, and event records in
`data/agent-runtime/observability.sqlite`. The snapshot includes a hub
watermark; the SSE endpoint replays persisted events after that offset and
continues with live events. Event capture is best-effort: persistence errors
are logged and do not fail an Agent request.

The page bootstraps a same-site, HttpOnly cookie scoped to
`/api/observability`. The runtime remains bound to loopback. Trace content can
include full prompts, user messages, model output, and tool arguments; treat
the database and dashboard as private local data.
Automatic retention and database cleanup are not implemented yet, so the file
can grow over time.

## Current coverage

- The OpenAI-compatible chat route creates a trace.
- Main Agent turns, model invocations, Memory Engine raw writes/recalls, and
  tool calls emit spans with parent relationships and captured input/output.
- The dashboard supports live SSE updates, trace details, raw payload viewing,
  a Gantt-style span timeline, node status, and paginated Memory browsing.
- The fixed topology shows code-level call boundaries. A node is highlighted
  only while a corresponding span is observed.

Voice capture/ASR/TTS, cross-process Worker traces, provider transport payloads,
artifact streaming, and replay controls are not instrumented yet. The UI does
not synthesize events for those paths.
