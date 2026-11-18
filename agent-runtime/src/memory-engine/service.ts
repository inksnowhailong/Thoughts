import { createHash, randomUUID } from 'node:crypto';
import { and, desc, eq, gt, gte, inArray, isNotNull, isNull, lt, lte, or } from 'drizzle-orm';
import type {
  CreateContextInput,
  CreateMemoryInput,
  EvaluatorRequest,
  ConsolidationTriggerInput,
  LifecycleRunInput,
  MemoryLifecycleAction,
  ProposalDecision,
  DeepRecallInput,
  ExpandInput,
  JobListInput,
  RetryJobInput,
  RawMemoryInput,
  RecallInput,
  UpdateMemoryInput,
} from './contracts.js';
import { DEFAULT_MEMORY_DATABASE_PATH, MemoryDatabaseClient } from './database.js';
import {
  contextMemoryLoads,
  contextRawMemoryLoads,
  contextRelationLoads,
  consolidationJobs,
  consolidationProposals,
  evaluatorProposals,
  memoryChangeLogs,
  memoryContexts,
  memoryEmbeddings,
  memoryIndexes,
  memoryRelations,
  memorySources,
  memoryVersions,
  memories,
  rawMemories,
} from './schema.js';
import { extractedMemorySchema, ModelRouter } from './evaluator.js';

type RecallRow = {
  id: string;
  content: string;
  brief_content: string;
  memory_type: string;
  importance: number;
  confidence: number;
  accessibility: number;
  temporal_type: string;
  origin: string;
  valid_from: Date | null;
  valid_until: Date | null;
  created_at: Date;
  tags: string[];
  entities: string[];
  relevance_rank: number;
};

export class MemoryEngineError extends Error {
  constructor(readonly statusCode: number, message: string) {
    super(message);
    this.name = 'MemoryEngineError';
  }
}

function keywords(text: string): string[] {
  return [...new Set(text.toLowerCase().split(/[^\p{L}\p{N}_-]+/u).filter((part) => part.length > 1))].slice(0, 100);
}

function chunk<T>(values: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let index = 0; index < values.length; index += size) {
    chunks.push(values.slice(index, index + size));
  }
  return chunks;
}

function rawFingerprint(raw: Pick<typeof rawMemories.$inferSelect, 'eventType' | 'source' | 'sessionId' | 'occurredAt' | 'content'>): string {
  return createHash('sha256').update(JSON.stringify({
    eventType: raw.eventType,
    source: raw.source,
    sessionId: raw.sessionId,
    occurredAt: raw.occurredAt.toISOString(),
    content: raw.content,
  })).digest('hex');
}

function cosineSimilarity(left: number[], right: number[]): number {
  if (left.length !== right.length || left.length === 0) return 0;
  let dot = 0;
  let leftNorm = 0;
  let rightNorm = 0;
  for (let index = 0; index < left.length; index += 1) {
    dot += left[index] * right[index];
    leftNorm += left[index] ** 2;
    rightNorm += right[index] ** 2;
  }
  return leftNorm && rightNorm ? dot / Math.sqrt(leftNorm * rightNorm) : 0;
}

function containsEvidence(content: unknown, evidenceText: string): boolean {
  if (typeof content === 'string') return content.includes(evidenceText);
  if (Array.isArray(content)) return content.some((item) => containsEvidence(item, evidenceText));
  if (content && typeof content === 'object') {
    return Object.values(content).some((item) => containsEvidence(item, evidenceText));
  }
  return false;
}

export function estimateTokens(text: string): number {
  const units = Array.from(text).reduce((total, character) =>
    total + (character.codePointAt(0)! <= 0x7f ? 0.25 : 1), 0);
  return Math.ceil(units) + 30;
}

export function decayAccessibility(accessibility: number, decayRate: number, elapsedDays: number): number {
  return Math.max(0.01, Math.min(1, accessibility * Math.exp(-decayRate * Math.max(0, elapsedDays))));
}

export function temporalScore(type: string, ageDays: number): number {
  if (type === 'transient') return Math.exp(-ageDays / 7);
  if (type === 'recurring') return Math.exp(-ageDays / 180);
  return 1;
}

export function reciprocalRankFusion(rankLists: string[][], k = 60): Map<string, number> {
  const scores = new Map<string, number>();
  for (const list of rankLists) {
    list.forEach((id, index) => scores.set(id, (scores.get(id) ?? 0) + 1 / (k + index + 1)));
  }
  return scores;
}

export class MemoryEngine {
  private readonly client: MemoryDatabaseClient;
  private readonly modelRouter: ModelRouter;
  private initialized = false;
  private scheduler: NodeJS.Timeout | null = null;
  private jobProcessor: NodeJS.Timeout | null = null;
  private processingJob = false;

  constructor(databasePath = DEFAULT_MEMORY_DATABASE_PATH, modelRouter = new ModelRouter()) {
    this.client = new MemoryDatabaseClient(databasePath);
    this.modelRouter = modelRouter;
  }

  async initialize(): Promise<void> {
    if (this.initialized) return;
    await this.client.migrate();
    this.initialized = true;
    this.scheduler = setInterval(() => {
      void this.triggerConsolidation({ jobType: 'all', batchSize: 20 })
        .catch((error: unknown) => console.error('[memory-engine] scheduled consolidation failed:', error));
    }, 12 * 60 * 60 * 1000);
    this.scheduler.unref();
    this.jobProcessor = setInterval(() => {
      void this.processConsolidationJob()
        .catch((error: unknown) => console.error('[memory-engine] consolidation worker failed:', error));
    }, 30_000);
    this.jobProcessor.unref();
    void this.backfillEmbeddings()
      .catch((error: unknown) => console.error('[memory-engine] embedding backfill failed:', error));
    void this.triggerConsolidation({ jobType: 'basic', batchSize: 20 })
      .catch((error: unknown) => console.error('[memory-engine] startup consolidation failed:', error));
  }

  async close(): Promise<void> {
    if (this.scheduler) clearInterval(this.scheduler);
    this.scheduler = null;
    if (this.jobProcessor) clearInterval(this.jobProcessor);
    this.jobProcessor = null;
    await this.client.close();
  }

  async createContext(input: CreateContextInput): Promise<Record<string, unknown>> {
    const [context] = await this.client.db.insert(memoryContexts).values({
      agentId: input.agentId,
      sessionId: input.sessionId,
      scene: input.scene,
      tokenBudget: input.tokenBudget ?? 1200,
    }).returning();
    return context;
  }

  async getContext(contextId: string): Promise<Record<string, unknown>> {
    const [context] = await this.client.db.select().from(memoryContexts)
      .where(eq(memoryContexts.id, contextId)).limit(1);
    if (!context) throw new MemoryEngineError(404, 'context_not_found');
    return context;
  }

  async closeContext(contextId: string): Promise<Record<string, unknown>> {
    const [context] = await this.client.db.update(memoryContexts)
      .set({ status: 'closed', closedAt: new Date() })
      .where(and(eq(memoryContexts.id, contextId), eq(memoryContexts.status, 'active')))
      .returning();
    if (!context) throw new MemoryEngineError(404, 'active_context_not_found');
    return context;
  }

  async writeRaw(input: RawMemoryInput): Promise<Record<string, unknown>> {
    await this.initialize();
    const [raw] = this.client.db.transaction((tx) => {
      const [created] = tx.insert(rawMemories).values({
        eventType: input.eventType,
        source: input.source,
        sessionId: input.sessionId,
        occurredAt: new Date(input.occurredAt),
        content: input.content,
        mediaRefs: input.mediaRefs ?? [],
        metadata: input.metadata ?? {},
        searchText: `${JSON.stringify(input.content)} ${JSON.stringify(input.metadata ?? {})}`,
      }).returning().all();
      tx.insert(memoryChangeLogs).values({
        changeType: 'raw_created',
        payload: { rawMemoryId: created.id, eventType: created.eventType },
      }).run();
      this.client.sqlite.prepare(
        'INSERT INTO raw_memory_search_fts (raw_memory_id, search_text) VALUES (?, ?)',
      ).run(created.id, created.searchText);
      return [created];
    });
    const pendingCount = this.client.sqlite.prepare(
      "SELECT count(*) AS count FROM raw_memories WHERE processing_status = 'pending'",
    ).get() as { count: number };
    if (pendingCount.count >= 50 && pendingCount.count % 50 === 0) {
      try {
        await this.triggerConsolidation({ jobType: 'basic', batchSize: 50 });
      } catch (error) {
        console.error('[memory-engine] raw threshold queue failed:', error);
      }
    }
    return raw;
  }

  async createMemory(input: CreateMemoryInput): Promise<Record<string, unknown>> {
    const briefContent = input.briefContent ?? input.content.slice(0, 280);
    const [memory] = this.client.db.transaction((tx) => {
      const [created] = tx.insert(memories).values({
        content: input.content,
        briefContent,
        memoryType: input.memoryType,
        tags: input.tags ?? [],
        entities: input.entities ?? [],
        importance: input.importance ?? 0.5,
        confidence: input.confidence ?? 0.5,
        accessibility: input.accessibility ?? 1,
        validFrom: input.validFrom ? new Date(input.validFrom) : null,
        validUntil: input.validUntil ? new Date(input.validUntil) : null,
        decayRate: input.decayRate ?? 0.01,
        temporalType: input.temporalType ?? 'stable',
        origin: input.origin ?? 'explicit',
        retentionPolicy: input.retentionPolicy ?? 'normal',
        createdBy: input.createdBy,
      }).returning().all();
      const sourceIds = [...new Set((input.sources ?? [])
        .map((source) => source.rawMemoryId)
        .filter((id): id is string => Boolean(id)))];
      if (sourceIds.length) {
        const found = tx.select({ id: rawMemories.id }).from(rawMemories)
          .where(inArray(rawMemories.id, sourceIds)).all();
        if (found.length !== sourceIds.length) throw new MemoryEngineError(400, 'source_raw_memory_not_found');
      }
      tx.insert(memoryVersions).values({
        memoryId: created.id,
        version: 1,
        content: created.content,
        metadata: {
          briefContent: created.briefContent,
          memoryType: created.memoryType,
          tags: created.tags,
          entities: created.entities,
          importance: created.importance,
          confidence: created.confidence,
          accessibility: created.accessibility,
          validFrom: created.validFrom?.toISOString() ?? null,
          validUntil: created.validUntil?.toISOString() ?? null,
          decayRate: created.decayRate,
          temporalType: created.temporalType,
          origin: created.origin,
          status: created.status,
          retentionPolicy: created.retentionPolicy,
        },
        changeReason: 'created',
        changedBy: input.createdBy,
      }).run();
      if (input.sources?.length) {
        tx.insert(memorySources).values(input.sources.map((source) => ({
          memoryId: created.id,
          rawMemoryId: source.rawMemoryId,
          evidenceType: source.evidenceType,
          evidenceText: source.evidenceText,
          confidence: source.confidence,
        }))).run();
      }
      tx.insert(memoryIndexes).values({
        memoryId: created.id,
        searchText: `${created.briefContent}\n${created.content}\n${created.tags.join(' ')}\n${created.entities.join(' ')}`,
        keywords: keywords(`${created.content} ${created.tags.join(' ')} ${created.entities.join(' ')}`),
      }).run();
      tx.insert(memoryChangeLogs).values({
        memoryId: created.id,
        changeType: 'created',
        payload: { version: 1, createdBy: input.createdBy },
      }).run();
      return [created];
    });
    const indexStatus = await this.refreshIndex(
      memory.id,
      `${briefContent}\n${input.content}\n${(input.tags ?? []).join(' ')}\n${(input.entities ?? []).join(' ')}`,
    );
    await this.queueImportanceAtThreshold();
    return { ...memory, indexStatus };
  }

