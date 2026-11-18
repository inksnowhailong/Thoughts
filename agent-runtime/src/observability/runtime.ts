import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import Database from 'better-sqlite3';
import { REPO_ROOT } from '../tools/paths.js';

export type ObservationStatus = 'running' | 'waiting' | 'completed' | 'failed';

export interface RuntimeEvent {
  eventId: string;
  traceId: string;
  spanId?: string;
  parentSpanId?: string;
  producerId: string;
  producerSequence: number;
  hubOffset: number;
  eventType: string;
  nodeId?: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface ObservedSpan {
  spanId: string;
  traceId: string;
  parentSpanId: string | null;
  nodeId: string;
  name: string;
  status: ObservationStatus;
  startedAt: string;
  endedAt: string | null;
  durationMs: number | null;
  input: unknown;
  output: unknown;
  error: string | null;
}

export interface ObservedTrace {
  traceId: string;
  sessionId: string;
  status: ObservationStatus;
  startedAt: string;
  endedAt: string | null;
  durationMs: number | null;
  input: unknown;
  output: unknown;
  error: string | null;
  spans: ObservedSpan[];
}

export const DEFAULT_OBSERVABILITY_DATABASE_PATH = path.join(
  REPO_ROOT,
  'data',
  'agent-runtime',
  'observability.sqlite',
);

export const OBSERVABILITY_NODES = [
  { id: 'agent-runtime', name: 'AGENT RUNTIME', type: 'processor', zone: 'RUNTIME' },
  { id: 'main-agent', name: 'MAIN AGENT', type: 'agent', zone: 'RUNTIME' },
  { id: 'memory-engine', name: 'MEMORY ENGINE', type: 'memory', zone: 'MEMORY' },
  { id: 'model-provider', name: 'MODEL PROVIDER', type: 'model', zone: 'PROVIDERS' },
  { id: 'tool-router', name: 'TOOL ROUTER', type: 'tool', zone: 'RUNTIME' },
] as const;

interface TraceRow {
  trace_id: string;
  session_id: string;
  status: ObservationStatus;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  input_json: string;
  output_json: string | null;
  error: string | null;
}

interface SpanRow {
  span_id: string;
  trace_id: string;
  parent_span_id: string | null;
  node_id: string;
  name: string;
  status: ObservationStatus;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  input_json: string;
  output_json: string | null;
  error: string | null;
}

function json(value: unknown): string {
  return JSON.stringify(sanitize(value === undefined ? null : value));
}

function sanitize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      /authorization|cookie|api.?key|access.?token|refresh.?token|credential|password|secret/i.test(key)
        ? '[REDACTED]'
        : sanitize(item),
    ]));
  }
  return value;
}

function parseJson(value: string | null): unknown {
  return value === null ? null : JSON.parse(value) as unknown;
}

function sanitizeError(value: string | null): string | null {
  return value?.replace(
    /(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|credential|password|secret)(\s*[:=]\s*|\s+)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
    '$1=[REDACTED]',
  ) ?? null;
}

export class ObservabilityHub {
  private readonly db: Database.Database;
  private readonly listeners = new Set<(event: RuntimeEvent) => void>();
  private producerSequence = 0;

