import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createMemorySchema,
  deepRecallSchema,
  expandSchema,
  rawMemorySchema,
} from '../src/memory-engine/contracts.js';
import { evaluatorProposalSchema, ModelRouter } from '../src/memory-engine/evaluator.js';
import { decayAccessibility, estimateTokens, MemoryEngine, reciprocalRankFusion, temporalScore } from '../src/memory-engine/service.js';
import { handleVoiceTurn } from '../src/main-agent/agent.js';
import { ConversationStore } from '../src/main-agent/conversation.js';
import { buildApp } from '../src/server/app.js';
import { ScriptedChatModel } from './helpers/scripted-model.js';

let temporaryDirectory: string | undefined;
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  if (temporaryDirectory) rmSync(temporaryDirectory, { recursive: true, force: true });
  temporaryDirectory = undefined;
});

describe('Memory Engine Phases 1–5', () => {
  it('refuses to modify an existing SQLite file without its migration ledger', () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-legacy-'));
    const databasePath = path.join(temporaryDirectory, 'legacy.sqlite3');
    const legacy = new Database(databasePath);
    legacy.exec('CREATE TABLE legacy_memory (content TEXT); INSERT INTO legacy_memory VALUES (\'keep\')');
    legacy.close();

    expect(() => new MemoryEngine(databasePath)).toThrow('refusing to modify an existing SQLite file');
    const verify = new Database(databasePath, { readonly: true });
    expect(verify.prepare('SELECT content FROM legacy_memory').get()).toEqual({ content: 'keep' });
    verify.close();
  });

  it('stores, searches, and expands memories in a temporary SQLite database', async () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-sqlite-'));
    vi.stubEnv('SILICONFLOW_API_KEY', '');
    const engine = new MemoryEngine(path.join(temporaryDirectory, 'memory.sqlite3'));
    try {
      await engine.initialize();
      const raw = await engine.writeRaw({
        eventType: 'conversation',
        source: 'test',
        occurredAt: new Date().toISOString(),
        content: { text: 'The Orion project selected SQLite for local storage.' },
      });
      const memory = await engine.createMemory({
        content: 'The Orion project uses SQLite for local storage.',
        briefContent: 'Orion uses SQLite.',
        memoryType: 'project',
        tags: ['Orion', 'SQLite'],
        createdBy: 'test',
        sources: [{
          rawMemoryId: String(raw.id),
          evidenceType: 'explicit',
          evidenceText: 'The Orion project selected SQLite for local storage.',
          confidence: 1,
        }],
      }) as { id: string };
      const context = await engine.createContext({
        agentId: 'test',
        sessionId: 'sqlite-test',
        tokenBudget: 2000,
      }) as { id: string };
      const recalled = await engine.recall({
        contextId: context.id,
        query: 'Orion SQLite',
      }) as { results: Array<{ memoryId: string }> };
      expect(recalled.results.map((result) => result.memoryId)).toContain(memory.id);

      const expanded = await engine.expand({
        contextId: context.id,
        memoryIds: [memory.id],
      }) as { memories: Array<{ memoryId: string; sources: unknown[] }> };
      expect(expanded.memories[0].memoryId).toBe(memory.id);
      expect(expanded.memories[0].sources).toHaveLength(1);
    } finally {
      await engine.close();
    }
  });

  it('automatically promotes raw events after checking evidence', async () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-consolidation-'));
    vi.stubEnv('SILICONFLOW_API_KEY', '');
    const engine = new MemoryEngine(path.join(temporaryDirectory, 'memory.sqlite3'));
    try {
      await engine.initialize();
      const evidenceText = 'The Orion project chose SQLite.';
      const raw = await engine.writeRaw({
        eventType: 'conversation',
        source: 'test',
        occurredAt: new Date().toISOString(),
        content: { text: evidenceText },
      });
      vi.stubEnv('SILICONFLOW_API_KEY', 'test-key');
      vi.stubGlobal('fetch', vi.fn(async (input) => {
        if (String(input).endsWith('/embeddings')) {
          return new Response(JSON.stringify({ data: [{ embedding: [0.1, 0.2, 0.3] }] }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({
          choices: [{
            message: {
              content: JSON.stringify({
                memories: [{
                  content: 'The Orion project uses SQLite.',
                  briefContent: 'Orion uses SQLite.',
                  memoryType: 'project_decision',
                  tags: ['Orion', 'SQLite'],
                  entities: ['Orion'],
                  importance: 0.8,
                  confidence: 0.9,
                  temporalType: 'stable',
                  sources: [{ rawMemoryId: raw.id, evidenceText }],
                }],
              }),
            },
          }],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }));

      await engine.triggerConsolidation({ jobType: 'basic', batchSize: 10 });
      const job = await engine.processConsolidationJob() as { status: string };
      expect(job.status).toBe('completed');
      const proposals = await engine.listConsolidationProposals('accepted');
      expect(proposals).toHaveLength(1);
      expect(proposals[0].status).toBe('accepted');
      const context = await engine.createContext({ agentId: 'test', tokenBudget: 2000 }) as { id: string };
      const recalled = await engine.recall({
        contextId: context.id,
        query: 'Orion SQLite',
      }) as { results: Array<{ memoryId: string }> };
      expect(recalled.results).toHaveLength(1);
      const expanded = await engine.expand({
        contextId: context.id,
        memoryIds: [recalled.results[0].memoryId],
      }) as { memories: Array<{ sources: Array<{ rawMemoryId: string | null }> }> };
      expect(expanded.memories[0].sources[0].rawMemoryId).toBe(raw.id);
    } finally {
      await engine.close();
    }
  });

  it('automatically merges semantic duplicates while preserving source evidence and versions', async () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-duplicates-'));
    vi.stubEnv('SILICONFLOW_API_KEY', '');
    const engine = new MemoryEngine(path.join(temporaryDirectory, 'memory.sqlite3'));
    try {
      await engine.initialize();
      const firstText = 'The project stores application settings in SQLite.';
      const secondText = 'SQLite is used to persist the app settings.';
      const firstRaw = await engine.writeRaw({
        eventType: 'conversation',
        source: 'test',
        occurredAt: new Date().toISOString(),
        content: { text: firstText },
      });
      const secondRaw = await engine.writeRaw({
        eventType: 'conversation',
        source: 'test',
        occurredAt: new Date().toISOString(),
        content: { text: secondText },
      });
      const primary = await engine.createMemory({
        content: 'Application settings are stored in SQLite.',
        briefContent: 'Settings use SQLite.',
        memoryType: 'project_fact',
        tags: ['application', 'settings', 'SQLite'],
        entities: ['application'],
        createdBy: 'test',
        sources: [{
          rawMemoryId: String(firstRaw.id),
          evidenceType: 'explicit',
          evidenceText: firstText,
          confidence: 1,
        }],
      }) as { id: string };
      const duplicate = await engine.createMemory({
        content: 'SQLite persists the application settings.',
        briefContent: 'SQLite persists app settings.',
        memoryType: 'project_fact',
        tags: ['application', 'settings', 'SQLite'],
        entities: ['application'],
        createdBy: 'test',
        sources: [{
          rawMemoryId: String(secondRaw.id),
          evidenceType: 'explicit',
          evidenceText: secondText,
          confidence: 1,
        }],
      }) as { id: string };

      vi.stubEnv('SILICONFLOW_API_KEY', 'test-key');
      vi.stubGlobal('fetch', vi.fn(async (_input, init) => {
        const request = JSON.parse(String(init?.body)) as {
          messages: Array<{ content: string }>;
        };
        const evidence = JSON.parse(request.messages[1].content) as {
          candidates: Array<{ id: string; sources: Array<{ id: string }> }>;
          memory: { sources: Array<{ id: string }> };
        };
        return new Response(JSON.stringify({
          choices: [{
            message: {
              content: JSON.stringify({
                duplicates: [{
                  targetMemoryId: evidence.candidates[0].id,
                  confidence: 0.94,
                  evidenceSourceIds: [
                    evidence.memory.sources[0].id,
                    evidence.candidates[0].sources[0].id,
                  ],
                }],
                reason: 'Both records state the same storage decision.',
              }),
            },
          }],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }));

      const decision = await engine.evaluateMemory({
        taskType: 'duplicates',
        memoryId: primary.id,
        candidateMemoryIds: [duplicate.id],
      }) as {
        proposal: { status: string };
        memory: { id: string; version: number; content: string };
      };
      expect(decision.proposal.status).toBe('accepted');
      expect(decision.memory).toMatchObject({
        id: primary.id,
        version: 2,
        content: 'Application settings are stored in SQLite.',
      });

      const context = await engine.createContext({ agentId: 'test', tokenBudget: 4000 }) as { id: string };
      const after = await engine.expand({
        contextId: context.id,
        memoryIds: [primary.id, duplicate.id],
      }) as { memories: Array<{ memoryId: string; status: string; sources: unknown[] }> };
      expect(Object.fromEntries(after.memories.map((memory) => [memory.memoryId, memory.status]))).toEqual({
        [primary.id]: 'active',
        [duplicate.id]: 'superseded',
      });
      expect(after.memories.find((memory) => memory.memoryId === primary.id)?.sources).toHaveLength(2);
    } finally {
      await engine.close();
    }
  });

  it('automatically applies supported relations and conflict winners', async () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-conflicts-'));
    vi.stubEnv('SILICONFLOW_API_KEY', '');
    const engine = new MemoryEngine(path.join(temporaryDirectory, 'memory.sqlite3'));
    try {
      await engine.initialize();
      const makeMemory = async (text: string, content: string) => {
        const raw = await engine.writeRaw({
          eventType: 'conversation',
          source: 'test',
          occurredAt: new Date().toISOString(),
          content: { text },
        });
        return engine.createMemory({
          content,
          briefContent: content,
          memoryType: 'project_fact',
          tags: ['database', 'project'],
          entities: ['Orion'],
          createdBy: 'test',
          sources: [{
            rawMemoryId: String(raw.id),
            evidenceType: 'explicit',
            evidenceText: text,
            confidence: 1,
          }],
        }) as Promise<{ id: string }>;
      };
      const primary = await makeMemory(
        'The project initially used PostgreSQL.',
        'The Orion project used PostgreSQL.',
      );
      const candidate = await makeMemory(
        'The project later switched to SQLite.',
        'The Orion project uses SQLite.',
      );
      const uncertainPrimary = await makeMemory(
        'The launch date is uncertain.',
        'The launch date is uncertain.',
      );
      const uncertainCandidate = await makeMemory(
        'The launch may be on Friday.',
        'The launch may be on Friday.',
      );

      vi.stubEnv('SILICONFLOW_API_KEY', 'test-key');
      vi.stubGlobal('fetch', vi.fn(async (_input, init) => {
        const request = JSON.parse(String(init?.body)) as {
          messages: Array<{ role: string; content: string }>;
        };
        const evidence = JSON.parse(request.messages[1].content) as {
          candidates: Array<{ id: string; sources: Array<{ id: string }> }>;
          memory: { content: string; sources: Array<{ id: string }> };
        };
        const candidateMemory = evidence.candidates[0];
        const response = request.messages[0].content.includes('task: conflicts')
          ? {
            conflicts: [{
              targetMemoryId: candidateMemory.id,
              confidence: 0.96,
              evidenceSourceIds: [
                evidence.memory.sources[0].id,
                candidateMemory.sources[0].id,
              ],
              resolution: evidence.memory.content.includes('uncertain') ? 'unresolved' : 'keep_candidate',
            }],
            reason: evidence.memory.content.includes('uncertain')
              ? 'The evidence does not establish which statement is current.'
              : 'The later explicit decision supersedes the earlier one.',
          }
          : {
            relations: [{
              targetMemoryId: candidateMemory.id,
              relationType: 'updates',
              confidence: 0.91,
              evidenceSourceIds: [evidence.memory.sources[0].id],
            }],
            reason: 'The memories describe successive project decisions.',
          };
        return new Response(JSON.stringify({
          choices: [{ message: { content: JSON.stringify(response) } }],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }));

      const relationResult = await engine.evaluateMemory({
        taskType: 'relations',
        memoryId: primary.id,
        candidateMemoryIds: [candidate.id],
      }) as { proposal: { status: string } };
      expect(relationResult.proposal.status).toBe('accepted');

      const conflictResult = await engine.evaluateMemory({
        taskType: 'conflicts',
        memoryId: primary.id,
        candidateMemoryIds: [candidate.id],
      }) as { proposal: { status: string }; memory: { status: string } };
      expect(conflictResult.proposal.status).toBe('accepted');
      expect(conflictResult.memory.status).toBe('superseded');
      const unresolvedResult = await engine.evaluateMemory({
        taskType: 'conflicts',
        memoryId: uncertainPrimary.id,
        candidateMemoryIds: [uncertainCandidate.id],
      }) as { proposal: { status: string }; memory: { status: string } };
      expect(unresolvedResult.proposal.status).toBe('accepted');
      expect(unresolvedResult.memory.status).toBe('active');

      const context = await engine.createContext({ agentId: 'test', tokenBudget: 4000 }) as { id: string };
      const expanded = await engine.expand({
        contextId: context.id,
        memoryIds: [primary.id, candidate.id, uncertainPrimary.id, uncertainCandidate.id],
        includeRelations: true,
      }) as {
        memories: Array<{ memoryId: string; status: string }>;
        relations: Array<{ relationType: string }>;
      };
      expect(Object.fromEntries(expanded.memories.map((memory) => [memory.memoryId, memory.status]))).toEqual({
        [primary.id]: 'superseded',
        [candidate.id]: 'active',
        [uncertainPrimary.id]: 'active',
        [uncertainCandidate.id]: 'active',
      });
      expect(expanded.relations.map((relation) => relation.relationType).sort()).toEqual([
        'contradicts',
        'contradicts',
        'updates',
      ]);
    } finally {
      await engine.close();
    }
  });

  it('estimates context budget consistently', () => {
    expect(estimateTokens('a'.repeat(100))).toBe(55);
  });

  it('combines retrieval channels with RRF and rewards higher ranks', () => {
    const scores = reciprocalRankFusion([
      ['a', 'b'],
      ['b', 'c'],
    ]);

    expect(scores.get('b')).toBeGreaterThan(scores.get('a') ?? 0);
    expect(scores.get('a')).toBeGreaterThan(scores.get('c') ?? 0);
  });

  it('keeps episodic memories recallable while transient memories decay faster', () => {
    expect(temporalScore('episodic', 365)).toBe(1);
    expect(temporalScore('transient', 365)).toBeLessThan(temporalScore('recurring', 365));
  });

  it('decays accessibility by the elapsed interval without crossing zero', () => {
    expect(decayAccessibility(0.8, 0.1, 0)).toBe(0.8);
    expect(decayAccessibility(0.8, 1, 1000)).toBe(0.01);
    expect(decayAccessibility(0.8, 0.1, -2)).toBe(0.8);
  });

  it('validates required provenance and memory score bounds', () => {
    expect(rawMemorySchema.safeParse({
      eventType: 'conversation',
      source: 'main',
      occurredAt: '2026-11-16T10:00:00Z',
      content: { text: 'keep this' },
    }).success).toBe(true);

    expect(createMemorySchema.safeParse({
      content: 'A preference',
      memoryType: 'preference',
      importance: 1.2,
      createdBy: 'main',
    }).success).toBe(false);
  });

  it('validates Expand and Deep Recall request shapes', () => {
    const contextId = 'c5139f95-789c-47ac-b9de-2f508888d444';
    const memoryId = '7cbe42c9-502e-4d15-b957-c4e62bca2bd2';
    expect(expandSchema.safeParse({ contextId, memoryIds: [memoryId], depth: 2 }).success).toBe(true);
    expect(deepRecallSchema.safeParse({
      contextId,
      query: 'project decision',
      relatedMemoryIds: [memoryId],
      expandTimeRange: true,
      includeLowAccessibility: true,
    }).success).toBe(true);
    expect(expandSchema.safeParse({ contextId, memoryIds: [], depth: 4 }).success).toBe(false);
    expect(expandSchema.safeParse({ contextId, memoryIds: [memoryId, memoryId] }).success).toBe(false);
  });

  it('validates evaluator proposals and requires evidence for relations', () => {
    const targetMemoryId = '7cbe42c9-502e-4d15-b957-c4e62bca2bd2';
    const sourceId = 'bb6c3e6f-f89a-44c9-86a6-617e41173e8a';
    expect(evaluatorProposalSchema.safeParse({
      relations: [{ targetMemoryId, relationType: 'supports', confidence: 0.8, evidenceSourceIds: [sourceId] }],
      reason: 'Shared explicit evidence.',
    }).success).toBe(true);
    expect(evaluatorProposalSchema.safeParse({
      relations: [{ targetMemoryId, relationType: 'caused_by', confidence: 0.8, evidenceSourceIds: [] }],
      reason: 'Unsupported.',
    }).success).toBe(false);
  });

  it('routes evaluator calls through a configured OpenAI-compatible endpoint', async () => {
    vi.stubEnv('SILICONFLOW_API_KEY', 'test-key');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      choices: [{ message: { content: JSON.stringify({ importance: 0.7, reason: 'Repeated explicit preference.' }) } }],
    }), { status: 200, headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    const result = await new ModelRouter().evaluate('importance', { memory: { id: 'm1' } });

    expect(result.model).toBe('XingChenAGI/XingChenASR-V3.2-Ultra');
    expect(result.proposal.importance).toBe(0.7);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('falls back to the configured low-cost model when the primary model is unavailable', async () => {
    vi.stubEnv('SILICONFLOW_API_KEY', 'test-key');
    const fetchMock = vi.fn(async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { model: string };
      if (request.model === 'XingChenAGI/XingChenASR-V3.2-Ultra') {
        return new Response('model unavailable', { status: 404 });
      }
      return new Response(JSON.stringify({
        choices: [{ message: { content: JSON.stringify({ importance: 0.6, reason: 'Fallback model works.' }) } }],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    });
    vi.stubGlobal('fetch', fetchMock);
    const result = await new ModelRouter().evaluate('importance', { memory: { id: 'm1' } });

    expect(result.model).toBe('Qwen/Qwen3-8B');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('creates embeddings automatically and uses them in recall', async () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-embeddings-'));
    vi.stubEnv('SILICONFLOW_API_KEY', '');
    const engine = new MemoryEngine(path.join(temporaryDirectory, 'memory.sqlite3'));
    try {
      await engine.initialize();
      vi.stubEnv('SILICONFLOW_API_KEY', 'test-key');
      const fetchMock = vi.fn(async () => new Response(JSON.stringify({
        data: [{ embedding: [1, 0, 0] }],
      }), { status: 200, headers: { 'content-type': 'application/json' } }));
      vi.stubGlobal('fetch', fetchMock);
      const memory = await engine.createMemory({
        content: 'The private reference is Ganymede.',
        briefContent: 'Reference: Ganymede.',
        memoryType: 'fact',
        tags: ['Ganymede'],
        createdBy: 'test',
      }) as { id: string; indexStatus: string };
      expect(memory.indexStatus).toBe('indexed');

      const context = await engine.createContext({ agentId: 'test', tokenBudget: 2000 }) as { id: string };
      const result = await engine.recall({
        contextId: context.id,
        query: 'Find the related stored item.',
      }) as { results: Array<{ memoryId: string }>; embeddingStatus: string };
      expect(result.embeddingStatus).toBe('available');
      expect(result.results.map((item) => item.memoryId)).toContain(memory.id);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      await engine.close();
    }
  });

  it('fails explicitly when the evaluator endpoint is not configured', async () => {
    vi.stubEnv('SILICONFLOW_API_KEY', '');
    await expect(new ModelRouter().evaluate('importance', {}))
      .rejects.toThrow('SILICONFLOW_API_KEY is required for memory AI features');
  });

  it('records each Main Agent user turn and recalls through the configured engine', async () => {
    temporaryDirectory = mkdtempSync(path.join(tmpdir(), 'memory-engine-agent-'));
    const rawEvents: Array<Record<string, unknown>> = [];
    const queries: string[] = [];
    const engine = {
      async writeRaw(input: Record<string, unknown>) {
        rawEvents.push(input);
        return {};
      },
      async recallForAgent(query: string) {
        queries.push(query);
        return '- Saved preference';
      },
    };
    const model = new ScriptedChatModel([{ content: '收到。' }]);
    const output: string[] = [];
    for await (const event of handleVoiceTurn('session-1', '请记住这个偏好', undefined, {
      model,
      store: new ConversationStore(temporaryDirectory),
      bridgeUrl: 'http://127.0.0.1:1',
      token: 'test',
      toolsFactory: () => [],
      memoryEngine: engine,
    })) {
      if (event.type === 'delta') output.push(event.text);
    }

    expect(rawEvents).toHaveLength(1);
    expect(rawEvents[0]).toMatchObject({
      eventType: 'conversation',
      source: 'main-agent',
      sessionId: 'session-1',
      content: { role: 'user', text: '请记住这个偏好' },
    });
    expect(queries).toEqual(['请记住这个偏好']);
    expect(output.join('')).toBe('收到。');
  });

  it('reports the memory API as unavailable when SQLite is not configured', async () => {
    const app = buildApp({ token: 'test-token' });
    const responses = await Promise.all([
      app.inject({
        method: 'POST',
        url: '/api/v1/contexts',
        headers: { authorization: 'Bearer test-token' },
        payload: { agentId: 'main' },
      }),
      app.inject({
        method: 'POST',
        url: '/api/v1/recall/expand',
        headers: { authorization: 'Bearer test-token' },
        payload: { contextId: 'c5139f95-789c-47ac-b9de-2f508888d444', memoryIds: ['7cbe42c9-502e-4d15-b957-c4e62bca2bd2'] },
      }),
      app.inject({
        method: 'POST',
        url: '/api/v1/recall/deep',
        headers: { authorization: 'Bearer test-token' },
        payload: { contextId: 'c5139f95-789c-47ac-b9de-2f508888d444', query: 'memory' },
      }),
    ]);

    expect(responses.map((response) => response.statusCode)).toEqual([503, 503, 503]);
    expect(responses.map((response) => response.json().error.message)).toEqual([
      'memory_engine_not_configured',
      'memory_engine_not_configured',
      'memory_engine_not_configured',
    ]);
    await app.close();
  });
});