  async updateMemory(memoryId: string, input: UpdateMemoryInput): Promise<Record<string, unknown>> {
    const updated = this.client.db.transaction((tx) => {
      const [current] = tx.select({
        id: memories.id,
        content: memories.content,
        version: memories.version,
      }).from(memories).where(eq(memories.id, memoryId)).limit(1).all();
      if (!current) throw new MemoryEngineError(404, 'memory_not_found');
      if (current.version !== input.expectedVersion) throw new MemoryEngineError(409, 'memory_version_conflict');

      const {
        expectedVersion: _expectedVersion,
        changeReason,
        changedBy,
        validFrom,
        validUntil,
        ...patch
      } = input;
      const values = {
        ...patch,
        ...(validFrom !== undefined ? { validFrom: validFrom === null ? null : new Date(validFrom) } : {}),
        ...(validUntil !== undefined ? { validUntil: validUntil === null ? null : new Date(validUntil) } : {}),
        version: current.version + 1,
        updatedAt: new Date(),
      };
      const [row] = tx.update(memories).set(values).where(eq(memories.id, memoryId)).returning().all();
      tx.insert(memoryVersions).values({
        memoryId,
        version: row.version,
        content: row.content,
        metadata: {
          briefContent: row.briefContent,
          memoryType: row.memoryType,
          tags: row.tags,
          entities: row.entities,
          importance: row.importance,
          confidence: row.confidence,
          accessibility: row.accessibility,
          temporalType: row.temporalType,
          origin: row.origin,
          status: row.status,
          retentionPolicy: row.retentionPolicy,
          validFrom: row.validFrom?.toISOString() ?? null,
          validUntil: row.validUntil?.toISOString() ?? null,
          decayRate: row.decayRate,
        },
        changeReason,
        changedBy,
      }).run();
      tx.insert(memoryChangeLogs).values({
        memoryId,
        changeType: 'updated',
        payload: { version: row.version, changeReason, changedBy },
      }).run();
      return row;
    }, { behavior: 'immediate' });
    await this.refreshIndex(memoryId, `${updated.briefContent}\n${updated.content}\n${updated.tags.join(' ')}\n${updated.entities.join(' ')}`);
    await this.queueImportanceAtThreshold();
    return updated;
  }

  private async queueImportanceAtThreshold(): Promise<void> {
    if (!this.modelRouter.isConfigured('importance')) return;
    const pending = this.client.sqlite.prepare(
      "SELECT count(*) AS count FROM memory_change_logs WHERE processing_status = 'pending' AND memory_id IS NOT NULL",
    ).get() as { count: number };
    const count = pending.count;
    if (count >= 20 && count % 20 === 0) {
      try {
        await this.triggerConsolidation({ jobType: 'importance', batchSize: 20 });
      } catch (error) {
        console.error('[memory-engine] canonical threshold queue failed:', error);
      }
    }
  }

  async evaluateMemory(input: EvaluatorRequest): Promise<Record<string, unknown>> {
    const [memory] = await this.client.db.select().from(memories)
      .where(eq(memories.id, input.memoryId)).limit(1);
    if (!memory) throw new MemoryEngineError(404, 'memory_not_found');
    const candidateIds = [...new Set((input.candidateMemoryIds ?? []).filter((id) => id !== input.memoryId))];
    if (candidateIds.length > 20) throw new MemoryEngineError(400, 'too_many_candidate_memories');
    if (input.taskType === 'conflicts' && candidateIds.length !== 1) {
      throw new MemoryEngineError(400, 'conflicts_require_one_candidate');
    }
    const candidates = candidateIds.length
      ? await this.client.db.select().from(memories).where(inArray(memories.id, candidateIds))
      : [];
    if (candidates.length !== candidateIds.length) throw new MemoryEngineError(400, 'candidate_memory_not_found');

    const sourceMemoryIds = [memory.id, ...candidates.map((candidate) => candidate.id)];
    const sources = await this.client.db.select().from(memorySources)
      .where(inArray(memorySources.memoryId, sourceMemoryIds));
    const sourceIds = new Set(sources.map((source) => source.id));
    const evidence = {
      memory: {
        id: memory.id,
        version: memory.version,
        content: memory.content,
        briefContent: memory.briefContent,
        memoryType: memory.memoryType,
        tags: memory.tags,
        entities: memory.entities,
        importance: memory.importance,
        confidence: memory.confidence,
        origin: memory.origin,
        status: memory.status,
        temporalType: memory.temporalType,
        validFrom: memory.validFrom,
        validUntil: memory.validUntil,
        sources: sources.filter((source) => source.memoryId === memory.id).slice(0, 30),
      },
      candidates: candidates.map((candidate) => ({
        id: candidate.id,
        version: candidate.version,
        content: candidate.content,
        briefContent: candidate.briefContent,
        memoryType: candidate.memoryType,
        tags: candidate.tags,
        entities: candidate.entities,
        sources: sources.filter((source) => source.memoryId === candidate.id).slice(0, 10),
      })),
    };
    let evaluated: Awaited<ReturnType<ModelRouter['evaluate']>>;
    try {
      evaluated = await this.modelRouter.evaluate(input.taskType, evidence);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message === 'memory_evaluator_not_configured') throw new MemoryEngineError(503, message);
      throw new MemoryEngineError(502, message);
    }

