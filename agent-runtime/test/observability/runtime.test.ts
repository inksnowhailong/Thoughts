import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { ObservabilityHub } from '../../src/observability/runtime.js';

let directory: string | undefined;

afterEach(() => {
  if (directory) rmSync(directory, { recursive: true, force: true });
  directory = undefined;
});

describe('ObservabilityHub', () => {
  it('persists traces and replays events by hub offset after restart', () => {
    directory = mkdtempSync(path.join(tmpdir(), 'thoughts-observability-'));
    const databasePath = path.join(directory, 'observability.sqlite');
    let hub = new ObservabilityHub(databasePath);
    const received: number[] = [];
    hub.subscribe((event) => received.push(event.hubOffset));

    const trace = hub.startTrace({ sessionId: 'session-1', userText: 'hello', model: 'test-model' });
    const model = trace.startSpan('model-provider', 'MODEL INVOCATION', {
      credential: 'private-token',
      messages: [{ role: 'user', content: 'hello' }],
    });
    model.finish('completed', { content: 'hi' });
    trace.finish('completed', { output: 'hi' });

    expect(received).toEqual([1, 2, 3, 4, 5, 6]);
    const modelStarted = hub.eventsSince(0).events.find(
      (event) => event.eventType === 'span.started' && event.nodeId === 'model-provider',
    );
    expect(modelStarted?.payload.input).toMatchObject({ credential: '[REDACTED]' });
    hub.close();

    hub = new ObservabilityHub(databasePath);
    const restored = hub.getTrace(trace.traceId);
    expect(restored?.status).toBe('completed');
    expect(restored?.spans.map((span) => span.nodeId)).toEqual([
      'agent-runtime',
      'model-provider',
    ]);
    expect(restored?.spans[1].input).toEqual({
      credential: '[REDACTED]',
      messages: [{ role: 'user', content: 'hello' }],
    });
    expect(restored?.spans[1].output).toEqual({ content: 'hi' });
    expect(hub.eventsSince(2).events.map((event) => event.hubOffset)).toEqual([3, 4, 5, 6]);
    expect(hub.snapshot(false).watermark).toBe(6);
    const next = hub.startTrace({ sessionId: 'session-2', userText: 'again', model: 'test-model' });
    expect(hub.eventsSince(6).events[0].producerSequence).toBe(7);
    next.finish('completed');
    hub.close();
  });

  it('ingests producer retries idempotently and reads artifacts in bounded byte ranges', async () => {
    directory = mkdtempSync(path.join(tmpdir(), 'thoughts-observability-'));
    const hub = new ObservabilityHub(path.join(directory, 'observability.sqlite'));
    const trace = hub.startTrace({ sessionId: 'session-retry', userText: 'hello', model: 'test-model' });
    const span = trace.startSpan('model-provider', 'MODEL INVOCATION', {});
    const event = {
      eventId: 'producer-event-1',
      producerId: 'python:test',
      producerSequence: 1,
      eventType: 'span.started',
      timestamp: new Date().toISOString(),
      traceId: trace.traceId,
      spanId: 'external-span-1',
      nodeId: 'asr',
      payload: { name: '语音识别', input: { text: 'hello' } },
    };

    const first = hub.recordExternalEvent(event);
    const duplicate = hub.recordExternalEvent(event);
    expect(duplicate?.hubOffset).toBe(first?.hubOffset);
    expect(hub.retentionStats().eventCount).toBe(4);

    const captured = await hub.captureArtifact({
      traceId: trace.traceId,
      spanId: span.id,
      type: 'test_payload',
      contentType: 'application/json',
      content: { payload: 'x'.repeat(32) },
    });
    expect(captured.status).toBe('complete');
    const artifactId = captured.reference!.artifactId;
    const part = hub.getArtifactChunk(artifactId, 0, 16);
    expect(part?.content).toBe(Buffer.from(JSON.stringify({ payload: 'x'.repeat(32) })).subarray(0, 16).toString('base64'));
    expect(part?.nextOffset).toBe(16);
    expect(hub.getArtifactChunk(artifactId, 16, 1024)?.nextOffset).toBeNull();
    expect(() => hub.getArtifactChunk(artifactId, 0, 1024 * 1024 + 1)).toThrow(RangeError);
    span.finish('completed');
    trace.finish('completed');
    hub.close();
  });

  it('retains pinned traces while cleaning expired unpinned trace data', () => {
    directory = mkdtempSync(path.join(tmpdir(), 'thoughts-observability-'));
    const hub = new ObservabilityHub(path.join(directory, 'observability.sqlite'));
    const startedAt = new Date(Date.now() - 90_000).toISOString();
    const pinned = hub.startTrace({
      sessionId: 'pinned', userText: 'keep', model: 'test-model', startedAt,
    });
    pinned.finish('completed');
    hub.setTracePinned(pinned.traceId, true);
    const expired = hub.startTrace({
      sessionId: 'expired', userText: 'remove', model: 'test-model', startedAt,
    });
    expired.finish('completed');

    expect(hub.cleanupExpired(60_000).traces).toBe(1);
    expect(hub.getTrace(pinned.traceId)?.pinned).toBe(true);
    expect(hub.getTrace(expired.traceId)).toBeNull();
    hub.close();
  });
});
