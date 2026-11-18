import Database from 'better-sqlite3';
import path from 'node:path';
import { REPO_ROOT } from '../tools/paths.js';

export interface GuanjiaMemoryRecord {
  id: string;
  key: string;
  content: string;
  memoryType: string;
  source: string;
  confidence: number;
  importance: number;
  retention: string;
  people: string[];
  evidence: unknown[];
  media: unknown[];
  createdAt: string;
  updatedAt: string;
  lastUsedAt: string | null;
  useCount: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== 'string') throw new Error(`invalid Guanjia memory field: ${key}`);
  return value;
}

function nullableString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') throw new Error(`invalid Guanjia memory field: ${key}`);
  return value;
}

function requiredNumber(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`invalid Guanjia memory field: ${key}`);
  }
  return value;
}

function requiredArray(record: Record<string, unknown>, key: string): unknown[] {
  const value = record[key];
  if (!Array.isArray(value)) throw new Error(`invalid Guanjia memory field: ${key}`);
  return value;
}

function decodeMemory(key: string, payload: string): GuanjiaMemoryRecord {
  const value: unknown = JSON.parse(payload);
  if (!isRecord(value)) throw new Error('invalid Guanjia memory payload');
  const people = requiredArray(value, 'people').map((person) => {
    if (typeof person !== 'string') throw new Error('invalid Guanjia memory field: people');
    return person;
  });
  return {
    id: requiredString(value, 'id'),
    key,
    content: requiredString(value, 'content'),
    memoryType: requiredString(value, 'memory_type'),
    source: requiredString(value, 'source'),
    confidence: requiredNumber(value, 'confidence'),
    importance: requiredNumber(value, 'importance'),
    retention: requiredString(value, 'retention'),
    people,
    evidence: requiredArray(value, 'evidence'),
    media: requiredArray(value, 'media'),
    createdAt: requiredString(value, 'created_at'),
    updatedAt: requiredString(value, 'updated_at'),
    lastUsedAt: nullableString(value, 'last_used_at'),
    useCount: requiredNumber(value, 'use_count'),
  };
}

export class GuanjiaMemoryExplorer {
  constructor(
    private readonly databasePath = path.join(REPO_ROOT, 'memory', 'memory.sqlite3'),
  ) {}

  browse(options: {
    limit: number;
    offset: number;
    query?: string;
    memoryType?: string;
  }): { items: Array<GuanjiaMemoryRecord & { briefContent: string }>; total: number; limit: number; offset: number } {
    const query = options.query?.trim().toLocaleLowerCase();
    const memoryType = options.memoryType?.trim();
    const records = this.readAll()
      .filter((record) => !memoryType || record.memoryType === memoryType)
      .filter((record) => !query || [
        record.key,
        record.content,
        record.memoryType,
        ...record.people,
      ].some((value) => value.toLocaleLowerCase().includes(query)))
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    const items = records.slice(options.offset, options.offset + options.limit).map((record) => ({
      ...record,
      briefContent: Array.from(record.content).slice(0, 180).join(''),
    }));
    return { items, total: records.length, limit: options.limit, offset: options.offset };
  }

  get(id: string): GuanjiaMemoryRecord | undefined {
    return this.readAll().find((record) => record.id === id);
  }

  private readAll(): GuanjiaMemoryRecord[] {
    const db = new Database(this.databasePath, { readonly: true, fileMustExist: true });
    try {
      const rows = db.prepare('SELECT key, payload FROM guanjia_memories')
        .all() as Array<{ key: string; payload: string }>;
      return rows.map((row) => decodeMemory(row.key, row.payload));
    } finally {
      db.close();
    }
  }
}
