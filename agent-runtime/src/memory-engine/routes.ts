import type { FastifyInstance } from 'fastify';
import { z } from 'zod';
import {
  createContextSchema,
  consolidationTriggerSchema,
  deepRecallSchema,
  deleteMemorySchema,
  expandSchema,
  createMemorySchema,
  jobListSchema,
  lifecycleRunSchema,
  memoryLifecycleActionSchema,
  proposalDecisionSchema,
  retryJobSchema,
  rawMemorySchema,
  recallSchema,
  updateMemorySchema,
} from './contracts.js';
import { MemoryEngine, MemoryEngineError } from './service.js';

const uuidSchema = z.string().uuid();

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  const result = schema.safeParse(value);
  if (!result.success) {
    const message = result.error.issues.map((issue) =>
      `${issue.path.join('.') || '(root)'}: ${issue.message}`).join('; ');
    throw new MemoryEngineError(400, message);
  }
  return result.data;
}

function requireEngine(engine?: MemoryEngine): MemoryEngine {
  if (!engine) throw new MemoryEngineError(503, 'memory_engine_not_configured');
  return engine;
}

export function registerMemoryRoutes(app: FastifyInstance, engine?: MemoryEngine): void {
  if (engine) {
    app.addHook('onRequest', async (request) => {
      if (request.url.startsWith('/api/v1/') || request.url.startsWith('/internal/evaluator/')
        || request.url.startsWith('/internal/consolidation/')) {
        await engine.initialize();
      }
    });
    app.addHook('onClose', async () => engine.close());
  }

  app.post('/api/v1/contexts', async (request, reply) => {
    const context = await requireEngine(engine).createContext(parse(createContextSchema, request.body));
    return reply.code(201).send(context);
  });

  app.get('/api/v1/contexts/:contextId', async (request) => {
    const params = parse(z.object({ contextId: uuidSchema }), request.params);
    return requireEngine(engine).getContext(params.contextId);
  });

  app.post('/api/v1/contexts/:contextId/close', async (request) => {
    const params = parse(z.object({ contextId: uuidSchema }), request.params);
    return requireEngine(engine).closeContext(params.contextId);
  });

  app.post('/api/v1/memories/raw', async (request, reply) => {
    const raw = await requireEngine(engine).writeRaw(parse(rawMemorySchema, request.body));
    return reply.code(201).send(raw);
  });

  app.post('/api/v1/memories', async (request, reply) => {
    const memory = await requireEngine(engine).createMemory(parse(createMemorySchema, request.body));
    return reply.code(201).send(memory);
  });

  app.patch('/api/v1/memories/:id', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const update = parse(updateMemorySchema, request.body);
    return requireEngine(engine).updateMemory(params.id, update);
  });

  app.post('/api/v1/recall', async (request) => {
    const input = parse(recallSchema, request.body);
    return requireEngine(engine).recall(input);
  });

  app.post('/api/v1/recall/expand', async (request) => {
    const input = parse(expandSchema, request.body);
    return requireEngine(engine).expand(input);
  });

  app.post('/api/v1/recall/deep', async (request) => {
    const input = parse(deepRecallSchema, request.body);
    return requireEngine(engine).deepRecall(input);
  });

  app.post('/api/v1/lifecycle/run', async (request) => {
    const input = parse(lifecycleRunSchema, request.body ?? {});
    return requireEngine(engine).runLifecycle(input);
  });

  app.post('/api/v1/memories/:id/archive', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const action = parse(memoryLifecycleActionSchema, request.body);
    return requireEngine(engine).changeMemoryLifecycle(params.id, 'archive', action);
  });

  app.post('/api/v1/memories/:id/restore', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const action = parse(memoryLifecycleActionSchema, request.body);
    return requireEngine(engine).changeMemoryLifecycle(params.id, 'restore', action);
  });

  app.delete('/api/v1/memories/:id', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const input = parse(deleteMemorySchema, request.body);
    return requireEngine(engine).deleteMemory(params.id, input.reason);
  });

  app.post('/internal/evaluator/:taskType', async (request) => {
    const params = parse(z.object({
      taskType: z.enum(['importance', 'relations', 'conflicts', 'duplicates', 'retention']),
    }), request.params);
    const body = parse(z.object({
      memoryId: uuidSchema,
      candidateMemoryIds: z.array(uuidSchema).max(20)
        .refine((ids) => new Set(ids).size === ids.length, 'candidateMemoryIds must be unique').optional(),
    }), request.body);
    return requireEngine(engine).evaluateMemory({ taskType: params.taskType, ...body });
  });

  app.get('/internal/evaluator/proposals', async (request) => {
    const query = parse(z.object({
      status: z.enum(['pending', 'accepted', 'rejected']).optional(),
      limit: z.coerce.number().int().min(1).max(100).optional(),
    }), request.query);
    return requireEngine(engine).listProposals(query.status, query.limit);
  });

  app.post('/internal/evaluator/proposals/:id/decision', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const decision = parse(proposalDecisionSchema, request.body);
    return requireEngine(engine).decideProposal(params.id, decision);
  });

  app.post('/api/v1/consolidation/trigger', async (request) => {
    const input = parse(consolidationTriggerSchema, request.body ?? {});
    return requireEngine(engine).triggerConsolidation(input);
  });

  app.get('/api/v1/consolidation/jobs', async (request) => {
    const query = parse(jobListSchema, request.query ?? {});
    return requireEngine(engine).listConsolidationJobs(query);
  });

  app.get('/api/v1/consolidation/jobs/:id', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    return requireEngine(engine).getConsolidationJob(params.id);
  });

  app.post('/api/v1/consolidation/jobs/:id/retry', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const input = parse(retryJobSchema, request.body);
    return requireEngine(engine).retryConsolidationJob(params.id, input);
  });

  app.post('/internal/consolidation/process', async () => {
    return requireEngine(engine).processConsolidationJob();
  });

  app.get('/internal/consolidation/proposals', async (request) => {
    const query = parse(z.object({
      status: z.enum(['pending', 'accepted', 'rejected']).optional(),
      limit: z.coerce.number().int().min(1).max(100).optional(),
    }), request.query);
    return requireEngine(engine).listConsolidationProposals(query.status, query.limit);
  });

  app.post('/internal/consolidation/proposals/:id/decision', async (request) => {
    const params = parse(z.object({ id: uuidSchema }), request.params);
    const decision = parse(proposalDecisionSchema, request.body);
    return requireEngine(engine).decideConsolidationProposal(params.id, decision);
  });
}