  constructor(databasePath = DEFAULT_OBSERVABILITY_DATABASE_PATH) {
    if (databasePath !== ':memory:') mkdirSync(path.dirname(path.resolve(databasePath)), { recursive: true });
    this.db = new Database(databasePath);
    this.db.pragma('journal_mode = WAL');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS observability_traces (
        trace_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_ms INTEGER,
        input_json TEXT NOT NULL,
        output_json TEXT,
        error TEXT
      );
      CREATE INDEX IF NOT EXISTS observability_traces_started_idx
        ON observability_traces(started_at DESC);
      CREATE TABLE IF NOT EXISTS observability_spans (
        span_id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL REFERENCES observability_traces(trace_id) ON DELETE CASCADE,
        parent_span_id TEXT,
        node_id TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_ms INTEGER,
        input_json TEXT NOT NULL,
        output_json TEXT,
        error TEXT
      );
      CREATE INDEX IF NOT EXISTS observability_spans_trace_idx
        ON observability_spans(trace_id, started_at);
      CREATE TABLE IF NOT EXISTS observability_events (
        hub_offset INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        trace_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_json TEXT NOT NULL
      );
    `);
    this.producerSequence = (this.db.prepare(`
      SELECT COALESCE(MAX(hub_offset), 0) AS watermark FROM observability_events
    `).get() as { watermark: number }).watermark;
  }

  startTrace(input: { sessionId: string; userText: string; model: string }): TraceRecorder {
    const traceId = randomUUID();
    const startedAt = new Date().toISOString();
    try {
      this.db.prepare(`
        INSERT INTO observability_traces
          (trace_id, session_id, status, started_at, input_json)
        VALUES (?, ?, 'running', ?, ?)
      `).run(traceId, input.sessionId, startedAt, json(input));
      this.emit(traceId, 'trace.started', undefined, undefined, 'agent-runtime', { input });
    } catch (error) {
      this.reportFailure('start trace', error);
    }
    return new TraceRecorder(this, traceId, input.sessionId, startedAt, input);
  }

  createSpan(
    traceId: string,
    parentSpanId: string | null,
    nodeId: string,
    name: string,
    input: unknown,
  ): SpanRecorder {
    const spanId = randomUUID();
    const startedAt = new Date().toISOString();
    try {
      this.db.prepare(`
        INSERT INTO observability_spans
          (span_id, trace_id, parent_span_id, node_id, name, status, started_at, input_json)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
      `).run(spanId, traceId, parentSpanId, nodeId, name, startedAt, json(input));
      this.emit(traceId, 'span.started', spanId, parentSpanId, nodeId, { name, input });
    } catch (error) {
      this.reportFailure('start span', error);
    }
    return new SpanRecorder({ traceId, spanId, parentSpanId, nodeId, name, startedAt, input }, this);
  }

  finishSpan(span: {
    traceId: string;
    spanId: string;
    parentSpanId: string | null;
    nodeId: string;
    name: string;
    startedAt: string;
    input: unknown;
  }, status: ObservationStatus, output: unknown, error: string | null): void {
    const endedAt = new Date().toISOString();
    const durationMs = Math.max(0, Date.parse(endedAt) - Date.parse(span.startedAt));
    const safeError = sanitizeError(error);
    try {
      this.db.prepare(`
        UPDATE observability_spans
        SET status = ?, ended_at = ?, duration_ms = ?, output_json = ?, error = ?
        WHERE span_id = ?
      `).run(status, endedAt, durationMs, json(output), safeError, span.spanId);
      const eventType = status === 'failed' ? 'span.failed'
        : status === 'waiting' ? 'span.waiting'
          : 'span.completed';
      this.emit(span.traceId, eventType,
        span.spanId, span.parentSpanId, span.nodeId, { name: span.name, status, durationMs, output, error: safeError });
    } catch (failure) {
      this.reportFailure('finish span', failure);
    }
  }

  finishTrace(
    trace: { traceId: string; startedAt: string },
    status: ObservationStatus,
    output: unknown,
    error: string | null,
  ): void {
    const endedAt = new Date().toISOString();
    const durationMs = Math.max(0, Date.parse(endedAt) - Date.parse(trace.startedAt));
    const safeError = sanitizeError(error);
    try {
      this.db.prepare(`
        UPDATE observability_traces
        SET status = ?, ended_at = ?, duration_ms = ?, output_json = ?, error = ?
        WHERE trace_id = ?
      `).run(status, endedAt, durationMs, json(output), safeError, trace.traceId);
      this.emit(trace.traceId, status === 'failed' ? 'trace.failed' : 'trace.completed',
        undefined, undefined, 'agent-runtime', { status, durationMs, output, error: safeError });
    } catch (failure) {
      this.reportFailure('finish trace', failure);
    }
  }

  listTraces(limit = 50): Omit<ObservedTrace, 'spans'>[] {
    const rows = this.db.prepare(`
      SELECT * FROM observability_traces ORDER BY started_at DESC LIMIT ?
    `).all(limit) as TraceRow[];
    return rows.map((row) => this.toTrace(row));
  }

  getTrace(traceId: string): ObservedTrace | null {
    const row = this.db.prepare('SELECT * FROM observability_traces WHERE trace_id = ?')
      .get(traceId) as TraceRow | undefined;
    if (!row) return null;
    const spans = this.db.prepare(`
      SELECT * FROM observability_spans WHERE trace_id = ? ORDER BY started_at
    `).all(traceId) as SpanRow[];
    return { ...this.toTrace(row), spans: spans.map((span) => this.toSpan(span)) };
  }

  snapshot(memoryEngineOnline: boolean): {
    watermark: number;
    nodes: Array<Record<string, unknown>>;
    activeTraces: Omit<ObservedTrace, 'spans'>[];
    recentTraces: Omit<ObservedTrace, 'spans'>[];
    recentEvents: RuntimeEvent[];
    systemStatus: { runtime: 'online'; memoryEngine: 'online' | 'offline' };
  } {
    const activeNodeIds = new Set((this.db.prepare(`
      SELECT DISTINCT node_id FROM observability_spans WHERE status = 'running'
    `).all() as Array<{ node_id: string }>).map((row) => row.node_id));
    const observedNodeIds = new Set((this.db.prepare(`
      SELECT DISTINCT node_id FROM observability_spans
    `).all() as Array<{ node_id: string }>).map((row) => row.node_id));
    const watermark = (this.db.prepare(`
      SELECT COALESCE(MAX(hub_offset), 0) AS watermark FROM observability_events
    `).get() as { watermark: number }).watermark;
    const recentTraces = this.listTraces(30);
    return {
      watermark,
      nodes: OBSERVABILITY_NODES.map((node) => ({
        ...node,
        health: node.id === 'agent-runtime' || (node.id === 'memory-engine' && memoryEngineOnline)
          ? 'online'
          : node.id === 'memory-engine' ? 'offline' : 'unknown',
        execution: activeNodeIds.has(node.id) ? 'running' : 'idle',
        observed: observedNodeIds.has(node.id),
      })),
      activeTraces: recentTraces.filter((trace) => trace.status === 'running'),
      recentTraces,
      recentEvents: this.recentEvents(50),
      systemStatus: { runtime: 'online', memoryEngine: memoryEngineOnline ? 'online' : 'offline' },
    };
  }

  recentEvents(limit = 50): RuntimeEvent[] {
    const rows = this.db.prepare(`
      SELECT event_json FROM observability_events ORDER BY hub_offset DESC LIMIT ?
    `).all(limit) as Array<{ event_json: string }>;
    return rows.reverse().map((row) => JSON.parse(row.event_json) as RuntimeEvent);
  }

  eventsSince(offset: number, limit = 1000): { events: RuntimeEvent[]; hasMore: boolean } {
    const rows = this.db.prepare(`
      SELECT event_json FROM observability_events
      WHERE hub_offset > ? ORDER BY hub_offset LIMIT ?
    `).all(offset, limit + 1) as Array<{ event_json: string }>;
    return {
      events: rows.slice(0, limit).map((row) => JSON.parse(row.event_json) as RuntimeEvent),
      hasMore: rows.length > limit,
    };
  }

  subscribe(listener: (event: RuntimeEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close(): void {
    this.db.close();
  }

  private emit(
    traceId: string,
    eventType: string,
    spanId: string | undefined,
    parentSpanId: string | null | undefined,
    nodeId: string | undefined,
    payload: Record<string, unknown>,
  ): void {
    try {
      const eventId = randomUUID();
      const event = {
        eventId,
        traceId,
        ...(spanId ? { spanId } : {}),
        ...(parentSpanId ? { parentSpanId } : {}),
        producerId: 'agent-runtime',
        producerSequence: ++this.producerSequence,
        hubOffset: 0,
        eventType,
        ...(nodeId ? { nodeId } : {}),
        timestamp: new Date().toISOString(),
        payload: JSON.parse(json(payload)) as Record<string, unknown>,
      } satisfies RuntimeEvent;
      const transaction = this.db.transaction(() => {
        const result = this.db.prepare(`
          INSERT INTO observability_events (event_id, trace_id, event_type, event_json)
          VALUES (?, ?, ?, '')
        `).run(eventId, traceId, eventType);
        const persisted = { ...event, hubOffset: Number(result.lastInsertRowid) };
        this.db.prepare('UPDATE observability_events SET event_json = ? WHERE event_id = ?')
          .run(JSON.stringify(persisted), eventId);
        return persisted;
      });
      const persisted = transaction();
      for (const listener of this.listeners) listener(persisted);
    } catch (error) {
      this.reportFailure('persist event', error);
    }
  }

  private toTrace(row: TraceRow): Omit<ObservedTrace, 'spans'> {
    return {
      traceId: row.trace_id,
      sessionId: row.session_id,
      status: row.status,
      startedAt: row.started_at,
      endedAt: row.ended_at,
      durationMs: row.duration_ms,
      input: parseJson(row.input_json),
      output: parseJson(row.output_json),
      error: row.error,
    };
  }

  private toSpan(row: SpanRow): ObservedSpan {
    return {
      spanId: row.span_id,
      traceId: row.trace_id,
      parentSpanId: row.parent_span_id,
      nodeId: row.node_id,
      name: row.name,
      status: row.status,
      startedAt: row.started_at,
      endedAt: row.ended_at,
      durationMs: row.duration_ms,
      input: parseJson(row.input_json),
      output: parseJson(row.output_json),
      error: row.error,
    };
  }

  private reportFailure(action: string, error: unknown): void {
    console.error(`[observability] failed to ${action}:`, error);
  }
}

export class SpanRecorder {
  private ended = false;

  constructor(private readonly data: {
    traceId: string;
    spanId: string;
    parentSpanId: string | null;
    nodeId: string;
    name: string;
    startedAt: string;
    input: unknown;
  }, private readonly hub: ObservabilityHub) {}

  get id(): string {
    return this.data.spanId;
  }

  finish(status: ObservationStatus, output: unknown = null, error: string | null = null): void {
    if (this.ended) return;
    this.ended = true;
    this.hub.finishSpan(this.data, status, output, error);
  }
}

export class TraceRecorder {
  private readonly rootSpan: SpanRecorder;
  private finished = false;

  constructor(
    private readonly hub: ObservabilityHub,
    readonly traceId: string,
    readonly sessionId: string,
    readonly startedAt: string,
    input: unknown,
  ) {
    this.rootSpan = hub.createSpan(traceId, null, 'agent-runtime', 'CHAT COMPLETION', input);
  }

  startSpan(nodeId: string, name: string, input: unknown, parentSpanId = this.rootSpan.id): SpanRecorder {
    return this.hub.createSpan(this.traceId, parentSpanId, nodeId, name, input);
  }

  finish(status: ObservationStatus, output: unknown = null, error: string | null = null): void {
    if (this.finished) return;
    this.finished = true;
    this.rootSpan.finish(status, output, error);
    this.hub.finishTrace(this, status, output, error);
  }
}
