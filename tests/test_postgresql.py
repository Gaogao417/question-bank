from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path

import psycopg
import pytest
from question_bank import db, backup
from question_bank.import_sqlite import import_sqlite
from question_bank.service import QuestionBankService
from question_bank.errors import ReceiptError
from tests.test_qb_manage_service import make_package


def test_requires_postgresql(monkeypatch,tmp_path):
    monkeypatch.delenv('QB_DATABASE_URL',raising=False)
    with pytest.raises(RuntimeError,match='PostgreSQL'):
        db.connect(tmp_path)
    assert not (tmp_path/'bank.sqlite3').exists()


def test_concurrent_edits_keep_human_revision(tmp_path):
    root=tmp_path/'bank'
    first=QuestionBankService(root)
    first.submit_candidate(make_package(tmp_path))
    second=QuestionBankService(root)
    barrier=threading.Barrier(2)
    def save(svc,n):
        barrier.wait(timeout=10)
        try:
            svc.save_edit('2026-DEMO-Q021',{'base_revision':1,'operation_id':f'parallel-{n}','stem':f'人工修订 {n}'})
            return 'saved'
        except ReceiptError as exc:
            assert 'BASE_REVISION_CONFLICT' in str(exc)
            return 'conflict'
    try:
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(save,first,1),pool.submit(save,second,2)]
            assert sorted(f.result(timeout=20) for f in futures)==['conflict','saved']
        assert first.conn.execute('SELECT COUNT(*) AS n FROM revisions').fetchone()['n']==2
        assert first.item_detail('2026-DEMO-Q021')['revision']==2
    finally:
        first.close();second.close()


def test_failed_migration_is_atomic(tmp_path,monkeypatch):
    original=Path.read_text
    def broken(path,*args,**kwargs):
        text=original(path,*args,**kwargs)
        return text+'\nTHIS IS INVALID SQL;' if path.name=='001_initial.sql' else text
    monkeypatch.setattr(Path,'read_text',broken)
    schema='qb_atomic_test'
    with pytest.raises(psycopg.Error):db.connect(tmp_path,database_schema=schema)
    with psycopg.connect(db.database_url()) as conn:
        assert conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,)).fetchone() is None


def test_readonly_sqlite_import_preserves_facts_and_sequences(tmp_path):
    svc=QuestionBankService(tmp_path/'original')
    try:
        svc.submit_candidate(make_package(tmp_path))
        svc.record_review('2026-DEMO-Q021',{'decision':'approved','base_revision':1,'operation_id':'review-original'})
        svc.publish('2026-DEMO-Q021',{'revision':1,'expected_previous':None,'operation_id':'publish-original'})
        backup.backup(svc.root,tmp_path/'snapshot')
    finally:svc.close()
    snapshot=json.loads((tmp_path/'snapshot/database.json').read_text())
    legacy=tmp_path/'legacy';legacy.mkdir()
    for name in ('assets','inputs','exports'):
        if (tmp_path/'snapshot'/name).exists():shutil.copytree(tmp_path/'snapshot'/name,legacy/name)
    # Build a standalone SQLite fixture with the source schema's column names.
    with sqlite3.connect(legacy/'bank.sqlite3') as conn:
        for table,rows in snapshot['tables'].items():
            if rows:
                columns=list(rows[0])
                conn.execute(f'CREATE TABLE "{table}" ('+', '.join(f'"{c}"' for c in columns)+')')
                conn.executemany(f'INSERT INTO "{table}" VALUES ('+','.join('?' for _ in columns)+')',[list(r.values()) for r in rows])
            else:conn.execute(f'CREATE TABLE "{table}" (unused TEXT)')
    before=hashlib.sha256((legacy/'bank.sqlite3').read_bytes()).hexdigest()
    result=import_sqlite(legacy,tmp_path/'imported')
    assert result['source_modified'] is False
    assert hashlib.sha256((legacy/'bank.sqlite3').read_bytes()).hexdigest()==before
    imported=QuestionBankService(tmp_path/'imported')
    try:
        assert imported.item_detail('2026-DEMO-Q021')['revision']==1
        assert imported.is_approved('2026-DEMO-Q021',1)
        assert imported.conn.execute('SELECT COUNT(*) AS n FROM publication_heads').fetchone()['n']==1
        receipt=imported.record_review('2026-DEMO-Q021',{'decision':'approved','base_revision':1,'operation_id':'review-after-import'})
        assert receipt['review_id']>snapshot['tables']['reviews'][0]['review_id']
        assert backup.verify(tmp_path/'imported')['items']==1
    finally:imported.close()
    with pytest.raises(ValueError,match='empty'):
        import_sqlite(legacy,tmp_path/'imported')
