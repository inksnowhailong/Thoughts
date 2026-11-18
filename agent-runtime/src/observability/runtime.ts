import { randomUUID } from 'node:crypto';
import { closeSync, mkdirSync, openSync, readSync, rmSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import { REPO_ROOT } from '../tools/paths.js';

export type ObservationStatus = 'running' | 'waiting' | 'completed' | 'failed';
export type NodeHealthStatus = 'online' | 'offline' | 'degraded' | 'unknown';

export interface NodeDefinition {
  id: string;
  name: string;
  type: string;
  zone: string;
  metadata?: Record<string, unknown>;
}

export interface NodeInstance {
  instanceId: string;
  nodeId: string;
  processId: number | null;
  hostname: string | null;
  health: NodeHealthStatus;
  lastHeartbeat: string | null;
}

export interface ArtifactReference {
  artifactId: string;
  traceId: string;
  spanId: string;
  type: string;
  contentType: string;
  completeness: 'complete' | 'partial' | 'failed';
  byteLength: number;
  sha256: string;
  createdAt: string;
}

export interface ArtifactCaptureResult {
  status: 'complete' | 'partial' | 'failed';
  reference?: ArtifactReference;
  error?: string;
}

export interface EventQuery {
  traceId?: string;
  nodeId?: string;
  eventTypes?: string[];
}

export interface RuntimeEvent {
  eventId: string;
  traceId?: string;
  spanId?: string;
  parentSpanId?: string;
  producerId: string;
  producerSequence: number;
  hubOffset: number;
  eventType: string;
  nodeId?: string;
  instanceId?: string;
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
  artifacts?: ArtifactReference[];
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
  spanCount?: number;
  modelCallCount?: number;
  pinned?: boolean;
}

export interface TraceQuery {
  offset?: number;
  status?: ObservationStatus;
  from?: string;
  to?: string;
}

export const DEFAULT_OBSERVABILITY_DATABASE_PATH = path.join(
  REPO_ROOT,
  'data',
  'agent-runtime',
  'observability.sqlite',
);

export const OBSERVABILITY_NODES = [
  { id: 'voice-runtime', name: 'VOICE RUNTIME', type: 'processor', zone: 'INPUT' },
  { id: 'audio-capture', name: 'AUDIO CAPTURE', type: 'processor', zone: 'INPUT' },
  { id: 'vad', name: 'VOICE ACTIVITY', type: 'processor', zone: 'INPUT' },
  { id: 'asr', name: 'SPEECH RECOGNITION', type: 'processor', zone: 'INPUT' },
  { id: 'speaker-recognition', name: 'SPEAKER RECOGNITION', type: 'processor', zone: 'INPUT' },
  { id: 'agent-runtime', name: 'AGENT RUNTIME', type: 'processor', zone: 'RUNTIME' },
  { id: 'main-agent', name: 'MAIN AGENT', type: 'agent', zone: 'RUNTIME' },
  { id: 'worker-runtime', name: 'WORKER RUNTIME', type: 'worker', zone: 'RUNTIME' },
  { id: 'memory-engine', name: 'MEMORY ENGINE', type: 'memory', zone: 'MEMORY' },
  { id: 'model-provider', name: 'MODEL PROVIDER', type: 'model', zone: 'PROVIDERS' },
  { id: 'tool-router', name: 'TOOL ROUTER', type: 'tool', zone: 'RUNTIME' },
  { id: 'tts', name: 'SPEECH SYNTHESIS', type: 'processor', zone: 'OUTPUT' },
  { id: 'audio-playback', name: 'AUDIO PLAYBACK', type: 'processor', zone: 'OUTPUT' },
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
  pinned?: number;
  span_count?: number;
  model_call_count?: number;
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
  private readonly artifactDirectory: string;
  private readonly temporaryArtifactDirectory: boolean;
  private readonly listeners = new Set<(event: RuntimeEvent) => void>();
  private producerSequence = 0;
  private readonly producerId = `agent-runtime:${process.pid}`;

  constructor(databasePath = DEFAULT_OBSERVABILITY_DATABASE_PATH) {
    if (databasePath !== ':memory:') mkdirSync(path.dirname(path.resolve(databasePath)), { recursive: true });
    this.db = new Database(databasePath);
    this.temporaryArtifactDirectory = databasePath === ':memory:';
    this.artifactDirectory = this.temporaryArtifactDirectory
      ? path.join(tmpdir(), `thoughts-observability-${randomUUID()}`)
      : path.join(path.dirname(path.resolve(databasePath)), 'artifacts');
    mkdirSync(this.artifactDirectory, { recursive: true });
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
        error TEXT,
        pinned INTEGER NOT NULL DEFAULT 0
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
        producer_id TEXT NOT NULL DEFAULT 'agent-runtime',
        producer_sequence INTEGER NOT NULL DEFAULT 0,
        trace_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT ''
      );
      CREATE INDEX IF NOT EXISTS observability_events_trace_idx
        ON observability_events(trace_id, hub_offset);
      CREATE TABLE IF NOT EXISTS observability_nodes (
        node_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        zone TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        registered_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS observability_instances (
        instance_id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        process_id INTEGER,
        hostname TEXT,
        health TEXT NOT NULL,
        last_heartbeat TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
      );
      CREATE INDEX IF NOT EXISTS observability_instances_node_idx
        ON observability_instances(node_id, last_heartbeat DESC);
      CREATE TABLE IF NOT EXISTS observability_artifacts (
        artifact_id TEXT PRIMARY KEY,
        trace_id TEXT NOT NULL,
        span_id TEXT NOT NULL,
        type TEXT NOT NULL,
        content_type TEXT NOT NULL,
        content_path TEXT NOT NULL,
        completeness TEXT NOT NULL,
        byte_length INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        pinned INTEGER NOT NULL DEFAULT 0
      );
      CREATE INDEX IF NOT EXISTS observability_artifacts_trace_idx
        ON observability_artifacts(trace_id, created_at);
    `);
    const eventColumns = new Set((this.db.pragma('table_info(observability_events)') as Array<{ name: string }>)
      .map((column) => column.name));
    if (!eventColumns.has('producer_id')) {
      this.db.exec("ALTER TABLE observability_events ADD COLUMN producer_id TEXT NOT NULL DEFAULT 'agent-runtime'");
    }
    if (!eventColumns.has('producer_sequence')) {
      this.db.exec('ALTER TABLE observability_events ADD COLUMN producer_sequence INTEGER NOT NULL DEFAULT 0');
    }
    if (!eventColumns.has('created_at')) {
      this.db.exec("ALTER TABLE observability_events ADD COLUMN created_at TEXT NOT NULL DEFAULT ''");
    }
    const traceColumns = new Set((this.db.pragma('table_info(observability_traces)') as Array<{ name: string }>)
      .map((column) => column.name));
    if (!traceColumns.has('pinned')) {
      this.db.exec('ALTER TABLE observability_traces ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0');
    }
    this.db.exec(`
      CREATE INDEX IF NOT EXISTS observability_events_created_idx ON observability_events(created_at);
      CREATE INDEX IF NOT EXISTS observability_events_producer_idx
        ON observability_events(producer_id, producer_sequence);
    `);
    const registerNode = this.db.prepare(`
      INSERT OR IGNORE INTO observability_nodes (node_id, name, type, zone, registered_at)
      VALUES (?, ?, ?, ?, ?)
    `);
    const registeredAt = new Date().toISOString();
    for (const node of OBSERVABILITY_NODES) {
      registerNode.run(node.id, node.name, node.type, node.zone, registeredAt);
    }
    this.producerSequence = (this.db.prepare(`
      SELECT COALESCE(MAX(producer_sequence), 0) AS watermark
      FROM observability_events WHERE producer_id = ?
    `).get(this.producerId) as { watermark: number }).watermark;
  }

  startTrace(input: {
    sessionId: string;
    userText: string;
    model: string;
    traceId?: string;
    parentSpanId?: string;
    startedAt?: string;
  }): TraceRecorder {
    const traceId = input.traceId ?? randomUUID();
    const startedAt = input.startedAt ?? new Date().toISOString();
    try {
      const inserted = this.db.prepare(`
        INSERT OR IGNORE INTO observability_traces
          (trace_id, session_id, status, started_at, input_json)
        VALUES (?, ?, 'running', ?, ?)
      `).run(traceId, input.sessionId, startedAt, json(input));
      if (inserted.changes > 0) this.emit(traceId, 'trace.started', undefined, undefined, 'agent-runtime', { input });
    } catch (error) {
      this.reportFailure('start trace', error);
    }
    return new TraceRecorder(this, traceId, input.sessionId, startedAt, input, input.parentSpanId ?? null);
  }

  registerNode(node: NodeDefinition): void {
    this.db.prepare(`
      INSERT INTO observability_nodes (node_id, name, type, zone, metadata_json, registered_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(node_id) DO UPDATE SET
        name = excluded.name, type = excluded.type, zone = excluded.zone,
        metadata_json = excluded.metadata_json
    `).run(node.id, node.name, node.type, node.zone, json(node.metadata ?? {}), new Date().toISOString());
    this.emit(undefined, 'node.registered', undefined, undefined, node.id, {
      name: node.name,
      type: node.type,
      zone: node.zone,
      metadata: node.metadata ?? {},
    });
  }

  registerInstance(input: {
    instanceId: string;
    nodeId: string;
    processId?: number;
    hostname?: string;
    metadata?: Record<string, unknown>;
  }): void {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO observability_instances
        (instance_id, node_id, process_id, hostname, health, last_heartbeat, metadata_json)
      VALUES (?, ?, ?, ?, 'online', ?, ?)
      ON CONFLICT(instance_id) DO UPDATE SET
        node_id = excluded.node_id, process_id = excluded.process_id,
        hostname = excluded.hostname, health = 'online',
        last_heartbeat = excluded.last_heartbeat, metadata_json = excluded.metadata_json
    `).run(input.instanceId, input.nodeId, input.processId ?? null, input.hostname ?? null, now,
      json(input.metadata ?? {}));
    this.emit(undefined, 'node.heartbeat', undefined, undefined, input.nodeId, {
      instanceId: input.instanceId,
      health: 'online',
      processId: input.processId ?? null,
      hostname: input.hostname ?? null,
      metadata: input.metadata ?? {},
    }, input.instanceId);
  }

  updateNodeHealth(nodeId: string, health: NodeHealthStatus, instanceId?: string): void {
    if (instanceId) {
      this.db.prepare(`
        UPDATE observability_instances SET health = ?, last_heartbeat = ?
        WHERE instance_id = ? AND node_id = ?
      `).run(health, new Date().toISOString(), instanceId, nodeId);
    } else {
      this.db.prepare(`
        INSERT INTO observability_instances
          (instance_id, node_id, process_id, hostname, health, last_heartbeat)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(instance_id) DO UPDATE SET
          health = excluded.health, last_heartbeat = excluded.last_heartbeat
      `).run(`${nodeId}:${process.pid}`, nodeId, process.pid, null, health, new Date().toISOString());
    }
    this.emit(undefined, 'node.health_changed', undefined, undefined, nodeId, { health, instanceId }, instanceId);
  }

  listNodes(): Array<NodeDefinition & {
    health: NodeHealthStatus;
    execution: 'idle' | 'running' | 'waiting' | 'error';
    instances: NodeInstance[];
    observed: boolean;
  }> {
    const nodes = this.db.prepare('SELECT * FROM observability_nodes ORDER BY zone, name')
      .all() as Array<{
        node_id: string; name: string; type: string; zone: string; metadata_json: string;
      }>;
    const instanceRows = this.db.prepare(`
      SELECT * FROM observability_instances ORDER BY node_id, last_heartbeat DESC
    `).all() as Array<{
      instance_id: string; node_id: string; process_id: number | null; hostname: string | null;
      health: NodeHealthStatus; last_heartbeat: string | null;
    }>;
    const spans = this.db.prepare(`
      SELECT node_id, status FROM observability_spans
      WHERE status IN ('running', 'waiting', 'failed')
    `).all() as Array<{ node_id: string; status: ObservationStatus }>;
    const spanStates = new Map<string, Set<ObservationStatus>>();
    for (const span of spans) {
      const current = spanStates.get(span.node_id) ?? new Set<ObservationStatus>();
      current.add(span.status);
      spanStates.set(span.node_id, current);
    }
    const instancesByNode = new Map<string, NodeInstance[]>();
    for (const row of instanceRows) {
      const values = instancesByNode.get(row.node_id) ?? [];
      values.push({
        instanceId: row.instance_id,
        nodeId: row.node_id,
        processId: row.process_id,
        hostname: row.hostname,
        health: row.health,
        lastHeartbeat: row.last_heartbeat,
      });
      instancesByNode.set(row.node_id, values);
    }
    return nodes.map((row) => {
      const states = spanStates.get(row.node_id) ?? new Set<ObservationStatus>();
      const instances = instancesByNode.get(row.node_id) ?? [];
      const health = instances.length
        ? instances.some((instance) => instance.health === 'online') ? 'online'
          : instances.some((instance) => instance.health === 'degraded') ? 'degraded' : 'offline'
        : row.node_id === 'agent-runtime' || row.node_id === 'main-agent' ? 'online' : 'unknown';
      const execution = states.has('running') ? 'running'
        : states.has('waiting') ? 'waiting'
          : states.has('failed') ? 'error' : 'idle';
      return {
        id: row.node_id,
        name: row.name,
        type: row.type,
        zone: row.zone,
        metadata: parseJson(row.metadata_json) as Record<string, unknown>,
        health,
        execution,
        instances,
        observed: states.size > 0 || this.hasNodeSpan(row.node_id),
      };
    });
  }

  getNode(nodeId: string): (NodeDefinition & {
    health: NodeHealthStatus;
    execution: 'idle' | 'running' | 'waiting' | 'error';
    instances: NodeInstance[];
    observed: boolean;
    recentSpans: ObservedSpan[];
  }) | null {
    const node = this.listNodes().find((item) => item.id === nodeId);
    if (!node) return null;
    const recentSpans = this.db.prepare(`
      SELECT * FROM observability_spans WHERE node_id = ? ORDER BY started_at DESC LIMIT 50
    `).all(nodeId) as SpanRow[];
    return { ...node, recentSpans: recentSpans.map((span) => this.toSpan(span)) };
  }

  recordExternalEvent(event: Omit<RuntimeEvent, 'hubOffset'>): RuntimeEvent | null {
    try {
      const existing = this.db.prepare(`
        SELECT event_json FROM observability_events WHERE event_id = ?
      `).get(event.eventId) as { event_json: string } | undefined;
      if (existing) {
        const duplicate = JSON.parse(existing.event_json) as RuntimeEvent;
        this.projectExternalEvent(duplicate);
        return duplicate;
      }
      const persisted = this.persistEvent(event);
      if (!persisted) {
        const duplicate = this.db.prepare(`
          SELECT event_json FROM observability_events WHERE event_id = ?
        `).get(event.eventId) as { event_json: string } | undefined;
        if (!duplicate) return null;
        const persistedDuplicate = JSON.parse(duplicate.event_json) as RuntimeEvent;
        this.projectExternalEvent(persistedDuplicate);
        return persistedDuplicate;
      }
      this.projectExternalEvent(persisted);
      for (const listener of this.listeners) listener(persisted);
      return persisted;
    } catch (error) {
      this.reportFailure('ingest external event', error);
      return null;
    }
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

  listTraces(limit = 50, query: TraceQuery = {}): Omit<ObservedTrace, 'spans'>[] {
    const filters = ['1 = 1'];
    const values: Array<string | number> = [];
    if (query.status) {
      filters.push('t.status = ?');
      values.push(query.status);
    }
    if (query.from) {
      filters.push('t.started_at >= ?');
      values.push(query.from);
    }
    if (query.to) {
      filters.push('t.started_at <= ?');
      values.push(query.to);
    }
    const offset = query.offset ?? 0;
    const rows = this.db.prepare(`
      SELECT t.*, COUNT(s.span_id) AS span_count,
        SUM(CASE WHEN s.node_id = 'model-provider' THEN 1 ELSE 0 END) AS model_call_count
      FROM observability_traces t
      LEFT JOIN observability_spans s ON s.trace_id = t.trace_id
      WHERE ${filters.join(' AND ')}
      GROUP BY t.trace_id
      ORDER BY t.started_at DESC LIMIT ? OFFSET ?
    `).all(...values, limit, offset) as TraceRow[];
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

  traceEvents(traceId: string, offset = 0, limit = 1000): {
    events: RuntimeEvent[]; hasMore: boolean; resyncRequired: boolean;
  } {
    return this.eventsSince(offset, limit, { traceId });
  }

  artifactsForSpan(spanId: string): ArtifactReference[] {
    const rows = this.db.prepare(`
      SELECT artifact_id, trace_id, span_id, type, content_type, completeness,
        byte_length, sha256, created_at
      FROM observability_artifacts WHERE span_id = ? ORDER BY created_at
    `).all(spanId) as Array<{
      artifact_id: string; trace_id: string; span_id: string; type: string;
      content_type: string; completeness: ArtifactReference['completeness'];
      byte_length: number; sha256: string; created_at: string;
    }>;
    return rows.map((row) => ({
      artifactId: row.artifact_id,
      traceId: row.trace_id,
      spanId: row.span_id,
      type: row.type,
      contentType: row.content_type,
      completeness: row.completeness,
      byteLength: row.byte_length,
      sha256: row.sha256,
      createdAt: row.created_at,
    }));
  }

  snapshot(memoryEngineOnline: boolean): {
    watermark: number;
    nodes: Array<NodeDefinition & {
      health: NodeHealthStatus;
      execution: 'idle' | 'running' | 'waiting' | 'error';
      instances: NodeInstance[];
      observed: boolean;
    }>;
    instances: NodeInstance[];
    connections: Array<{ source: string; target: string }>;
    activeTraces: Omit<ObservedTrace, 'spans'>[];
    recentTraces: Omit<ObservedTrace, 'spans'>[];
    recentEvents: RuntimeEvent[];
    systemStatus: { runtime: 'online'; memoryEngine: 'online' | 'offline' };
  } {
    const readSnapshot = this.db.transaction(() => {
      const watermark = (this.db.prepare(`
        SELECT COALESCE(MAX(hub_offset), 0) AS watermark FROM observability_events
      `).get() as { watermark: number }).watermark;
      const recentTraces = this.listTraces(100);
      const nodes = this.listNodes();
      const memory = nodes.find((node) => node.id === 'memory-engine');
      if (memory) memory.health = memoryEngineOnline ? 'online' : 'offline';
      return {
        watermark,
        nodes,
        instances: nodes.flatMap((node) => node.instances),
        connections: [
          { source: 'audio-capture', target: 'vad' },
          { source: 'vad', target: 'asr' },
          { source: 'asr', target: 'main-agent' },
          { source: 'voice-runtime', target: 'agent-runtime' },
          { source: 'agent-runtime', target: 'main-agent' },
          { source: 'main-agent', target: 'memory-engine' },
          { source: 'main-agent', target: 'model-provider' },
          { source: 'main-agent', target: 'worker-runtime' },
          { source: 'main-agent', target: 'tool-router' },
          { source: 'main-agent', target: 'tts' },
          { source: 'tts', target: 'audio-playback' },
        ],
        activeTraces: recentTraces.filter((trace) => trace.status === 'running'),
        recentTraces,
        recentEvents: this.recentEvents(100),
        systemStatus: { runtime: 'online' as const, memoryEngine: memoryEngineOnline ? 'online' as const : 'offline' as const },
      };
    });
    return readSnapshot();
  }

  recentEvents(limit = 50): RuntimeEvent[] {
    const rows = this.db.prepare(`
      SELECT event_json FROM observability_events ORDER BY hub_offset DESC LIMIT ?
    `).all(limit) as Array<{ event_json: string }>;
    return rows.reverse().map((row) => JSON.parse(row.event_json) as RuntimeEvent);
  }

  eventsSince(offset: number, limit = 1000, query: EventQuery = {}): {
    events: RuntimeEvent[];
    hasMore: boolean;
    resyncRequired: boolean;
  } {
    const minimum = (this.db.prepare(`
      SELECT MIN(hub_offset) AS minimum FROM observability_events
    `).get() as { minimum: number | null }).minimum;
    const resyncRequired = minimum !== null && offset < minimum - 1;
    const filters = ['hub_offset > ?'];
    const values: Array<string | number> = [offset];
    if (query.traceId) {
      filters.push('trace_id = ?');
      values.push(query.traceId);
    }
    if (query.nodeId) {
      filters.push("json_extract(event_json, '$.nodeId') = ?");
      values.push(query.nodeId);
    }
    if (query.eventTypes?.length) {
      filters.push(`event_type IN (${query.eventTypes.map(() => '?').join(',')})`);
      values.push(...query.eventTypes);
    }
    const rows = this.db.prepare(`
      SELECT event_json FROM observability_events
      WHERE ${filters.join(' AND ')} ORDER BY hub_offset LIMIT ?
    `).all(...values, limit + 1) as Array<{ event_json: string }>;
    return {
      events: rows.slice(0, limit).map((row) => JSON.parse(row.event_json) as RuntimeEvent),
      hasMore: rows.length > limit,
      resyncRequired,
    };
  }

  async captureArtifact(input: {
    traceId: string;
    spanId: string;
    type: string;
    contentType: string;
    content: unknown;
    completeness?: 'complete' | 'partial';
  }): Promise<ArtifactCaptureResult> {
    const artifactId = randomUUID();
    const createdAt = new Date().toISOString();
    const content = Buffer.from(json(input.content), 'utf8');
    const sha256 = createHash('sha256').update(content).digest('hex');
    const contentPath = path.join(this.artifactDirectory, `${artifactId}.json`);
    try {
      await writeFile(contentPath, content, { flag: 'wx' });
      this.db.prepare(`
        INSERT INTO observability_artifacts
          (artifact_id, trace_id, span_id, type, content_type, content_path, completeness,
           byte_length, sha256, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(artifactId, input.traceId, input.spanId, input.type, input.contentType, contentPath,
        input.completeness ?? 'complete', content.byteLength, sha256, createdAt);
      const reference: ArtifactReference = {
        artifactId,
        traceId: input.traceId,
        spanId: input.spanId,
        type: input.type,
        contentType: input.contentType,
        completeness: input.completeness ?? 'complete',
        byteLength: content.byteLength,
        sha256,
        createdAt,
      };
      this.emit(input.traceId, 'artifact.created', input.spanId, null, undefined, { reference });
      return { status: input.completeness ?? 'complete', reference };
    } catch (error) {
      rmSync(contentPath, { force: true });
      const message = error instanceof Error ? error.message : String(error);
      this.reportFailure('capture artifact', error);
      return { status: 'failed', error: message };
    }
  }

  getArtifactChunk(artifactId: string, offset = 0, limit = 512 * 1024): {
    reference: ArtifactReference;
    content: string;
    nextOffset: number | null;
  } | null {
    const row = this.db.prepare(`
      SELECT artifact_id, trace_id, span_id, type, content_type, content_path,
        completeness, byte_length, sha256, created_at
      FROM observability_artifacts WHERE artifact_id = ?
    `).get(artifactId) as {
      artifact_id: string; trace_id: string; span_id: string; type: string;
      content_type: string; content_path: string; completeness: ArtifactReference['completeness'];
      byte_length: number; sha256: string; created_at: string;
    } | undefined;
    if (!row) return null;
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > row.byte_length
      || !Number.isSafeInteger(limit) || limit < 1 || limit > 1024 * 1024) {
      throw new RangeError('invalid artifact range');
    }
    const end = Math.min(row.byte_length, offset + limit);
    const buffer = Buffer.alloc(end - offset);
    const fd = openSync(row.content_path, 'r');
    try {
      readSync(fd, buffer, 0, buffer.length, offset);
    } finally {
      closeSync(fd);
    }
    return {
      reference: {
        artifactId: row.artifact_id,
        traceId: row.trace_id,
        spanId: row.span_id,
        type: row.type,
        contentType: row.content_type,
        completeness: row.completeness,
        byteLength: row.byte_length,
        sha256: row.sha256,
        createdAt: row.created_at,
      },
      content: buffer.toString('base64'),
      nextOffset: end < row.byte_length ? end : null,
    };
  }

  setTracePinned(traceId: string, pinned: boolean): boolean {
    const result = this.db.prepare('UPDATE observability_traces SET pinned = ? WHERE trace_id = ?')
      .run(pinned ? 1 : 0, traceId);
    return result.changes > 0;
  }

  retentionStats(): { eventCount: number; traceCount: number; artifactCount: number; artifactBytes: number } {
    const events = this.db.prepare('SELECT count(*) AS count FROM observability_events').get() as { count: number };
    const traces = this.db.prepare('SELECT count(*) AS count FROM observability_traces').get() as { count: number };
    const artifacts = this.db.prepare(`
      SELECT count(*) AS count, COALESCE(sum(byte_length), 0) AS bytes FROM observability_artifacts
    `).get() as { count: number; bytes: number };
    return { eventCount: events.count, traceCount: traces.count, artifactCount: artifacts.count, artifactBytes: artifacts.bytes };
  }

  cleanupExpired(retentionMs = 30 * 24 * 60 * 60 * 1000): {
    traces: number; events: number; artifacts: number; bytesFreed: number;
  } {
    if (!Number.isSafeInteger(retentionMs) || retentionMs < 60_000) {
      throw new RangeError('retention must be at least one minute');
    }
    const cutoff = new Date(Date.now() - retentionMs).toISOString();
    const expiredTraces = (this.db.prepare(`
      SELECT trace_id FROM observability_traces
      WHERE pinned = 0 AND status != 'running' AND started_at < ?
    `).all(cutoff) as Array<{ trace_id: string }>).map((row) => row.trace_id);
    const expiredArtifacts = this.db.prepare(`
      SELECT artifact_id, content_path, byte_length FROM observability_artifacts
      WHERE trace_id IN (
        SELECT trace_id FROM observability_traces WHERE pinned = 0 AND status != 'running'
          AND started_at < ?
      )
    `).all(cutoff) as Array<{ artifact_id: string; content_path: string; byte_length: number }>;
    const remove = this.db.transaction(() => {
      if (expiredTraces.length > 0) {
        this.db.prepare(`DELETE FROM observability_spans WHERE trace_id IN (${expiredTraces.map(() => '?').join(',')})`)
          .run(...expiredTraces);
        this.db.prepare(`DELETE FROM observability_traces WHERE trace_id IN (${expiredTraces.map(() => '?').join(',')})`)
          .run(...expiredTraces);
        this.db.prepare(`DELETE FROM observability_events WHERE trace_id IN (${expiredTraces.map(() => '?').join(',')})`)
          .run(...expiredTraces);
        this.db.prepare(`DELETE FROM observability_artifacts WHERE trace_id IN (${expiredTraces.map(() => '?').join(',')})`)
          .run(...expiredTraces);
      }
      const oldEvents = this.db.prepare(`
        DELETE FROM observability_events WHERE created_at < ?
          AND (trace_id = '' OR trace_id NOT IN (SELECT trace_id FROM observability_traces))
      `).run(cutoff);
      return oldEvents.changes;
    });
    const events = remove();
    for (const artifact of expiredArtifacts) rmSync(artifact.content_path, { force: true });
    return {
      traces: expiredTraces.length,
      events,
      artifacts: expiredArtifacts.length,
      bytesFreed: expiredArtifacts.reduce((sum, item) => sum + item.byte_length, 0),
    };
  }

  subscribe(listener: (event: RuntimeEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close(): void {
    this.db.close();
    if (this.temporaryArtifactDirectory) rmSync(this.artifactDirectory, { recursive: true, force: true });
  }

  private emit(
    traceId: string | undefined,
    eventType: string,
    spanId: string | undefined,
    parentSpanId: string | null | undefined,
    nodeId: string | undefined,
    payload: Record<string, unknown>,
    instanceId?: string,
  ): void {
    try {
      const event = {
        eventId: randomUUID(),
        ...(traceId ? { traceId } : {}),
        ...(spanId ? { spanId } : {}),
        ...(parentSpanId ? { parentSpanId } : {}),
        producerId: this.producerId,
        producerSequence: ++this.producerSequence,
        eventType,
        ...(nodeId ? { nodeId } : {}),
        ...(instanceId ? { instanceId } : {}),
        timestamp: new Date().toISOString(),
        payload: JSON.parse(json(payload)) as Record<string, unknown>,
      } satisfies Omit<RuntimeEvent, 'hubOffset'>;
      const persisted = this.persistEvent(event);
      if (!persisted) return;
      for (const listener of this.listeners) listener(persisted);
    } catch (error) {
      this.reportFailure('persist event', error);
    }
  }

  private persistEvent(event: Omit<RuntimeEvent, 'hubOffset'>): RuntimeEvent | null {
    const eventJson = JSON.stringify({
      ...event,
      payload: JSON.parse(json(event.payload)) as Record<string, unknown>,
    });
    const result = this.db.transaction(() => {
      const inserted = this.db.prepare(`
        INSERT OR IGNORE INTO observability_events
          (event_id, producer_id, producer_sequence, trace_id, event_type, event_json, created_at)
        VALUES (?, ?, ?, ?, ?, '', ?)
      `).run(event.eventId, event.producerId, event.producerSequence, event.traceId ?? '',
        event.eventType, event.timestamp);
      if (inserted.changes === 0) return null;
      const persisted = { ...JSON.parse(eventJson) as Omit<RuntimeEvent, 'hubOffset'>,
        hubOffset: Number(inserted.lastInsertRowid) };
      this.db.prepare('UPDATE observability_events SET event_json = ? WHERE event_id = ?')
        .run(JSON.stringify(persisted), event.eventId);
      return persisted;
    })();
    return result;
  }

  private projectExternalEvent(event: RuntimeEvent): void {
    const payload = event.payload;
    if (event.eventType === 'trace.started' && event.traceId) {
      const input = (payload.input && typeof payload.input === 'object'
        ? payload.input : payload) as Record<string, unknown>;
      this.db.prepare(`
        INSERT OR IGNORE INTO observability_traces
          (trace_id, session_id, status, started_at, input_json)
        VALUES (?, ?, 'running', ?, ?)
      `).run(event.traceId, String(payload.sessionId ?? input.sessionId ?? 'unknown'),
        event.timestamp, json(input));
      return;
    }
    if (event.eventType === 'span.started' && event.traceId && event.spanId && event.nodeId) {
      this.db.prepare(`
        INSERT OR IGNORE INTO observability_spans
          (span_id, trace_id, parent_span_id, node_id, name, status, started_at, input_json)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
      `).run(event.spanId, event.traceId, event.parentSpanId ?? null, event.nodeId,
        String(payload.name ?? event.eventType), event.timestamp, json(payload.input ?? null));
      return;
    }
    if (event.eventType.startsWith('span.') && event.eventType !== 'span.started' && event.spanId) {
      const status = event.eventType === 'span.failed' ? 'failed'
        : event.eventType === 'span.waiting' ? 'waiting' : 'completed';
      this.db.prepare(`
        UPDATE observability_spans SET status = ?, ended_at = ?, duration_ms = ?,
          output_json = ?, error = ? WHERE span_id = ?
      `).run(status, event.timestamp, typeof payload.durationMs === 'number' ? payload.durationMs : null,
        json(payload.output ?? null), sanitizeError(typeof payload.error === 'string' ? payload.error : null),
        event.spanId);
      return;
    }
    if ((event.eventType === 'trace.completed' || event.eventType === 'trace.failed')
      && event.traceId) {
      const status = event.eventType === 'trace.failed' ? 'failed' : 'completed';
      this.db.prepare(`
        UPDATE observability_traces SET status = ?, ended_at = ?, duration_ms = ?,
          output_json = ?, error = ? WHERE trace_id = ?
      `).run(status, event.timestamp, typeof payload.durationMs === 'number' ? payload.durationMs : null,
        json(payload.output ?? null), sanitizeError(typeof payload.error === 'string' ? payload.error : null),
        event.traceId);
      return;
    }
    if (event.eventType === 'node.registered' && event.nodeId) {
      this.db.prepare(`
        INSERT INTO observability_nodes (node_id, name, type, zone, metadata_json, registered_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
          name = excluded.name, type = excluded.type, zone = excluded.zone,
          metadata_json = excluded.metadata_json
      `).run(event.nodeId, String(payload.name ?? event.nodeId), String(payload.type ?? 'processor'),
        String(payload.zone ?? 'RUNTIME'), json(payload.metadata ?? {}), event.timestamp);
      return;
    }
    if (event.eventType === 'node.heartbeat' && event.nodeId && typeof payload.instanceId === 'string') {
      this.db.prepare(`
        INSERT INTO observability_instances
          (instance_id, node_id, process_id, hostname, health, last_heartbeat, metadata_json)
        VALUES (?, ?, ?, ?, 'online', ?, ?)
        ON CONFLICT(instance_id) DO UPDATE SET
          health = 'online', last_heartbeat = excluded.last_heartbeat,
          process_id = excluded.process_id, hostname = excluded.hostname
      `).run(payload.instanceId, event.nodeId,
        typeof payload.processId === 'number' ? payload.processId : null,
        typeof payload.hostname === 'string' ? payload.hostname : null,
        event.timestamp, json(payload.metadata ?? {}));
      return;
    }
    if (event.eventType === 'node.health_changed' && event.nodeId) {
      const health = payload.health;
      if (health === 'online' || health === 'offline' || health === 'degraded' || health === 'unknown') {
        const instanceId = typeof payload.instanceId === 'string' ? payload.instanceId : null;
        if (instanceId) {
          this.db.prepare(`
            UPDATE observability_instances SET health = ?, last_heartbeat = ?
            WHERE instance_id = ? AND node_id = ?
          `).run(health, event.timestamp, instanceId, event.nodeId);
        }
      }
    }
  }

  private hasNodeSpan(nodeId: string): boolean {
    return Boolean(this.db.prepare(`
      SELECT 1 FROM observability_spans WHERE node_id = ? LIMIT 1
    `).get(nodeId));
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
      ...(row.pinned === undefined ? {} : { pinned: row.pinned === 1 }),
      ...(row.span_count === undefined ? {} : { spanCount: row.span_count }),
      ...(row.model_call_count === undefined ? {} : { modelCallCount: row.model_call_count }),
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
      artifacts: this.artifactsForSpan(row.span_id),
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
  private readonly ownsTrace: boolean;
  private finished = false;

  constructor(
    private readonly hub: ObservabilityHub,
    readonly traceId: string,
    readonly sessionId: string,
    readonly startedAt: string,
    input: unknown,
    parentSpanId: string | null = null,
  ) {
    this.ownsTrace = parentSpanId === null;
    this.rootSpan = hub.createSpan(traceId, parentSpanId, 'agent-runtime', 'CHAT COMPLETION', input);
  }

  startSpan(nodeId: string, name: string, input: unknown, parentSpanId = this.rootSpan.id): SpanRecorder {
    return this.hub.createSpan(this.traceId, parentSpanId, nodeId, name, input);
  }

  captureArtifact(input: {
    spanId: string;
    type: string;
    contentType: string;
    content: unknown;
    completeness?: 'complete' | 'partial';
  }): Promise<ArtifactCaptureResult> {
    return this.hub.captureArtifact({ ...input, traceId: this.traceId });
  }

  finish(status: ObservationStatus, output: unknown = null, error: string | null = null): void {
    if (this.finished) return;
    this.finished = true;
    this.rootSpan.finish(status, output, error);
    if (this.ownsTrace) this.hub.finishTrace(this, status, output, error);
  }
}
