"""Review UI import regressions; models and object storage are always fakes."""
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from question_bank import revision_ai
from question_bank.assets import AssetStore
from question_bank.auto_repair import PaperRepair, attached_images
from question_bank.errors import QbError
from question_bank.service import QuestionBankService


def test_repair_model_contract(tmp_path, monkeypatch):
    detail = {'item_id': 'test', 'revision': 1,
              'body': {'stem': '计算', 'answer': '', 'solution': ''}, 'issues': []}
    patch = {**detail['body'], 'reason': '证据不足，保留原文', 'issue_decisions': []}
    attachment = tmp_path / 'source.png'
    attachment.write_bytes(b'fixture')
    monkeypatch.setattr(revision_ai.shutil, 'which', lambda _: 'codex')
    def run(command, **kwargs):
        assert command[command.index('--image') + 1] == str(attachment)
        assert 'issue_decisions' in json.loads((kwargs['cwd'] / 'schema.json').read_text())['required']
        assert 'attached_images' in kwargs['input']
        (kwargs['cwd'] / 'response.json').write_text(json.dumps(patch))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(revision_ai.subprocess, 'run', run)
    assert revision_ai.generate_revision(tmp_path, detail, '修复', auto_repair=True,
        attached_images=[{'asset_id': 'test', 'path': str(attachment)}]) == patch


def test_oss_attachment_materialized_and_hash_bound(tmp_path, monkeypatch):
    store = AssetStore(tmp_path, oss=False)
    buffer = io.BytesIO()
    Image.new('RGB', (4, 4)).save(buffer, format='PNG')
    raw = buffer.getvalue()
    row = store.register_bytes(raw)
    store.oss = True
    from question_bank import oss_store
    monkeypatch.setattr(oss_store, 'get_object', lambda key: raw)
    service = SimpleNamespace(assets=store, conn=SimpleNamespace(
        execute=lambda *a: SimpleNamespace(fetchone=lambda: row)))
    images = attached_images(service, {'item_id': 'test', 'evidence': [row]}, tmp_path / 'run')
    assert Path(images[0]['path']).read_bytes() == raw
    other = 'sha256:' + '0' * 64
    with pytest.raises(QbError):
        store.verify_registered_asset(other, {**row, 'asset_id': other, 'sha256': other})


def test_worker_cleanup_preserves_other_connections():
    closed = []
    worker = SimpleNamespace(close=lambda: closed.append('worker'))
    main = SimpleNamespace(close=lambda: closed.append('main'))
    svc = QuestionBankService.__new__(QuestionBankService)
    svc._local = threading.local()
    svc._local.conn = worker
    svc._connections_lock = threading.Lock()
    svc._connections = [main, worker]
    svc.close_thread_connection()
    assert closed == ['worker']
    assert svc._connections == [main]


def test_repair_issue_only_change_uses_existing_proposal_api(tmp_path, monkeypatch):
    detail = {'item_id': 'test', 'revision': 1, 'body': {'stem': '计算', 'answer': '2', 'solution': '1+1=2'},
              'issues': [{'issue_id': 'issue1', 'state': 'open', 'type': 'answer', 'reason': '复核'}]}
    decisions = [{'issue_id': 'issue1', 'state': 'dismissed', 'note': '已核算'}]
    monkeypatch.setattr(revision_ai, 'generate_revision', lambda *a, **k:
                        {**detail['body'], 'reason': '复核', 'issue_decisions': decisions})
    captured = {}
    def accept(pid, request, **kwargs):
        captured.update(request=request, kwargs=kwargs)
        return {'save_receipt': {'revision': 2}}
    def item_detail(item_id, revision=None):
        return detail if revision is None else {**detail, 'issues': [], 'check': {'passed': True}}
    svc = SimpleNamespace(root=tmp_path, item_detail=item_detail,
                          submit_proposal=lambda *a, **k: captured.update(proposal=a[1]), accept_proposal=accept)
    manager = PaperRepair(svc)
    item = {'item_id': 'test', 'base_revision': 1, 'question_number': 1}
    manager._repair({'job_id': 'auto-repair-test', 'stop_requested': False}, item)
    assert item['status'] == 'fixed'
    assert captured['request']['issue_ops'] == decisions
    assert captured['kwargs']['issue_resolution']['actor_kind'] == 'ai'
    assert captured['proposal']['stem'] == detail['body']['stem']


def test_issue_resolution_keeps_ai_provenance():
    svc = QuestionBankService.__new__(QuestionBankService)
    issues = [{'issue_id': 'one', 'state': 'open'}]
    svc._apply_issue_ops(issues, [{'issue_id': 'one', 'state': 'resolved', 'note': '核算'}], 'test',
                         issue_resolution={'actor_kind': 'ai', 'actor_id': 'job'})
    assert issues[0]['resolution']['actor'] == 'ai'
    assert issues[0]['resolution']['actor_id'] == 'job'


def test_review_routes_and_static_files(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from question_bank import server
    monkeypatch.setattr(server, 'QuestionBankService', lambda root: SimpleNamespace(root=root, close=lambda: None))
    async def check():
        app = server.create_app(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert (await client.get('/api/papers/test/auto-repair')).json() == {'job': None}
            assert (await client.post('/api/papers/test/auto-repair', json={})).status_code == 400
            assert (await client.post('/api/papers/test/auto-repair/stop')).status_code == 404
    asyncio.run(check())
    html = (server.STATIC_DIR / 'index.html').read_text()
    for name in ('paper-repair.js', 'paper-repair.css', 'review-filters.js', 'source-capture.js'):
        assert name in html
        assert (server.STATIC_DIR / name).is_file()


def test_postgresql_repair_and_review_metadata(tmp_path, monkeypatch):
    import os
    if not os.environ.get('QB_DATABASE_URL'):
        pytest.skip('Dedicated PostgreSQL test database required')
    from tests.test_qb_manage_service import make_package
    svc = QuestionBankService(tmp_path / 'bank', oss=False)
    try:
        svc.submit_candidate(make_package(tmp_path, issues=[{
            'issue_id': 'check-answer', 'type': 'answer', 'reason': '复核答案',
            'target': 'answer', 'blocking': False}]))
        item_id = '2026-DEMO-Q021'
        detail = svc.item_detail(item_id)
        assert svc.list_items()[0]['review_task'] == 'ai'
        monkeypatch.setattr(revision_ai, 'generate_revision', lambda *a, **k: {
            **detail['body'], 'reason': '答案核算正确', 'issue_decisions': [
                {'issue_id': 'check-answer', 'state': 'dismissed', 'note': '已核算'}]})
        item = {'item_id': item_id, 'base_revision': 1, 'question_number': 21}
        PaperRepair(svc)._repair({'job_id': 'auto-repair-test', 'stop_requested': False}, item)
        assert item['status'] == 'fixed'
        updated = svc.item_detail(item_id)
        assert updated['revision'] == 2
        assert updated['issues'][0]['resolution']['actor'] == 'ai'
        assert updated['review_workflow'] == 'ai_revised'
        assert svc.list_items()[0]['review_task'] == 'review_ai'
        assert updated['reviews'] == []
    finally:
        svc.close()
