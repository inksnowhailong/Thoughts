import { existsSync, mkdirSync, statSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import Database from 'better-sqlite3';
import { drizzle, type BetterSQLite3Database } from 'drizzle-orm/better-sqlite3';
import * as schema from './schema.js';
import { AGENT_RUNTIME_ROOT, REPO_ROOT } from '../tools/paths.js';

export type MemoryDatabase = BetterSQLite3Database<typeof schema>;
export const DEFAULT_MEMORY_DATABASE_PATH = path.join(
  REPO_ROOT,
  'data',
  'agent-runtime',
  'memory-engine.sqlite',
);

export class MemoryDatabaseClient {
  readonly sqlite: Database.Database;
  readonly db: MemoryDatabase;

  constructor(databasePath: string) {
    const resolvedPath = databasePath === ':memory:' ? databasePath : path.resolve(databasePath);
    if (resolvedPath !== ':memory:') mkdirSync(path.dirname(resolvedPath), { recursive: true });
    if (resolvedPath !== ':memory:' && existsSync(resolvedPath) && statSync(resolvedPath).size > 0) {
      const existing = new Database(resolvedPath, { readonly: true, fileMustExist: true });
      try {
        const hasMemorySchema = existing.prepare(
          "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
        ).get();
        if (!hasMemorySchema) {
          throw new Error('refusing to modify an existing SQLite file without the memory-engine migration ledger');
        }
      } finally {
        existing.close();
      }
    }
    this.sqlite = new Database(resolvedPath);
    this.sqlite.pragma('foreign_keys = ON');
    this.sqlite.pragma('journal_mode = WAL');
    this.sqlite.pragma('busy_timeout = 5000');
    this.db = drizzle(this.sqlite, { schema });
  }

  async migrate(): Promise<void> {
    this.sqlite.exec(`
      CREATE TABLE IF NOT EXISTS schema_migrations (
        name TEXT PRIMARY KEY,
        applied_at INTEGER NOT NULL
      )
    `);
    const migrations = [
      '0001_sqlite.sql',
      '0002_consolidation_proposals.sql',
      '0003_memory_embeddings.sql',
    ];
    for (const name of migrations) {
      const applied = this.sqlite.prepare('SELECT 1 FROM schema_migrations WHERE name = ?').get(name);
      if (applied) continue;
      const file = path.join(AGENT_RUNTIME_ROOT, 'src', 'memory-engine', 'migrations', name);
      const migration = await readFile(file, 'utf8');
      this.sqlite.transaction(() => {
        this.sqlite.exec(migration);
        this.sqlite.prepare('INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)')
          .run(name, Date.now());
      })();
    }
  }

  async close(): Promise<void> {
    this.sqlite.close();
  }
}
