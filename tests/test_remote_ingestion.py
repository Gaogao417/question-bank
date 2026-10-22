"""Remote transport contracts; DB writes use conftest's isolated test schema."""
import io
import json
from pathlib import Path
import stat
import zipfile

from fastapi.testclient import TestClient
import pytest

from question_bank.errors import QbError
from question_bank import remote_ingestion as remote
from question_bank.server import create_app
from tests.test_qb_manage_service import make_package


def zipped(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../escape', 'a\\escape', 'C:escape', './candidate.json', 'a//x'])
def test_unsafe_paths(tmp_path, name):
    with pytest.raises(QbError):
        remote.unpack(zipped([(name, '{}')]), tmp_path)


def test_duplicates_and_symlinks(tmp_path):
    link = zipfile.ZipInfo('link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for entries in [[('candidate.json', '{}'), ('candidate.json', '{}')], [(link, 'outside')], [('dir/', ''), ('dir', '')]]:
        with pytest.raises(QbError):
            remote.unpack(zipped(entries), tmp_path)


@pytest.mark.parametrize('payload', [[], None, {'images': None}, {'images': [None]}, {'images': [{'asset': 2}]}])
def test_schema_errors_are_json(tmp_path, payload):
    with TestClient(create_app(tmp_path / 'root')) as client:
        response = client.post('/api/submissions', content=zipped([('candidate.json', json.dumps(payload))]))
        assert response.status_code == 422
        assert response.json()['errors'][0]['code'] == 'SCHEMA_INVALID'


def test_asset_escape_and_missing_are_rejected(tmp_path):
    package = make_package(tmp_path / 'packages')
    path = package / 'candidate.json'
    payload = json.loads(path.read_text())
    for ref in ['../outside.png', '/etc/passwd', 'asset:existing', 'missing.png']:
        payload['images'][0]['asset']['ref'] = ref
        path.write_text(json.dumps(payload))
        target = tmp_path / str(len(list(tmp_path.iterdir())))
        target.mkdir()
        with pytest.raises(QbError) as exc:
            remote.unpack(remote.package_bytes(package), target)
        assert exc.value.errors[0]['code'] in {'UPLOAD_ASSET_MISSING', 'SCHEMA_INVALID'}


def test_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'MAX_UPLOAD', 10)
    with pytest.raises(QbError) as exc:
        remote.unpack(b'x' * 11, tmp_path)
    assert exc.value.errors[0]['code'] == 'UPLOAD_TOO_LARGE'
    monkeypatch.setattr(remote, 'MAX_UPLOAD', 10000)
    monkeypatch.setattr(remote, 'MAX_EXPANDED', 10)
    with pytest.raises(QbError):
        remote.unpack(zipped([('candidate.json', 'x' * 11)]), tmp_path)


def test_http_receipt_replay_conflict_and_archive(tmp_path, monkeypatch):
    monkeypatch.delenv('QB_OSS_BUCKET', raising=False)
    from question_bank import oss_store
    oss_store._config.cache_clear()
    package = make_package(tmp_path / 'packages')
    root = tmp_path / 'root'
    with TestClient(create_app(root)) as client:
        raw = remote.package_bytes(package)
        first = client.post('/api/submissions', content=raw)
        assert first.status_code == 200, first.text
        assert first.json()['receipt']['status'] == 'accepted'
        replay = client.post('/api/submissions', content=raw).json()['receipt']
        assert replay['replayed'] is True
        assert client.get('/api/submissions/sub-001').json()['receipt']['submission_id'] == 'sub-001'
        assert client.get('/api/submissions/missing').status_code == 404
        payload = json.loads((package / 'candidate.json').read_text())
        payload['item']['stem'] += 'changed'
        (package / 'candidate.json').write_text(json.dumps(payload))
        conflict = client.post('/api/submissions', content=remote.package_bytes(package))
        assert conflict.status_code == 409
        assert conflict.json()['errors'][0]['code'] == 'SUBMISSION_ID_REUSED'
    attempts = list((root / 'inputs' / 'remote').iterdir())
    assert len(attempts) == 3
    assert all((p / 'upload.zip').is_file() for p in attempts)
    assert sum((p / 'receipt.json').is_file() for p in attempts) == 2
    assert sum((p / 'error.json').is_file() for p in attempts) == 1


def test_archive_failure_never_reports_success(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(remote, 'durable_write', fail)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/submissions', content=b'zip')
        assert response.status_code == 503
        assert response.json()['outcome_unknown'] is True


def test_remote_path_never_opens_database_and_rejects_conflicting_root(tmp_path, monkeypatch):
    from question_bank import agent_tools
    def forbidden(*args, **kwargs):
        raise AssertionError('remote client accessed DB')
    monkeypatch.setattr(agent_tools.db_mod, 'is_initialized', forbidden)
    monkeypatch.setattr(agent_tools, 'process_remote', lambda *a, **k: {'ok': True})
    assert agent_tools.process('m', 't', server='http://example')['ok']
    with pytest.raises(agent_tools.ToolError):
        agent_tools.process('m', 't', root=tmp_path, server='http://example')


@pytest.mark.parametrize('body', [b'[]', b'null', b'<html>gateway error</html>'])
def test_remote_malformed_response_marks_submission_unknown(monkeypatch, body):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(body)
    monkeypatch.setattr(remote.urllib.request, 'build_opener', lambda *a: Opener())
    result = remote.request('http://example', '/api/submissions', raw=b'zip')
    assert result['ok'] is False and result['outcome_unknown'] is True


@pytest.mark.parametrize('body', [b'[]', b'null', b'<html>gateway error</html>'])
def test_remote_malformed_http_error_is_stable(monkeypatch, body):
    import urllib.error
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('http://example', 502, 'gateway', {}, io.BytesIO(body))
    monkeypatch.setattr(remote.urllib.request, 'build_opener', lambda *a: Opener())
    result = remote.request('http://example', '/api/submissions', raw=b'zip')
    assert result['http_status'] == 502 and result['outcome_unknown'] is True


def test_receipt_archive_failure_preserves_authoritative_receipt(tmp_path, monkeypatch):
    from question_bank import oss_store
    monkeypatch.delenv('QB_OSS_BUCKET', raising=False)
    oss_store._config.cache_clear()
    package = make_package(tmp_path / 'packages')
    original = remote.durable_write
    def fail_receipt(path, raw):
        if path.name == 'receipt.json':
            raise OSError('disk full after DB commit')
        return original(path, raw)
    monkeypatch.setattr(remote, 'durable_write', fail_receipt)
    with TestClient(create_app(tmp_path / 'root')) as client:
        result = client.post('/api/submissions', content=remote.package_bytes(package))
        assert result.status_code == 503 and result.json()['outcome_unknown'] is True
        receipt = client.get('/api/submissions/sub-001')
        assert receipt.status_code == 200 and receipt.json()['receipt']['status'] == 'accepted'
