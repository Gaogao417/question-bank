"""Contracts requiring a dedicated PostgreSQL test database, never real OSS."""
import pytest

from question_bank.service import QuestionBankService
from tests.test_qb_manage_service import make_package
from tests.test_qb_oss_store import fake_oss  # noqa: F401 -- shared pytest fixture


def test_candidate_with_oss_evidence_is_accepted(fake_oss, tmp_path):
    svc = QuestionBankService(tmp_path / "bank")
    try:
        receipt = svc.submit_candidate(make_package(tmp_path / "packages"))
        assert len(fake_oss.objects) == 4
        assert receipt["status"] == "accepted", receipt
    finally:
        svc.close()


def test_identical_rejected_submission_replays_original_receipt(tmp_path):
    svc = QuestionBankService(tmp_path / "bank", oss=False)
    try:
        package = make_package(tmp_path / "packages", answer="")
        first = svc.submit_candidate(package)
        assert first["status"] == "rejected"
        replay = svc.submit_candidate(package)
        assert replay["status"] == first["status"]
        assert replay["errors"] == first["errors"]
        assert replay["replayed"] is True
    finally:
        svc.close()


def test_rejected_same_errors_different_request_conflicts(tmp_path):
    import json
    from question_bank.errors import ReceiptError
    svc = QuestionBankService(tmp_path / "bank", oss=False)
    try:
        package = make_package(tmp_path / "packages", answer="")
        first = svc.submit_candidate(package)
        path = package / "candidate.json"
        payload = json.loads(path.read_text())
        payload["item"]["stem"] += " changed"
        path.write_text(json.dumps(payload))
        with pytest.raises(ReceiptError) as exc:
            svc.submit_candidate(package)
        assert exc.value.errors[0]["code"] == "SUBMISSION_ID_REUSED"
        assert svc.submission_receipt(first["submission_id"])["errors"] == first["errors"]
    finally:
        svc.close()


def test_missing_oss_evidence_stays_rejected(fake_oss, tmp_path, monkeypatch):
    svc = QuestionBankService(tmp_path / "bank")
    original = svc.validate_content
    def missing(content, **kwargs):
        row = svc.conn.execute("SELECT path FROM assets WHERE asset_id=%s", (content.evidence[0].asset_id,)).fetchone()
        fake_oss.objects.pop(row["path"])
        return original(content, **kwargs)
    monkeypatch.setattr(svc, "validate_content", missing)
    try:
        receipt = svc.submit_candidate(make_package(tmp_path / "packages"))
        assert receipt["status"] == "rejected"
        assert any(err["code"] == "EVIDENCE_ASSET_UNAVAILABLE" for err in receipt["errors"])
        assert svc.conn.execute("SELECT count(*) AS n FROM items").fetchone()["n"] == 0
    finally:
        svc.close()


def test_oss_validation_reads_concurrently_with_bounded_workers(fake_oss, tmp_path, monkeypatch):
    import threading
    import time
    from question_bank import oss_store
    lock = threading.Lock()
    active = peak = 0
    original = fake_oss.get_object
    def slow_read(key):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            return original(key)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(oss_store, 'get_object', slow_read)
    svc = QuestionBankService(tmp_path / 'bank')
    try:
        assert svc.submit_candidate(make_package(tmp_path / 'packages'))['status'] == 'accepted'
        assert 2 <= peak <= 4
        assert active == 0
    finally:
        svc.close()


def test_shared_service_concurrent_submission_stages_in_parallel_and_replays(fake_oss, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    svc = QuestionBankService(tmp_path / 'bank')
    package = make_package(tmp_path / 'packages')
    barrier = threading.Barrier(2)
    original = svc.assets.register_bytes
    local = threading.local()
    def register(*args, **kwargs):
        if not getattr(local, 'entered', False):
            local.entered = True
            # A process-wide submission mutex would make this time out.
            barrier.wait(timeout=10)
        return original(*args, **kwargs)
    monkeypatch.setattr(svc.assets, 'register_bytes', register)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: svc.submit_candidate(package), range(2)))
        assert sorted(r['replayed'] for r in receipts) == [False, True]
        assert {r['revision'] for r in receipts} == {1}
        assert svc.conn.execute('SELECT count(*) AS n FROM revisions').fetchone()['n'] == 1
        assert svc.conn.execute('SELECT count(*) AS n FROM submissions').fetchone()['n'] == 1
    finally:
        svc.close()
