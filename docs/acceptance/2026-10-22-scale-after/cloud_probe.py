"""Run with deployment env loaded. Creates/drops its own DB and OSS prefix only."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from question_bank import backup, oss_store, db
from question_bank.service import QuestionBankService
from tests.test_qb_manage_service import make_package

out = Path(__file__).resolve().parent
metrics = {}
def measured(name, fn):
    def call(*args, **kwargs):
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            entry = metrics.setdefault(name, {'calls': 0, 'ms': 0})
            entry['calls'] += 1
            entry['ms'] += (time.perf_counter() - start) * 1000
    return call
original_execute = db.Connection.execute
def execute(conn, query, *args, **kwargs):
    kind = 'db_lock' if 'pg_advisory_xact_lock' in str(query) else ('db_commit' if str(query) == 'COMMIT' else 'db_sql')
    return measured(kind, original_execute)(conn, query, *args, **kwargs)
db.Connection.execute = execute
base = os.environ['QB_DATABASE_URL']
run_id = uuid.uuid4().hex
name = 'qb_acceptance_' + run_id[:16]
prefix = 'acceptance/' + run_id + '/'
report = {'test_database': name, 'object_prefix': prefix, 'planned_sample_count': 30,
          'concurrency': 1, 'measurement': 'synchronous service call including OSS and DB; excludes package generation',
          'arrival_model': 'closed loop, sequential; no fixed offered rate',
          'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
keys = set()
services = []
created_db = False
bucket = None
try:
    assert oss_store.enabled(), 'OSS must be configured'
    bucket = oss_store._bucket()
    # Application path remains logical; all real I/O is scoped to our random prefix.
    def put(key, raw, content_type):
        scoped = prefix + key
        keys.add(scoped)
        bucket.put_object(scoped, raw, headers={'Content-Type': content_type})
    oss_store.put_object = put
    original_get = oss_store.get_object
    oss_store.get_object = lambda key: original_get(prefix + key)
    oss_store.object_exists = lambda key: bucket.object_exists(prefix + key)
    for operation in ('put_object', 'get_object', 'object_exists'):
        setattr(oss_store, operation, measured('oss_' + operation, getattr(oss_store, operation)))
    request = urllib.request.Request('http://127.0.0.1:8890/healthz')
    password = os.environ.get('QB_UI_PASSWORD', '')
    if password:
        token = base64.b64encode((os.environ.get('QB_UI_USER', 'admin') + ':' + password).encode()).decode()
        request.add_header('Authorization', 'Basic ' + token)
    with urllib.request.urlopen(request, timeout=10) as response:
        report['service_health'] = {'http_status': response.status, 'ok': json.load(response)['ok']}
    with psycopg.connect(base, autocommit=True) as admin:
        report['server_version'] = admin.execute('SELECT version()').fetchone()[0]
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created_db = True
    u = urlsplit(base)
    os.environ['QB_DATABASE_URL'] = urlunsplit((u.scheme, u.netloc, '/' + name, u.query, u.fragment))
    os.environ['QB_DATABASE_SCHEMA'] = 'cloud_probe'
    with tempfile.TemporaryDirectory(prefix='qb-cloud-acceptance-') as tmp:
        tmp = Path(tmp)
        svc = QuestionBankService(tmp / 'root')
        services.append(svc)
        samples = []
        for i in range(30):
            package = make_package(tmp / 'packages', submission_id=f'cloud-{i}', question_number=i+1)
            asset_bytes = sum(p.stat().st_size for p in package.rglob('*.png'))
            before_metrics = {k: dict(v) for k, v in metrics.items()}
            start = time.perf_counter()
            receipt = svc.submit_candidate(package)
            elapsed = time.perf_counter() - start
            if receipt['status'] != 'accepted':
                report['attempted_requests'] = i + 1
                report['successful_requests'] = i
                report['failed_request_elapsed_ms'] = elapsed * 1000
                assets = svc.conn.execute('SELECT * FROM assets').fetchall()
                for asset in assets:
                    raw = oss_store.get_object(asset['path'])
                    assert 'sha256:' + hashlib.sha256(raw).hexdigest() == asset['sha256']
                report['real_oss_hashes_verified'] = len(assets)
                report['unexpected_receipt'] = receipt
                raise AssertionError('unexpected receipt; see cloud.json')
            samples.append({'request_id': f'cloud-{i}', 'elapsed_ms': elapsed*1000,
                            'asset_bytes': asset_bytes, 'asset_count': 4, 'status': receipt['status'], 'stages': {k: {field: value - before_metrics.get(k, {}).get(field, 0) for field, value in values.items()} for k, values in metrics.items()}})
        report['samples'] = samples
        values = sorted(s['elapsed_ms'] for s in samples)
        report['latency_ms'] = {'p50': values[math.ceil(len(values)*.5)-1],
                                'p95': values[math.ceil(len(values)*.95)-1],
                                'p99': values[math.ceil(len(values)*.99)-1]}
        report['total_measured_seconds'] = sum(s['elapsed_ms'] for s in samples)/1000
        report['service_time_throughput_per_second'] = len(samples)/report['total_measured_seconds']
        report['cache'] = 'first request creates four objects; later requests reuse keys with fresh byte verification; no byte-cache shortcut'
        # Discard first response and replay by the same durable key.
        replay = svc.submit_candidate(tmp / 'packages' / 'cloud-0')
        assert replay['replayed'] and replay['revision'] == 1
        assert svc.conn.execute('SELECT count(*) AS n FROM revisions').fetchone()['n'] == 30
        report['lost_response_replay'] = 'passed'
        # Fail after all DB writes but before transaction commit.
        original = svc._create_item_from_candidate
        def fail_before_commit(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('injected pre-commit failure')
        svc._create_item_from_candidate = fail_before_commit
        package = make_package(tmp / 'packages', submission_id='rollback', question_number=99)
        try:
            svc.submit_candidate(package)
            raise AssertionError('failure injection did not fire')
        except RuntimeError as exc:
            assert str(exc) == 'injected pre-commit failure'
        finally:
            svc._create_item_from_candidate = original
        assert svc.conn.execute("SELECT count(*) AS n FROM items WHERE question_number=99").fetchone()['n'] == 0
        assert svc.conn.execute("SELECT count(*) AS n FROM submissions WHERE submission_id='rollback'").fetchone()['n'] == 0
        assert svc.submit_candidate(package)['status'] == 'accepted'
        report['rollback_and_retry'] = 'passed'
        assets = svc.conn.execute('SELECT * FROM assets').fetchall()
        for asset in assets:
            raw = oss_store.get_object(asset['path'])
            assert 'sha256:' + hashlib.sha256(raw).hexdigest() == asset['sha256']
            assert len(raw) == asset['size_bytes']
        report['real_oss_hashes_verified'] = len(assets)
        snapshot = tmp / 'snapshot'
        backup.backup(svc.root, snapshot)
        before = backup.verify(snapshot, full=True)
        os.environ['QB_DATABASE_SCHEMA'] = 'cloud_restore'
        backup.restore(snapshot, tmp / 'restored')
        restored = QuestionBankService(tmp / 'restored')
        services.append(restored)
        after = backup.verify(restored.root, full=True)
        assert before == after
        for table in backup.TABLES:
            left = svc.conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall()
            right = restored.conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall()
            assert sorted(map(lambda row: json.dumps(row, sort_keys=True), left)) == sorted(map(lambda row: json.dumps(row, sort_keys=True), right)), table
        report['backup_restore_all_tables_and_assets'] = 'passed'
        report['business_table_count'] = len(backup.TABLES)
        report['result'] = 'passed'
except Exception as exc:
    report['result'] = 'failed'
    report['error_type'] = type(exc).__name__
    raise
finally:
    for svc in services:
        svc.close()
    if bucket is not None:
        for key in keys:
            assert key.startswith(prefix)
            bucket.delete_object(key)
        report['test_objects_removed'] = all(not bucket.object_exists(key) for key in keys)
        report['test_object_count'] = len(keys)
    if created_db:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            report['test_database_removed'] = admin.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)', (name,)).fetchone()[0]
    (out / 'cloud.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({k: v for k, v in report.items() if k != 'samples'}, ensure_ascii=False, indent=2))
