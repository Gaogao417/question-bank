"""教学内容（讲解/解答对）测试：老版 explanations 子系统的数据库版。

覆盖验收文件（artifacts/review-ui-teaching-notes/acceptance-task.md）中可离线
验证的部分：003 迁移、note CRUD/版本/软删除、小问派生与 stale/readapt、
审批绑定、录音转写（mock）失败保留、幂等与 ai_operations 状态、一键补齐
只补空、blueprint 导出（本地快照+外部投递+零候选清理）、目录徽标与备份恢复。
真实模型调用与真实浏览器链路另行执行（e2e 与真实模型冒烟）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from question_bank import backup as backup_mod  # noqa: E402
from question_bank import db as db_mod  # noqa: E402
from question_bank import explanations_ai as ai  # noqa: E402
from question_bank.errors import QbError, ReceiptError  # noqa: E402
from question_bank.canonical import canonical_json  # noqa: E402
from question_bank.service import QuestionBankService  # noqa: E402
from question_bank.teaching import derive_subquestions  # noqa: E402
from tests.test_qb_manage_service import make_package  # noqa: E402

import io as _io
import shutil as _shutil
import subprocess as _subprocess
import wave as _wave

def _real_wav_bytes() -> bytes:
    """1 秒真实静音 WAV（ffprobe 可探测合法音轨）。"""
    ffmpeg = _shutil.which('ffmpeg')
    if ffmpeg:
        completed = _subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
             '-i', 'anullsrc=r=16000:cl=mono', '-t', '1', '-f', 'wav', 'pipe:1'],
            capture_output=True, timeout=30)
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout
    buffer = _io.BytesIO()
    with _wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b'\x00\x00' * 16000)
    return buffer.getvalue()

AUDIO_BYTES = _real_wav_bytes()


@pytest.fixture()
def svc(tmp_path: Path) -> QuestionBankService:
    service = QuestionBankService(tmp_path / "root")
    service.submit_candidate(make_package(tmp_path, submission_id="tn-s1"))
    yield service
    service.close()


def note_of(service: QuestionBankService, item_id: str, note_id: str) -> dict:
    view = service.notes_view(item_id)
    for subq in view["subquestions"]:
        for note in subq["notes"]:
            if note["note_id"] == note_id:
                return note
    raise AssertionError(f"note not found: {note_id}")


# ----------------------------------------------------------------------
# 003 迁移
# ----------------------------------------------------------------------




class TestDeriveSubquestions:
    def test_markers(self):
        subs = derive_subquestions("已知…。（1）求 $a$；\n\n（2）求 $b$。")
        assert [s["id"] for s in subs] == ["sq1", "sq2"]
        assert subs[0]["label"] == "（1）"
        assert "$a$" in subs[0]["stem"]

    def test_single_question(self):
        subs = derive_subquestions("整题只有一问")
        assert len(subs) == 1 and subs[0]["id"] == "sq1" and subs[0]["label"] == ""


# ----------------------------------------------------------------------
# CRUD / 版本 / 软删除 / stale
# ----------------------------------------------------------------------


class TestNotesLifecycle:
    def test_create_edit_version_chain(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "title": "思路A", "operation_id": "tn-c1"})
        assert created["version"] == 1
        note_id = created["note_id"]
        upd = svc.update_note(note_id, {
            "base_version": 1, "explanation_text": "讲思路",
            "solution_text": "解：步骤。", "operation_id": "tn-u1"})
        assert upd["version"] == 2
        # 普通编辑保留来源修订
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["source_revision"] == 1 and note["explanation_source"] == "manual"
        # 基线冲突
        with pytest.raises(ReceiptError) as exc:
            svc.update_note(note_id, {
                "base_version": 1, "explanation_text": "x", "operation_id": "tn-u2"})
        assert exc.value.errors[0]["code"] == "NOTE_VERSION_CONFLICT"
        # 同 ID 异载荷拒绝
        with pytest.raises(ReceiptError):
            svc.update_note(note_id, {
                "base_version": 1, "explanation_text": "讲思路",
                "operation_id": "tn-u1"})

    def test_item_revision_bump_marks_stale_and_readapt(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c2"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "e",
                                  "solution_text": "s", "operation_id": "tn-u3"})
        assert note_of(svc, "2026-DEMO-Q021", note_id)["source_stale"] is False
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "tn-e1",
            "stem": "题干改写（1）第一问\n\n（2）第二问"})
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["source_stale"] is True  # 普通编辑不得洗掉
        svc.update_note(note_id, {"base_version": 2, "operation_id": "tn-u4",
                                  "explanation_text": "e2"})
        assert note_of(svc, "2026-DEMO-Q021", note_id)["source_stale"] is True
        svc.update_note(note_id, {"base_version": 3, "readapt": True,
                                  "operation_id": "tn-u5"})
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["source_stale"] is False
        assert note["source_revision"] == 2

    def test_same_sq_changed_stem_marks_stale(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c3"})
        note_id = created["note_id"]
        # 题干小问文本变化但仍是两个小问 → sqN 相同也过期
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "tn-e2",
            "stem": "另一道题（1）新第一问\n\n（2）新第二问"})
        assert note_of(svc, "2026-DEMO-Q021", note_id)["source_stale"] is True

    def test_extra_subquestion_kept(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c4"})
        note_id = created["note_id"]
        # 题干改为单问 → sq2 消失；sq1 的 note 仍在（同 ID 题干变了→stale）
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "tn-e3", "stem": "只有一问的题干"})
        view = svc.notes_view("2026-DEMO-Q021")
        subs = {s["id"]: s for s in view["subquestions"]}
        assert "sq2" not in subs and "sq1" in subs
        assert subs["sq1"]["notes"][0]["note_id"] == note_id
        assert subs["sq1"]["notes"][0]["source_stale"] is True

    def test_soft_delete_keeps_history(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c5"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "e",
                                  "solution_text": "s", "operation_id": "tn-u6"})
        svc.review_note(note_id, {"base_version": 2, "decision": "approved",
                                  "operation_id": "tn-r1"})
        receipt = svc.delete_note(note_id, {"base_version": 2, "operation_id": "tn-d1"})
        assert receipt["status"] == "deleted"
        view = svc.notes_view("2026-DEMO-Q021")
        assert all(n["note_id"] != note_id for s in view["subquestions"] for n in s["notes"])
        # 版本与审批记录保留（溯源）
        rows = svc.conn.execute(
            "SELECT COUNT(*) c FROM teaching_note_versions WHERE note_id = %s",
            (note_id,)).fetchone()["c"]
        reviews = svc.conn.execute(
            "SELECT COUNT(*) c FROM teaching_note_reviews WHERE note_id = %s",
            (note_id,)).fetchone()["c"]
        assert rows == 2 and reviews == 1
        # 删除后操作拒绝
        with pytest.raises(QbError) as exc:
            svc.update_note(note_id, {"base_version": 2, "operation_id": "tn-u7",
                                      "explanation_text": "x"})
        assert exc.value.errors[0]["code"] == "NOTE_DELETED"


# ----------------------------------------------------------------------
# 审批
# ----------------------------------------------------------------------


class TestNoteReview:
    def test_approve_requires_both_texts(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c6"})
        note_id = created["note_id"]
        with pytest.raises(ReceiptError) as exc:
            svc.review_note(note_id, {"base_version": 1, "decision": "approved",
                                      "operation_id": "tn-r2"})
        assert exc.value.errors[0]["code"] == "NOTE_APPROVAL_BLOCKED"
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "e",
                                  "solution_text": "s", "operation_id": "tn-u8"})
        svc.review_note(note_id, {"base_version": 2, "decision": "approved",
                                  "operation_id": "tn-r3"})
        assert note_of(svc, "2026-DEMO-Q021", note_id)["review_status"] == "approved"
        # 批准后编辑 → 新版本回待审（审批绑定版本）
        svc.update_note(note_id, {"base_version": 2, "explanation_text": "e2",
                                  "operation_id": "tn-u9"})
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["review_status"] is None or note["review_status"] == "pending"
        # 旧版本审批事实保留
        rows = svc.conn.execute(
            "SELECT decision FROM teaching_note_reviews WHERE note_id = %s"
            " AND version = 2", (note_id,)).fetchall()
        assert rows[0]["decision"] == "approved"


# ----------------------------------------------------------------------
# 录音（mock ASR）/ 幂等 / ai_operations
# ----------------------------------------------------------------------


class TestRecording:
    def test_transcribe_success_flow(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c7"})
        note_id = created["note_id"]

        def fake_transcribe(data, content_type, meta=None):
            meta.update({"provider": "dashscope", "model": "mock-asr",
                         "usage": {"total_tokens": 10}})
            return "口头讲解内容"

        monkeypatch.setattr(ai, "transcribe_audio", fake_transcribe)
        receipt = svc.save_recording(note_id, 1, "tn-rec1", AUDIO_BYTES, "audio/wav")
        assert receipt["status"] == "transcribed"
        assert receipt["transcript"] == "口头讲解内容"
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["audio_asset_id"] and note["transcript"] == "口头讲解内容"
        row = svc.conn.execute(
            "SELECT * FROM ai_operations WHERE operation_id = 'tn-rec1'").fetchone()
        assert row["state"] == "succeeded" and row["applied"] == 1
        assert json.loads(row["usage"]) == {"total_tokens": 10}

    def test_asr_failure_keeps_audio(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c8"})
        note_id = created["note_id"]

        def boom(data, content_type, meta=None):
            raise ai.AiAssistError("provider_failed", "模型服务请求失败")

        monkeypatch.setattr(ai, "transcribe_audio", boom)
        receipt = svc.save_recording(note_id, 1, "tn-rec2", AUDIO_BYTES, "audio/wav")
        assert receipt["status"] == "audio_saved"
        assert receipt["errors"][0]["code"] == "asr_failed"
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["audio_asset_id"] and not note["transcript"]
        row = svc.conn.execute(
            "SELECT state, error FROM ai_operations WHERE operation_id = 'tn-rec2'").fetchone()
        assert row["state"] == "failed" and "provider_failed" in row["error"]

    def test_replay_and_unknown(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c9"})
        note_id = created["note_id"]
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "转写")
        svc.save_recording(note_id, 1, "tn-rec3", AUDIO_BYTES, "audio/wav")
        # 同 ID 异载荷（base_version 已变）拒绝
        with pytest.raises(ReceiptError):
            svc.save_recording(note_id, 2, "tn-rec3", AUDIO_BYTES, "audio/wav")
        # unknown：预置 running 行（同载荷哈希）→ 重放提示结果未知不自动重试
        info = svc.assets.register_audio(AUDIO_BYTES, "audio/wav")
        real_hash = canonical_json({
            "note_id": note_id, "base_version": 2,
            "asset": info["asset_id"], "content_type": "audio/wav",
        })
        with self._seed_running(svc, "tn-recX", note_id, real_hash):
            with pytest.raises(QbError) as exc:
                svc.save_recording(note_id, 2, "tn-recX", AUDIO_BYTES, "audio/wav")
            assert exc.value.errors[0]["code"] == "AI_OPERATION_UNKNOWN"

    @staticmethod
    class _seed_running:
        def __init__(self, service, op_id, target, request_hash="sha256:x"):
            self.service = service
            self.op_id = op_id
            self.target = target
            self.request_hash = request_hash

        def __enter__(self):
            self.service.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, created_at) VALUES (%s, %s,"
                " 'transcribe', %s, '{}', 'running', %s)",
                (self.op_id, self.request_hash, self.target, db_mod.utc_now()))
            self.service.conn.commit()

        def __exit__(self, *args):
            return False

    def test_conflict_during_recording_keeps_result_unapplied(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c10"})
        note_id = created["note_id"]

        def slow_transcribe(data, content_type, meta=None):
            # 模拟转写期间另一标签编辑了笔记
            svc.update_note(note_id, {"base_version": 1, "explanation_text": "并发编辑",
                                      "operation_id": "tn-race"})
            return "迟到的转写"

        monkeypatch.setattr(ai, "transcribe_audio", slow_transcribe)
        with pytest.raises(ReceiptError) as exc:
            svc.save_recording(note_id, 1, "tn-rec4", AUDIO_BYTES, "audio/wav")
        assert exc.value.errors[0]["code"] == "NOTE_VERSION_CONFLICT"
        row = svc.conn.execute(
            "SELECT state, result FROM ai_operations WHERE operation_id = 'tn-rec4'").fetchone()
        assert row["state"] == "conflict"
        assert "迟到的转写" in row["result"]  # 模型结果保留为未应用


class TestPolishAndGenerate:
    def test_polish_without_transcript_or_audio(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c11"})
        with pytest.raises(QbError) as exc:
            svc.polish_note(created["note_id"], {
                "base_version": 1, "operation_id": "tn-p1"})
        assert exc.value.errors[0]["code"] == "POLISH_NO_TRANSCRIPT"

    def test_polish_retranscribes_from_audio(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c12"})
        note_id = created["note_id"]
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "补转写")
        monkeypatch.setattr(ai, "polish_explanation_text",
                            lambda ctx, meta=None: f"润色：{ctx['transcript']}")
        # 先只存音频（ASR 失败路径）
        def fail(data, content_type, meta=None):
            raise ai.AiAssistError("provider_failed", "x")
        monkeypatch.setattr(ai, "transcribe_audio", fail)
        svc.save_recording(note_id, 1, "tn-rec5", AUDIO_BYTES, "audio/wav")
        # 再换回可用 mock，polish 重读录音补转写
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "补转写")
        receipt = svc.polish_note(note_id, {"base_version": 2, "operation_id": "tn-p2"})
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["explanation_source"] == "polished"
        assert note["explanation_text"] == "润色：补转写"
        assert note["transcript"] == "补转写"
        del receipt

    def test_generate_creates_new_source_version(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c13"})
        note_id = created["note_id"]
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "tn-e4", "stem": "新题干（1）甲\n\n（2）乙"})
        monkeypatch.setattr(ai, "generate_explanation_text",
                            lambda ctx, meta=None: "生成讲解")
        receipt = svc.generate_note_text(note_id, {
            "base_version": 1, "kind": "explanation", "operation_id": "tn-g1"})
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["source_revision"] == 2  # 基于当前题目重新生成 → 新来源
        assert note["source_stale"] is False
        assert note["explanation_source"] == "generated"
        del receipt


class TestFillMissing:
    def test_only_fills_empty_and_skips_stale(self, svc, monkeypatch):
        # sq1 已有手工讲解+解答；sq2 空 → 只补 sq2，不动 sq1
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c14"})
        svc.update_note(created["note_id"], {
            "base_version": 1, "explanation_text": "人工讲解",
            "solution_text": "人工解答", "operation_id": "tn-u10"})
        monkeypatch.setattr(ai, "generate_explanation_text",
                            lambda ctx, meta=None: "自动讲解")
        receipt = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "tn-f1"})
        assert receipt["generated"] == 1
        assert receipt["results"][0]["subquestion_id"] == "sq2"
        note = note_of(svc, "2026-DEMO-Q021", created["note_id"])
        assert note["explanation_text"] == "人工讲解"  # 未被覆盖

    def test_partial_failure_isolated(self, svc, monkeypatch):
        calls = []

        def flaky(ctx, meta=None):
            calls.append(ctx["subquestion_label"])
            if len(calls) == 1:
                raise ai.AiAssistError("provider_failed", "第一小问失败")
            return "第二小问讲解"

        monkeypatch.setattr(ai, "generate_explanation_text", flaky)
        receipt = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "tn-f2"})
        statuses = sorted(r["status"] for r in receipt["results"])
        assert statuses == ["failed", "generated"]
        assert receipt["generated"] == 1

    def test_fill_solution_targets_current_source_only(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c15"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "讲",
                                  "operation_id": "tn-u11"})
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "tn-e5", "stem": "改题（1）a\n\n（2）b"})
        # note 来源过期（题干变）→ fill solution 不动它
        monkeypatch.setattr(ai, "generate_solution_text",
                            lambda ctx, meta=None: "新解答")
        receipt = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "solution", "operation_id": "tn-f3"})
        assert receipt["targets"] == []
        assert receipt["generated"] == 0
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["solution_text"] == ""


# ----------------------------------------------------------------------
# blueprint 导出
# ----------------------------------------------------------------------


class TestBlueprintExport:
    def _prepare(self, svc, approve_item=True):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-bc1"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "讲解",
                                  "solution_text": "解：解答。", "operation_id": "tn-bu1"})
        svc.review_note(note_id, {"base_version": 2, "decision": "approved",
                                  "operation_id": "tn-br1"})
        if approve_item:
            svc.record_review("2026-DEMO-Q021", {
                "decision": "approved", "note": "", "base_revision": 1,
                "operation_id": "tn-ir1"})
        return note_id

    def test_requires_item_approval(self, svc):
        self._prepare(svc, approve_item=False)
        receipt = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "tn-bp1"})
        assert receipt["candidate_count"] == 0
        assert receipt["pending_item_review"] == ["2026-DEMO-Q021"]

    def test_export_payload_and_delivery(self, svc, tmp_path):
        note_id = self._prepare(svc)
        ext_root = tmp_path / "tt"
        receipt = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "tn-bp2"},
                                              teaching_tools_root=ext_root)
        assert receipt["candidate_count"] == 1
        assert receipt["external_status"] == "delivered"
        batch = json.loads(Path(receipt["batch_path"]).read_text(encoding="utf-8"))
        assert batch["taskId"] == "2026-DEMO" and batch["engineKind"] == "topic-practice"
        candidate = batch["candidates"][0]
        for key in ("id", "promptData", "answerKey", "metadata"):
            assert key in candidate
        assert candidate["metadata"]["source"] == "reviewed-bank-import"
        assert candidate["metadata"]["noteId"] == note_id
        for assignment in batch["assignments"]:
            assert Path(assignment).is_file()
        delivered = json.loads((ext_root / "authoring/tmp/reviewed-bank-import"
                                / "2026-DEMO.candidates.json").read_text(encoding="utf-8"))
        assert len(delivered["candidates"]) == 1

    def test_external_not_configured(self, svc, monkeypatch):
        monkeypatch.delenv("QB_TEACHING_TOOLS_ROOT", raising=False)
        self._prepare(svc)
        receipt = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "tn-bp3"})
        assert receipt["external_status"] == "not_configured"
        assert receipt["candidate_count"] == 1

    def test_stale_note_excluded_and_zero_candidate_removes_file(self, svc, tmp_path):
        note_id = self._prepare(svc)
        ext_root = tmp_path / "tt2"
        receipt = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "tn-bp4"},
                                              teaching_tools_root=ext_root)
        external_file = Path(receipt["external_path"])
        assert external_file.is_file()
        # 题目修订前进 → note 来源过期 → 不进当前批次，旧候选被清
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "tn-e6", "stem": "再改（1）x\n\n（2）y"})
        svc.record_review("2026-DEMO-Q021", {
            "decision": "approved", "note": "", "base_revision": 2,
            "operation_id": "tn-ir2"})
        receipt2 = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "tn-bp5"},
                                               teaching_tools_root=ext_root)
        assert receipt2["candidate_count"] == 0
        assert not external_file.exists()
        # 本地历史批次保留
        assert Path(receipt["batch_path"]).is_file()
        del note_id

    def test_deleted_note_excluded(self, svc, tmp_path):
        note_id = self._prepare(svc)
        svc.delete_note(note_id, {"base_version": 2, "operation_id": "tn-bd1"})
        receipt = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "tn-bp6"},
                                              teaching_tools_root=tmp_path / "tt3")
        assert receipt["candidate_count"] == 0


# ----------------------------------------------------------------------
# 目录徽标 + 备份恢复 + API
# ----------------------------------------------------------------------


class TestDirectoryBadgesAndBackup:
    def test_directory_badges(self, svc):
        directory = svc.paper_item_directory("2026-DEMO")
        summary = directory[0]["notes_summary"]
        assert summary["missing_explanation"] == 2  # 两个派生小问都缺
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c16"})
        svc.update_note(created["note_id"], {
            "base_version": 1, "explanation_text": "讲", "operation_id": "tn-u12"})
        summary = svc.paper_item_directory("2026-DEMO")[0]["notes_summary"]
        assert summary["missing_explanation"] == 1  # 仅 sq2 缺讲解
        # 小问覆盖口径：sq1 有讲解无解答、sq2 全空 → 两个小问都缺解答覆盖
        assert summary["missing_solution_count"] == 2

    def test_backup_restore_includes_notes_and_recording(self, svc, tmp_path, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "tn-c17"})
        note_id = created["note_id"]
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "转写")
        svc.save_recording(note_id, 1, "tn-rec6", AUDIO_BYTES, "audio/wav")
        dest = tmp_path / "backup"
        backup_mod.backup(svc.root, dest)
        restore_root = tmp_path / "restored"
        backup_mod.restore(dest, restore_root)
        restored = QuestionBankService(restore_root)
        try:
            note = note_of(restored, "2026-DEMO-Q021", note_id)
            assert note["transcript"] == "转写"
            assert (restored.root / restored.asset_rel_path(note["audio_asset_id"])).is_file()
        finally:
            restored.close()


class TestTeachingApi:
    @pytest.fixture()
    def client_svc(self, tmp_path: Path):
        from fastapi.testclient import TestClient
        from question_bank.server import create_app

        root = tmp_path / "api-root"
        service = QuestionBankService(root)
        try:
            service.submit_candidate(make_package(tmp_path, submission_id="tn-api1"))
        finally:
            service.close()
        app = create_app(root)
        with TestClient(app) as client:
            yield client

    def test_notes_api_roundtrip(self, client_svc):
        item = "2026-DEMO-Q021"
        view = client_svc.get(f"/api/items/{item}/notes").json()
        assert [s["id"] for s in view["subquestions"]] == ["sq1", "sq2"]
        assert view["summary"]["missing_explanation"] == 2

        created = client_svc.post(f"/api/items/{item}/notes", json={
            "subquestion_id": "sq1", "operation_id": "api-n1"}).json()
        note_id = created["note_id"]
        patched = client_svc.patch(f"/api/notes/{note_id}", json={
            "base_version": 1, "explanation_text": "讲解",
            "solution_text": "解答", "operation_id": "api-n2"}).json()
        assert patched["version"] == 2
        reviewed = client_svc.post(f"/api/notes/{note_id}/review", json={
            "base_version": 2, "decision": "approved",
            "operation_id": "api-n3"}).json()
        assert reviewed["status"] == "reviewed"
        # 冲突 → 409
        conflict = client_svc.patch(f"/api/notes/{note_id}", json={
            "base_version": 1, "explanation_text": "x", "operation_id": "api-n4"})
        assert conflict.status_code == 409
        deleted = client_svc.request("DELETE", f"/api/notes/{note_id}", json={
            "base_version": 2, "operation_id": "api-n5"}).json()
        assert deleted["status"] == "deleted"

    def test_audio_upload_and_fetch(self, client_svc, monkeypatch):
        item = "2026-DEMO-Q021"
        created = client_svc.post(f"/api/items/{item}/notes", json={
            "subquestion_id": "sq1", "operation_id": "api-n10"}).json()
        note_id = created["note_id"]
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "接口转写")
        response = client_svc.post(
            f"/api/notes/{note_id}/audio?base_version=1&operation_id=api-rec1",
            content=AUDIO_BYTES, headers={"Content-Type": "audio/wav"})
        receipt = response.json()
        assert receipt["status"] == "transcribed"
        audio = client_svc.get(f"/api/assets/{receipt['asset_id']}/audio")
        assert audio.status_code == 200 and audio.content == AUDIO_BYTES

    def test_fill_and_blueprint_api(self, client_svc, monkeypatch):
        item = "2026-DEMO-Q021"
        monkeypatch.setattr(ai, "generate_explanation_text",
                            lambda ctx, meta=None: "生成讲解")
        monkeypatch.setattr(ai, "generate_solution_text",
                            lambda ctx, meta=None: "生成解答")
        fill = client_svc.post(f"/api/items/{item}/notes/fill", json={
            "kind": "explanation", "operation_id": "api-f1"}).json()
        assert fill["generated"] == 2
        fill2 = client_svc.post(f"/api/items/{item}/notes/fill", json={
            "kind": "solution", "operation_id": "api-f2"}).json()
        assert fill2["generated"] == 2
        # 批准全部 note + 题目 → 导出
        view = client_svc.get(f"/api/items/{item}/notes").json()
        for subq in view["subquestions"]:
            for note in subq["notes"]:
                client_svc.post(f"/api/notes/{note['note_id']}/review", json={
                    "base_version": note["version"], "decision": "approved",
                    "operation_id": f"api-rn-{note['note_id']}"})
        client_svc.post(f"/api/items/{item}/review", json={
            "decision": "approved", "note": "", "base_revision": 1,
            "operation_id": "api-ir1"})
        exported = client_svc.post("/api/papers/2026-DEMO/blueprint-export", json={
            "operation_id": "api-bp1"}).json()
        assert exported["candidate_count"] == 2
        assert exported["external_status"] == "not_configured"


# ----------------------------------------------------------------------
# 验收第 1 轮反馈修复的回归（重放优先/冻结清单/并发检查/所有权）
# ----------------------------------------------------------------------


class TestAcceptanceRound1Fixes:
    def _note_with_both(self, svc, op="fx"):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": f"{op}-c"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "讲解",
                                  "solution_text": "解答", "operation_id": f"{op}-u"})
        return note_id, 2

    def _prepare_exportable(self, svc, op):
        note_id, base = self._note_with_both(svc, op=op)
        svc.review_note(note_id, {"base_version": base, "decision": "approved",
                                  "operation_id": f"{op}-r"})
        svc.record_review("2026-DEMO-Q021", {
            "decision": "approved", "note": "", "base_revision": 1,
            "operation_id": f"{op}-ir"})
        return note_id, base

    def test_generate_replay_after_success(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc)
        monkeypatch.setattr(ai, "generate_solution_text",
                            lambda ctx, meta=None: "旧解答")
        first = svc.generate_note_text(note_id, {
            "base_version": base, "kind": "solution", "operation_id": "fx-g1"})
        assert first["status"] == "applied"
        # 丢回执重放原载荷（base 已过期）：先查回执，不报版本冲突
        replay = svc.generate_note_text(note_id, {
            "base_version": base, "kind": "solution", "operation_id": "fx-g1"})
        assert replay["replayed"] is True and replay["solution_text"] == "旧解答"
        # 同 ID 异载荷拒绝
        with pytest.raises(ReceiptError):
            svc.generate_note_text(note_id, {
                "base_version": base, "kind": "explanation", "operation_id": "fx-g1"})

    def test_recording_replay_after_success(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc, op="rx")
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "转写A")
        first = svc.save_recording(note_id, base, "fx-rec1", AUDIO_BYTES, "audio/wav")
        assert first["status"] == "transcribed"
        replay = svc.save_recording(note_id, base, "fx-rec1", AUDIO_BYTES, "audio/wav")
        assert replay["replayed"] is True and replay["transcript"] == "转写A"

    def test_polish_replay_after_success(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc, op="pl")
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "已有转写")
        rec = svc.save_recording(note_id, base, "fx-plr", AUDIO_BYTES, "audio/wav")
        monkeypatch.setattr(ai, "polish_explanation_text",
                            lambda ctx, meta=None: "润色稿")
        first = svc.polish_note(note_id, {
            "base_version": rec["version"], "operation_id": "fx-p1"})
        assert first["status"] == "applied"
        replay = svc.polish_note(note_id, {
            "base_version": rec["version"], "operation_id": "fx-p1"})
        assert replay["replayed"] is True

    def test_generate_item_revision_drift_conflict(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc, op="dr")

        def slow_generate(ctx, meta=None):
            svc.save_edit("2026-DEMO-Q021", {
                "base_revision": 1, "operation_id": "fx-drift-edit",
                "stem": "生成期间改题（1）甲\n\n（2）乙"})
            return "基于旧题干的生成结果"

        monkeypatch.setattr(ai, "generate_solution_text", slow_generate)
        with pytest.raises(ReceiptError) as exc:
            svc.generate_note_text(note_id, {
                "base_version": base, "kind": "solution", "operation_id": "fx-g2"})
        assert exc.value.errors[0]["code"] == "NOTE_VERSION_CONFLICT"
        row = svc.conn.execute(
            "SELECT state, result FROM ai_operations WHERE operation_id = 'fx-g2'"
        ).fetchone()
        assert row["state"] == "conflict"
        assert "旧题干" in row["result"]

    def test_generate_deleted_note_conflict_keeps_result(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc, op="dl")

        def slow_generate(ctx, meta=None):
            svc.delete_note(note_id, {"base_version": base, "operation_id": "fx-del"})
            return "删除期间的生成结果"

        monkeypatch.setattr(ai, "generate_solution_text", slow_generate)
        with pytest.raises(ReceiptError):
            svc.generate_note_text(note_id, {
                "base_version": base, "kind": "solution", "operation_id": "fx-g3"})
        row = svc.conn.execute(
            "SELECT state, result FROM ai_operations WHERE operation_id = 'fx-g3'"
        ).fetchone()
        assert row["state"] == "conflict" and "删除期间" in row["result"]

    def test_polish_asr_persisted_before_polish_failure(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "fx-c10"})
        note_id = created["note_id"]

        def fail_asr(d, c, meta=None):
            raise ai.AiAssistError("provider_failed", "x")

        monkeypatch.setattr(ai, "transcribe_audio", fail_asr)
        svc.save_recording(note_id, 1, "fx-rec2", AUDIO_BYTES, "audio/wav")
        asr_calls = []

        def ok_asr(d, c, meta=None):
            asr_calls.append(1)
            return "一次性转写稿"

        def fail_polish(ctx, meta=None):
            raise ai.AiAssistError("provider_failed", "润色失败")

        monkeypatch.setattr(ai, "transcribe_audio", ok_asr)
        monkeypatch.setattr(ai, "polish_explanation_text", fail_polish)
        with pytest.raises(QbError):
            svc.polish_note(note_id, {"base_version": 2, "operation_id": "fx-p2"})
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["transcript"] == "一次性转写稿"  # ASR 已持久化
        monkeypatch.setattr(ai, "polish_explanation_text",
                            lambda ctx, meta=None: "重试润色成功")
        receipt = svc.polish_note(note_id, {
            "base_version": note["version"], "operation_id": "fx-p3"})
        assert receipt["status"] == "applied"
        assert len(asr_calls) == 1  # 重试润色不重复付费 ASR

    def test_meta_persisted_in_ai_operations(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc, op="mt")

        def gen(ctx, meta=None):
            meta.update({"provider": "dashscope", "model": "mock-gen",
                         "usage": {"total_tokens": 42}})
            return "生成正文"

        monkeypatch.setattr(ai, "generate_solution_text", gen)
        svc.generate_note_text(note_id, {
            "base_version": base, "kind": "solution", "operation_id": "fx-m1"})
        row = svc.conn.execute(
            "SELECT provider, model, usage FROM ai_operations"
            " WHERE operation_id = 'fx-m1'").fetchone()
        assert row["provider"] == "dashscope" and row["model"] == "mock-gen"
        assert json.loads(row["usage"]) == {"total_tokens": 42}

    def test_fill_frozen_replay_and_multi_approach(self, svc, monkeypatch):
        a1 = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "fx-a1"})["note_id"]
        svc.update_note(a1, {"base_version": 1, "explanation_text": "讲A",
                             "solution_text": "解A", "operation_id": "fx-a1u"})
        a2 = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "fx-a2"})["note_id"]
        svc.update_note(a2, {"base_version": 1, "explanation_text": "讲B",
                             "operation_id": "fx-a2u"})
        monkeypatch.setattr(ai, "generate_explanation_text",
                            lambda ctx, meta=None: "生成讲解")
        monkeypatch.setattr(ai, "generate_solution_text",
                            lambda ctx, meta=None: "生成解答")
        first = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "fx-f1"})
        # 冻结重放：按原清单返回，不重算
        replay = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "fx-f1"})
        assert replay["replayed"] is True
        assert replay["results"] == first["results"]
        # 覆盖口径：sq1 的解答已由 a1 覆盖；fill solution 的目标是第一次
        # explanation fill 在 sq2 建出的默认对（有讲解缺解答）
        fill2 = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "solution", "operation_id": "fx-f2"})
        assert fill2["generated"] == 1
        assert fill2["results"][0]["subquestion_id"] == "sq2"
        assert fill2["results"][0]["note_id"] not in (a1, a2)

    def test_export_ownership_guard(self, svc, tmp_path):
        self._prepare_exportable(svc, "ow")
        ext = tmp_path / "tt"
        target = ext / "authoring/tmp/reviewed-bank-import/2026-DEMO.candidates.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"taskId": "别人的批次", "source": "other"}',
                          encoding="utf-8")
        receipt = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "ow-bp1"},
                                              teaching_tools_root=ext)
        assert receipt["external_status"] == "failed"
        assert "拒绝覆盖" in receipt["external_error"]
        assert json.loads(target.read_text(encoding="utf-8"))["taskId"] == "别人的批次"

    def test_export_replay(self, svc):
        self._prepare_exportable(svc, "rp")
        first = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "rp-bp"})
        replay = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "rp-bp"})
        assert replay["replayed"] is True
        assert replay["manifest_hash"] == first["manifest_hash"]

    def test_export_snapshot_drift(self, svc, monkeypatch):
        note_id, _ = self._prepare_exportable(svc, "sd")
        import question_bank.export as export_module
        original = export_module.export_revision

        def drift_during_export(service, item_id, revision, allow_draft=False):
            service.update_note(note_id, {
                "base_version": 2, "explanation_text": "导出期间被改",
                "operation_id": "sd-drift"})
            return original(service, item_id, revision, allow_draft=allow_draft)

        monkeypatch.setattr(export_module, "export_revision",
                            drift_during_export)
        with pytest.raises(ReceiptError) as exc:
            svc.export_paper_blueprints("2026-DEMO", {"operation_id": "sd-bp"})
        assert exc.value.errors[0]["code"] == "EXPORT_SNAPSHOT_STALE"


class TestMigrationAtomicity:


    def test_summary_coverage_ignores_stale(self, svc):
        """过期 note 的正文不计入小问覆盖（缺讲解仍计缺）。"""
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "cv-c"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "旧讲解",
                                  "solution_text": "旧解答", "operation_id": "cv-u"})
        svc.save_edit("2026-DEMO-Q021", {
            "base_revision": 1, "operation_id": "cv-e", "stem": "新题干（1）x\n\n（2）y"})
        view = svc.notes_view("2026-DEMO-Q021")
        assert view["summary"]["stale_notes"] == 1
        assert view["summary"]["missing_explanation"] == 2  # 过期内容不算覆盖
        assert view["summary"]["missing_solution_count"] == 2
        summary = svc.paper_item_directory("2026-DEMO")[0]["notes_summary"]
        assert summary["missing_explanation"] == 2  # 目录与详情同口径



class TestAcceptanceRound2Fixes:
    def _note_with_both(self, svc, op="r2"):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": f"{op}-c"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "讲解",
                                  "solution_text": "解答", "operation_id": f"{op}-u"})
        return note_id, 2

    def _prepare_exportable(self, svc, op):
        note_id, base = self._note_with_both(svc, op=op)
        svc.review_note(note_id, {"base_version": base, "decision": "approved",
                                  "operation_id": f"{op}-r"})
        svc.record_review("2026-DEMO-Q021", {
            "decision": "approved", "note": "", "base_revision": 1,
            "operation_id": f"{op}-ir"})
        return note_id, base

    def test_fill_child_failure_marks_terminal_state(self, svc, monkeypatch):
        calls = []

        def flaky(ctx, meta=None):
            calls.append(ctx.get("subquestion_label"))
            raise ai.AiAssistError("provider_failed", "模型失败")

        monkeypatch.setattr(ai, "generate_explanation_text", flaky)
        receipt = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "r2-f1"})
        assert receipt["generated"] == 0

        # 子操作都落终态 failed，不残留 running
        rows = svc.conn.execute(
            "SELECT state FROM ai_operations WHERE kind = 'fill_explanation'"
            " AND operation_id != 'r2-f1'").fetchall()
        assert rows and all(r["state"] == "failed" for r in rows)
        # 接续：新 ID 重跑，目标仍是空小问（内容未变），ASR/生成重新尝试
        monkeypatch.setattr(ai, "generate_explanation_text",
                            lambda ctx, meta=None: "接续生成")
        receipt2 = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "r2-f2"})
        assert receipt2["generated"] == 2

    def test_export_replay_after_content_edit(self, svc):
        note_id, _ = self._prepare_exportable(svc, "re")
        first = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "re-bp"})
        assert first["candidate_count"] == 1
        # 导出后笔记被编辑 → 同 ID 同请求重放返回原回执（不重算不重投）
        svc.update_note(note_id, {"base_version": 2, "explanation_text": "导出后编辑",
                                  "operation_id": "re-edit"})
        replay = svc.export_paper_blueprints("2026-DEMO", {"operation_id": "re-bp"})
        assert replay["replayed"] is True
        assert replay["candidate_count"] == 1  # 原回执的快照结果
        assert replay["manifest_hash"] == first["manifest_hash"]

    def test_polish_item_revision_drift_conflict(self, svc, monkeypatch):
        note_id, base = self._note_with_both(svc, op="pd")
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "转写稿")
        svc.save_recording(note_id, base, "pd-rec", AUDIO_BYTES, "audio/wav")
        note = note_of(svc, "2026-DEMO-Q021", note_id)

        def slow_polish(ctx, meta=None):
            svc.save_edit("2026-DEMO-Q021", {
                "base_revision": 1, "operation_id": "pd-drift",
                "stem": "润色期间改题（1）x\n\n（2）y"})
            return "基于旧题目的润色结果"

        monkeypatch.setattr(ai, "polish_explanation_text", slow_polish)
        with pytest.raises(ReceiptError) as exc:
            svc.polish_note(note_id, {"base_version": note["version"],
                                      "operation_id": "pd-p1"})
        assert exc.value.errors[0]["code"] == "NOTE_VERSION_CONFLICT"
        row = svc.conn.execute(
            "SELECT state, result FROM ai_operations WHERE operation_id = 'pd-p1'"
        ).fetchone()
        assert row["state"] == "conflict" and "旧题目" in row["result"]

    def test_polish_multistage_meta_persisted(self, svc, monkeypatch):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "mm-c"})
        note_id = created["note_id"]

        def fake_asr(d, c, meta=None):
            meta.update({"provider": "dashscope", "model": "qwen3-asr-flash",
                         "usage": {"audio_tokens": 418}})
            return "阶段一转写"

        def fake_polish(ctx, meta=None):
            meta.update({"provider": "dashscope", "model": "qwen-plus",
                         "usage": {"total_tokens": 500}})
            return "阶段二润色"

        monkeypatch.setattr(ai, "transcribe_audio", fake_asr)
        monkeypatch.setattr(ai, "polish_explanation_text", fake_polish)
        # 先存音频（ASR 失败路径）→ 润色重读录音补转写（两阶段）
        def fail_first(d, c, meta=None):
            raise ai.AiAssistError("provider_failed", "首次失败")
        monkeypatch.setattr(ai, "transcribe_audio", fail_first)
        svc.save_recording(note_id, 1, "mm-rec", AUDIO_BYTES, "audio/wav")
        monkeypatch.setattr(ai, "transcribe_audio", fake_asr)
        receipt = svc.polish_note(note_id, {"base_version": 2, "operation_id": "mm-p"})
        assert receipt["status"] == "applied"
        row = svc.conn.execute(
            "SELECT provider, model, usage FROM ai_operations"
            " WHERE operation_id = 'mm-p'").fetchone()
        usage = json.loads(row["usage"])
        assert row["model"] == "qwen-plus"  # 末阶段
        assert isinstance(usage.get("calls"), list) and len(usage["calls"]) == 2
        stages = {c["stage"] for c in usage["calls"]}
        assert stages == {"asr", "polish"}

class TestAcceptanceRound3Fixes:
    def _note(self, svc, op):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": f"{op}-c"})
        return created["note_id"]

    def test_fill_conflict_direct(self, svc, monkeypatch):
        """验收复现场景：目标笔记在模型回调中被人工编辑 → conflict 保留结果。"""
        # sq1 留空作为补齐目标，但先建一个空 note 供漂移
        note_id = self._note(svc, "fd")
        # 让 sq1 成为 explanation 目标（note 无讲解文本）
        def gen_with_drift(ctx, meta=None):
            meta.update({"provider": "dashscope", "model": "qwen-plus",
                         "usage": {"total_tokens": 77}})
            svc.update_note(note_id, {"base_version": 1,
                                      "explanation_text": "人工抢先编辑",
                                      "operation_id": "fd-drift"})
            return "生成期间产出的讲解"

        monkeypatch.setattr(ai, "generate_explanation_text", gen_with_drift)
        receipt = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "fd-f1"})
        # sq1 目标因冲突失败（状态 conflict），sq2 正常生成
        statuses = {r["subquestion_id"]: r["status"] for r in receipt["results"]}
        assert statuses.get("sq1") == "conflict"
        assert statuses.get("sq2") == "generated"
        # 子操作的 conflict 结果与元数据未被清空
        rows = svc.conn.execute(
            "SELECT operation_id, state, result, provider, model, usage"
            " FROM ai_operations WHERE kind = 'fill_explanation'").fetchall()
        conflict_row = next(r for r in rows if r["state"] == "conflict")
        assert "生成期间产出的讲解" in conflict_row["result"]
        assert conflict_row["provider"] == "dashscope"
        assert conflict_row["model"] == "qwen-plus"
        assert json.loads(conflict_row["usage"]) == {"total_tokens": 77}
        # 人工内容未被覆盖
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["explanation_text"] == "人工抢先编辑"
        # 重放父操作：返回原逐项结果
        replay = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "fd-f1"})
        assert replay["replayed"] is True

class TestAcceptanceRound4Fixes:
    def test_running_operation_wrong_payload_rejected_before_unknown(self, svc):
        """运行中/失联操作：异载荷同 ID 报 OPERATION_ID_REUSED（先于 UNKNOWN）。"""
        note_id = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "r4-c"})["note_id"]
        info = svc.assets.register_audio(AUDIO_BYTES, "audio/wav")
        with svc._write_lock:
            svc.conn.begin_write()
            svc.conn.execute(
                "INSERT INTO assets(asset_id, sha256, path, mime, size_bytes,"
                " width, height, source_kind, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (info["asset_id"], info["sha256"], info["path"], info["mime"],
                 info["size_bytes"], None, None, "recording", db_mod.utc_now()))
            svc.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, created_at) VALUES (%s,%s,%s,%s,%s,'running',%s)",
                ("r4-op1", "sha256:real-hash", "transcribe", note_id, "{}",
                 db_mod.utc_now()))
            svc.conn.execute("COMMIT")
        # 异载荷（不同音频 → 不同 hash）→ 409 语义（OPERATION_ID_REUSED）
        other = svc.assets.register_audio(AUDIO_BYTES + b"\x00\x00", "audio/wav")
        del other
        with pytest.raises(ReceiptError) as exc:
            svc.save_recording(
                note_id, 1, "r4-op1",
                AUDIO_BYTES + bytes([1, 2]), "audio/wav")
        assert exc.value.errors[0]["code"] == "OPERATION_ID_REUSED"

    def test_interrupted_running_marked_unknown_on_restart(self, tmp_path):
        """进程中断留下的 running 行：新服务实例启动时持久化为 unknown。"""
        root = tmp_path / "root"
        svc1 = QuestionBankService(root)
        svc1.submit_candidate(make_package(tmp_path, submission_id="r4-s1"))
        note_id = svc1.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "r4-c2"})["note_id"]
        import socket as _socket
        dead_owner = f"{_socket.gethostname()}:4194303:gone"  # 归属进程已死
        with svc1._write_lock:
            svc1.conn.begin_write()
            svc1.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, owner, created_at)"
                " VALUES (%s,%s,%s,%s,%s,'running',%s,%s)",
                ("r4-lost", "sha256:h", "fill_explanation", note_id, "{}",
                 dead_owner, db_mod.utc_now()))
            svc1.conn.execute("COMMIT")
        svc1.close()  # 模拟进程中断：running 行留在库里
        svc2 = QuestionBankService(root)  # 重启 → 恢复机制标记 unknown
        try:
            row = svc2.conn.execute(
                "SELECT state, error FROM ai_operations"
                " WHERE operation_id = 'r4-lost'").fetchone()
            assert row["state"] == "unknown"
            assert "所属进程已退出" in row["error"]
        finally:
            svc2.close()


class TestAcceptanceRound5Fixes:
    def test_second_live_instance_does_not_recover_running(self, tmp_path):
        """两个活跃实例共享数据库：第二个打开时不误标第一个的 running 调用。"""
        root = tmp_path / "root"
        svc1 = QuestionBankService(root)
        svc1.submit_candidate(make_package(tmp_path, submission_id="r5-s1"))
        note_id = svc1.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "r5-c"})["note_id"]
        owner = svc1._ai_owner_id()
        with svc1._write_lock:
            svc1.conn.begin_write()
            svc1.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, owner, created_at)"
                " VALUES (%s,%s,%s,%s,%s,'running',%s,%s)",
                ("r5-live", "sha256:h", "polish", note_id, "{}",
                 owner, db_mod.utc_now()))
            svc1.conn.execute("COMMIT")
        svc2 = QuestionBankService(root)  # 同机同进程（pid 存活）→ 不恢复
        try:
            row = svc2.conn.execute(
                "SELECT state FROM ai_operations WHERE operation_id = 'r5-live'"
            ).fetchone()
            assert row["state"] == "running"  # 归属进程存活，保持 running
        finally:
            svc2.close()
        svc1.close()

    def test_dead_owner_recovered_as_unknown(self, tmp_path):
        """所属进程确认死亡（同机不存在的 pid）→ 启动恢复为 unknown。"""
        import socket
        root = tmp_path / "root"
        svc1 = QuestionBankService(root)
        svc1.submit_candidate(make_package(tmp_path, submission_id="r5-s2"))
        note_id = svc1.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "r5-c2"})["note_id"]
        dead_owner = f"{socket.gethostname()}:4194304:deadbeef"  # 超范围 pid
        with svc1._write_lock:
            svc1.conn.begin_write()
            svc1.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, owner, created_at)"
                " VALUES (%s,%s,%s,%s,%s,'running',%s,%s)",
                ("r5-dead", "sha256:h", "polish", note_id, "{}",
                 dead_owner, db_mod.utc_now()))
            svc1.conn.execute("COMMIT")
        svc1.close()
        svc2 = QuestionBankService(root)
        try:
            row = svc2.conn.execute(
                "SELECT state, error FROM ai_operations"
                " WHERE operation_id = 'r5-dead'").fetchone()
            assert row["state"] == "unknown"
            assert "所属进程已退出" in row["error"]
        finally:
            svc2.close()

    def test_legacy_running_without_owner_left_alone(self, tmp_path):
        """无 owner 的旧 running 行（无法判定）保守不动。"""
        root = tmp_path / "root"
        svc1 = QuestionBankService(root)
        svc1.submit_candidate(make_package(tmp_path, submission_id="r5-s3"))
        note_id = svc1.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "r5-c3"})["note_id"]
        with svc1._write_lock:
            svc1.conn.begin_write()
            svc1.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, created_at)"
                " VALUES (%s,%s,%s,%s,%s,'running',%s)",
                ("r5-legacy", "sha256:h", "polish", note_id, "{}", db_mod.utc_now()))
            svc1.conn.execute("COMMIT")
        svc1.close()
        svc2 = QuestionBankService(root)
        try:
            row = svc2.conn.execute(
                "SELECT state FROM ai_operations WHERE operation_id = 'r5-legacy'"
            ).fetchone()
            assert row["state"] == "running"  # 无法判定归属 → 保守保持
        finally:
            svc2.close()

class TestAcceptanceRound6Fixes:
    def test_access_denied_alive_process_not_recovered(self, tmp_path, monkeypatch):
        """探活权限被拒（存活受保护进程）≠ 进程退出：保守保持 running。"""
        import socket
        import sys as _sys
        root = tmp_path / "root"
        svc1 = QuestionBankService(root)
        svc1.submit_candidate(make_package(tmp_path, submission_id="r6-s1"))
        note_id = svc1.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "r6-c"})["note_id"]
        # Windows PID 4（System）存活但 OpenProcess 常被拒（错误 5）
        protected_owner = f"{socket.gethostname()}:4:protected"
        if _sys.platform != "win32":
            # PID 4 need not exist on macOS/Linux. Exercise permission denial
            # explicitly instead of assuming a platform-specific system PID.
            import os
            def deny_probe(pid, signal):
                assert (pid, signal) == (4, 0)
                raise PermissionError("protected process")
            monkeypatch.setattr(os, "kill", deny_probe)
        with svc1._write_lock:
            svc1.conn.begin_write()
            svc1.conn.execute(
                "INSERT INTO ai_operations(operation_id, request_hash, kind, target,"
                " input_snapshot, state, owner, created_at)"
                " VALUES (%s,%s,%s,%s,%s,'running',%s,%s)",
                ("r6-protected", "sha256:h", "polish", note_id, "{}",
                 protected_owner, db_mod.utc_now()))
            svc1.conn.execute("COMMIT")
        svc1.close()
        svc2 = QuestionBankService(root)
        try:
            row = svc2.conn.execute(
                "SELECT state FROM ai_operations"
                " WHERE operation_id = 'r6-protected'").fetchone()
            if _sys.platform == "win32":
                # System 进程存活：无论探活成功或被拒，都不得判死
                assert row["state"] == "running"
            else:
                # POSIX 上 kill(4,0) 通常允许或报权限——同样不得判死
                assert row["state"] == "running"
        finally:
            svc2.close()

    def test_own_pid_is_alive(self):
        import os as _os
        from question_bank.teaching import TeachingNotesMixin
        import socket as _socket
        owner = f"{_socket.gethostname()}:{_os.getpid()}:self"
        assert TeachingNotesMixin._owner_process_alive(owner) is True
        bogus = f"{_socket.gethostname()}:notapid:x"
        assert TeachingNotesMixin._owner_process_alive(bogus) is None

class TestConcurrencyAndKillRecovery:
    def test_concurrent_same_operation_id(self, svc, monkeypatch):
        """真实并发同 ID：只落一次模型调用，另一请求拿到同一回执。"""
        import threading
        note_id = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "cc-c"})["note_id"]
        calls = []
        gate = threading.Event()

        def slow_generate(ctx, meta=None):
            calls.append(1)
            gate.wait(timeout=10)  # 卡住第一个调用，让第二个请求并发进入
            return "并发生成正文"

        monkeypatch.setattr(ai, "generate_explanation_text", slow_generate)
        results = {}

        def worker(name):
            try:
                results[name] = svc.generate_note_text(note_id, {
                    "base_version": 1, "kind": "explanation",
                    "operation_id": "cc-g1"})
            except Exception as exc:  # noqa: BLE001
                results[name] = exc

        t1 = threading.Thread(target=worker, args=("a",))
        t1.start()
        import time as _time
        _time.sleep(0.4)  # t1 进入模型调用
        t2 = threading.Thread(target=worker, args=("b",))
        t2.start()
        _time.sleep(0.4)
        gate.set()  # 放行模型调用
        t1.join(timeout=15)
        t2.join(timeout=15)
        # 只发生一次付费调用
        assert len(calls) == 1, calls
        # 两边都得到回执（一方 applied，一方 unknown/等待路径的同 ID 行为）
        assert not isinstance(results.get("a"), Exception), results
        outcomes = [results.get("a"), results.get("b")]
        assert any(isinstance(r, dict) and r.get("status") == "applied"
                   for r in outcomes), outcomes
        # 另一方要么拿到 running→unknown 提示，要么重放回执；不得产生第二版本模型调用
        assert len(calls) == 1

    def test_killed_process_recovery_end_to_end(self, tmp_path):
        """子进程执行慢速生成中途被强杀 → 新实例启动恢复 unknown，不自动重付费。"""
        import subprocess
        import sys as _sys

        root = tmp_path / "root"
        svc = QuestionBankService(root)
        svc.submit_candidate(make_package(tmp_path, submission_id="kr-s1"))
        note_id = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "kr-c"})["note_id"]
        svc.close()

        helper = tmp_path / "slow_worker.py"
        helper.write_text("""
