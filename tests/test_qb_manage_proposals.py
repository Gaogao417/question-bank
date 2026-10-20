"""阶段 D proposal 接口测试：建议独立保存、接受复用保存事务、过期不覆盖。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from question_bank.errors import QbError  # noqa: E402
from question_bank.service import QuestionBankService  # noqa: E402


CANDIDATE = {
    "schema": "question_bank_candidate/v1",
    "submission_id": "sub-prop-001",
    "base_revision": None,
    "paper": {"paper_id": "PROP-TEST", "title": "建议测试卷"},
    "item": {
        "question_number": 1,
        "question_type": "problem",
        "points": 10,
        "stem": "计算 $1+1$ 的值．",
        "answer": "$2$．",
        "solution": "解：$1+1=2$．",
    },
}

CANDIDATE_ISSUE = {
    **CANDIDATE,
    "submission_id": "sub-prop-issue-001",
    "paper": {"paper_id": "PROP-ISSUE", "title": "疑点建议测试卷"},
    "issues": [{
        "issue_id": "q1-figure-manual",
        "type": "missing_image",
        "target": "stem",
        "reason": "原页有本题插图，按宁缺勿滥转人工补图",
        "blocking": True,
    }],
}


@pytest.fixture()
def service_with_issue(tmp_path):
    from question_bank.service import QuestionBankService

    svc = QuestionBankService(tmp_path / "manage-root")
    package = tmp_path / "package-issue"
    package.mkdir()
    (package / "candidate.json").write_text(
        json.dumps(CANDIDATE_ISSUE, ensure_ascii=False), encoding="utf-8"
    )
    receipt = svc.submit_candidate(package)
    assert receipt["status"] == "accepted_with_issues"
    return svc


@pytest.fixture()
def service(tmp_path):
    from question_bank.service import QuestionBankService

    svc = QuestionBankService(tmp_path / "manage-root")
    package = tmp_path / "package"
    package.mkdir()
    (package / "candidate.json").write_text(
        json.dumps(CANDIDATE, ensure_ascii=False), encoding="utf-8"
    )
    receipt = svc.submit_candidate(package)
    assert receipt["status"] == "accepted"
    return svc


def test_submit_proposal_keeps_current_content_unchanged(service):
    before = service.item_detail("PROP-TEST-Q001")
    receipt = service.submit_proposal("PROP-TEST-Q001", {
        "proposal_id": "prop-001",
        "base_revision": 1,
        "answer": "$2$．（验算：$1+1=2$）",
        "reason": "补充验算说明",
    })
    assert receipt["status"] == "open"
    assert receipt["replayed"] is False
    after = service.item_detail("PROP-TEST-Q001")
    assert after["current_revision"] == before["current_revision"] == 1
    assert after["body"]["answer"] == before["body"]["answer"]


def test_submit_proposal_replays_same_payload_rejects_different(service):
    service.submit_proposal("PROP-TEST-Q001", {
        "proposal_id": "prop-001", "base_revision": 1, "answer": "A",
    })
    replay = service.submit_proposal("PROP-TEST-Q001", {
        "proposal_id": "prop-001", "base_revision": 1, "answer": "A",
    })
    assert replay["replayed"] is True
    with pytest.raises(QbError) as exc:
        service.submit_proposal("PROP-TEST-Q001", {
            "proposal_id": "prop-001", "base_revision": 1, "answer": "B",
        })
    assert any(e["code"] == "PROPOSAL_ID_REUSED" for e in exc.value.errors)


def test_submit_proposal_requires_current_base(service):
    with pytest.raises(QbError) as exc:
        service.submit_proposal("PROP-TEST-Q001", {
            "proposal_id": "prop-stale", "base_revision": 99, "answer": "X",
        })
    assert any(e["code"] == "BASE_REVISION_STALE" for e in exc.value.errors)


def test_accept_proposal_creates_new_revision_with_ai_actor(service):
    service.submit_proposal("PROP-TEST-Q001", {
        "proposal_id": "prop-acc", "base_revision": 1,
        "solution": "解：$1+1=2$．两边各加 0 不变。",
    })
    receipt = service.accept_proposal("prop-acc", {"operation_id": "op-acc-1"})
    assert receipt["status"] == "accepted"
    assert receipt["save_receipt"]["status"] == "saved"
    assert receipt["accepted_revision"] == 2
    detail = service.item_detail("PROP-TEST-Q001")
    assert detail["current_revision"] == 2
    assert "两边各加 0" in detail["body"]["solution"]
    rev2 = next(r for r in detail["revisions"] if r["revision"] == 2)
    assert rev2["actor_kind"] == "ai"
    assert rev2["origin_operation_id"] == "op-acc-1"
    # 未改动的 stem 保持不变
    assert detail["body"]["stem"] == "计算 $1+1$ 的值．"


def test_accept_proposal_applies_issue_ops_in_same_revision(service_with_issue):
    svc = service_with_issue
    item_id = "PROP-ISSUE-Q001"
    svc.submit_proposal(item_id, {
        "proposal_id": "prop-issue", "base_revision": 1,
        "stem": "计算 $1+1$ 的值．（配图见题图）",
        "reason": "按疑点意见补图说明",
    })
    receipt = svc.accept_proposal("prop-issue", {
        "operation_id": "op-acc-issue-1",
        "issue_ops": [
            {"issue_id": "q1-figure-manual", "state": "resolved",
             "note": "AI 修订已采用"},
        ],
    })
    assert receipt["status"] == "accepted"
    assert receipt["save_receipt"]["status"] == "saved"
    detail = svc.item_detail(item_id)
    assert detail["current_revision"] == 2
    issue = detail["issues"][0]
    assert issue["state"] == "resolved"
    assert issue["resolution"]["note"] == "AI 修订已采用"
    assert detail["approval_blockers"] == []
    assert "配图见题图" in detail["body"]["stem"]


def test_accept_stale_proposal_marks_expired_without_overwrite(service):
    service.submit_proposal("PROP-TEST-Q001", {
        "proposal_id": "prop-old", "base_revision": 1, "answer": "建议答案",
    })
    # 人工先保存新修订
    service.save_edit("PROP-TEST-Q001", {
        "base_revision": 1, "operation_id": "op-human-1",
        "answer": "人工答案",
    }, actor_kind="human")
    with pytest.raises(QbError) as exc:
        service.accept_proposal("prop-old", {"operation_id": "op-acc-old"})
    assert any(e["code"] == "PROPOSAL_BASE_STALE" for e in exc.value.errors)
    detail = service.item_detail("PROP-TEST-Q001")
    assert detail["current_revision"] == 2
    assert detail["body"]["answer"] == "人工答案"
    prop = next(p for p in service.list_proposals("PROP-TEST-Q001")
                if p["proposal_id"] == "prop-old")
    assert prop["status"] == "expired"


def test_reject_proposal(service):
    service.submit_proposal("PROP-TEST-Q001", {
        "proposal_id": "prop-rej", "base_revision": 1, "answer": "拒绝我",
    })
    receipt = service.reject_proposal("prop-rej", {"reason": "不需要"})
    assert receipt["status"] == "rejected"
    with pytest.raises(QbError) as exc:
        service.accept_proposal("prop-rej", {"operation_id": "op-rej"})
    assert any(e["code"] == "PROPOSAL_NOT_OPEN" for e in exc.value.errors)


def test_proposal_body_validation(service):
    with pytest.raises(QbError) as exc:
        service.submit_proposal("PROP-TEST-Q001", {
            "proposal_id": "prop-empty", "base_revision": 1,
        })
    assert any(e["code"] == "SCHEMA_INVALID" for e in exc.value.errors)
