import json

import pytest
from fastapi.testclient import TestClient

from question_bank import revision_ai, explanations_ai
from question_bank.server import create_app
from question_bank.service import QuestionBankService


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'bank'
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'candidate.json').write_text(json.dumps({
        'schema': 'question_bank_candidate/v1', 'submission_id': 'revision-ui-test',
        'base_revision': None, 'paper': {'paper_id': 'REVISION-TEST', 'title': '修订测试'},
        'item': {'question_number': 1, 'question_type': 'problem', 'points': 2,
                 'stem': '计算 $1+1$。', 'answer': '$2$', 'solution': '解：$1+1=2$。'},
    }), encoding='utf-8')
    svc = QuestionBankService(root)
    svc.submit_candidate(package)
    svc.close()
    with TestClient(create_app(root)) as client:
        yield client


def test_generated_proposal_does_not_change_content(setup, monkeypatch):
    monkeypatch.setattr(revision_ai, 'generate_revision', lambda *a: {
        'stem': '计算 $1+1$。', 'answer': '$2$', 'solution': '解：一个加一个是两个。', 'reason': '补充依据'})
    url = '/api/items/REVISION-TEST-Q001'
    result = setup.post(url + '/revision-assist', json={'instruction': '补充依据', 'base_revision': 1})
    assert result.status_code == 200
    assert result.json()['status'] == 'open'
    assert setup.get(url).json()['current_revision'] == 1
    proposal = result.json()['proposal_id']
    assert setup.post(f'/api/proposals/{proposal}/accept', json={'operation_id': 'accept-test'}).status_code == 200
    assert setup.get(url).json()['body']['solution'] == '解：一个加一个是两个。'


def test_stale_and_invalid_requests_do_not_call_model(setup, monkeypatch):
    def forbidden(*args):
        pytest.fail('must not call model')
    monkeypatch.setattr(revision_ai, 'generate_revision', forbidden)
    url = '/api/items/REVISION-TEST-Q001/revision-assist'
    assert setup.post(url, json={'instruction': '修订', 'base_revision': 2}).status_code == 409
    assert setup.post(url, json={'instruction': '', 'base_revision': 1}).status_code == 400


def test_model_failure_is_visible_without_revision(setup, monkeypatch):
    def fail(*args):
        raise RuntimeError('模型不可用')
    monkeypatch.setattr(revision_ai, 'generate_revision', fail)
    url = '/api/items/REVISION-TEST-Q001'
    response = setup.post(url + '/revision-assist', json={'instruction': '修订', 'base_revision': 1})
    assert response.status_code == 502
    assert '模型不可用' in response.text
    assert setup.get(url).json()['current_revision'] == 1


def test_transcription_and_failure(setup, monkeypatch):
    monkeypatch.setattr(explanations_ai, 'transcribe_audio', lambda raw, mime: '请补充解答依据')
    response = setup.post('/api/revision-assist/transcribe', content=b'audio', headers={'content-type':'audio/webm'})
    assert response.json()['transcript'] == '请补充解答依据'
    def fail(*a):
        raise explanations_ai.AiAssistError('asr_failed', '转写失败')
    monkeypatch.setattr(explanations_ai, 'transcribe_audio', fail)
    assert setup.post('/api/revision-assist/transcribe', content=b'audio').status_code == 400


def test_codex_output_validation_and_failure_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(revision_ai.shutil, 'which', lambda _: 'codex')
    def run(command, **kwargs):
        assert command[command.index('--model') + 1] == 'gpt-6.1-sol'
        assert command[-1] == '-'
        assert '修订' in kwargs['input']
        (kwargs['cwd'] / 'response.json').write_text('{"stem": "incomplete"}', encoding='utf-8')
        class Result: returncode = 0
        return Result()
    monkeypatch.setattr(revision_ai.subprocess, 'run', run)
    with pytest.raises(ValueError):
        revision_ai.generate_revision(tmp_path, {'item_id':'test','revision':1,'body':{}}, '修订')
    assert len(list(tmp_path.glob('runs/*/failure.txt'))) == 1