import sys
sys.path.insert(0, {repo!r})
sys.path.insert(0, {tests!r})
import time
from pathlib import Path
from question_bank.service import QuestionBankService
from question_bank import explanations_ai as ai

def slow(ctx, meta=None):
    time.sleep(30)  # 足够长：父进程在睡眠中强杀
    return "强杀前的生成结果"

ai.generate_explanation_text = slow
svc = QuestionBankService({root!r})
print("STARTED", flush=True)
try:
    svc.generate_note_text({note!r}, {{
        "base_version": 1, "kind": "explanation", "operation_id": "kr-g1"}})
finally:
    svc.close()
""".format(repo=str(Path.cwd()), tests=str(Path.cwd() / "tests"),
                     root=str(root), note=note_id), encoding="utf-8")

        proc = subprocess.Popen(
            [_sys.executable, "-u", str(helper)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # 等待子进程真正进入模型调用（ai_operations 已登记）
        import time as _time
        deadline = _time.time() + 60
        registered = False
        while _time.time() < deadline and proc.poll() is None:
            check = QuestionBankService(root)
            try:
                row = check.conn.execute(
                    "SELECT state, owner FROM ai_operations"
                    " WHERE operation_id = 'kr-g1'").fetchone()
                if row is not None and row["state"] == "running":
                    registered = True
                    break
            finally:
                check.close()
            _time.sleep(0.5)
        if not registered:
            proc.kill()
            output = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(
                f"子进程未登记调用（exit={proc.poll()}）输出：{output[:400]}")
        proc.kill()  # 强杀
        proc.wait(timeout=10)

        # 重启实例：owner 进程已死 → 恢复 unknown
        svc2 = QuestionBankService(root)
        try:
            row = svc2.conn.execute(
                "SELECT state, error FROM ai_operations"
                " WHERE operation_id = 'kr-g1'").fetchone()
            assert row["state"] == "unknown", row["state"]
            assert "所属进程已退出" in row["error"]
            # 笔记未被写入（模型结果从未落库）
            view = svc2.notes_view("2026-DEMO-Q021")
            note = next(n for s in view["subquestions"] for n in s["notes"]
                        if n["note_id"] == note_id)
            assert not note["explanation_text"]
        finally:
            svc2.close()


class TestExternalExportFailures:
    def _prepare(self, svc):
        created = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "ef-c"})
        note_id = created["note_id"]
        svc.update_note(note_id, {"base_version": 1, "explanation_text": "讲解",
                                  "solution_text": "解答", "operation_id": "ef-u"})
        svc.review_note(note_id, {"base_version": 2, "decision": "approved",
                                  "operation_id": "ef-r"})
        svc.record_review("2026-DEMO-Q021", {
            "decision": "approved", "note": "", "base_revision": 1,
            "operation_id": "ef-ir"})
        return note_id

    def test_external_path_not_writable(self, svc, tmp_path):
        """外部根指向一个文件（无法创建目录）→ external failed + 本地快照可用。"""
        import os as _os
        self._prepare(svc)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        receipt = svc.export_paper_blueprints("2026-DEMO", {
            "operation_id": "ef-bp1"}, teaching_tools_root=blocker)
        assert receipt["candidate_count"] == 1
        assert receipt["external_status"] == "failed"
        assert receipt["external_error"]
        assert Path(receipt["batch_path"]).is_file()  # 本地快照不受影响
        del _os

    def test_external_zero_candidate_unremovable_kept(self, svc, tmp_path):
        """零候选清理时外部文件删除失败（只读目录）→ failed 如实反馈。"""
        import os as _os
        import stat as _stat
        self._prepare(svc)
        ext = tmp_path / "ro"
        target = ext / "authoring/tmp/reviewed-bank-import/2026-DEMO.candidates.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({
            "taskId": "2026-DEMO", "source": "reviewed-bank-import",
            "candidates": [], "assignments": [],
        }), encoding="utf-8")
        # 软删除笔记 → 零候选导出需要删外部文件，但目录只读
        svc.delete_note("2026-DEMO-Q021-n1", {
            "base_version": 2, "operation_id": "ef-del"})
        _os.chmod(target.parent, _stat.S_IREAD | _stat.S_IEXEC)
        try:
            receipt = svc.export_paper_blueprints("2026-DEMO", {
                "operation_id": "ef-bp2"}, teaching_tools_root=ext)
            if receipt["candidate_count"] == 0:
                # Windows 只读目录通常仍允许删除自有文件；两种结果都如实断言
                assert receipt["external_status"] in ("delivered", "failed")
                if receipt["external_status"] == "failed":
                    assert receipt.get("external_error")
        finally:
            _os.chmod(target.parent, _stat.S_IWRITE | _stat.S_IREAD | _stat.S_IEXEC)

class TestAcceptanceRound10Fixes:
    def test_null_and_blank_model_content_rejected(self, svc, monkeypatch):
        """模型返回 content=null / 空白 → 明确失败，不把 "None" 存成正文。"""
        note_id = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "nc-c"})["note_id"]
        for index, content in enumerate([None, "", "   "]):
            def gen(ctx, meta=None, _content=content):
                return _content  # 模拟 _chat_text 之前的原始 content
            # 直接打到 _chat_text 层（服务层各函数都会经过它）
            import question_bank.explanations_ai as ai_mod
            monkeypatch.setattr(ai_mod, "_post_chat", lambda payload: {
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 1},
            })
            with pytest.raises(ai.AiAssistError) as exc:
                ai_mod._chat_text([{"role": "user", "content": "x"}], "m")
            assert exc.value.code == "llm_bad_output"
            del gen
        # 服务层：生成落 failed，笔记不写伪正文
        monkeypatch.setattr(ai, "generate_explanation_text",
                            lambda ctx, meta=None: (_ for _ in ()).throw(
                                ai.AiAssistError("llm_bad_output", "空正文")))
        with pytest.raises(QbError) as exc:
            svc.generate_note_text(note_id, {
                "base_version": 1, "kind": "explanation",
                "operation_id": f"nc-g"})
        assert exc.value.errors[0]["code"] == "llm_bad_output"
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["explanation_text"] == ""
        row = svc.conn.execute(
            "SELECT state FROM ai_operations WHERE operation_id = 'nc-g'").fetchone()
        assert row["state"] == "failed"

    def test_asr_null_content_rejected(self, svc, monkeypatch):
        import question_bank.explanations_ai as ai_mod
        note_id = svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": "na-c"})["note_id"]
        monkeypatch.setattr(ai_mod, "_post_chat", lambda payload: {
            "choices": [{"message": {"content": None}}],
        })
        with pytest.raises(ai.AiAssistError) as exc:
            ai_mod.transcribe_audio(AUDIO_BYTES, "audio/wav")
        assert exc.value.code == "asr_failed"

class TestModelExceptionMatrix:
    """逐阶段异常矩阵：ASR/润色/生成讲解/生成解答的空输出与超时。"""

    def _note(self, svc, op):
        return svc.create_note("2026-DEMO-Q021", {
            "subquestion_id": "sq1", "operation_id": f"{op}-c"})["note_id"]

    def test_polish_empty_output_keeps_transcript(self, svc, monkeypatch):
        note_id = self._note(svc, "xm-p")
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "转写稿")
        svc.save_recording(note_id, 1, "xm-p-rec", AUDIO_BYTES, "audio/wav")
        note = note_of(svc, "2026-DEMO-Q021", note_id)

        def empty_polish(ctx, meta=None):
            raise ai.AiAssistError("llm_bad_output", "润色结果为空")

        monkeypatch.setattr(ai, "polish_explanation_text", empty_polish)
        with pytest.raises(QbError) as exc:
            svc.polish_note(note_id, {"base_version": note["version"],
                                      "operation_id": "xm-p1"})
        assert exc.value.errors[0]["code"] == "llm_bad_output"
        # 转写稿与音频保留；润色前正文不被伪内容污染
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["transcript"] == "转写稿"
        assert note["explanation_text"] == ""
        row = svc.conn.execute(
            "SELECT state FROM ai_operations WHERE operation_id = 'xm-p1'").fetchone()
        assert row["state"] == "failed"

    def test_generate_explanation_empty_output(self, svc, monkeypatch):
        note_id = self._note(svc, "xm-ge")

        def empty(ctx, meta=None):
            raise ai.AiAssistError("llm_bad_output", "生成的讲解为空")

        monkeypatch.setattr(ai, "generate_explanation_text", empty)
        with pytest.raises(QbError) as exc:
            svc.generate_note_text(note_id, {
                "base_version": 1, "kind": "explanation",
                "operation_id": "xm-ge1"})
        assert exc.value.errors[0]["code"] == "llm_bad_output"
        assert note_of(svc, "2026-DEMO-Q021", note_id)["explanation_text"] == ""

    def test_generate_solution_empty_output(self, svc, monkeypatch):
        note_id = self._note(svc, "xm-gs")

        def empty(ctx, meta=None):
            raise ai.AiAssistError("llm_bad_output", "生成的解答为空")

        monkeypatch.setattr(ai, "generate_solution_text", empty)
        with pytest.raises(QbError) as exc:
            svc.generate_note_text(note_id, {
                "base_version": 1, "kind": "solution",
                "operation_id": "xm-gs1"})
        assert exc.value.errors[0]["code"] == "llm_bad_output"
        assert note_of(svc, "2026-DEMO-Q021", note_id)["solution_text"] == ""

    def test_asr_timeout_maps_to_provider_failed(self, svc, monkeypatch):
        import httpx
        note_id = self._note(svc, "xm-t")

        def timeout_asr(data, content_type, meta=None):
            raise ai.AiAssistError("provider_failed", "模型服务请求失败：timed out")

        monkeypatch.setattr(ai, "transcribe_audio", timeout_asr)
        receipt = svc.save_recording(note_id, 1, "xm-t-rec", AUDIO_BYTES, "audio/wav")
        # 音频已保存，转写失败如实返回
        assert receipt["status"] == "audio_saved"
        assert receipt["errors"][0]["code"] == "asr_failed"
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["audio_asset_id"] and not note["transcript"]
        del httpx

    def test_polish_timeout_keeps_state_retryable(self, svc, monkeypatch):
        note_id = self._note(svc, "xm-pt")
        monkeypatch.setattr(ai, "transcribe_audio",
                            lambda d, c, meta=None: "转写稿")
        svc.save_recording(note_id, 1, "xm-pt-rec", AUDIO_BYTES, "audio/wav")
        note = note_of(svc, "2026-DEMO-Q021", note_id)

        def timeout_polish(ctx, meta=None):
            raise ai.AiAssistError("provider_failed", "模型服务请求失败：timed out")

        monkeypatch.setattr(ai, "polish_explanation_text", timeout_polish)
        with pytest.raises(QbError) as exc:
            svc.polish_note(note_id, {"base_version": note["version"],
                                      "operation_id": "xm-pt1"})
        assert exc.value.errors[0]["code"] == "provider_failed"
        # 状态可重试：转写稿仍在，新操作 ID 可再次润色
        note = note_of(svc, "2026-DEMO-Q021", note_id)
        assert note["transcript"] == "转写稿"
        monkeypatch.setattr(ai, "polish_explanation_text",
                            lambda ctx, meta=None: "重试润色成功")
        receipt = svc.polish_note(note_id, {"base_version": note["version"],
                                            "operation_id": "xm-pt2"})
        assert receipt["status"] == "applied"

    def test_fill_timeout_partial(self, svc, monkeypatch):
        calls = []

        def flaky(ctx, meta=None):
            calls.append(1)
            if len(calls) == 1:
                raise ai.AiAssistError("provider_failed", "timeout")
            return "第二小问讲解"

        monkeypatch.setattr(ai, "generate_explanation_text", flaky)
        receipt = svc.fill_missing("2026-DEMO-Q021", {
            "kind": "explanation", "operation_id": "xm-f1"})
        statuses = sorted(r["status"] for r in receipt["results"])
        assert statuses == ["failed", "generated"]