    const proposal = evaluated.proposal;
    if (input.taskType === 'importance' && proposal.importance === undefined) {
      throw new MemoryEngineError(502, 'evaluator_proposal_missing_importance');
    }
    if (input.taskType === 'retention' && proposal.retention === undefined && proposal.validity === undefined) {
      throw new MemoryEngineError(502, 'evaluator_proposal_missing_retention');
    }
    const allowedTargets = new Set(candidates.map((candidate) => candidate.id));
    for (const relation of proposal.relations ?? []) {
      if (!allowedTargets.has(relation.targetMemoryId) || relation.targetMemoryId === memory.id) {
        throw new MemoryEngineError(502, 'evaluator_relation_target_not_in_candidates');
      }
      if (relation.evidenceSourceIds.some((id) => !sourceIds.has(id))) {
        throw new MemoryEngineError(502, 'evaluator_relation_evidence_not_found');
      }
    }
    for (const duplicate of proposal.duplicates ?? []) {
      if (!allowedTargets.has(duplicate.targetMemoryId) || duplicate.targetMemoryId === memory.id) {
        throw new MemoryEngineError(502, 'evaluator_duplicate_target_not_in_candidates');
      }
      if (duplicate.evidenceSourceIds.some((id) => !sourceIds.has(id))) {
        throw new MemoryEngineError(502, 'evaluator_duplicate_evidence_not_found');
      }
    }
    for (const conflict of proposal.conflicts ?? []) {
      if (!allowedTargets.has(conflict.targetMemoryId) || conflict.targetMemoryId === memory.id) {
        throw new MemoryEngineError(502, 'evaluator_conflict_target_not_in_candidates');
      }
      if (conflict.evidenceSourceIds.some((id) => !sourceIds.has(id))) {
        throw new MemoryEngineError(502, 'evaluator_conflict_evidence_not_found');
      }
    }
    const [created] = await this.client.db.insert(evaluatorProposals).values({
      taskType: input.taskType,
      memoryId: memory.id,
      inputVersion: memory.version,
      proposal: {
        data: proposal,
        candidateVersions: Object.fromEntries(candidates.map((candidate) => [candidate.id, candidate.version])),
      },
      model: evaluated.model,
    }).returning();
    if (['duplicates', 'relations', 'conflicts'].includes(input.taskType)) {
      return this.decideProposal(created.id, {
        decision: 'accept',
        reason: `Automatically applied by evaluator: ${proposal.reason}`,
      });
    }
    return created;
  }

  async decideProposal(proposalId: string, decision: ProposalDecision): Promise<Record<string, unknown>> {
    return this.client.db.transaction((tx) => {
      const [proposalRow] = tx.select().from(evaluatorProposals)
        .where(eq(evaluatorProposals.id, proposalId)).limit(1).all();
      if (!proposalRow) throw new MemoryEngineError(404, 'evaluator_proposal_not_found');
      if (proposalRow.status !== 'pending') throw new MemoryEngineError(409, 'evaluator_proposal_already_decided');
      if (decision.decision === 'reject') {
        const [rejected] = tx.update(evaluatorProposals).set({
          status: 'rejected',
          decisionReason: decision.reason,
          decidedAt: new Date(),
        }).where(eq(evaluatorProposals.id, proposalId)).returning().all();
        return rejected;
      }

      const [current] = tx.select().from(memories)
        .where(eq(memories.id, proposalRow.memoryId)).limit(1).all();
      if (!current) throw new MemoryEngineError(404, 'memory_not_found');
      if (current.version !== proposalRow.inputVersion) throw new MemoryEngineError(409, 'memory_version_conflict');
      const proposal = proposalRow.proposal as {
        data?: Record<string, unknown>;
        candidateVersions?: Record<string, number>;
      };
      const data = proposal.data;
      if (!data) throw new MemoryEngineError(409, 'evaluator_proposal_invalid');
      const candidateVersions = proposal.candidateVersions ?? {};
      const targetIds = [
        ...((data.relations as Array<{ targetMemoryId: string }> | undefined) ?? []).map((item) => item.targetMemoryId),
        ...((data.duplicates as Array<{ targetMemoryId: string }> | undefined) ?? []).map((item) => item.targetMemoryId),
        ...((data.conflicts as Array<{ targetMemoryId: string }> | undefined) ?? []).map((item) => item.targetMemoryId),
      ];
      const uniqueTargetIds = [...new Set(targetIds)];
      const targetRows = uniqueTargetIds.length
        ? tx.select({ id: memories.id, version: memories.version }).from(memories)
          .where(inArray(memories.id, uniqueTargetIds)).all()
        : [];
      const targetById = new Map(targetRows.map((target) => [target.id, target]));
      for (const targetId of uniqueTargetIds) {
        const target = targetById.get(targetId);
        if (!target || target.version !== candidateVersions[targetId]) {
          throw new MemoryEngineError(409, 'evaluator_candidate_version_conflict');
        }
      }
      const currentSources = tx.select({ id: memorySources.id }).from(memorySources)
        .where(eq(memorySources.memoryId, current.id)).all();
      const targetSources = targetIds.length
        ? tx.select({ id: memorySources.id }).from(memorySources)
          .where(inArray(memorySources.memoryId, [...new Set(targetIds)])).all()
        : [];
      const allowedSourceIds = new Set([...currentSources, ...targetSources].map((source) => source.id));
      const relationItems = (data.relations as Array<{
        targetMemoryId: string; relationType: string; confidence: number; evidenceSourceIds: string[];
      }> | undefined) ?? [];
      const conflictItems = (data.conflicts as Array<{
        targetMemoryId: string; confidence: number; evidenceSourceIds: string[];
        resolution: 'keep_primary' | 'keep_candidate' | 'unresolved';
      }> | undefined) ?? [];
      const duplicateItems = (data.duplicates as Array<{
        targetMemoryId: string; confidence: number; evidenceSourceIds: string[];
      }> | undefined) ?? [];
      const relationProposals = [
        ...relationItems,
        ...conflictItems.map((item) => ({ ...item, relationType: 'contradicts' })),
      ];
      for (const relation of relationProposals) {
        if (relation.evidenceSourceIds.some((id) => !allowedSourceIds.has(id))) {
          throw new MemoryEngineError(409, 'evaluator_evidence_changed');
        }
      }

      const retention = data.retention as string | undefined;
      const currentLosesConflict = conflictItems.some((item) => item.resolution === 'keep_candidate');
      const newStatus = currentLosesConflict
        ? 'superseded'
        : retention === 'archive_candidate' ? 'archived' : current.status;
      const importance = typeof data.importance === 'number' ? data.importance : current.importance;
      const changed = newStatus !== current.status || importance !== current.importance || duplicateItems.length > 0;
      let updated = current;
      if (changed) {
        const [row] = tx.update(memories).set({
          status: newStatus,
          importance,
          version: current.version + 1,
          updatedAt: new Date(),
        }).where(eq(memories.id, current.id)).returning().all();
        updated = { ...current, ...row };
        tx.insert(memoryVersions).values({
          memoryId: current.id,
          version: row.version,
          content: row.content,
          metadata: {
            briefContent: row.briefContent,
            memoryType: row.memoryType,
            tags: row.tags,
            entities: row.entities,
            importance: row.importance,
            confidence: row.confidence,
            accessibility: row.accessibility,
            temporalType: row.temporalType,
            origin: row.origin,
            status: row.status,
            retentionPolicy: row.retentionPolicy,
          },
          changeReason: `accepted evaluator proposal: ${decision.reason}`,
          changedBy: 'memory-evaluator',
        }).run();
        tx.insert(memoryChangeLogs).values({
          memoryId: current.id,
          changeType: 'evaluator_applied',
          payload: { proposalId, version: row.version, decisionReason: decision.reason },
        }).run();
      }
      if (relationProposals.length) {
        tx.insert(memoryRelations).values(relationProposals.map((relation) => ({
          sourceMemoryId: current.id,
          targetMemoryId: relation.targetMemoryId,
          relationType: relation.relationType,
          confidence: relation.confidence,
          origin: 'model',
          evidenceIds: relation.evidenceSourceIds,
        }))).run();
      }
      if (duplicateItems.length) {
        const duplicateIds = [...new Set(duplicateItems.map((item) => item.targetMemoryId))];
        if (duplicateIds.length !== duplicateItems.length) {
          throw new MemoryEngineError(409, 'evaluator_duplicate_target_repeated');
        }
        if (current.status !== 'active') throw new MemoryEngineError(409, 'evaluator_duplicate_primary_not_active');
        const duplicateRows = tx.select().from(memories)
          .where(inArray(memories.id, duplicateIds)).all();
        if (duplicateRows.length !== duplicateIds.length || duplicateRows.some((row) => row.status !== 'active')) {
          throw new MemoryEngineError(409, 'evaluator_duplicate_target_not_active');
        }
        const primarySources = tx.select().from(memorySources)
          .where(eq(memorySources.memoryId, current.id)).all();
        const duplicateSources = tx.select().from(memorySources)
          .where(inArray(memorySources.memoryId, duplicateIds)).all();
        const sourceIdentity = (source: typeof primarySources[number]) =>
          `${source.rawMemoryId ?? ''}\u0000${source.evidenceType}\u0000${source.evidenceText}`;
        const existingSourceKeys = new Set(primarySources.map(sourceIdentity));
        const transferredSources = duplicateSources.filter((source) => {
          const identity = sourceIdentity(source);
          if (existingSourceKeys.has(identity)) return false;
          existingSourceKeys.add(identity);
          return true;
        });
        if (transferredSources.length) {
          tx.insert(memorySources).values(transferredSources.map((source) => ({
            memoryId: current.id,
            rawMemoryId: source.rawMemoryId,
            evidenceType: source.evidenceType,
            evidenceText: source.evidenceText,
            confidence: source.confidence,
          }))).run();
        }
        for (const duplicate of duplicateRows) {
          const nextVersion = duplicate.version + 1;
          const [superseded] = tx.update(memories).set({
            status: 'superseded',
            version: nextVersion,
            updatedAt: new Date(),
          }).where(and(
            eq(memories.id, duplicate.id),
            eq(memories.version, duplicate.version),
            eq(memories.status, 'active'),
          )).returning().all();
          if (!superseded) throw new MemoryEngineError(409, 'evaluator_duplicate_target_version_conflict');
          tx.insert(memoryVersions).values({
            memoryId: duplicate.id,
            version: nextVersion,
            content: duplicate.content,
            metadata: {
              briefContent: duplicate.briefContent,
              memoryType: duplicate.memoryType,
              tags: duplicate.tags,
              entities: duplicate.entities,
              importance: duplicate.importance,
              confidence: duplicate.confidence,
              accessibility: duplicate.accessibility,
              temporalType: duplicate.temporalType,
              origin: duplicate.origin,
              status: 'superseded',
              retentionPolicy: duplicate.retentionPolicy,
            },
            changeReason: `merged into memory ${current.id}: ${decision.reason}`,
            changedBy: 'user',
          }).run();
          tx.insert(memoryChangeLogs).values({
            memoryId: duplicate.id,
            changeType: 'semantic_duplicate_merged',
            processingStatus: 'completed',
            payload: { proposalId, canonicalMemoryId: current.id, version: nextVersion },
          }).run();
        }
        if (conflictItems.length) {
          const conflictTargetIds = [...new Set(conflictItems.map((item) => item.targetMemoryId))];
          if (conflictTargetIds.length !== conflictItems.length || conflictTargetIds.length !== 1) {
            throw new MemoryEngineError(409, 'evaluator_conflict_target_repeated');
          }
          if (current.status !== 'active') throw new MemoryEngineError(409, 'evaluator_conflict_primary_not_active');
          const [candidate] = tx.select().from(memories)
            .where(eq(memories.id, conflictTargetIds[0])).limit(1).all();
          if (!candidate || candidate.status !== 'active') {
            throw new MemoryEngineError(409, 'evaluator_conflict_target_not_active');
          }
          for (const conflict of conflictItems) {
            const winnerId = conflict.resolution === 'keep_primary'
              ? current.id
              : conflict.resolution === 'keep_candidate' ? conflict.targetMemoryId : null;
            const loserId = conflict.resolution === 'keep_primary'
              ? conflict.targetMemoryId
              : conflict.resolution === 'keep_candidate' ? current.id : null;
            if (loserId && loserId !== current.id) {
              const [loser] = tx.select().from(memories)
                .where(eq(memories.id, loserId)).limit(1).all();
              if (!loser || loser.status !== 'active') {
                throw new MemoryEngineError(409, 'evaluator_conflict_target_not_active');
              }
              const nextVersion = loser.version + 1;
              const [superseded] = tx.update(memories).set({
                status: 'superseded',
                version: nextVersion,
                updatedAt: new Date(),
              }).where(and(
                eq(memories.id, loser.id),
                eq(memories.version, loser.version),
                eq(memories.status, 'active'),
              )).returning().all();
              if (!superseded) throw new MemoryEngineError(409, 'evaluator_conflict_target_version_conflict');
              tx.insert(memoryVersions).values({
                memoryId: loser.id,
                version: nextVersion,
                content: loser.content,
                metadata: {
                  briefContent: loser.briefContent,
                  memoryType: loser.memoryType,
                  tags: loser.tags,
                  entities: loser.entities,
                  importance: loser.importance,
                  confidence: loser.confidence,
                  accessibility: loser.accessibility,
                  temporalType: loser.temporalType,
                  origin: loser.origin,
                  status: 'superseded',
                  retentionPolicy: loser.retentionPolicy,
                },
                changeReason: `AI conflict decision kept ${winnerId}: ${proposalRow.model}`,
                changedBy: 'memory-evaluator',
              }).run();
            }
            tx.insert(memoryChangeLogs).values({
              memoryId: current.id,
              changeType: 'evaluator_conflict_resolved',
              processingStatus: 'completed',
              payload: {
                proposalId,
                primaryMemoryId: current.id,
                candidateMemoryId: conflict.targetMemoryId,
                resolution: conflict.resolution,
                winnerMemoryId: winnerId,
                loserMemoryId: loserId,
                confidence: conflict.confidence,
                reason: decision.reason,
              },
            }).run();
          }
        }
        tx.insert(memoryChangeLogs).values({
          memoryId: current.id,
          changeType: 'semantic_duplicate_sources_merged',
          processingStatus: 'completed',
          payload: {
            proposalId,
            duplicateMemoryIds: duplicateIds,
            transferredSourceCount: transferredSources.length,
          },
        }).run();
      }
      const [accepted] = tx.update(evaluatorProposals).set({
        status: 'accepted',
        decisionReason: decision.reason,
        decidedAt: new Date(),
      }).where(eq(evaluatorProposals.id, proposalId)).returning().all();
      return { proposal: accepted, memory: updated };
    }, { behavior: 'immediate' });
  }

  async listProposals(status = 'pending', limit = 50): Promise<Record<string, unknown>[]> {
    const rows = await this.client.db.select().from(evaluatorProposals)
      .where(eq(evaluatorProposals.status, status))
      .orderBy(evaluatorProposals.createdAt)
      .limit(Math.min(100, Math.max(1, limit)));
    return rows;
  }

  async runLifecycle(input: LifecycleRunInput): Promise<Record<string, unknown>> {
    const batchSize = input.batchSize ?? 100;
    const dryRun = input.dryRun ?? false;
    const candidates = this.client.db.transaction((tx) => {
      const records = tx.select().from(memories)
        .where(eq(memories.status, 'active'))
        .orderBy(memories.accessibilityUpdatedAt, memories.accessibility)
        .limit(batchSize).all();
      const now = new Date();
      const output: Array<Record<string, unknown>> = [];
      for (const record of records) {
        const elapsedDays = Math.max(0, (now.getTime() - record.accessibilityUpdatedAt.getTime()) / 86_400_000);
        const decayed = decayAccessibility(record.accessibility, record.decayRate, elapsedDays);
        const expired = record.validUntil !== null && record.validUntil.getTime() <= now.getTime();
        const relationCount = tx.select({ id: memoryRelations.id }).from(memoryRelations)
          .where(and(
            eq(memoryRelations.status, 'active'),
            or(eq(memoryRelations.sourceMemoryId, record.id), eq(memoryRelations.targetMemoryId, record.id)),
          )).all().length;
        const forgetCandidate = record.retentionPolicy !== 'protected'
          && record.importance < 0.2
          && decayed < 0.2
          && relationCount === 0;
        if (!dryRun) {
          tx.update(memories).set({
            accessibility: decayed,
            accessibilityUpdatedAt: now,
          }).where(eq(memories.id, record.id)).run();
          if (forgetCandidate || expired) {
            tx.insert(memoryChangeLogs).values({
              memoryId: record.id,
              changeType: 'forget_candidate',
              payload: {
                version: record.version,
                expired,
                importance: record.importance,
                accessibility: decayed,
                reason: expired ? 'valid_until_elapsed' : 'low_importance_and_accessibility',
              },
            }).run();
          }
        }
        if (forgetCandidate || expired) {
          output.push({
            memoryId: record.id,
            version: record.version,
            expired,
            forgetCandidate,
            importance: record.importance,
            previousAccessibility: record.accessibility,
            accessibility: decayed,
            retentionPolicy: record.retentionPolicy,
            temporalType: record.temporalType,
            lastAccessedAt: record.lastAccessedAt,
            lastConfirmedAt: record.lastConfirmedAt,
            relationCount,
            suggestedAction: record.retentionPolicy === 'protected' ? 'keep'
              : expired || forgetCandidate ? 'review_for_archive' : 'keep',
          });
        }
      }
      return { processed: records.length, forgetCandidates: output };
    }, { behavior: 'immediate' });
    return { dryRun, ...candidates };
  }

  async changeMemoryLifecycle(
    memoryId: string,
    action: 'archive' | 'restore',
    input: MemoryLifecycleAction,
  ): Promise<Record<string, unknown>> {
    return this.client.db.transaction((tx) => {
      const [current] = tx.select().from(memories).where(eq(memories.id, memoryId)).limit(1).all();
      if (!current) throw new MemoryEngineError(404, 'memory_not_found');
      if (current.version !== input.expectedVersion) throw new MemoryEngineError(409, 'memory_version_conflict');
      if (action === 'archive' && current.status !== 'active') throw new MemoryEngineError(409, 'memory_not_active');
      if (action === 'restore' && current.status !== 'archived') throw new MemoryEngineError(409, 'memory_not_archived');
      const status = action === 'archive' ? 'archived' : 'active';
      const [updated] = tx.update(memories).set({
        status,
        version: current.version + 1,
        updatedAt: new Date(),
      }).where(eq(memories.id, memoryId)).returning().all();
      tx.insert(memoryVersions).values({
        memoryId,
        version: updated.version,
        content: updated.content,
        metadata: {
          briefContent: updated.briefContent,
          memoryType: updated.memoryType,
          tags: updated.tags,
          entities: updated.entities,
          importance: updated.importance,
          confidence: updated.confidence,
          accessibility: updated.accessibility,
          temporalType: updated.temporalType,
          origin: updated.origin,
          status: updated.status,
          retentionPolicy: updated.retentionPolicy,
          validFrom: updated.validFrom?.toISOString() ?? null,
          validUntil: updated.validUntil?.toISOString() ?? null,
          decayRate: updated.decayRate,
        },
        changeReason: input.reason,
        changedBy: 'user',
      }).run();
      tx.insert(memoryChangeLogs).values({
        memoryId,
        changeType: action === 'archive' ? 'archived' : 'restored',
        payload: { version: updated.version, reason: input.reason },
      }).run();
      return updated;
    }, { behavior: 'immediate' });
  }

  async deleteMemory(memoryId: string, reason: string): Promise<{ id: string; deleted: true }> {
    return this.client.db.transaction((tx) => {
      const [current] = tx.select({ id: memories.id, version: memories.version })
        .from(memories).where(eq(memories.id, memoryId)).limit(1).all();
      if (!current) throw new MemoryEngineError(404, 'memory_not_found');
      tx.insert(memoryChangeLogs).values({
        memoryId,
        changeType: 'deleted_by_user',
        payload: { version: current.version, reason, deletedAt: new Date().toISOString() },
      }).run();
      tx.delete(memories).where(eq(memories.id, memoryId)).run();
      return { id: memoryId, deleted: true };
    }, { behavior: 'immediate' });
  }

  async triggerConsolidation(input: ConsolidationTriggerInput): Promise<Record<string, unknown>> {
    const batchSize = input.batchSize ?? 20;
    const configuredEvaluationTypes = (['importance', 'relations', 'conflicts', 'duplicates', 'retention'] as const)
      .filter((taskType) => this.modelRouter.isConfigured(taskType));
    const requested = input.jobType === 'all'
      ? ['basic', 'forgetting', ...configuredEvaluationTypes]
      : [input.jobType];
    const createdJobs: Array<Record<string, unknown>> = [];
    for (const jobType of requested) {
      if (jobType === 'basic') {
        const pending = await this.client.db.select({ id: rawMemories.id }).from(rawMemories)
          .where(eq(rawMemories.processingStatus, 'pending'))
          .orderBy(rawMemories.occurredAt)
          .limit(batchSize);
        for (const batch of chunk(pending.map((item) => item.id), batchSize)) {
          if (!batch.length) continue;
          createdJobs.push(await this.insertJob(jobType, batch, batch));
        }
        continue;
      }
      if (jobType === 'forgetting') {
        createdJobs.push(await this.insertJob(jobType, [], [`forgetting:${new Date().toISOString().slice(0, 13)}`]));
        continue;
      }
      const changes = await this.client.db.select({
        id: memoryChangeLogs.id,
        memoryId: memoryChangeLogs.memoryId,
      }).from(memoryChangeLogs)
        .where(and(eq(memoryChangeLogs.processingStatus, 'pending'), isNotNull(memoryChangeLogs.memoryId)))
        .orderBy(memoryChangeLogs.createdAt)
        .limit(batchSize * 10);
      const memoryIds = [...new Set(changes.map((item) => item.memoryId).filter((id): id is string => Boolean(id)))];
      for (const batch of chunk(memoryIds, batchSize)) {
        if (!batch.length) continue;
        const changeIds = changes.filter((change) => change.memoryId && batch.includes(change.memoryId)).map((change) => String(change.id));
        createdJobs.push(await this.insertJob(jobType, batch, changeIds));
      }
    }
    return { created: createdJobs.length, jobs: createdJobs };
  }

  async listConsolidationJobs(input: JobListInput): Promise<Record<string, unknown>[]> {
    const limit = input.limit ?? 50;
    if (input.status) {
      return this.client.db.select().from(consolidationJobs)
        .where(eq(consolidationJobs.status, input.status))
        .orderBy(consolidationJobs.createdAt).limit(limit);
    }
    return this.client.db.select().from(consolidationJobs)
      .orderBy(consolidationJobs.createdAt).limit(limit);
  }

  async listConsolidationProposals(status = 'pending', limit = 50): Promise<Record<string, unknown>[]> {
    return this.client.db.select().from(consolidationProposals)
      .where(eq(consolidationProposals.status, status))
      .orderBy(consolidationProposals.createdAt)
      .limit(Math.min(100, Math.max(1, limit)));
  }

  async decideConsolidationProposal(
    proposalId: string,
    decision: ProposalDecision,
  ): Promise<Record<string, unknown>> {
    const result = this.client.db.transaction((tx) => {
      const [proposal] = tx.select().from(consolidationProposals)
        .where(eq(consolidationProposals.id, proposalId)).limit(1).all();
      if (!proposal) throw new MemoryEngineError(404, 'consolidation_proposal_not_found');
      if (proposal.status !== 'pending') {
        throw new MemoryEngineError(409, 'consolidation_proposal_already_decided');
      }

      const raws = tx.select().from(rawMemories)
        .where(inArray(rawMemories.id, proposal.inputRawIds)).all();
      const rawById = new Map(raws.map((raw) => [raw.id, raw]));
      if (rawById.size !== proposal.inputRawIds.length) {
        throw new MemoryEngineError(409, 'consolidation_proposal_source_missing');
      }
      for (const rawId of proposal.inputRawIds) {
        const raw = rawById.get(rawId)!;
        if (rawFingerprint(raw) !== proposal.inputFingerprints[rawId]) {
          throw new MemoryEngineError(409, 'consolidation_proposal_source_changed');
        }
      }

      if (decision.decision === 'reject') {
        const [rejected] = tx.update(consolidationProposals).set({
          status: 'rejected',
          decisionReason: decision.reason,
          decidedAt: new Date(),
        }).where(eq(consolidationProposals.id, proposalId)).returning().all();
        tx.update(rawMemories).set({ processingStatus: 'processed' })
          .where(inArray(rawMemories.id, proposal.inputRawIds)).run();
        return { proposal: rejected, memories: [] as Array<{ id: string; searchText: string }> };
      }

      const drafts = proposal.proposals.map((item) => extractedMemorySchema.parse(item));
      const created: Array<{ id: string; searchText: string }> = [];
      for (const draft of drafts) {
        for (const source of draft.sources) {
          const raw = rawById.get(source.rawMemoryId);
          if (!raw || !containsEvidence(raw.content, source.evidenceText)) {
            throw new MemoryEngineError(409, 'consolidation_proposal_evidence_changed');
          }
        }
        const [existing] = tx.select().from(memories).where(and(
          eq(memories.status, 'active'),
          eq(memories.memoryType, draft.memoryType),
          eq(memories.content, draft.content),
        )).limit(1).all();
        if (existing) {
          const existingSources = tx.select().from(memorySources)
            .where(eq(memorySources.memoryId, existing.id)).all();
          const linked = new Set(existingSources.map((source) => source.rawMemoryId));
          const newSources = draft.sources.filter((source) => !linked.has(source.rawMemoryId));
          if (newSources.length) {
            tx.insert(memorySources).values(newSources.map((source) => ({
              memoryId: existing.id,
              rawMemoryId: source.rawMemoryId,
              evidenceType: 'supporting',
              evidenceText: source.evidenceText,
              confidence: Math.min(0.9, draft.confidence),
            }))).run();
            tx.insert(memoryChangeLogs).values({
              memoryId: existing.id,
              changeType: 'consolidation_evidence_added',
              payload: { proposalId, rawMemoryIds: newSources.map((source) => source.rawMemoryId) },
            }).run();
          }
          continue;
        }

        const [memory] = tx.insert(memories).values({
          content: draft.content,
          briefContent: draft.briefContent,
          memoryType: draft.memoryType,
          tags: draft.tags,
          entities: draft.entities,
          importance: draft.importance,
          confidence: Math.min(0.9, draft.confidence),
          temporalType: draft.temporalType,
          origin: 'derived',
          createdBy: 'memory-consolidation',
        }).returning().all();
        tx.insert(memorySources).values(draft.sources.map((source) => ({
          memoryId: memory.id,
          rawMemoryId: source.rawMemoryId,
          evidenceType: 'supporting',
          evidenceText: source.evidenceText,
          confidence: Math.min(0.9, draft.confidence),
        }))).run();
        tx.insert(memoryVersions).values({
          memoryId: memory.id,
          version: 1,
          content: memory.content,
          metadata: {
            briefContent: memory.briefContent,
            memoryType: memory.memoryType,
            tags: memory.tags,
            entities: memory.entities,
            importance: memory.importance,
            confidence: memory.confidence,
            origin: memory.origin,
            temporalType: memory.temporalType,
            status: memory.status,
          },
          changeReason: `accepted consolidation proposal ${proposalId}`,
          changedBy: 'user',
        }).run();
        const searchText = `${memory.briefContent}\n${memory.content}\n${memory.tags.join(' ')}\n${memory.entities.join(' ')}`;
        tx.insert(memoryIndexes).values({
          memoryId: memory.id,
          searchText,
          keywords: keywords(searchText),
        }).run();
        tx.insert(memoryChangeLogs).values({
          memoryId: memory.id,
          changeType: 'consolidated_memory_created',
          payload: { proposalId, sourceRawMemoryIds: draft.sources.map((source) => source.rawMemoryId) },
        }).run();
        created.push({ id: memory.id, searchText });
      }

      const [accepted] = tx.update(consolidationProposals).set({
        status: 'accepted',
        decisionReason: decision.reason,
        decidedAt: new Date(),
      }).where(eq(consolidationProposals.id, proposalId)).returning().all();
      tx.update(rawMemories).set({ processingStatus: 'processed' })
        .where(inArray(rawMemories.id, proposal.inputRawIds)).run();
      return { proposal: accepted, memories: created };
    }, { behavior: 'immediate' });

    for (const memory of result.memories) {
      this.client.sqlite.prepare(
        'INSERT INTO memory_search_fts (memory_id, search_text) VALUES (?, ?)',
      ).run(memory.id, memory.searchText);
      try {
        await this.saveEmbedding(memory.id, memory.searchText);
      } catch (error) {
        console.error(`[memory-engine] embedding generation failed for memory ${memory.id}:`, error);
      }
    }
    return {
      proposal: result.proposal,
      memories: result.memories.map((memory) => ({ id: memory.id })),
    };
  }

  async getConsolidationJob(jobId: string): Promise<Record<string, unknown>> {
    const [job] = await this.client.db.select().from(consolidationJobs)
      .where(eq(consolidationJobs.id, jobId)).limit(1);
    if (!job) throw new MemoryEngineError(404, 'consolidation_job_not_found');
    return job;
  }

  async retryConsolidationJob(jobId: string, input: RetryJobInput): Promise<Record<string, unknown>> {
    return this.client.db.transaction((tx) => {
      const [current] = tx.select().from(consolidationJobs).where(and(
        eq(consolidationJobs.id, jobId),
        eq(consolidationJobs.status, 'failed'),
      )).limit(1).all();
      if (!current) throw new MemoryEngineError(409, 'only_failed_jobs_can_be_retried');
      const [job] = tx.update(consolidationJobs).set({
        status: 'pending',
        retryCount: 0,
        dedupeKey: `retry:${jobId}:${randomUUID()}`,
        checkpoint: { ...current.checkpoint, retryReason: input.reason },
        updatedAt: new Date(),
      }).where(eq(consolidationJobs.id, jobId)).returning().all();
      return job;
    }, { behavior: 'immediate' });
  }

  async processConsolidationJob(): Promise<Record<string, unknown> | null> {
    if (this.processingJob) return null;
    this.processingJob = true;
    try {
      const job = this.client.db.transaction((tx) => {
        const [next] = tx.select().from(consolidationJobs).where(or(
          inArray(consolidationJobs.status, ['pending', 'retrying']),
          and(
            eq(consolidationJobs.status, 'processing'),
            lt(consolidationJobs.updatedAt, new Date(Date.now() - 10 * 60_000)),
          ),
        )).orderBy(consolidationJobs.createdAt).limit(1).all();
        if (!next) return undefined;
        const [claimed] = tx.update(consolidationJobs).set({
          status: 'processing',
          updatedAt: new Date(),
        }).where(eq(consolidationJobs.id, next.id)).returning().all();
        return claimed;
      }, { behavior: 'immediate' });
      if (!job) return null;
      const processedIds = new Set((job.checkpoint.processedIds as string[] | undefined) ?? []);
      try {
        if (job.jobType === 'basic') {
          if (job.inputIds.length) {
            const batchSize = 20;
            for (const batch of chunk(job.inputIds, batchSize)) {
              const pendingIds = batch.filter((id) => !processedIds.has(id));
              if (!pendingIds.length) continue;
              await this.processRawBatch(pendingIds);
              pendingIds.forEach((id) => processedIds.add(id));
              await this.client.db.update(consolidationJobs).set({
                checkpoint: { processedIds: [...processedIds] },
                updatedAt: new Date(),
              }).where(eq(consolidationJobs.id, job.id));
            }
          }
        } else if (job.jobType === 'forgetting') {
          await this.runLifecycle({ batchSize: 100, dryRun: false });
        } else {
          const taskType = job.jobType as EvaluatorRequest['taskType'];
          for (const memoryId of job.inputIds) {
            if (processedIds.has(memoryId)) continue;
            const candidates = taskType === 'importance' || taskType === 'retention'
              ? []
              : await this.findRelatedCandidates(memoryId);
            if (candidates.length || taskType === 'importance' || taskType === 'retention') {
              if (taskType === 'conflicts') {
                for (const candidateMemoryId of candidates) {
                  await this.evaluateMemory({ taskType, memoryId, candidateMemoryIds: [candidateMemoryId] });
                  const [primary] = await this.client.db.select({ status: memories.status }).from(memories)
                    .where(eq(memories.id, memoryId)).limit(1);
                  if (primary?.status !== 'active') break;
                }
              } else {
                await this.evaluateMemory({ taskType, memoryId, candidateMemoryIds: candidates });
              }
            }
            processedIds.add(memoryId);
            await this.client.db.update(consolidationJobs).set({
              checkpoint: { processedIds: [...processedIds] },
              updatedAt: new Date(),
            }).where(eq(consolidationJobs.id, job.id));
          }
          if (job.inputIds.length) {
            await this.client.db.update(memoryChangeLogs).set({ processingStatus: 'completed' })
              .where(inArray(memoryChangeLogs.memoryId, job.inputIds));
          }
        }
        const [completed] = await this.client.db.update(consolidationJobs).set({
          status: 'completed',
          checkpoint: { completedAt: new Date().toISOString() },
          updatedAt: new Date(),
        }).where(eq(consolidationJobs.id, job.id)).returning();
        return completed;
      } catch (error) {
        const retryCount = job.retryCount + 1;
        const status = retryCount >= 3 ? 'failed' : 'retrying';
        const message = error instanceof Error ? error.message : String(error);
        const [failed] = await this.client.db.update(consolidationJobs).set({
          status,
          retryCount,
          checkpoint: { ...job.checkpoint, processedIds: [...processedIds], lastError: message },
          updatedAt: new Date(),
        }).where(eq(consolidationJobs.id, job.id)).returning();
        if (status === 'failed') console.error(`[memory-engine] consolidation job ${job.id} failed:`, message);
        return failed;
      }
    } finally {
      this.processingJob = false;
    }
  }

  private async processRawBatch(rawIds: string[]): Promise<void> {
    const raws = await this.client.db.select().from(rawMemories)
      .where(inArray(rawMemories.id, rawIds));
    let extracted: Awaited<ReturnType<ModelRouter['extractMemories']>> | undefined;
    if (this.modelRouter.isConfigured('retention')) {
      const events = raws.map((raw) => ({
        rawMemoryId: raw.id,
        eventType: raw.eventType,
        source: raw.source,
        occurredAt: raw.occurredAt.toISOString(),
        content: JSON.stringify(raw.content).slice(0, 4000),
      }));
      extracted = await this.modelRouter.extractMemories(events);
    }

    const rawById = new Map(raws.map((raw) => [raw.id, raw]));
    const seenContent = new Set<string>();
    const candidates = (extracted?.memories ?? []).flatMap((candidate) => {
      if (candidate.sources.some((source) => {
        const raw = rawById.get(source.rawMemoryId);
        return !raw || !containsEvidence(raw.content, source.evidenceText);
      })) {
        console.warn('[memory-engine] skipped extraction candidate with unverifiable evidence');
        return [];
      }
      const fingerprint = candidate.content.toLowerCase().replace(/\s+/g, ' ').trim();
      if (seenContent.has(fingerprint)) return [];
      seenContent.add(fingerprint);
      return [extractedMemorySchema.parse(candidate)];
    });
    const fingerprints = Object.fromEntries(raws.map((raw) => [raw.id, rawFingerprint(raw)]));
    const dedupeKey = `raw:${createHash('sha256').update([...rawIds].sort().join(',')).digest('hex')}`;
    const processingStatus = candidates.length ? 'proposed' : 'processed';

    const proposalId = this.client.db.transaction((tx) => {
      let existingProposalId: string | undefined;
      if (candidates.length && extracted) {
        const [created] = tx.insert(consolidationProposals).values({
          dedupeKey,
          inputRawIds: raws.map((raw) => raw.id),
          inputFingerprints: fingerprints,
          proposals: candidates,
          model: extracted.model,
        }).onConflictDoNothing({ target: consolidationProposals.dedupeKey }).returning().all();
        if (created) {
          existingProposalId = created.id;
        } else {
          const [existing] = tx.select({ id: consolidationProposals.id })
            .from(consolidationProposals)
            .where(eq(consolidationProposals.dedupeKey, dedupeKey))
            .limit(1).all();
          existingProposalId = existing?.id;
        }
      }
      for (const raw of raws) {
        const searchText = `${JSON.stringify(raw.content)} ${JSON.stringify(raw.metadata)}`;
        tx.update(rawMemories).set({ searchText, processingStatus })
          .where(eq(rawMemories.id, raw.id)).run();
        this.client.sqlite.prepare('DELETE FROM raw_memory_search_fts WHERE raw_memory_id = ?').run(raw.id);
        this.client.sqlite.prepare(
          'INSERT INTO raw_memory_search_fts (raw_memory_id, search_text) VALUES (?, ?)',
        ).run(raw.id, searchText);
      }
      const changeLogs = tx.select().from(memoryChangeLogs)
        .where(eq(memoryChangeLogs.processingStatus, 'pending')).all();
      for (const log of changeLogs) {
        const rawId = log.payload.rawMemoryId;
        if (typeof rawId === 'string' && rawIds.includes(rawId)) {
          tx.update(memoryChangeLogs).set({ processingStatus: 'completed' })
            .where(eq(memoryChangeLogs.id, log.id)).run();
        }
      }
      return existingProposalId;
    }, { behavior: 'immediate' });
    if (proposalId) {
      const [proposal] = await this.client.db.select({ status: consolidationProposals.status })
        .from(consolidationProposals)
        .where(eq(consolidationProposals.id, proposalId))
        .limit(1);
      if (proposal?.status === 'pending') {
        await this.decideConsolidationProposal(proposalId, {
          decision: 'accept',
          reason: 'Automatically accepted after source fingerprint and evidence validation.',
        });
      }
    }
  }

  private async insertJob(jobType: string, inputIds: string[], dedupeInputs: string[]): Promise<Record<string, unknown>> {
    const dedupeKey = `${jobType}:${createHash('sha256').update([...dedupeInputs].sort().join(',')).digest('hex')}`;
    const [job] = await this.client.db.insert(consolidationJobs).values({
      jobType,
      inputIds,
      dedupeKey,
    }).onConflictDoNothing({ target: consolidationJobs.dedupeKey }).returning();
    if (job) return job;
    const [existing] = await this.client.db.select().from(consolidationJobs)
      .where(eq(consolidationJobs.dedupeKey, dedupeKey)).limit(1);
    if (!existing) throw new Error('consolidation job insert conflict without existing row');
    return existing;
  }

  private async findRelatedCandidates(memoryId: string): Promise<string[]> {
    const [memory] = await this.client.db.select({
      memoryType: memories.memoryType,
      tags: memories.tags,
      entities: memories.entities,
    }).from(memories).where(eq(memories.id, memoryId)).limit(1);
    if (!memory) throw new MemoryEngineError(404, 'memory_not_found');
    const terms = [...new Set([...memory.tags, ...memory.entities])].slice(0, 20);
    if (!terms.length) return [];
    const candidates = await this.client.db.select({
      id: memories.id,
      tags: memories.tags,
      entities: memories.entities,
    }).from(memories).where(and(
      eq(memories.status, 'active'),
      eq(memories.memoryType, memory.memoryType),
    )).orderBy(desc(memories.importance), desc(memories.updatedAt)).limit(50);
    const termSet = new Set(terms);
    return candidates.filter((candidate) =>
      candidate.id !== memoryId
      && [...candidate.tags, ...candidate.entities].some((term) => termSet.has(term)))
      .slice(0, 10)
      .map((candidate) => candidate.id);
  }

  async recall(
    input: RecallInput,
    options: {
      deep?: boolean;
      includeLowAccessibility?: boolean;
      expandTimeRange?: boolean;
      seedMemoryIds?: string[];
      rawQuery?: string;
      rawTimeRange?: DeepRecallInput['timeRange'];
    } = {},
  ): Promise<Record<string, unknown>> {
    const initialContext = await this.getContext(input.contextId) as {
      status: string; tokenBudget: number; scene: string | null;
    };
    if (initialContext.status !== 'active') throw new MemoryEngineError(409, 'context_closed');
    const scene = input.scene ?? initialContext.scene ?? '';
    const searchQuery = `${input.query} ${scene}`.trim();
    const searchTerms = keywords(searchQuery).slice(0, 20);
    const ftsQuery = searchTerms.map((term) => `"${term.replace(/"/g, '""')}"`).join(' OR ');
    let queryEmbedding: number[] | undefined;
    let queryEmbeddingModel: string | undefined;
    let embeddingStatus: 'available' | 'not_configured' | 'failed' = 'not_configured';
    if (this.modelRouter.isConfigured('importance')) {
      try {
        const embedded = await this.modelRouter.embedText(searchQuery);
        queryEmbedding = embedded.embedding;
        queryEmbeddingModel = embedded.model;
        embeddingStatus = 'available';
      } catch (error) {
        embeddingStatus = 'failed';
        console.error('[memory-engine] query embedding failed; using lexical retrieval:', error);
      }
    }
    const storedEmbeddings = queryEmbedding
      ? await this.client.db.select({
        memoryId: memoryEmbeddings.memoryId,
        model: memoryEmbeddings.model,
        vector: memoryEmbeddings.vector,
      }).from(memoryEmbeddings)
      : [];
    const vectorScores = new Map(storedEmbeddings
      .filter((item) => item.model === queryEmbeddingModel)
      .map((item) => [
      item.memoryId,
      cosineSimilarity(queryEmbedding!, item.vector),
      ]));
    const vectorIds = [...vectorScores.entries()]
      .sort((left, right) => right[1] - left[1])
      .slice(0, 100)
      .map(([memoryId]) => memoryId);

    return this.client.db.transaction((tx) => {
      const [context] = tx.select().from(memoryContexts)
        .where(eq(memoryContexts.id, input.contextId)).limit(1).all();
      if (!context) throw new MemoryEngineError(404, 'context_not_found');
      if (context.status !== 'active') throw new MemoryEngineError(409, 'context_closed');

      const budget = Math.min(input.tokenBudget ?? context.tokenBudget, context.tokenBudget);
      let remainingTokens = Math.max(0, budget - context.tokensUsed);
      const limit = input.limit ?? 8;
      const now = new Date();
      const filters = [
        eq(memories.status, 'active'),
        or(isNull(memories.validFrom), lte(memories.validFrom, now)),
        or(isNull(memories.validUntil), gt(memories.validUntil, now)),
      ];
      if (!options.includeLowAccessibility) filters.push(gte(memories.accessibility, 0.1));
      if (!options.expandTimeRange && input.timeRange?.from) {
        filters.push(gte(memories.createdAt, new Date(input.timeRange.from)));
      }
      if (!options.expandTimeRange && input.timeRange?.to) {
        filters.push(lte(memories.createdAt, new Date(input.timeRange.to)));
      }
      const candidateLimit = options.deep ? 500 : 100;
      const ftsRows = ftsQuery
        ? this.client.sqlite.prepare(
          'SELECT memory_id FROM memory_search_fts WHERE memory_search_fts MATCH ? ORDER BY bm25(memory_search_fts) LIMIT ?',
        ).all(ftsQuery, candidateLimit) as Array<{ memory_id: string }>
        : [];
      const ftsRanks = new Map(ftsRows.map((row, index) => [row.memory_id, 1 / (index + 1)]));
      const existingLoads = tx.select().from(contextMemoryLoads)
        .where(eq(contextMemoryLoads.contextId, input.contextId)).all();
      const loadsByMemoryId = new Map(existingLoads.map((load) => [load.memoryId, load]));
      const recentCandidates = tx.select().from(memories)
        .where(and(...filters))
        .orderBy(desc(memories.updatedAt))
        .limit(candidateLimit).all();
      const ftsCandidates = ftsRows.length
        ? tx.select().from(memories).where(and(
          inArray(memories.id, ftsRows.map((row) => row.memory_id)),
          ...filters,
        )).all()
        : [];
      const vectorCandidates = vectorIds.length
        ? tx.select().from(memories).where(and(
          inArray(memories.id, vectorIds),
          ...filters,
        )).all()
        : [];
      const candidatesById = new Map([...ftsCandidates, ...vectorCandidates, ...recentCandidates].map((row) => [row.id, row]));
      const candidates = [...candidatesById.values()].slice(0, candidateLimit * 2);
      const candidateIds = candidates.map((row) => row.id);
      const indexRows = candidateIds.length
        ? tx.select().from(memoryIndexes).where(inArray(memoryIndexes.memoryId, candidateIds)).all()
        : [];
      const indexByMemoryId = new Map(indexRows.map((row) => [row.memoryId, row]));
      const sparse = candidates.flatMap((row): RecallRow[] => {
        const loaded = loadsByMemoryId.get(row.id);
        if (!input.refresh && loaded) return [];
        const indexText = indexByMemoryId.get(row.id)?.searchText ?? '';
        const searchable = `${indexText} ${row.content} ${row.briefContent} ${row.tags.join(' ')} ${row.entities.join(' ')}`
          .toLowerCase();
        const matched = searchTerms.filter((term) => searchable.includes(term.toLowerCase())).length;
        const exactMatch = searchable.includes(searchQuery.toLowerCase());
        const ftsRank = ftsRanks.get(row.id) ?? 0;
        const vectorScore = vectorScores.get(row.id) ?? 0;
        if (!matched && !exactMatch && !ftsRank && vectorScore <= 0) return [];
        return [{
          id: row.id,
          content: row.content,
          brief_content: row.briefContent,
          memory_type: row.memoryType,
          importance: row.importance,
          confidence: row.confidence,
          accessibility: row.accessibility,
          temporal_type: row.temporalType,
          origin: row.origin,
          valid_from: row.validFrom,
          valid_until: row.validUntil,
          created_at: row.createdAt,
          tags: row.tags,
          entities: row.entities,
          relevance_rank: Math.max(
            ftsRank,
            matched / Math.max(1, searchTerms.length),
            exactMatch ? 0.5 : 0,
            vectorScore,
          ),
        }];
      }).sort((a, b) => b.relevance_rank - a.relevance_rank);
      const seedRows = options.seedMemoryIds?.length
        ? tx.select().from(memories).where(inArray(memories.id, options.seedMemoryIds)).all()
        : [];
      const loadedSeedRows = !input.refresh && options.seedMemoryIds?.length
        ? tx.select({ memoryId: contextMemoryLoads.memoryId }).from(contextMemoryLoads).where(and(
          eq(contextMemoryLoads.contextId, context.id),
          inArray(contextMemoryLoads.memoryId, options.seedMemoryIds),
        )).all()
        : [];
      const loadedSeedIds = new Set(loadedSeedRows.map((row) => row.memoryId));
      const seedIds = seedRows
        .filter((row) => row.status === 'active' && !loadedSeedIds.has(row.id))
        .map((row) => row.id);
      const byId = new Map<string, RecallRow>(sparse.map((row) => [row.id, row]));
      for (const row of seedRows) {
        if (seedIds.includes(row.id)) {
          byId.set(row.id, {
            id: row.id,
            content: row.content,
            brief_content: row.briefContent,
            memory_type: row.memoryType,
            importance: row.importance,
            confidence: row.confidence,
            accessibility: row.accessibility,
            temporal_type: row.temporalType,
            origin: row.origin,
            valid_from: row.validFrom,
            valid_until: row.validUntil,
            created_at: row.createdAt,
            tags: row.tags,
            entities: row.entities,
            relevance_rank: 1,
          });
        }
      }
      const ranks = reciprocalRankFusion([
        sparse.map((row) => row.id),
        seedIds,
      ]);
      const maxRrf = Math.max(...ranks.values(), Number.EPSILON);
      const sceneWords = keywords(scene);
      const ranked = [...byId.values()].map((row) => {
        const relevance = (ranks.get(row.id) ?? 0) / maxRrf;
        const ageDays = Math.max(0, (now.getTime() - row.created_at.getTime()) / 86_400_000);
        const temporal = temporalScore(row.temporal_type, ageDays);
        const searchContext = `${row.tags.join(' ')} ${row.entities.join(' ')}`.toLowerCase();
        const contextRelevance = sceneWords.length
          ? sceneWords.filter((word) => searchContext.includes(word)).length / sceneWords.length
          : 0.5;
        const score = 0.4 * relevance + 0.18 * row.importance + 0.14 * temporal
          + 0.12 * row.accessibility + 0.10 * contextRelevance + 0.06 * row.confidence;
        return { row, relevance, score };
      }).sort((a, b) => b.score - a.score);

      const selected: Array<Record<string, unknown>> = [];
      let tokensAdded = 0;
      for (const item of ranked) {
        if (selected.length >= limit) break;
        const existing = loadsByMemoryId.get(item.row.id);
        if (existing && existing.loadLevel === 'full' && !input.refresh) continue;
        const full = input.includeBrief === false;
        const resultText = full ? item.row.content : item.row.brief_content;
        const cost = estimateTokens(resultText);
        const increment = existing?.loadLevel === 'full' ? 0 : Math.max(0, cost - (existing?.estimatedTokens ?? 0));
        if (increment > remainingTokens) continue;
        remainingTokens -= increment;
        tokensAdded += increment;
        const nextLevel = full || existing?.loadLevel === 'full' ? 'full' : 'brief';
        const storedTokens = nextLevel === 'full' ? estimateTokens(item.row.content) : estimateTokens(item.row.brief_content);
        selected.push({
          memoryId: item.row.id,
          brief: resultText,
          importance: item.row.importance,
          confidence: item.row.confidence,
          relevance: item.relevance,
          origin: item.row.origin,
          temporalType: item.row.temporal_type,
          validFrom: item.row.valid_from,
          validUntil: item.row.valid_until,
          hasMoreDetails: item.row.content !== item.row.brief_content,
          estimatedTokens: increment,
        });
        tx.insert(contextMemoryLoads).values({
          contextId: input.contextId,
          memoryId: item.row.id,
          loadLevel: nextLevel,
          estimatedTokens: storedTokens,
        }).onConflictDoUpdate({
          target: [contextMemoryLoads.contextId, contextMemoryLoads.memoryId],
          set: { loadLevel: nextLevel, estimatedTokens: storedTokens, loadedAt: new Date() },
        }).run();
        const [loadedMemory] = tx.select({
          accessCount: memories.accessCount,
          accessibility: memories.accessibility,
        }).from(memories).where(eq(memories.id, item.row.id)).limit(1).all();
        tx.update(memories).set({
          accessCount: (loadedMemory?.accessCount ?? 0) + 1,
          lastAccessedAt: new Date(),
          accessibility: Math.min(1, (loadedMemory?.accessibility ?? 0) + 0.05),
          accessibilityUpdatedAt: new Date(),
        }).where(eq(memories.id, item.row.id)).run();
      }
      const rawResults: Array<Record<string, unknown>> = [];
      let rawTokens = 0;
      let rawHasMore = false;
      const rawTerms = keywords(options.rawQuery ?? '');
      if (options.deep && options.rawQuery && !rawTerms.length) rawTerms.push(options.rawQuery);
      if (options.deep && options.rawQuery && rawTerms.length) {
        const loadedRawRows = tx.select({ rawMemoryId: contextRawMemoryLoads.rawMemoryId })
          .from(contextRawMemoryLoads).where(eq(contextRawMemoryLoads.contextId, input.contextId)).all();
        const loadedRawIds = new Set(loadedRawRows.map((row) => row.rawMemoryId));
        const rawDateFilters = [];
        if (!options.expandTimeRange && options.rawTimeRange?.from) {
          rawDateFilters.push(gte(rawMemories.occurredAt, new Date(options.rawTimeRange.from)));
        }
        if (!options.expandTimeRange && options.rawTimeRange?.to) {
          rawDateFilters.push(lte(rawMemories.occurredAt, new Date(options.rawTimeRange.to)));
        }
        const recentRawRows = tx.select().from(rawMemories)
          .where(rawDateFilters.length ? and(...rawDateFilters) : undefined)
          .orderBy(desc(rawMemories.occurredAt))
          .limit(501)
          .all();
        const rawFtsQuery = rawTerms.slice(0, 20).map((term) => `"${term.replace(/"/g, '""')}"`).join(' OR ');
        const rawFtsRows = rawFtsQuery
          ? this.client.sqlite.prepare(
            'SELECT raw_memory_id FROM raw_memory_search_fts WHERE raw_memory_search_fts MATCH ? ORDER BY bm25(raw_memory_search_fts) LIMIT 500',
          ).all(rawFtsQuery) as Array<{ raw_memory_id: string }>
          : [];
        const rawCandidateIds = [...new Set([
          ...rawFtsRows.map((row) => row.raw_memory_id),
          ...recentRawRows.map((row) => row.id),
        ])];
        const rawCandidateRows = rawCandidateIds.length
          ? tx.select().from(rawMemories).where(and(
            inArray(rawMemories.id, rawCandidateIds),
            ...rawDateFilters,
          )).all()
          : [];
        const rawRows = rawCandidateRows
          .filter((raw) => {
            if (loadedRawIds.has(raw.id)) return false;
            const searchable = `${raw.searchText} ${JSON.stringify(raw.content)} ${JSON.stringify(raw.metadata)}`
              .toLowerCase();
            return rawTerms.some((term) => searchable.includes(term.toLowerCase()));
          })
          .sort((a, b) => b.occurredAt.getTime() - a.occurredAt.getTime())
          .slice(0, 501);
        for (const raw of rawRows.slice(0, 100)) {
          const cost = estimateTokens(JSON.stringify({
            eventType: raw.eventType,
            source: raw.source,
            sessionId: raw.sessionId,
            occurredAt: raw.occurredAt,
            content: raw.content,
            mediaRefs: raw.mediaRefs,
          }));
          if (cost > remainingTokens) continue;
          remainingTokens -= cost;
          rawTokens += cost;
          rawResults.push({
            rawMemoryId: raw.id,
            eventType: raw.eventType,
            source: raw.source,
            sessionId: raw.sessionId,
            occurredAt: raw.occurredAt,
            content: raw.content,
            mediaRefs: raw.mediaRefs,
            estimatedTokens: cost,
          });
          tx.insert(contextRawMemoryLoads).values({
            contextId: input.contextId,
            rawMemoryId: raw.id,
            estimatedTokens: cost,
          }).onConflictDoNothing().run();
        }
        rawHasMore = rawRows.length > 100 || rawRows.slice(0, 100).length > rawResults.length;
      }
      const totalTokensAdded = tokensAdded + rawTokens;
      if (totalTokensAdded > 0) {
        tx.update(memoryContexts).set({
          tokensUsed: context.tokensUsed + totalTokensAdded,
        }).where(eq(memoryContexts.id, input.contextId)).run();
      }
      return {
        results: selected,
        embeddingStatus,
        hasMore: ranked.length > selected.length,
        ...(options.deep ? { rawResults, rawHasMore } : {}),
        estimatedTokensUsed: totalTokensAdded,
        contextTokensUsed: context.tokensUsed + totalTokensAdded,
      };
    }, { behavior: 'immediate' });
  }

  async expand(input: ExpandInput): Promise<Record<string, unknown>> {
    return this.client.db.transaction((tx) => {
      const [context] = tx.select().from(memoryContexts)
        .where(eq(memoryContexts.id, input.contextId)).limit(1).all();
      if (!context) throw new MemoryEngineError(404, 'context_not_found');
      if (context.status !== 'active') throw new MemoryEngineError(409, 'context_closed');

      const roots = tx.select().from(memories).where(inArray(memories.id, input.memoryIds)).all();
      const foundRootIds = new Set(roots.map((row) => row.id));
      const missingMemoryIds = input.memoryIds.filter((id) => !foundRootIds.has(id));
      const memoryById = new Map(roots.map((row) => [row.id, row]));
      const relationMap = new Map<string, typeof memoryRelations.$inferSelect>();
      let frontier = roots.map((row) => row.id);
      const depth = input.includeRelations === false ? 0 : input.depth ?? 1;
      for (let level = 0; level < depth && frontier.length; level += 1) {
        const levelRelations = tx.select().from(memoryRelations).where(and(
          eq(memoryRelations.status, 'active'),
          or(
            inArray(memoryRelations.sourceMemoryId, frontier),
            inArray(memoryRelations.targetMemoryId, frontier),
          ),
        )).all();
        const nextFrontier = new Set<string>();
        for (const relation of levelRelations) {
          relationMap.set(relation.id, relation);
          const neighbor = frontier.includes(relation.sourceMemoryId)
            ? relation.targetMemoryId
            : relation.sourceMemoryId;
          if (!memoryById.has(neighbor)) nextFrontier.add(neighbor);
        }
        frontier = [...nextFrontier];
        if (frontier.length) {
          const related = tx.select().from(memories).where(inArray(memories.id, frontier)).all();
          for (const row of related) memoryById.set(row.id, row);
          frontier = related.map((row) => row.id);
        }
      }

      const allIds = [...memoryById.keys()];
      const sources = allIds.length
        ? tx.select().from(memorySources).where(inArray(memorySources.memoryId, allIds)).all()
        : [];
      const sourcesById = new Map<string, typeof sources>();
      for (const source of sources) sourcesById.set(source.memoryId, [...(sourcesById.get(source.memoryId) ?? []), source]);
      const existingLoads = allIds.length
        ? tx.select().from(contextMemoryLoads).where(and(
          eq(contextMemoryLoads.contextId, input.contextId),
          inArray(contextMemoryLoads.memoryId, allIds),
        )).all()
        : [];
      const loadsById = new Map(existingLoads.map((load) => [load.memoryId, load]));
      const budget = Math.min(input.tokenBudget ?? context.tokenBudget, context.tokenBudget);
      let remaining = Math.max(0, budget - context.tokensUsed);
      let tokensAdded = 0;
      const expanded: Array<Record<string, unknown>> = [];
      const omittedMemoryIds: string[] = [];
      const orderedMemories = [
        ...roots,
        ...[...memoryById.values()].filter((row) => !foundRootIds.has(row.id)),
      ];
      for (const memory of orderedMemories) {
        const existing = loadsById.get(memory.id);
        if (existing?.loadLevel === 'full' && !input.refresh) continue;
        const evidence = sourcesById.get(memory.id) ?? [];
        const fullCost = estimateTokens(memory.content)
          + evidence.reduce((sum, item) => sum + estimateTokens(item.evidenceText), 0);
        const increment = existing?.loadLevel === 'full'
          ? 0
          : Math.max(0, fullCost - (existing?.estimatedTokens ?? 0));
        if (increment > remaining) {
          omittedMemoryIds.push(memory.id);
          continue;
        }
        remaining -= increment;
        tokensAdded += increment;
        expanded.push({
          memoryId: memory.id,
          content: memory.content,
          brief: memory.briefContent,
          memoryType: memory.memoryType,
          tags: memory.tags,
          entities: memory.entities,
          importance: memory.importance,
          confidence: memory.confidence,
          origin: memory.origin,
          status: memory.status,
          temporalType: memory.temporalType,
          validFrom: memory.validFrom,
          validUntil: memory.validUntil,
          sources: evidence,
          estimatedTokens: increment,
        });
        tx.insert(contextMemoryLoads).values({
          contextId: input.contextId,
          memoryId: memory.id,
          loadLevel: 'full',
          estimatedTokens: fullCost,
        }).onConflictDoUpdate({
          target: [contextMemoryLoads.contextId, contextMemoryLoads.memoryId],
          set: { loadLevel: 'full', estimatedTokens: fullCost, loadedAt: new Date() },
        }).run();
        tx.update(memories).set({
          accessCount: memory.accessCount + 1,
          lastAccessedAt: new Date(),
          accessibility: Math.min(1, memory.accessibility + 0.05),
          accessibilityUpdatedAt: new Date(),
        }).where(eq(memories.id, memory.id)).run();
      }

      const returnedMemoryIds = new Set(expanded.map((item) => item.memoryId as string));
      const relatedRelations = [...relationMap.values()].filter((relation) =>
        (returnedMemoryIds.has(relation.sourceMemoryId) || foundRootIds.has(relation.sourceMemoryId))
        && (returnedMemoryIds.has(relation.targetMemoryId) || foundRootIds.has(relation.targetMemoryId)));
      const loadedRelations = relatedRelations.length && !input.refresh
        ? tx.select({ relationId: contextRelationLoads.relationId }).from(contextRelationLoads).where(and(
          eq(contextRelationLoads.contextId, input.contextId),
          inArray(contextRelationLoads.relationId, relatedRelations.map((relation) => relation.id)),
        )).all()
        : [];
      const loadedRelationIds = new Set(loadedRelations.map((row) => row.relationId));
      let relationTokens = 0;
      const relations: typeof relatedRelations = [];
      for (const relation of relatedRelations) {
        if (loadedRelationIds.has(relation.id)) continue;
        const cost = estimateTokens(`${relation.relationType} ${(relation.evidenceIds ?? []).join(' ')}`);
        if (cost > remaining) continue;
        remaining -= cost;
        relationTokens += cost;
        relations.push(relation);
        tx.insert(contextRelationLoads).values({
          contextId: input.contextId,
          relationId: relation.id,
          estimatedTokens: cost,
        }).onConflictDoNothing().run();
      }

      const conflicts = relations.filter((relation) => relation.relationType === 'contradicts');
      const contradictingSources = sources.filter((source) =>
        source.evidenceType === 'contradicting' && returnedMemoryIds.has(source.memoryId));
      const added = tokensAdded + relationTokens;
      if (added > 0) {
        tx.update(memoryContexts).set({
          tokensUsed: context.tokensUsed + added,
        }).where(eq(memoryContexts.id, input.contextId)).run();
      }
      return {
        memories: expanded,
        relations,
        conflicts: { relations: conflicts, evidence: contradictingSources },
        missingMemoryIds,
        omittedMemoryIds,
        estimatedTokensUsed: added,
        contextTokensUsed: context.tokensUsed + added,
      };
    }, { behavior: 'immediate' });
  }

  async deepRecall(input: DeepRecallInput): Promise<Record<string, unknown>> {
    await this.getContext(input.contextId);
    const relatedIds = [...new Set(input.relatedMemoryIds ?? [])];
    const relatedRows = relatedIds.length
      ? await this.client.db.select({
        id: memories.id,
        briefContent: memories.briefContent,
      }).from(memories).where(inArray(memories.id, relatedIds))
      : [];
    if (relatedRows.length !== relatedIds.length) throw new MemoryEngineError(404, 'related_memory_not_found');
    const expandedQuery = [input.query, ...relatedRows.map((row) => row.briefContent)].join(' ').slice(0, 4000);
    return this.recall({
      contextId: input.contextId,
      query: expandedQuery,
      timeRange: input.expandTimeRange ? undefined : input.timeRange,
      tokenBudget: input.tokenBudget,
    }, {
      deep: true,
      includeLowAccessibility: input.includeLowAccessibility ?? true,
      expandTimeRange: input.expandTimeRange ?? false,
      seedMemoryIds: relatedIds,
      rawQuery: input.query,
      rawTimeRange: input.timeRange,
    });
  }

  async recallForAgent(
    query: string,
    sessionId: string,
    agentId = 'main',
    scene = 'voice',
  ): Promise<string> {
    await this.initialize();
    const context = await this.createContext({ agentId, sessionId, scene, tokenBudget: 1200 }) as { id: string };
    try {
      const packet = await this.recall({ contextId: context.id, query, limit: 3, tokenBudget: 1200 }) as {
        results: Array<{ brief: string }>;
      };
      return packet.results.map((result) => `- ${result.brief}`).join('\n').slice(0, 1500);
    } finally {
      await this.closeContext(context.id);
    }
  }

  private async saveEmbedding(memoryId: string, text: string): Promise<void> {
    if (!this.modelRouter.isConfigured('importance')) return;
    const { embedding, model } = await this.modelRouter.embedText(text);
    await this.client.db.insert(memoryEmbeddings).values({
      memoryId,
      vector: embedding,
      model,
    }).onConflictDoUpdate({
      target: memoryEmbeddings.memoryId,
      set: { vector: embedding, model, updatedAt: new Date() },
    });
  }

  private async backfillEmbeddings(): Promise<void> {
    if (!this.modelRouter.isConfigured('importance')) return;
    const [allMemories, existingEmbeddings] = await Promise.all([
      this.client.db.select({
        id: memories.id,
        content: memories.content,
        briefContent: memories.briefContent,
        tags: memories.tags,
        entities: memories.entities,
      }).from(memories).where(eq(memories.status, 'active')),
      this.client.db.select({ memoryId: memoryEmbeddings.memoryId }).from(memoryEmbeddings),
    ]);
    const indexedIds = new Set(existingEmbeddings.map((item) => item.memoryId));
    for (const memory of allMemories) {
      if (indexedIds.has(memory.id)) continue;
      const searchText = `${memory.briefContent}\n${memory.content}\n${memory.tags.join(' ')}\n${memory.entities.join(' ')}`;
      await this.saveEmbedding(memory.id, searchText);
    }
  }

  private async refreshIndex(memoryId: string, searchText: string): Promise<'indexed' | 'text_only' | 'embedding_pending'> {
    await this.client.db.update(memoryIndexes).set({
      searchText,
      keywords: keywords(searchText),
      indexedAt: new Date(),
    }).where(eq(memoryIndexes.memoryId, memoryId));
    this.client.sqlite.prepare('DELETE FROM memory_search_fts WHERE memory_id = ?').run(memoryId);
    this.client.sqlite.prepare(
      'INSERT INTO memory_search_fts (memory_id, search_text) VALUES (?, ?)',
    ).run(memoryId, searchText);
    if (!this.modelRouter.isConfigured('importance')) return 'text_only';
    try {
      await this.saveEmbedding(memoryId, searchText);
      return 'indexed';
    } catch (error) {
      console.error(`[memory-engine] embedding generation failed for memory ${memoryId}:`, error);
      return 'embedding_pending';
    }
  }
}
