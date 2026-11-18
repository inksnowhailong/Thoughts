import { z } from 'zod';

const objectValue = z.record(z.string(), z.unknown());
const stringArray = z.array(z.string().min(1).max(256)).max(100);
const dateValue = z.string().datetime({ offset: true });
const score = z.number().min(0).max(1);

export const createContextSchema = z.object({
  agentId: z.string().min(1).max(128),
  sessionId: z.string().max(256).optional(),
  scene: z.string().max(2000).optional(),
  tokenBudget: z.number().int().positive().max(100_000).optional(),
});

export const rawMemorySchema = z.object({
  eventType: z.string().min(1).max(64),
  source: z.string().min(1).max(128),
  sessionId: z.string().max(256).optional(),
  occurredAt: dateValue,
  content: objectValue,
  mediaRefs: z.array(z.string().min(1).max(2048)).max(100).optional(),
  metadata: objectValue.optional(),
});

export const memorySourceSchema = z.object({
  rawMemoryId: z.string().uuid().optional(),
  evidenceType: z.enum(['explicit', 'supporting', 'contradicting']),
  evidenceText: z.string().min(1).max(4000),
  confidence: score,
});

export const createMemorySchema = z.object({
  content: z.string().min(1).max(50_000),
  briefContent: z.string().min(1).max(2000).optional(),
  memoryType: z.string().min(1).max(64),
  tags: stringArray.optional(),
  entities: stringArray.optional(),
  importance: score.optional(),
  confidence: score.optional(),
  accessibility: score.optional(),
  validFrom: dateValue.optional(),
  validUntil: dateValue.optional(),
  decayRate: z.number().min(0).max(10).optional(),
  temporalType: z.enum(['transient', 'episodic', 'stable', 'recurring']).optional(),
  origin: z.enum(['explicit', 'derived', 'inferred']).optional(),
  retentionPolicy: z.enum(['normal', 'protected', 'expirable']).optional(),
  createdBy: z.string().min(1).max(128),
  sources: z.array(memorySourceSchema).max(50).optional(),
});

export const updateMemorySchema = z.object({
  expectedVersion: z.number().int().positive(),
  changeReason: z.string().min(1).max(2000),
  changedBy: z.string().min(1).max(128),
  content: z.string().min(1).max(50_000).optional(),
  briefContent: z.string().min(1).max(2000).optional(),
  memoryType: z.string().min(1).max(64).optional(),
  tags: stringArray.optional(),
  entities: stringArray.optional(),
  importance: score.optional(),
  confidence: score.optional(),
  accessibility: score.optional(),
  validFrom: dateValue.nullable().optional(),
  validUntil: dateValue.nullable().optional(),
  decayRate: z.number().min(0).max(10).optional(),
  temporalType: z.enum(['transient', 'episodic', 'stable', 'recurring']).optional(),
  origin: z.enum(['explicit', 'derived', 'inferred']).optional(),
  status: z.enum(['active', 'superseded', 'archived', 'retracted']).optional(),
  retentionPolicy: z.enum(['normal', 'protected', 'expirable']).optional(),
}).refine((value) => Object.keys(value).some((key) => !['expectedVersion', 'changeReason', 'changedBy'].includes(key)), {
  message: 'at least one memory field must be updated',
});

export const recallSchema = z.object({
  contextId: z.string().uuid(),
  query: z.string().min(1).max(4000),
  scene: z.string().max(2000).optional(),
  timeRange: z.object({ from: dateValue.optional(), to: dateValue.optional() })
    .refine((value) => !value.from || !value.to || Date.parse(value.from) <= Date.parse(value.to), {
      message: 'timeRange.from must be before or equal to timeRange.to',
    }).optional(),
  limit: z.number().int().min(1).max(50).optional(),
  tokenBudget: z.number().int().positive().max(100_000).optional(),
  includeBrief: z.boolean().optional(),
  refresh: z.boolean().optional(),
});

export const expandSchema = z.object({
  contextId: z.string().uuid(),
  memoryIds: z.array(z.string().uuid()).min(1).max(50)
    .refine((ids) => new Set(ids).size === ids.length, 'memoryIds must be unique'),
  depth: z.number().int().min(0).max(3).optional(),
  includeRelations: z.boolean().optional(),
  tokenBudget: z.number().int().positive().max(100_000).optional(),
  refresh: z.boolean().optional(),
});

export const deepRecallSchema = z.object({
  contextId: z.string().uuid(),
  query: z.string().min(1).max(4000),
  relatedMemoryIds: z.array(z.string().uuid()).max(25)
    .refine((ids) => new Set(ids).size === ids.length, 'relatedMemoryIds must be unique').optional(),
  timeRange: z.object({ from: dateValue.optional(), to: dateValue.optional() })
    .refine((value) => !value.from || !value.to || Date.parse(value.from) <= Date.parse(value.to), {
      message: 'timeRange.from must be before or equal to timeRange.to',
    }).optional(),
  expandTimeRange: z.boolean().optional(),
  includeLowAccessibility: z.boolean().optional(),
  tokenBudget: z.number().int().positive().max(100_000).optional(),
});

export const evaluatorRequestSchema = z.object({
  taskType: z.enum(['importance', 'relations', 'conflicts', 'duplicates', 'retention']),
  memoryId: z.string().uuid(),
  candidateMemoryIds: z.array(z.string().uuid()).max(20).optional(),
});

export const proposalDecisionSchema = z.object({
  decision: z.enum(['accept', 'reject']),
  reason: z.string().min(1).max(2000),
});

export const memoryLifecycleActionSchema = z.object({
  expectedVersion: z.number().int().positive(),
  reason: z.string().min(1).max(2000),
});

export const deleteMemorySchema = z.object({
  confirm: z.literal(true),
  reason: z.string().min(1).max(2000),
});

export const lifecycleRunSchema = z.object({
  batchSize: z.number().int().min(1).max(500).optional(),
  dryRun: z.boolean().optional(),
});

export const consolidationTriggerSchema = z.object({
  jobType: z.enum(['basic', 'importance', 'relations', 'conflicts', 'duplicates', 'forgetting', 'all']).default('basic'),
  batchSize: z.number().int().min(1).max(100).optional(),
});

export const jobListSchema = z.object({
  status: z.enum(['pending', 'processing', 'completed', 'retrying', 'failed']).optional(),
  limit: z.number().int().min(1).max(100).optional(),
});

export const retryJobSchema = z.object({
  reason: z.string().min(1).max(2000),
});

export type CreateContextInput = z.infer<typeof createContextSchema>;
export type RawMemoryInput = z.infer<typeof rawMemorySchema>;
export type CreateMemoryInput = z.infer<typeof createMemorySchema>;
export type UpdateMemoryInput = z.infer<typeof updateMemorySchema>;
export type RecallInput = z.infer<typeof recallSchema>;
export type ExpandInput = z.infer<typeof expandSchema>;
export type DeepRecallInput = z.infer<typeof deepRecallSchema>;
export type EvaluatorRequest = z.infer<typeof evaluatorRequestSchema>;
export type ProposalDecision = z.infer<typeof proposalDecisionSchema>;
export type MemoryLifecycleAction = z.infer<typeof memoryLifecycleActionSchema>;
export type DeleteMemoryInput = z.infer<typeof deleteMemorySchema>;
export type LifecycleRunInput = z.infer<typeof lifecycleRunSchema>;
export type ConsolidationTriggerInput = z.infer<typeof consolidationTriggerSchema>;
export type JobListInput = z.infer<typeof jobListSchema>;
export type RetryJobInput = z.infer<typeof retryJobSchema>;
