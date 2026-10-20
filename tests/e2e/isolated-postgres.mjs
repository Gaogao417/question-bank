// Never seed into a user's selected business schema, even when its URL is reused.
import { randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
if (!process.env.QB_DATABASE_URL) throw new Error('Set QB_DATABASE_URL to a dedicated PostgreSQL test database');
process.env.QB_DATABASE_SCHEMA = 'qb_e2e_' + randomUUID().replaceAll('-', '');
const repo = path.resolve(fileURLToPath(new URL('../..', import.meta.url)));
const python = path.join(repo, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
process.on('exit', () => {
  const cleanup = spawnSync(python, ['-c', `
import os, psycopg
from psycopg import sql
with psycopg.connect(os.environ['QB_DATABASE_URL'], autocommit=True, connect_timeout=3, options='-c lock_timeout=3000') as conn:
    conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(os.environ['QB_DATABASE_SCHEMA'])))
`], {timeout: 6000, encoding:'utf8'});
  if (cleanup.status !== 0) console.error('Test schema cleanup failed; schema:', process.env.QB_DATABASE_SCHEMA);
});
