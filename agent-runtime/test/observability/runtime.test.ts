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
});
