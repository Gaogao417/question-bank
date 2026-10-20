"""试卷目录与连续审核（老版体验复刻）测试。

覆盖：元信息确定性解析（年份/区/考试类型/联合区/未知不猜）、
目录统计基于当前修订（approved/rejected/pending/stale 互斥、blocked 重叠）、
筛选与 facets、单卷题目目录、全卷一键通过（部分成功语义、幂等、
清单覆盖校验）与对应 HTTP 接口。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from question_bank.catalog import parse_paper_meta  # noqa: E402
from question_bank.errors import QbError, ReceiptError  # noqa: E402
from question_bank.service import QuestionBankService  # noqa: E402
from tests.test_qb_manage_service import make_package  # noqa: E402


# ----------------------------------------------------------------------
# 元信息确定性解析
# ----------------------------------------------------------------------


class TestParsePaperMeta:
    def test_typical_exam_paper_id(self):
        meta = parse_paper_meta("2014-HUANGPU-YIMO")
        assert meta["year"] == 2014
        assert meta["districts"] == ["黄浦"]
        assert meta["district_label"] == "黄浦"
        assert meta["exam_type"] == "一模"
        assert meta["grade"] is None  # 不猜默认年级

    def test_joint_districts_kept(self):
        meta = parse_paper_meta("2012-BAOSHAN-JIADING-ERMO")
        assert meta["districts"] == ["宝山", "嘉定"]
        assert meta["exam_type"] == "二模"

    def test_zhabei_legacy_district(self):
        meta = parse_paper_meta("2014-ZHABEI-YIMO")
        assert meta["districts"] == ["闸北"]

    def test_unknown_tokens_stay_unknown(self):
        meta = parse_paper_meta("SPECIAL-COLLECTION")
        assert meta["year"] is None
        assert meta["districts"] is None
        assert meta["exam_type"] is None

    def test_title_fallback_and_grade(self):
        meta = parse_paper_meta(
            "MISC-ID", "2023 届上海市静安区初三第二学期期末数学试卷"
        )
        assert meta["year"] == 2023
        assert meta["districts"] == ["静安"]
        assert meta["exam_type"] == "期末"
        assert meta["grade"] == "初三"

    def test_paper_id_wins_over_title(self):
        meta = parse_paper_meta("2014-HUANGPU-YIMO", "2015 年徐汇区一模")
        assert meta["year"] == 2014
        assert meta["districts"] == ["黄浦"]

    def test_explicit_grade_column(self):
        meta = parse_paper_meta("2014-HUANGPU-YIMO", grade="九年级")
        assert meta["grade"] == "九年级"


# ----------------------------------------------------------------------
# 播种：两份试卷、多种审核状态
# ----------------------------------------------------------------------


def seed_bank(service: QuestionBankService, tmp_path: Path) -> None:
    """2024-DEMO-A：3 题；2025-DEMO-B：2 题（其中 1 题阻断疑点）。"""
    plans = [
        ("2024-DEMO-A", 1, "seed-a1", []),
        ("2024-DEMO-A", 2, "seed-a2", []),
        ("2024-DEMO-A", 3, "seed-a3", []),
        ("2025-DEMO-B", 1, "seed-b1", []),
        ("2025-DEMO-B", 2, "seed-b2", [{
            "issue_id": "iss-crop", "type": "needs_human_crop",
            "target": "images[fig-01]", "reason": "边界不清", "blocking": True,
        }]),
    ]
    for paper_id, number, submission_id, issues in plans:
        service.submit_candidate(make_package(
            tmp_path, submission_id=submission_id, paper_id=paper_id,
            title=f"{paper_id} 示例卷", question_number=number, issues=issues,
        ))


def review(service: QuestionBankService, item_id: str, revision: int,
           decision: str, operation_id: str) -> dict:
    return service.record_review(item_id, {
        "decision": decision, "note": "", "base_revision": revision,
        "operation_id": operation_id,
    })


@pytest.fixture()
def bank(tmp_path: Path) -> QuestionBankService:
    service = QuestionBankService(tmp_path / "catalog-root")
    seed_bank(service, tmp_path)
    yield service
    service.close()


def entry_of(catalog_data: dict, paper_id: str) -> dict:
    return next(p for p in catalog_data["papers"] if p["paper_id"] == paper_id)


# ----------------------------------------------------------------------
# 目录统计与筛选
# ----------------------------------------------------------------------


class TestPaperCatalog:
    def test_stats_all_pending_initially(self, bank):
        data = bank.paper_catalog()
        assert len(data["papers"]) == 2
        entry = entry_of(data, "2024-DEMO-A")
        assert entry["stats"] == {
            "total_received": 3, "pending": 3, "approved": 0, "rejected": 0,
            "stale": 0, "blocked": 0, "published_current": 0,
        }
        assert entry["meta"]["year"] == 2024
        assert entry["meta"]["exam_type"] is None  # DEMO 卷无考试类型
        assert entry["first_item_id"] == "2024-DEMO-A-Q001"

    def test_filters_and_facets(self, bank):
        review(bank, "2024-DEMO-A-Q001", 1, "approved", "rv-a1")
        data = bank.paper_catalog(year=2024)
        assert [p["paper_id"] for p in data["papers"]] == ["2024-DEMO-A"]
        assert data["facets"]["years"] == [2025, 2024]
        assert "黄浦" not in data["facets"]["districts"]  # 未解析的卷不计入 facets

        data = bank.paper_catalog(q="2025")
        assert [p["paper_id"] for p in data["papers"]] == ["2025-DEMO-B"]
        data = bank.paper_catalog(q="不存在")
        assert data["papers"] == []

    def test_stale_not_counted_as_approved(self, bank, tmp_path):
        review(bank, "2024-DEMO-A-Q001", 1, "approved", "rv-a1")
        # 人工保存新修订 r2：旧批准过期
        bank.save_edit("2024-DEMO-A-Q001", {
            "base_revision": 1, "operation_id": "op-edit-a1",
            "stem": "修改后的题干 $y=x^2$。",
        })
        directory = bank.paper_item_directory("2024-DEMO-A")
        by_id = {i["item_id"]: i for i in directory}
        assert by_id["2024-DEMO-A-Q001"]["review_status"] == "stale"
        assert by_id["2024-DEMO-A-Q001"]["revision"] == 2
        stats = entry_of(bank.paper_catalog(), "2024-DEMO-A")["stats"]
        assert stats["approved"] == 0
        assert stats["stale"] == 1
        assert stats["pending"] == 2

    def test_rejected_and_blocked_dimensions(self, bank):
        review(bank, "2024-DEMO-A-Q002", 1, "rejected", "rv-a2")
        directory = bank.paper_item_directory("2025-DEMO-B")
        blocked = [i for i in directory if i["open_blocking_issues"]]
        assert [i["item_id"] for i in blocked] == ["2025-DEMO-B-Q002"]
        assert blocked[0]["blocked"] is True
        stats = entry_of(bank.paper_catalog(), "2024-DEMO-A")["stats"]
        assert stats["rejected"] == 1

    def test_published_current_dimension(self, bank):
        review(bank, "2024-DEMO-A-Q001", 1, "approved", "rv-a1")
        bank.publish("2024-DEMO-A-Q001", {
            "revision": 1, "expected_previous": None, "operation_id": "pub-a1",
        })
        stats = entry_of(bank.paper_catalog(), "2024-DEMO-A")["stats"]
        assert stats["published_current"] == 1
        # 保存 r2 后发布仍指向 r1：published_current 归零
        bank.save_edit("2024-DEMO-A-Q001", {
            "base_revision": 1, "operation_id": "op-edit-a1b",
            "stem": "再次修改的题干。",
        })
        stats = entry_of(bank.paper_catalog(), "2024-DEMO-A")["stats"]
        assert stats["published_current"] == 0


# ----------------------------------------------------------------------
# 全卷一键通过
# ----------------------------------------------------------------------


def all_payload(service: QuestionBankService, paper_id: str,
                operation_id: str = "rvall-1") -> dict:
    directory = service.paper_item_directory(paper_id)
    return {
        "operation_id": operation_id,
        "items": [
            {"item_id": i["item_id"], "base_revision": i["revision"]}
            for i in directory
        ],
    }


class TestReviewPaperAll:
    def test_approve_whole_paper(self, bank):
        receipt = bank.review_paper_all("2024-DEMO-A", all_payload(bank, "2024-DEMO-A"))
        assert receipt["status"] == "completed"
        assert len(receipt["approved"]) == 3
        assert receipt["failed"] == []
        assert receipt["counts"]["approved"] == 3
        assert receipt["counts"]["pending"] == 0

    def test_partial_failure_semantics(self, bank):
        receipt = bank.review_paper_all("2025-DEMO-B", all_payload(bank, "2025-DEMO-B"))
        assert receipt["status"] == "partial"
        approved_ids = [a["item_id"] for a in receipt["approved"]]
        assert approved_ids == ["2025-DEMO-B-Q001"]
        assert [f["item_id"] for f in receipt["failed"]] == ["2025-DEMO-B-Q002"]
        assert receipt["failed"][0]["errors"][0]["code"] == "APPROVAL_BLOCKED"
        # 失败题保持待审，未伪称整卷事务全成全败
        assert receipt["counts"]["approved"] == 1
        assert receipt["counts"]["pending"] == 1

    def test_already_approved_skipped(self, bank):
        review(bank, "2024-DEMO-A-Q001", 1, "approved", "rv-a1")
        receipt = bank.review_paper_all("2024-DEMO-A", all_payload(bank, "2024-DEMO-A"))
        assert [s["item_id"] for s in receipt["skipped"]] == ["2024-DEMO-A-Q001"]
        assert len(receipt["approved"]) == 2
        # 不重复写审核记录
        rows = bank.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE item_id = '2024-DEMO-A-Q001'"
        ).fetchone()["n"]
        assert rows == 1

    def test_stale_base_revision_fails_per_item(self, bank):
        bank.save_edit("2024-DEMO-A-Q002", {
            "base_revision": 1, "operation_id": "op-edit-a2",
            "stem": "先改一题形成 r2。",
        })
        # 清单仍携带 r1（用户看到的旧目录）：逐题失败，其余照常
        receipt = bank.review_paper_all("2024-DEMO-A", {
            "operation_id": "rvall-stale",
            "items": [
                {"item_id": "2024-DEMO-A-Q001", "base_revision": 1},
                {"item_id": "2024-DEMO-A-Q002", "base_revision": 1},
                {"item_id": "2024-DEMO-A-Q003", "base_revision": 1},
            ],
        })
        assert receipt["status"] == "partial"
        failed = receipt["failed"][0]
        assert failed["item_id"] == "2024-DEMO-A-Q002"
        assert failed["errors"][0]["code"] == "BASE_REVISION_CONFLICT"
        assert len(receipt["approved"]) == 2

    def test_replay_same_operation_id(self, bank):
        payload = all_payload(bank, "2024-DEMO-A")
        first = bank.review_paper_all("2024-DEMO-A", payload)
        second = bank.review_paper_all("2024-DEMO-A", payload)
        assert second["replayed"] is True
        assert second["approved"] == first["approved"]
        rows = bank.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews"
            " WHERE item_id LIKE '2024-DEMO-A-%'"
        ).fetchone()["n"]
        assert rows == 3  # 重放不重复写

    def test_same_operation_id_different_payload_rejected(self, bank):
        payload = all_payload(bank, "2024-DEMO-A")
        bank.review_paper_all("2024-DEMO-A", payload)
        with pytest.raises(ReceiptError) as exc_info:
            bank.review_paper_all("2024-DEMO-A", {**payload, "note": "different"})
        assert exc_info.value.errors[0]["code"] == "OPERATION_ID_REUSED"

    def test_foreign_and_coverage_validation(self, bank):
        with pytest.raises(QbError) as exc_info:
            bank.review_paper_all("2024-DEMO-A", {
                "operation_id": "rvall-foreign",
                "items": [{"item_id": "2025-DEMO-B-Q001", "base_revision": 1}],
            })
        assert exc_info.value.errors[0]["code"] == "REVIEW_ALL_ITEM_FOREIGN"

        with pytest.raises(QbError) as exc_info:
            bank.review_paper_all("2024-DEMO-A", {
                "operation_id": "rvall-partial-list",
                "items": [{"item_id": "2024-DEMO-A-Q001", "base_revision": 1}],
            })
        assert exc_info.value.errors[0]["code"] == "REVIEW_ALL_COVERAGE"

        with pytest.raises(QbError) as exc_info:
            bank.review_paper_all("NO-SUCH-PAPER", {
                "operation_id": "rvall-nopaper",
                "items": [{"item_id": "x", "base_revision": 1}],
            })
        assert exc_info.value.errors[0]["code"] == "PAPER_NOT_FOUND"

    def test_no_auto_publish(self, bank):
        receipt = bank.review_paper_all("2024-DEMO-A", all_payload(bank, "2024-DEMO-A"))
        assert receipt["status"] == "completed"
        heads = bank.conn.execute("SELECT COUNT(*) AS n FROM publication_heads").fetchone()["n"]
        assert heads == 0


# ----------------------------------------------------------------------
# HTTP 接口层
# ----------------------------------------------------------------------


class TestCatalogApi:
    @pytest.fixture()
    def client_bank(self, tmp_path: Path):
        from fastapi.testclient import TestClient
        from question_bank.server import create_app

        root = tmp_path / "api-root"
        service = QuestionBankService(root)
        try:
            seed_bank(service, tmp_path)
        finally:
            service.close()
        app = create_app(root)
        with TestClient(app) as client:
            yield client

    def test_papers_endpoint_filters_and_facets(self, client_bank):
        data = client_bank.get("/api/papers").json()
        assert {p["paper_id"] for p in data["papers"]} == {
            "2024-DEMO-A", "2025-DEMO-B"}
        assert data["facets"]["years"] == [2025, 2024]

        data = client_bank.get("/api/papers", params={"q": "2024"}).json()
        assert [p["paper_id"] for p in data["papers"]] == ["2024-DEMO-A"]

        data = client_bank.get("/api/papers", params={"q": "无匹配"}).json()
        assert data["papers"] == []

    def test_items_endpoint_paper_directory(self, client_bank):
        data = client_bank.get(
            "/api/items", params={"paper_id": "2025-DEMO-B"}).json()
        assert [i["item_id"] for i in data["items"]] == [
            "2025-DEMO-B-Q001", "2025-DEMO-B-Q002"]
        assert data["items"][0]["review_status"] == "pending"
        response = client_bank.get(
            "/api/items", params={"paper_id": "NO-SUCH"})
        assert response.status_code == 404
        # 不带 paper_id 保持旧行为（全库列表）
        data = client_bank.get("/api/items").json()
        assert len(data["items"]) == 5

    def test_review_all_endpoint(self, client_bank):
        directory = client_bank.get(
            "/api/items", params={"paper_id": "2024-DEMO-A"}).json()["items"]
        payload = {
            "operation_id": "api-rvall-1",
            "items": [{"item_id": i["item_id"], "base_revision": i["revision"]}
                      for i in directory],
        }
        response = client_bank.post("/api/papers/2024-DEMO-A/review-all",
                                    json=payload)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["status"] == "completed"
        assert receipt["counts"]["approved"] == 3

        # 重放
        replay = client_bank.post("/api/papers/2024-DEMO-A/review-all",
                                  json=payload).json()
        assert replay["replayed"] is True

        # 同 ID 异载荷
        conflict = client_bank.post(
            "/api/papers/2024-DEMO-A/review-all",
            json={**payload, "note": "x"})
        assert conflict.status_code == 409

        # 试卷不存在 → 404（换新 operation_id，避免先命中幂等复用检查）
        missing = client_bank.post("/api/papers/NO-SUCH/review-all",
                                   json={**payload, "operation_id": "api-rvall-2"})
        assert missing.status_code == 404
