"""题库管理（数据库真源层）service 级测试。

覆盖设计文档 §10 验收标准中可离线验证的部分：
接收幂等、无半题、乐观并发、人工修改保护、批准门槛、指定修订导出、
发布指针稳定、备份恢复与重启一致性。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from question_bank import backup as backup_mod  # noqa: E402
from question_bank import export as export_mod  # noqa: E402
from question_bank.errors import QbError, ReceiptError  # noqa: E402
from question_bank.service import QuestionBankService  # noqa: E402


# ----------------------------------------------------------------------
# 固定候选（与真实结构一致的最小样例；不调用模型）
# ----------------------------------------------------------------------

STEM_TEXT = (
    "已知二次函数 $y=x^2-2ax+3$。\n\n"
    "（1）当 $a=2$ 时，求函数的对称轴；\n\n"
    "（2）若函数图像经过点 $P(1,0)$，求 $a$ 的值。\n\n"
    "![题图](asset:fig-01)"
)
ANSWER_TEXT = "（1）$x=2$；（2）$a=2$。"
SOLUTION_TEXT = (
    "（1）当 $a=2$ 时，函数为 $y=x^2-4x+3$，所以对称轴为 $x=2$。\n"
    "（2）将 $P(1,0)$ 代入，得 $0=1-2a+3$，解得 $a=2$。\n\n"
    "![解答图](asset:fig-sol)"
)


def make_png(path: Path, size=(300, 200), color=(120, 40, 160)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")


def make_package(
    base: Path,
    *,
    submission_id: str = "sub-001",
    base_revision: int | None = None,
    stem: str = STEM_TEXT,
    answer: str = ANSWER_TEXT,
    solution: str = SOLUTION_TEXT,
    issues: list[dict] | None = None,
    images: list[dict] | None = None,
    evidence: list[dict] | None = None,
    question_type: str = "problem",
    answer_placeholder: bool = False,
    paper_id: str = "2026-DEMO",
    title: str = "2026 年示例卷",
    grade: str = "九年级",
    question_number: int = 21,
) -> Path:
    package = base / submission_id
    media = package / "media"
    sources = package / "sources"
    make_png(media / "fig01.png", (240, 160))
    make_png(media / "figsol.png", (200, 120), (40, 120, 60))
    make_png(sources / "page-001.png", (400, 560), (200, 200, 210))
    make_png(sources / "page-005.png", (400, 560), (210, 200, 200))
    envelope = {
        "schema": "question_bank_candidate/v1",
        "submission_id": submission_id,
        "base_revision": base_revision,
        "paper": {
            "paper_id": paper_id,
            "title": title,
            "grade": grade,
            "subject": "数学",
            "source_archive": "documents/初三/2026-DEMO",
        },
        "item": {
            "question_number": question_number,
            "question_type": question_type,
            "points": 12,
            "section_title": "三、解答题",
            "stem": stem,
            "answer": "" if answer_placeholder else answer,
            "solution": solution,
        },
        "images": images
        or [
            {
                "key": "fig-01",
                "asset": {"ref": "media/fig01.png"},
                "role": "stem",
                "crop_px": None,
                "rotate": 0,
                "width": "110mm",
                "ordinal": 0,
            },
            {
                "key": "fig-sol",
                "asset": {"ref": "media/figsol.png"},
                "role": "solution",
                "crop_px": None,
                "rotate": 0,
                "width": "100mm",
                "ordinal": 1,
            },
        ],
        "evidence": evidence
        or [
            {"role": "question", "asset": {"ref": "sources/page-001.png"}, "page_number": 1},
            {"role": "solution", "asset": {"ref": "sources/page-005.png"}, "page_number": 5},
        ],
        "issues": issues or [],
    }
    (package / "candidate.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return package


@pytest.fixture()
def service(tmp_path: Path) -> QuestionBankService:
    return QuestionBankService(tmp_path / "manage-root")


@pytest.fixture()
def submitted(service: QuestionBankService, tmp_path: Path) -> dict:
    package = make_package(tmp_path)
    return service.submit_candidate(package)


# ----------------------------------------------------------------------
# 接收与幂等
# ----------------------------------------------------------------------

class TestCandidateAcceptance:
    def test_first_submission_accepted(self, service, submitted):
        assert submitted["status"] == "accepted"
        assert submitted["item_id"] == "2026-DEMO-Q021"
        assert submitted["revision"] == 1
        assert submitted["replayed"] is False
        assert submitted["errors"] == []
        detail = service.item_detail("2026-DEMO-Q021")
        assert detail["body"]["stem"] == STEM_TEXT
        assert detail["check"]["passed"] is True

    def test_replay_same_submission_is_idempotent(self, service, tmp_path, submitted):
        receipt = service.submit_candidate(
            make_package(tmp_path, submission_id="sub-001")
        )
        assert receipt["replayed"] is True
        assert receipt["revision"] == 1
        rows = service.conn.execute(
            "SELECT COUNT(*) AS n FROM revisions"
        ).fetchone()["n"]
        assert rows == 1

    def test_same_submission_id_different_payload_rejected(self, service, tmp_path):
        service.submit_candidate(make_package(tmp_path))
        with pytest.raises(ReceiptError) as exc_info:
            service.submit_candidate(
                make_package(tmp_path, submission_id="sub-001",
                             answer="（1）$x=1$；")
            )
        assert exc_info.value.errors[0]["code"] == "SUBMISSION_ID_REUSED"
        # 原回执保持不变
        receipt = service.submission_receipt("sub-001")
        assert receipt["status"] == "accepted"

    def test_hard_rejection_creates_no_half_item(self, service, tmp_path):
        package = make_package(
            tmp_path,
            submission_id="sub-bad",
            stem="题干缺图标记 ![题图](asset:missing-01)",
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "rejected"
        codes = [e["code"] for e in receipt["errors"]]
        assert "IMAGE_MARKER_UNBOUND" in codes
        assert service.conn.execute("SELECT COUNT(*) n FROM items").fetchone()["n"] == 0
        assert service.conn.execute("SELECT COUNT(*) n FROM revisions").fetchone()["n"] == 0
        assert service.conn.execute(
            "SELECT COUNT(*) n FROM paper_items"
        ).fetchone()["n"] == 0

    def test_missing_answer_with_declared_issue_accepted_with_issues(
        self, service, tmp_path
    ):
        package = make_package(
            tmp_path,
            submission_id="sub-noans",
            answer_placeholder=True,
            issues=[
                {
                    "issue_id": "iss-answer",
                    "type": "missing_answer",
                    "target": "body.answer",
                    "reason": "原卷答案页缺第 21 题答案",
                    "blocking": True,
                }
            ],
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "accepted_with_issues"
        blockers = service.approval_blockers(receipt["item_id"], 1)
        assert any("疑点" in b for b in blockers)

    def test_undeclared_empty_answer_rejected(self, service, tmp_path):
        package = make_package(
            tmp_path, submission_id="sub-silent", answer_placeholder=True
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "rejected"
        assert "ANSWER_UNDECLARED_EMPTY" in [
            e["code"] for e in receipt["errors"]
        ]

    def test_ai_crop_rejected_human_crop_kept(self, service, tmp_path):
        """AI 候选不允许代人工裁图；人工 PATCH 裁框不受影响。"""
        package = make_package(
            tmp_path,
            submission_id="sub-crop",
            images=[
                {
                    "key": "fig-01",
                    "asset": {"ref": "media/fig01.png"},
                    "role": "stem",
                    "crop_px": [0, 0, 120, 80],
                }
            ],
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "rejected"
        assert "AI_CROP_NOT_ALLOWED" in [e["code"] for e in receipt["errors"]]
        assert service.conn.execute(
            "SELECT COUNT(*) n FROM items"
        ).fetchone()["n"] == 0
        # 人工在 UI 保存裁框（走 PATCH，允许）
        service.submit_candidate(make_package(tmp_path, submission_id="sub-full"))
        detail = service.item_detail("2026-DEMO-Q021")
        images = []
        for binding in detail["images"]:
            payload = {k: binding[k] for k in (
                "key", "asset_id", "role", "source_asset_id", "crop_px",
                "rotate", "width", "ordinal")}
            if binding["key"] == "fig-01":
                payload["crop_px"] = [0, 0, 120, 80]
            images.append(payload)
        saved = service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "op-human-crop", "images": images,
        })
        assert saved["status"] == "saved"

    def test_choice_answer_must_be_letter(self, service, tmp_path):
        package = make_package(
            tmp_path,
            submission_id="sub-choice",
            stem="下列结论正确的是（　）\n\nA. $a^4\\cdot a^2=a^8$\n\nB. $a^4\\cdot a^2=a^6$",
            answer="第二个",
            solution="同底数幂相乘，指数相加，故选 B。",
            question_type="choice",
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "rejected"
        assert "ANSWER_FORMAT_CHOICE" in [e["code"] for e in receipt["errors"]]

    def test_receipt_query_recovers_lost_response(self, service, submitted):
        receipt = service.submission_receipt("sub-001")
        assert receipt["submission_id"] == "sub-001"
        assert receipt["revision"] == 1


class TestResubmissionRules:
    def test_identical_content_is_noop(self, service, tmp_path, submitted):
        receipt = service.submit_candidate(
            make_package(tmp_path, submission_id="sub-again", base_revision=1)
        )
        assert receipt["status"] == "noop"
        assert receipt["revision"] == 1

    def test_resubmit_without_base_rejected(self, service, tmp_path, submitted):
        receipt = service.submit_candidate(
            make_package(tmp_path, submission_id="sub-noBase")
        )
        assert receipt["status"] == "rejected"
        assert receipt["errors"][0]["code"] == "SUBMISSION_CONFLICT"

    def test_stale_base_conflict(self, service, tmp_path, submitted):
        receipt = service.submit_candidate(
            make_package(tmp_path, submission_id="sub-stale", base_revision=1,
                         answer="改后的答案")
        )
        assert receipt["status"] == "rejected"

    def test_different_content_requires_proposal_not_overwrite(
        self, service, tmp_path, submitted
    ):
        """人改完后 AI 再提交不同内容：不覆盖，要求走 UI/proposal。"""
        # 人工先保存 r2
        service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1,
            "operation_id": "op-human-1",
            "answer": "（1）$x=2$；（2）$a=2$。（人工核对后补充说明）",
        })
        # AI 携带旧基线重交 → conflict
        receipt = service.submit_candidate(
            make_package(tmp_path, submission_id="sub-oldBase", base_revision=1,
                         answer="AI 的旧答案")
        )
        assert receipt["status"] == "conflict"
        assert receipt["errors"][0]["code"] == "BASE_REVISION_STALE"
        # AI 携带最新基线但内容不同 → 拒绝覆盖，人工修订仍在
        receipt = service.submit_candidate(
            make_package(tmp_path, submission_id="sub-newBase", base_revision=2,
                         answer="AI 的新答案")
        )
        assert receipt["status"] == "rejected"
        assert receipt["errors"][0]["code"] == "REVISION_REQUIRES_PROPOSAL"
        detail = service.item_detail("2026-DEMO-Q021")
        assert detail["revision"] == 2
        assert "人工核对后补充说明" in detail["body"]["answer"]


# ----------------------------------------------------------------------
# 保存、并发与疑点
# ----------------------------------------------------------------------

class TestSaveEdit:
    def test_save_creates_new_revision_and_keeps_old(self, service, submitted):
        receipt = service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1,
            "operation_id": "op-1",
            "stem": STEM_TEXT + "\n\n（人工补充）",
        })
        assert receipt["status"] == "saved"
        assert receipt["revision"] == 2
        old = service.item_detail("2026-DEMO-Q021", revision=1)
        assert "（人工补充）" not in old["body"]["stem"]
        new = service.item_detail("2026-DEMO-Q021")
        assert "（人工补充）" in new["body"]["stem"]

    def test_operation_replay_and_reuse(self, service, submitted):
        payload = {
            "base_revision": 1, "operation_id": "op-r", "answer": "新答案文本",
        }
        first = service.save_edit("2026-DEMO-Q021", payload)
        replay = service.save_edit("2026-DEMO-Q021", payload)
        assert replay["replayed"] is True
        assert replay["revision"] == first["revision"]
        with pytest.raises(ReceiptError) as exc_info:
            service.save_edit("2026-DEMO-Q021", {
                "base_revision": first["revision"], "operation_id": "op-r",
                "answer": "不同载荷",
            })
        assert exc_info.value.errors[0]["code"] == "OPERATION_ID_REUSED"

    def test_stale_base_revision_returns_conflict_fields(self, service, submitted):
        service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "op-a", "answer": "第一次修改",
        })
        with pytest.raises(ReceiptError) as exc_info:
            service.save_edit("2026-DEMO-Q021", {
                "base_revision": 1, "operation_id": "op-b", "answer": "并发修改",
            })
        conflict = exc_info.value.conflict
        assert conflict["current_revision"] == 2
        assert "body.answer" in conflict["changed_fields"]
        assert "answer" in conflict["attempted_fields"]
        # 第二次保存没有产生修订
        assert service.conn.execute(
            "SELECT COUNT(*) n FROM revisions"
        ).fetchone()["n"] == 2

    def test_noop_when_nothing_changed(self, service, submitted):
        receipt = service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "op-noop", "stem": STEM_TEXT,
        })
        assert receipt["status"] == "noop"
        assert receipt["changed"] is False
        assert service.conn.execute(
            "SELECT COUNT(*) n FROM revisions"
        ).fetchone()["n"] == 1

    def test_invalid_edit_rejected_without_revision(self, service, submitted):
        with pytest.raises(QbError) as exc_info:
            service.save_edit("2026-DEMO-Q021", {
                "base_revision": 1, "operation_id": "op-x", "stem": "   ",
            })
        codes = [e["code"] for e in exc_info.value.errors]
        assert "STEM_EMPTY" in codes
        assert service.conn.execute(
            "SELECT COUNT(*) n FROM revisions"
        ).fetchone()["n"] == 1

    def test_image_replace_and_crop_update(self, service, submitted, tmp_path):
        # 上传新图 → 更新 fig-01 绑定（其余绑定原样保留）→ 保存
        info = service.register_asset(_png_bytes((180, 90)), source_kind="manual")
        detail = service.item_detail("2026-DEMO-Q021")
        images = []
        for binding in detail["images"]:
            if binding["key"] == "fig-01":
                images.append({
                    "key": "fig-01",
                    "asset_id": info["asset_id"],
                    "role": "stem",
                    "source_asset_id": info["asset_id"],
                    "crop_px": [10, 10, 170, 80],
                    "rotate": 90,
                    "width": "100mm",
                    "ordinal": 0,
                })
            else:
                images.append({
                    "key": binding["key"],
                    "asset_id": binding["asset_id"],
                    "role": binding["role"],
                    "source_asset_id": binding["source_asset_id"],
                    "crop_px": binding["crop_px"],
                    "rotate": binding["rotate"],
                    "width": binding["width"],
                    "ordinal": binding["ordinal"],
                })
        receipt = service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1,
            "operation_id": "op-img",
            "images": images,
        })
        assert receipt["status"] == "saved"
        updated = {
            b["key"]: b for b in service.item_detail("2026-DEMO-Q021")["images"]
        }
        assert updated["fig-01"]["asset_id"] == info["asset_id"]
        assert updated["fig-01"]["crop_px"] == [10, 10, 170, 80]
        assert updated["fig-01"]["rotate"] == 90
        assert updated["fig-sol"]["key"] == "fig-sol"

    def test_issue_resolution_creates_revision(self, service, tmp_path):
        service.submit_candidate(make_package(
            tmp_path, submission_id="sub-iss",
            answer_placeholder=True,
            issues=[{
                "issue_id": "iss-1", "type": "missing_answer",
                "target": "body.answer", "reason": "缺答案", "blocking": True,
            }],
        ))
        receipt = service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1,
            "operation_id": "op-fix",
            "answer": ANSWER_TEXT,
            "issue_ops": [{"issue_id": "iss-1", "state": "resolved",
                           "note": "人工补齐答案"}],
        })
        assert receipt["revision"] == 2
        detail = service.item_detail("2026-DEMO-Q021")
        states = {i["issue_id"]: i["state"] for i in detail["issues"]}
        assert states["iss-1"] == "resolved"
        # 内容 hash 变了 + 疑点处置也在同一修订
        assert detail["approval_blockers"] == []


def _png_bytes(size=(180, 90), color=(30, 144, 255)) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


# ----------------------------------------------------------------------
# 复核、发布、导出
# ----------------------------------------------------------------------

class TestReviewPublishExport:
    def _prepared(self, service, tmp_path) -> str:
        service.submit_candidate(make_package(
            tmp_path, submission_id="sub-rev",
            answer_placeholder=True,
            issues=[{
                "issue_id": "iss-1", "type": "missing_answer",
                "target": "body.answer", "reason": "缺答案", "blocking": True,
            }],
        ))
        return "2026-DEMO-Q021"

    def test_approval_blocked_until_issue_resolved_and_answer_filled(
        self, service, tmp_path
    ):
        item_id = self._prepared(service, tmp_path)
        with pytest.raises(ReceiptError) as exc_info:
            service.record_review(item_id, {
                "decision": "approved", "base_revision": 1,
                "operation_id": "op-appr-1",
            })
        assert exc_info.value.errors[0]["code"] == "APPROVAL_BLOCKED"
        service.save_edit(item_id, {
            "base_revision": 1, "operation_id": "op-fix-1",
            "answer": ANSWER_TEXT,
            "issue_ops": [{"issue_id": "iss-1", "state": "resolved"}],
        })
        receipt = service.record_review(item_id, {
            "decision": "approved", "base_revision": 2,
            "operation_id": "op-appr-2", "note": "内容核对无误",
        })
        assert receipt["decision"] == "approved"
        assert receipt["revision"] == 2

    def test_publish_pointer_stable_after_new_edit(self, service, tmp_path):
        item_id = self._prepared(service, tmp_path)
        service.save_edit(item_id, {
            "base_revision": 1, "operation_id": "op-fix",
            "answer": ANSWER_TEXT,
            "issue_ops": [{"issue_id": "iss-1", "state": "resolved"}],
        })
        service.record_review(item_id, {
            "decision": "approved", "base_revision": 2, "operation_id": "op-appr",
        })
        published = service.publish(item_id, {
            "revision": 2, "expected_previous": None,
            "operation_id": "op-pub",
        })
        assert published["status"] == "published"
        # 新编辑产生 r3（待审），旧发布指针不变
        service.save_edit(item_id, {
            "base_revision": 2, "operation_id": "op-edit3",
            "solution": SOLUTION_TEXT + "\n（补充）",
        })
        detail = service.item_detail(item_id)
        assert detail["publication"]["revision"] == 2
        assert detail["review_state"] is None  # r3 未复核
        # r3 未批准不能发布
        with pytest.raises(ReceiptError) as exc_info:
            service.publish(item_id, {
                "revision": 3, "expected_previous": published["publication_id"],
                "operation_id": "op-pub3",
            })
        assert exc_info.value.errors[0]["code"] == "PUBLISH_NOT_APPROVED"
        # 撤回：expected_previous 必须匹配
        with pytest.raises(ReceiptError):
            service.withdraw(item_id, {
                "expected_previous": 999, "operation_id": "op-wd-bad",
            })
        withdrawn = service.withdraw(item_id, {
            "expected_previous": published["publication_id"],
            "operation_id": "op-wd",
        })
        assert withdrawn["status"] == "withdrawn"
        assert service.item_detail(item_id)["publication"] is None

    def test_export_approved_revision(self, service, tmp_path):
        item_id = self._prepared(service, tmp_path)
        service.save_edit(item_id, {
            "base_revision": 1, "operation_id": "op-fix",
            "answer": ANSWER_TEXT + "（人工核对）",
            "issue_ops": [{"issue_id": "iss-1", "state": "resolved"}],
        })
        # 未批准不能正式导出
        with pytest.raises(ReceiptError) as exc_info:
            export_mod.export_revision(service, item_id, 2)
        assert exc_info.value.errors[0]["code"] == "EXPORT_NOT_APPROVED"
        # 草稿预览可以
        draft = export_mod.export_revision(service, item_id, 2, allow_draft=True)
        assert draft["status"] == "ready"
        service.record_review(item_id, {
            "decision": "approved", "base_revision": 2, "operation_id": "op-appr",
        })
        result = export_mod.export_revision(service, item_id, 2)
        assert result["status"] == "ready"
        export_dir = service.root / result["path"]
        teacher = yaml.safe_load(
            (export_dir / "teacher.assignment.yaml").read_text(encoding="utf-8")
        )
        student = yaml.safe_load(
            (export_dir / "student.assignment.yaml").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (export_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert teacher["meta"]["revision"] == 2
        assert "（人工核对）" in teacher["sections"][0]["blocks"][0]["answer"]
        block = student["sections"][0]["blocks"][0]
        assert "answer" not in block and "solution_steps" not in block
        # 题干图（prompt/clean）保留给学生版；解答图（teacher_only）剥离
        assert [image["key"] for image in block["images"]] == ["fig-01"]
        teacher_block = teacher["sections"][0]["blocks"][0]
        assert [image["key"] for image in teacher_block["images"]] == [
            "fig-01", "fig-sol"
        ]
        assert (export_dir / "media" / "fig-sol.png").is_file()
        assert manifest["content_hash"] == service.item_detail(item_id, 2)["content_hash"]
        media = export_dir / "media" / "fig-01.png"
        assert media.is_file()
        # 重试同一修订：复用既有导出
        again = export_mod.export_revision(service, item_id, 2)
        assert again["replayed"] is True and again["path"] == result["path"]

    def test_export_binds_to_requested_revision_not_current(self, service, tmp_path):
        item_id = self._prepared(service, tmp_path)
        service.save_edit(item_id, {
            "base_revision": 1, "operation_id": "op-fix",
            "answer": ANSWER_TEXT,
            "issue_ops": [{"issue_id": "iss-1", "state": "resolved"}],
        })
        service.record_review(item_id, {
            "decision": "approved", "base_revision": 2, "operation_id": "op-appr",
        })
        export_mod.export_revision(service, item_id, 2)
        # 之后又保存 r3，不影响 r2 的既有导出
        service.save_edit(item_id, {
            "base_revision": 2, "operation_id": "op-edit3", "points": 14,
        })
        again = export_mod.export_revision(service, item_id, 2)
        assert again["replayed"] is True


# ----------------------------------------------------------------------
# 一致性、重启、备份
# ----------------------------------------------------------------------

class TestPersistenceAndBackup:
    def test_reopen_root_sees_same_state(self, service, submitted, tmp_path):
        service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "op-keep",
            "answer": ANSWER_TEXT + "（保存后重开仍一致）",
        })
        service.close()
        reopened = QuestionBankService(service.root)
        try:
            detail = reopened.item_detail("2026-DEMO-Q021")
            assert detail["revision"] == 2
            assert "保存后重开仍一致" in detail["body"]["answer"]
            assert detail["images"][0]["key"] == "fig-01"
        finally:
            reopened.close()

    def test_backup_and_restore_consistent(self, service, submitted, tmp_path):
        service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "op-fix",
            "answer": ANSWER_TEXT + "（人工修订）",
        })
        service.record_review("2026-DEMO-Q021", {
            "decision": "approved", "base_revision": 2, "operation_id": "op-appr",
        })
        service.publish("2026-DEMO-Q021", {
            "revision": 2, "expected_previous": None, "operation_id": "op-pub",
        })
        dest = tmp_path / "backup-1"
        result = backup_mod.backup(service.root, dest)
        assert result["dest"]
        summary = backup_mod.verify(dest)
        assert summary["items"] == 1
        assert summary["revisions"] == 2
        assert summary["reviews"] == 1
        assert summary["publication_heads"] == 1
        restore_root = tmp_path / "restored"
        backup_mod.restore(dest, restore_root)
        restored = QuestionBankService(restore_root)
        try:
            detail = restored.item_detail("2026-DEMO-Q021")
            assert detail["revision"] == 2
            assert detail["publication"]["revision"] == 2
            # 人工文字与媒体引用一致
            _, fig_bytes = restored.asset_bytes(detail["images"][0]["asset_id"])
            _, orig_fig = service.asset_bytes(
                service.item_detail("2026-DEMO-Q021")["images"][0]["asset_id"]
            )
            assert fig_bytes == orig_fig
        finally:
            restored.close()

    def test_restore_detects_missing_media(self, service, submitted, tmp_path):
        dest = tmp_path / "backup-2"
        backup_mod.backup(service.root, dest)
        for png in (dest / "assets").rglob("*.png"):
            png.unlink()
        with pytest.raises(QbError) as exc_info:
            backup_mod.verify(dest)
        assert exc_info.value.errors[0]["code"] == "RESTORE_MEDIA_MISSING"

    def test_old_revision_immutable_after_edits(self, service, submitted):
        service.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "op-r2", "stem": "改写题干",
        })
        r1 = service.conn.execute(
            "SELECT content FROM revisions WHERE item_id=%s AND revision=1",
            ("2026-DEMO-Q021",),
        ).fetchone()["content"]
        assert json.loads(r1)["stem"] == STEM_TEXT


class TestAcceptanceRegressionFixes:
    """首轮 codex-accept 发现缺陷的回归测试。"""

    def test_answer_marker_unbound_rejected(self, service, tmp_path):
        package = make_package(
            tmp_path, submission_id="sub-ansmark",
            answer="答案见 ![答案图](asset:missing-ans)",
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "rejected"
        err = [e for e in receipt["errors"] if e["code"] == "IMAGE_MARKER_UNBOUND"]
        assert err and err[0]["field_path"] == "body.answer"

    def test_answer_only_image_teacher_yes_student_no(self, service, tmp_path):
        """仅被答案引用的图片：教师版包含，学生版剥离。"""
        package = make_package(
            tmp_path, submission_id="sub-ansimg",
            answer="（1）$x=2$；（2）$a=2$。\n\n![答案图](asset:fig-ans)",
            images=[
                {"key": "fig-01", "asset": {"ref": "media/fig01.png"},
                 "role": "stem", "crop_px": None, "rotate": 0, "width": None,
                 "ordinal": 0},
                {"key": "fig-ans", "asset": {"ref": "media/figsol.png"},
                 "role": "stem", "crop_px": None, "rotate": 0, "width": None,
                 "ordinal": 1},
                {"key": "fig-sol", "asset": {"ref": "media/figsol.png"},
                 "role": "solution", "crop_px": None, "rotate": 0, "width": None,
                 "ordinal": 2},
            ],
        )
        receipt = service.submit_candidate(package)
        assert receipt["status"] == "accepted", receipt["errors"]
        service.record_review("2026-DEMO-Q021", {
            "decision": "approved", "base_revision": 1, "operation_id": "op-appr",
        })
        result = export_mod.export_revision(service, "2026-DEMO-Q021", 1)
        export_dir = service.root / result["path"]
        teacher = yaml.safe_load(
            (export_dir / "teacher.assignment.yaml").read_text(encoding="utf-8")
        )
        student = yaml.safe_load(
            (export_dir / "student.assignment.yaml").read_text(encoding="utf-8")
        )
        teacher_keys = [
            i["key"] for i in teacher["sections"][0]["blocks"][0]["images"]
        ]
        student_keys = [
            i["key"] for i in student["sections"][0]["blocks"][0]["images"]
        ]
        # fig-ans 只被答案引用：教师有、学生无；即使声明 role=stem
        assert set(teacher_keys) >= {"fig-01", "fig-ans", "fig-sol"}
        assert student_keys == ["fig-01"]

    def test_choice_single_letter_answer_exports_cleanly(self, service, tmp_path):
        """答案为单字母 A、选项含 A/B/C/D 的选择题：学生版不误报泄漏。"""
        package = make_package(
            tmp_path, submission_id="sub-choiceok",
            stem="下列结论正确的是（　）\n\nA. $a^4\cdot a^2=a^8$\n\nB. $a^4\cdot a^2=a^6$\n\nC. $a^4\cdot a^2=a^{16}$\n\nD. $a^4\cdot a^2=a^2$",
            answer="A",
            solution="同底数幂相乘，底数不变指数相加，故选 A。",
            question_type="choice",
        )
        service.submit_candidate(package)
        service.record_review("2026-DEMO-Q021", {
            "decision": "approved", "base_revision": 1, "operation_id": "op-appr",
        })
        result = export_mod.export_revision(service, "2026-DEMO-Q021", 1)
        assert result["status"] == "ready"

    def test_rejected_after_approved_blocks_publish_and_export(
        self, service, tmp_path
    ):
        """同修订先批准再驳回：最新决定生效，发布与正式导出被拒。"""
        service.submit_candidate(make_package(tmp_path, submission_id="sub-flip"))
        service.record_review("2026-DEMO-Q021", {
            "decision": "approved", "base_revision": 1, "operation_id": "op-a1",
        })
        assert service.is_approved("2026-DEMO-Q021", 1) is True
        service.record_review("2026-DEMO-Q021", {
            "decision": "rejected", "base_revision": 1,
            "operation_id": "op-r1", "note": "复查发现转写错误",
        })
        assert service.is_approved("2026-DEMO-Q021", 1) is False
        assert service.item_detail("2026-DEMO-Q021")["review_state"] == "rejected"
        with pytest.raises(ReceiptError) as pub_exc:
            service.publish("2026-DEMO-Q021", {
                "revision": 1, "expected_previous": None,
                "operation_id": "op-pub",
            })
        assert pub_exc.value.errors[0]["code"] == "PUBLISH_NOT_APPROVED"
        with pytest.raises(ReceiptError) as export_exc:
            export_mod.export_revision(service, "2026-DEMO-Q021", 1)
        assert export_exc.value.errors[0]["code"] == "EXPORT_NOT_APPROVED"

    def test_backup_detects_corrupted_media(self, service, submitted, tmp_path):
        dest = tmp_path / "backup-corrupt"
        backup_mod.backup(service.root, dest)
        for png in (dest / "assets").rglob("*.png"):
            png.write_bytes(b"corrupted image bytes")
            break
        with pytest.raises(QbError) as exc_info:
            backup_mod.verify(dest)
        assert exc_info.value.errors[0]["code"] == "RESTORE_MEDIA_CORRUPT"
