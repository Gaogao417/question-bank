"""Consistent PostgreSQL logical snapshots plus media, with restore verification."""
from __future__ import annotations
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from psycopg import sql
from question_bank import db as db_mod
from question_bank import oss_store
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
    oss = oss_store.enabled()
    conn = db_mod.connect(root)
    try:
        conn.begin_write()
        data = {table: conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall() for table in TABLES}
        # Files are immutable and all application writers share the transaction lock.
        # OSS 模式下媒体已在对象存储（不可变、内容寻址），快照不重复打包字节。
        copy_names = ('inputs','exports') if oss else ('assets','inputs','exports')
        for name in copy_names:
            if (root/name).exists(): shutil.copytree(root/name,dest/name)
        conn.execute('COMMIT')
        files = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file()}
        snapshot = {'format':'question-bank-postgresql/v1','source_root':str(root), 'tables':data, 'files':files}
        if oss: snapshot['oss'] = oss_store.describe()
        (dest/'database.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
        verify(dest)
        return {'dest':str(dest),'counts':{k:len(v) for k,v in data.items()},'files':len(files),
                **({'oss_assets':len(data['assets'])} if oss else {})}
    finally:
        conn.rollback()
        conn.close()


def _safe_file(root: Path, name: str) -> Path:
    path=(root/name).resolve()
    if not path.is_relative_to(root): raise ValueError(f'Path escapes backup: {name}')
    return path


def verify(root: str | Path, *, full: bool = False) -> dict[str, Any]:
    root=Path(root).resolve()
    oss_snapshot=None
    if (root/'database.json').is_file():
        snapshot=json.loads((root/'database.json').read_text())
        if snapshot.get('format') != 'question-bank-postgresql/v1': raise ValueError('Unsupported snapshot')
        oss_snapshot=snapshot.get('oss')
        for name, digest in snapshot['files'].items():
            path=_safe_file(root,name)
            if not path.is_file():
                raise QbError([error('RESTORE_MEDIA_MISSING',f'Missing file: {name}',retryable=False)])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise QbError([error('RESTORE_MEDIA_CORRUPT',f'Changed file: {name}',retryable=False)])
        data=snapshot['tables']
    else:
        oss_snapshot=oss_store.describe() if oss_store.enabled() else None
        conn=db_mod.connect(root)
        try: data={table:conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall() for table in TABLES}
        finally: conn.close()
    for asset in data['assets']:
        if oss_snapshot is not None:
            # OSS 模式：key 即 path；full 时下载复核 sha256（restore 前强制）。
            if not oss_store.object_exists(asset['path']):
                raise QbError([error('RESTORE_MEDIA_MISSING',f"Missing OSS object: {asset['path']}",retryable=False)])
            if full and 'sha256:'+hashlib.sha256(oss_store.get_object(asset['path'])).hexdigest()!=asset['sha256']:
                raise QbError([error('RESTORE_MEDIA_CORRUPT',f"Changed OSS object: {asset['path']}",retryable=False)])
            continue
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
    # full=True：OSS 对象逐个下载复核 sha256（AGENTS：备份连同引用文件验证恢复）。
    verify(source, full=True)
    snapshot=json.loads((source/'database.json').read_text())
    if root.exists() and any(root.iterdir()): raise ValueError('Restore needs a new or empty media directory')
    oss=oss_store.enabled()
    legacy_assets = oss and snapshot.get('oss') is None and (source/'assets').is_dir()
    conn=db_mod.connect(root)
    try:
        conn.begin_write()
        # Check emptiness before copying; load_rows checks again within this lock.
        for table in TABLES:
            if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
                raise ValueError('Restore requires an empty database schema')
        # OSS 模式：新快照的媒体本就在对象存储（不可变、key 恒定，无需回拷）；
        # 旧本地快照的 assets/ 已由上面的 verify 全量复核，这里按 key=path 上传。
        if legacy_assets:
            mime_by_path={a['path']:a['mime'] for a in snapshot['tables']['assets']}
            for p in sorted((source/'assets').rglob('*')):
                if p.is_file():
                    key=str(p.relative_to(source)).replace('\\','/')
                    oss_store.put_object(key,p.read_bytes(),mime_by_path.get(key,'application/octet-stream'))
        if not oss:
            for name in ('assets','inputs','exports'):
                if (source/name).is_dir(): shutil.copytree(source/name,root/name,dirs_exist_ok=True)
        else:
            for name in ('inputs','exports'):
                if (source/name).is_dir(): shutil.copytree(source/name,root/name,dirs_exist_ok=True)
        counts=load_rows(conn,snapshot['tables'],snapshot['source_root'],root)
        conn.execute('COMMIT')
        return {'root':str(root),'counts':counts,**({'oss':oss_store.describe()['bucket']} if oss else {})}
    except BaseException:
        conn.rollback()
        raise
    finally: conn.close()
