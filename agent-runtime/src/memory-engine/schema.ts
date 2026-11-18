import { randomUUID } from 'node:crypto';
import {
  index,
  integer,
  primaryKey,
  real,
  sqliteTable,
  text,
  uniqueIndex,
} from 'drizzle-orm/sqlite-core';

const id = (name = 'id') => text(name).primaryKey().$defaultFn(() => randomUUID());
const createdAt = (name = 'created_at') =>
  integer(name, { mode: 'timestamp_ms' }).notNull().$defaultFn(() => new Date());
const updatedAt = (name = 'updated_at') =>
  integer(name, { mode: 'timestamp_ms' }).notNull().$defaultFn(() => new Date());
const json = <T>(name: string) => text(name, { mode: 'json' }).$type<T>();

export const rawMemories = sqliteTable('raw_memories', {
  id: id(),
  eventType: text('event_type').notNull(),
  source: text('source').notNull(),
  sessionId: text('session_id'),
  occurredAt: integer('occurred_at', { mode: 'timestamp_ms' }).notNull(),
  receivedAt: createdAt('received_at'),
  content: json<Record<string, unknown>>('content').notNull(),
  mediaRefs: json<string[]>('media_refs').notNull().$default(() => []),
  metadata: json<Record<string, unknown>>('metadata').notNull().$default(() => ({})),
  searchText: text('search_text').notNull().default(''),
  processingStatus: text('processing_status').notNull().default('pending'),
  createdAt: createdAt(),
}, (table) => [
  index('raw_memories_status_occurred_idx').on(table.processingStatus, table.occurredAt),
]);

export const memories = sqliteTable('memories', {
  id: id(),
  content: text('content').notNull(),
  briefContent: text('brief_content').notNull(),
  memoryType: text('memory_type').notNull(),
  tags: json<string[]>('tags').notNull().$default(() => []),
  entities: json<string[]>('entities').notNull().$default(() => []),
  importance: real('importance').notNull().default(0.5),
  confidence: real('confidence').notNull().default(0.5),
  accessibility: real('accessibility').notNull().default(1),
  accessibilityUpdatedAt: updatedAt('accessibility_updated_at'),
  accessCount: integer('access_count').notNull().default(0),
  createdAt: createdAt(),
  updatedAt: updatedAt(),
  lastAccessedAt: integer('last_accessed_at', { mode: 'timestamp_ms' }),
  lastConfirmedAt: integer('last_confirmed_at', { mode: 'timestamp_ms' }),
  validFrom: integer('valid_from', { mode: 'timestamp_ms' }),
  validUntil: integer('valid_until', { mode: 'timestamp_ms' }),
  decayRate: real('decay_rate').notNull().default(0.01),
  temporalType: text('temporal_type').notNull().default('stable'),
  origin: text('origin').notNull().default('explicit'),
  status: text('status').notNull().default('active'),
  retentionPolicy: text('retention_policy').notNull().default('normal'),
  createdBy: text('created_by').notNull(),
  version: integer('version').notNull().default(1),
}, (table) => [
  index('memories_status_importance_idx').on(table.status, table.importance),
  index('memories_last_accessed_idx').on(table.lastAccessedAt),
  index('memories_type_status_idx').on(table.memoryType, table.status),
  index('memories_valid_until_idx').on(table.validUntil),
]);

export const memorySources = sqliteTable('memory_sources', {
  id: id(),
  memoryId: text('memory_id').notNull().references(() => memories.id, { onDelete: 'cascade' }),
  rawMemoryId: text('raw_memory_id').references(() => rawMemories.id, { onDelete: 'set null' }),
  evidenceType: text('evidence_type').notNull(),
  evidenceText: text('evidence_text').notNull(),
  confidence: real('confidence').notNull(),
  createdAt: createdAt(),
}, (table) => [
  index('memory_sources_memory_idx').on(table.memoryId),
  index('memory_sources_raw_idx').on(table.rawMemoryId),
]);

export const memoryRelations = sqliteTable('memory_relations', {
  id: id(),
  sourceMemoryId: text('source_memory_id').notNull().references(() => memories.id, { onDelete: 'cascade' }),
  targetMemoryId: text('target_memory_id').notNull().references(() => memories.id, { onDelete: 'cascade' }),
  relationType: text('relation_type').notNull(),
  confidence: real('confidence').notNull(),
  origin: text('origin').notNull(),
  evidenceIds: json<string[]>('evidence_ids').notNull().$default(() => []),
  createdAt: createdAt(),
  status: text('status').notNull().default('active'),
}, (table) => [
  index('memory_relations_source_idx').on(table.sourceMemoryId),
  index('memory_relations_target_idx').on(table.targetMemoryId),
]);

export const memoryVersions = sqliteTable('memory_versions', {
  id: id(),
  memoryId: text('memory_id').notNull().references(() => memories.id, { onDelete: 'cascade' }),
  version: integer('version').notNull(),
  content: text('content').notNull(),
  metadata: json<Record<string, unknown>>('metadata').notNull().$default(() => ({})),
  changeReason: text('change_reason').notNull(),
  changedBy: text('changed_by').notNull(),
  createdAt: createdAt(),
}, (table) => [
  uniqueIndex('memory_versions_memory_version_uidx').on(table.memoryId, table.version),
]);

