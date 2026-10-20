"""Explicit, read-only legacy import into an empty PostgreSQL bank."""
import json
import shutil
import sqlite3
import hashlib
from pathlib import Path
from question_bank import backup


def import_sqlite(source: str | Path, root: str | Path) -> dict:
    source,root=Path(source).resolve(),Path(root).resolve()
    # Never open the legacy DB writable and never invent missing migration data.
    conn=sqlite3.connect((source/'bank.sqlite3').as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('BEGIN')
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing=set(backup.TABLES)-tables
        if missing: raise ValueError(f'Legacy database needs its original migrations first; missing tables: {sorted(missing)}')
        if conn.execute('PRAGMA foreign_key_check').fetchall(): raise ValueError('Legacy database has foreign key violations')
        data={t:[dict(r) for r in conn.execute(f'SELECT * FROM "{t}"')] for t in backup.TABLES}
        # Package a frozen input snapshot; restore uses the same verified path.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='qb-import-') as temp:
            temp=Path(temp)
            for name in ('assets','inputs','exports'):
                if (source/name).is_dir(): shutil.copytree(source/name,temp/name)
            files={str(p.relative_to(temp)):hashlib.sha256(p.read_bytes()).hexdigest() for p in temp.rglob('*') if p.is_file()}
            (temp/'database.json').write_text(json.dumps({'format':'question-bank-postgresql/v1','source_root':str(source),'tables':data,'files':files},ensure_ascii=False))
            result=backup.restore(temp,root)
            return {**result,'source':str(source),'source_modified':False}
    finally: conn.close()
