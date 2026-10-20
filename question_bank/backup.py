"""Consistent PostgreSQL logical snapshots plus media, with restore verification."""
from __future__ import annotations
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from psycopg import sql
from question_bank import db as db_mod
from question_bank.errors import QbError, error

# Dependency order; all business tables, never schema_migrations from another engine.
TABLES = ('papers','paper_items','items','revisions','assets','revision_assets',
          'issues','checks','reviews','submissions','operations','publications',
          'publication_heads','exports','proposals','teaching_notes',
          'teaching_note_versions','teaching_note_reviews','ai_operations',
          'blueprint_batches','blueprint_candidates')
IDENTITIES = {'checks':'check_id','reviews':'review_id','publications':'publication_id',
              'exports':'export_id','teaching_note_reviews':'review_id','blueprint_batches':'batch_id'}


def backup(root: str | Path, dest: str | Path) -> dict[str, Any]:
    root, dest = Path(root).resolve(), Path(dest).resolve()
    if dest == root or dest.is_relative_to(root):
        raise ValueError('Backup destination must be outside the data root')
    dest.mkdir(parents=True, exist_ok=False)
    conn = db_mod.connect(root)
    try:
        conn.begin_write()
        data = {table: conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall() for table in TABLES}
        # Files are immutable and all application writers share the transaction lock.
        for name in ('assets','inputs','exports'):
            if (root/name).exists(): shutil.copytree(root/name,dest/name)
        conn.execute('COMMIT')
        files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file()}
        snapshot = {'format':'question-bank-postgresql/v1','source_root':str(root), 'tables':data, 'files':files}
        (dest/'database.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
        verify(dest)
        return {'dest':str(dest),'counts':{k:len(v) for k,v in data.items()},'files':len(files)}
    finally:
        conn.rollback()
        conn.close()


def _safe_file(root: Path, name: str) -> Path:
    path=(root/name).resolve()
    if not path.is_relative_to(root): raise ValueError(f'Path escapes backup: {name}')
    return path


def verify(root: str | Path) -> dict[str, Any]:
    root=Path(root).resolve()
    if (root/'database.json').is_file():
        snapshot=json.loads((root/'database.json').read_text())
        if snapshot.get('format') != 'question-bank-postgresql/v1': raise ValueError('Unsupported snapshot')
        for name, digest in snapshot['files'].items():
            path=_safe_file(root,name)
            if not path.is_file():
                raise QbError([error('RESTORE_MEDIA_MISSING',f'Missing file: {name}',retryable=False)])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise QbError([error('RESTORE_MEDIA_CORRUPT',f'Changed file: {name}',retryable=False)])
        data=snapshot['tables']
    else:
        conn=db_mod.connect(root)
        try: data={table:conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall() for table in TABLES}
        finally: conn.close()
    for asset in data['assets']:
        path=_safe_file(root,asset['path'])
        if not path.is_file():
            raise QbError([error('RESTORE_MEDIA_MISSING',f"Missing media: {asset['path']}",retryable=False)])
        if 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:
            raise QbError([error('RESTORE_MEDIA_CORRUPT',f"Changed media: {asset['path']}",retryable=False)])
    revisions={(r['item_id'],r['revision']) for r in data['revisions']}
    for item in data['items']:
        if item['current_revision'] is not None and (item['item_id'],item['current_revision']) not in revisions:
            raise ValueError(f"Invalid revision pointer: {item['item_id']}")
    return {t:len(data[t]) for t in ('items','revisions','reviews','publications','publication_heads')}


def load_rows(conn, data: dict, source_root: str, target_root: Path) -> dict:
    for table in TABLES:
        if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
            raise ValueError('Restore/import requires an empty target database schema')
    for table in TABLES:
        if table not in data: raise ValueError(f'Snapshot missing table: {table}')
        for source_row in data[table]:
            row=dict(source_row)
            for field in ('path','local_path'):
                value=row.get(field)
                if isinstance(value,str) and value.startswith(source_root+'/'):
                    row[field]=str(target_root / value[len(source_root)+1:])
            statement=sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,row)),sql.SQL(',').join(sql.Placeholder() for _ in row))
            conn.execute(statement,list(row.values()))
        if table in IDENTITIES:
            key=IDENTITIES[table]
            conn.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,%s), COALESCE(MAX({}),1), MAX({}) IS NOT NULL) FROM {}").format(sql.Identifier(key),sql.Identifier(key),sql.Identifier(table)),(table,key))
    return {k:len(v) for k,v in data.items()}


def restore(source: str | Path, root: str | Path) -> dict:
    source,root=Path(source).resolve(),Path(root).resolve()
    verify(source)
    snapshot=json.loads((source/'database.json').read_text())
    if root.exists() and any(root.iterdir()): raise ValueError('Restore needs a new or empty media directory')
    conn=db_mod.connect(root)
    try:
        conn.begin_write()
        # Check emptiness before copying; load_rows checks again within this lock.
        for table in TABLES:
            if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
                raise ValueError('Restore requires an empty database schema')
        for name in ('assets','inputs','exports'):
            if (source/name).is_dir(): shutil.copytree(source/name,root/name,dirs_exist_ok=True)
        counts=load_rows(conn,snapshot['tables'],snapshot['source_root'],root)
        conn.execute('COMMIT')
        return {'root':str(root),'counts':counts}
    except BaseException:
        conn.rollback()
        raise
    finally: conn.close()