export const memoryIndexes = sqliteTable('memory_indexes', {
  memoryId: text('memory_id').primaryKey().references(() => memories.id, { onDelete: 'cascade' }),
  searchText: text('search_text').notNull(),
  keywords: json<string[]>('keywords').notNull().$default(() => []),
  indexVersion: integer('index_version').notNull().default(1),
  indexedAt: createdAt('indexed_at'),
});

export const memoryEmbeddings = sqliteTable('memory_embeddings', {
  memoryId: text('memory_id').primaryKey().references(() => memories.id, { onDelete: 'cascade' }),
  model: text('model').notNull(),
  vector: json<number[]>('vector').notNull(),
  updatedAt: updatedAt(),
});

export const memoryContexts = sqliteTable('memory_contexts', {
  id: id(),
  agentId: text('agent_id').notNull(),
  sessionId: text('session_id'),
  scene: text('scene'),
  tokenBudget: integer('token_budget').notNull().default(1200),
  tokensUsed: integer('tokens_used').notNull().default(0),
  status: text('status').notNull().default('active'),
  createdAt: createdAt(),
  closedAt: integer('closed_at', { mode: 'timestamp_ms' }),
});

export const contextMemoryLoads = sqliteTable('context_memory_loads', {
  contextId: text('context_id').notNull().references(() => memoryContexts.id, { onDelete: 'cascade' }),
  memoryId: text('memory_id').notNull().references(() => memories.id, { onDelete: 'cascade' }),
  loadLevel: text('load_level').notNull(),
  estimatedTokens: integer('estimated_tokens').notNull().default(0),
  loadedAt: createdAt('loaded_at'),
}, (table) => [
  primaryKey({ columns: [table.contextId, table.memoryId] }),
]);

export const contextRawMemoryLoads = sqliteTable('context_raw_memory_loads', {
  contextId: text('context_id').notNull().references(() => memoryContexts.id, { onDelete: 'cascade' }),
  rawMemoryId: text('raw_memory_id').notNull().references(() => rawMemories.id, { onDelete: 'cascade' }),
  estimatedTokens: integer('estimated_tokens').notNull().default(0),
  loadedAt: createdAt('loaded_at'),
}, (table) => [
  primaryKey({ columns: [table.contextId, table.rawMemoryId] }),
]);

export const contextRelationLoads = sqliteTable('context_relation_loads', {
  contextId: text('context_id').notNull().references(() => memoryContexts.id, { onDelete: 'cascade' }),
  relationId: text('relation_id').notNull().references(() => memoryRelations.id, { onDelete: 'cascade' }),
  estimatedTokens: integer('estimated_tokens').notNull().default(0),
  loadedAt: createdAt('loaded_at'),
}, (table) => [
  primaryKey({ columns: [table.contextId, table.relationId] }),
]);

export const consolidationJobs = sqliteTable('consolidation_jobs', {
  id: id(),
  dedupeKey: text('dedupe_key').notNull(),
  jobType: text('job_type').notNull(),
  inputIds: json<string[]>('input_ids').notNull().$default(() => []),
  checkpoint: json<Record<string, unknown>>('checkpoint').notNull().$default(() => ({})),
  status: text('status').notNull().default('pending'),
  retryCount: integer('retry_count').notNull().default(0),
  createdAt: createdAt(),
  updatedAt: updatedAt(),
}, (table) => [
  uniqueIndex('consolidation_jobs_dedupe_key_uidx').on(table.dedupeKey),
  index('consolidation_jobs_status_created_idx').on(table.status, table.createdAt),
]);

export const consolidationProposals = sqliteTable('consolidation_proposals', {
  id: id(),
  dedupeKey: text('dedupe_key').notNull(),
  inputRawIds: json<string[]>('input_raw_ids').notNull(),
  inputFingerprints: json<Record<string, string>>('input_fingerprints').notNull(),
  proposals: json<Array<Record<string, unknown>>>('proposals').notNull(),
  model: text('model').notNull(),
  status: text('status').notNull().default('pending'),
  decisionReason: text('decision_reason'),
  createdAt: createdAt(),
  decidedAt: integer('decided_at', { mode: 'timestamp_ms' }),
}, (table) => [
  uniqueIndex('consolidation_proposals_dedupe_key_uidx').on(table.dedupeKey),
  index('consolidation_proposals_status_created_idx').on(table.status, table.createdAt),
]);

export const memoryChangeLogs = sqliteTable('memory_change_logs', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  memoryId: text('memory_id'),
  changeType: text('change_type').notNull(),
  payload: json<Record<string, unknown>>('payload').notNull().$default(() => ({})),
  processingStatus: text('processing_status').notNull().default('pending'),
  createdAt: createdAt(),
}, (table) => [
  index('memory_change_logs_status_created_idx').on(table.processingStatus, table.createdAt),
]);

export const evaluatorProposals = sqliteTable('evaluator_proposals', {
  id: id(),
  taskType: text('task_type').notNull(),
  memoryId: text('memory_id').notNull().references(() => memories.id, { onDelete: 'cascade' }),
  inputVersion: integer('input_version').notNull(),
  proposal: json<Record<string, unknown>>('proposal').notNull(),
  model: text('model').notNull(),
  status: text('status').notNull().default('pending'),
  decisionReason: text('decision_reason'),
  createdAt: createdAt(),
  decidedAt: integer('decided_at', { mode: 'timestamp_ms' }),
}, (table) => [
  index('evaluator_proposals_status_created_idx').on(table.status, table.createdAt),
  index('evaluator_proposals_memory_idx').on(table.memoryId),
]);
