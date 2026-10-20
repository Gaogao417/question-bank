"""教学内容（讲解/解答对）业务层：老版 explanations 子系统的数据库版。

设计决定见 docs/question-bank-management-design.md §12 与
artifacts/review-ui-teaching-notes/acceptance-task.md，要点：

- 一条 teaching_note = 一个讲解/解答对（approach）；同一小问可多对；
  软删除保留版本、审核、音频与导出溯源。
- 版本只追加（(note_id, version) 主键）；title/ordinal/subquestion 归属也
  进版本（并发前置条件 base_version）。普通编辑保留 source_revision——
  不得借保存洗掉"来源已变化"；显式 readapt 或基于当前题重新生成才建立
  新来源版本，且需重新审批。
- 小问从三块正文 stem 派生（老版（1）/(2)/① 标记规则）；sqN 只作分组，
  同 ID 题干变化即过期（stem_fingerprint 比对）；不再匹配的 note 显示为
  额外小问，不删除、不自动重绑。
- 模型调用（转写/润色/生成）先登记 ai_operations 再调用，调用期间不持
  写事务；结果落库时复核 note 版本与删除状态，冲突保留模型结果为未应用；
  同 ID 重放不重复付费调用；远端成功本地未保存标 unknown。
- 笔记批准 ≠ 题目批准；blueprint 导出要求题目修订已批准 + 笔记版本已批准
  且来源匹配当前题目；导出 = 本地不可变快照 + 配置外部投递，零候选也要
  处理当前外部文件，不残留旧候选冒充当前结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from question_bank import db as db_mod
import tempfile
from pathlib import Path
from typing import Any

from question_bank import db as db_mod
from question_bank import explanations_ai as ai
from question_bank.canonical import canonical_json, sha256_bytes
from question_bank.errors import QbError, ReceiptError, error
from question_bank.models import OPERATION_ID_PATTERN

# 老版小问切分规则（.codex/skills/math-topic-question-bank 保持原样，此处等价移植）：
# 识别 （1）/(2)/① 三类标记；少于 2 个标记按单一"整题"处理。
_SUBQUESTION_MARKER = re.compile(
    r"（\s*[0-9一二三四五六七八九十]{1,3}\s*）"
    r"|\(\s*[0-9]{1,2}\s*\)"
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫]"
)

EXPLANATION_SOURCES = ("manual", "recorded", "polished", "generated")
SOLUTION_SOURCES = ("manual", "generated")


def derive_subquestions(stem: str) -> list[dict[str, Any]]:
    """从题干文本派生小问骨架（id/label/stem）；sqN 只作分组标识。"""
    text = str(stem or "").strip()
    matches = list(_SUBQUESTION_MARKER.finditer(text))
    if len(matches) < 2:
        return [{"id": "sq1", "label": "", "stem": text}]
    subquestions = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        subquestions.append({
            "id": f"sq{index + 1}",
            "label": match.group(0),
            "stem": text[match.start():end].strip(),
        })
    return subquestions


def stem_fingerprint(stem: str) -> str:
    return "sha256:" + sha256_bytes(str(stem or "").strip().encode("utf-8"))


def _validate_operation_id(operation_id: Any, what: str) -> str:
    value = str(operation_id or "").strip()
    if not OPERATION_ID_PATTERN.match(value):
        raise QbError([
            error("SCHEMA_INVALID",
                  f"{what} operation_id 只能包含字母、数字、下划线、连字符",
                  retryable=False)
        ])
    return value


def _ai_error(code: str, message: str) -> QbError:
    return QbError([error(code, message, retryable=False)])


class TeachingNotesMixin:
    """挂在 QuestionBankService 上：讲解/解答对的全部业务操作。"""

    def _ai_owner_id(self) -> str:
        import os as _os
        import uuid as _uuid
        if getattr(self, "_owner_id", None) is None:
            import socket as _socket
            self._owner_id = (
                f"{_socket.gethostname()}:{_os.getpid()}:{_uuid.uuid4().hex[:8]}")
        return self._owner_id

    @staticmethod
    def _owner_process_alive(owner: str | None) -> bool | None:
        """判定 owner 进程是否存活。

        返回 True=确认存活；False=确认不存在；None=无法判定（跨主机、
        无 owner、探活权限被拒等）——一律保守视为存活，不触发恢复。
        """
        import ctypes
        import os as _os
        import socket as _socket
        if not owner:
            return None  # 旧数据无归属：无法确认，保守不恢复
        parts = owner.split(":")
        if len(parts) < 2:
            return None
        host, pid_text = parts[0], parts[1]
        try:
            pid = int(pid_text)
        except ValueError:
            return None
        if host != _socket.gethostname():
            return None
        if pid <= 0:
            return False
        if _os.name == "nt":
            try:
                kernel32 = ctypes.WinDLL(
                    "kernel32", use_last_error=True)
            except OSError:
                return None
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not handle:
                # 句柄为空不等于进程不存在：ERROR_INVALID_PARAMETER(87) 才是
                # "没有此进程"；ERROR_ACCESS_DENIED(5) 等是"存活但无权查询"。
                error = ctypes.get_last_error()
                if error == 87:  # ERROR_INVALID_PARAMETER
                    return False
                return None  # 权限拒绝等不确定情形：保守视为存活
            # 句柄存在还要看退出码：被杀进程的内核对象在被引用期间仍可
            # 打开（僵尸态），ExitCode != STILL_ACTIVE 即已终止。
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            if not ok:
                return None  # 查询失败：保守视为存活
            STILL_ACTIVE = 259
            if exit_code.value == STILL_ACTIVE:
                return True
            return False
        try:
            _os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return None  # 存活但无权发信号：保守视为存活
        except OSError:
            return None

    def recover_interrupted_ai_operations(self) -> list[str]:
        """把确认失联的 running 调用持久化为 unknown。

        只恢复能确认失联的调用：owner 进程已死（同机 pid 探活）。
        其他实例仍在执行的调用保持 running；无法判定（跨主机/旧数据无
        owner）时保守不动。不自动重付费，重试须用新 operation_id。
        """
        rows = self.conn.execute(
            "SELECT operation_id, owner FROM ai_operations WHERE state = 'running'"
        ).fetchall()
        lost = [
            row["operation_id"] for row in rows
            if self._owner_process_alive(row["owner"]) is False
        ]
        if not lost:
            return []
        with self._write_lock:
            self.conn.begin_write()
            try:
                for op_id in lost:
                    self.conn.execute(
                        "UPDATE ai_operations SET state = 'unknown', error = %s,"
                        " completed_at = %s WHERE operation_id = %s AND state = 'running'",
                        ("所属进程已退出：远端结果未知，未自动重新调用付费接口",
                         db_mod.utc_now(), op_id),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self._rollback_quiet()
                raise
        return lost

    # ------------------------------------------------------------------
    # 查询：视图与摘要
    # ------------------------------------------------------------------

    def notes_view(self, item_id: str, revision: int | None = None) -> dict[str, Any]:
        item_row = self._item_row(item_id)
        if item_row is None or item_row["current_revision"] is None:
            raise QbError([
                error("ITEM_NOT_FOUND", f"题目不存在或尚无修订: {item_id}",
                      item_key=item_id, retryable=False)
            ])
        current = item_row["current_revision"]
        revision = revision or current
        content = self._content_of(item_id, revision)
        derived = derive_subquestions(content.stem)
        derived_by_id = {entry["id"]: entry for entry in derived}
        current_prints = {entry["id"]: stem_fingerprint(entry["stem"]) for entry in derived}

        notes = self.conn.execute(
            "SELECT * FROM teaching_notes WHERE item_id = %s AND deleted = 0"
            " ORDER BY subquestion_id, ordinal, note_id",
            (item_id,),
        ).fetchall()
        note_views = []
        for row in notes:
            version = row["current_version"]
            vrow = self._note_version_row(row["note_id"], version)
            decision = self._note_review_decision(row["note_id"], version)
            subq = derived_by_id.get(vrow["subquestion_id"])
            source_stale = (
                vrow["source_revision"] != current
                or (subq is None)
                or (vrow["stem_fingerprint"] != current_prints[vrow["subquestion_id"]])
            )
            note_views.append({
                "note_id": row["note_id"],
                "subquestion_id": vrow["subquestion_id"],
                "title": vrow["title"],
                "ordinal": vrow["ordinal"],
                "version": version,
                "explanation_text": vrow["explanation_text"],
                "explanation_source": vrow["explanation_source"],
                "transcript": vrow["transcript"],
                "audio_asset_id": vrow["audio_asset_id"],
                "solution_text": vrow["solution_text"],
                "solution_source": vrow["solution_source"],
                "review_status": decision or "pending",
                "source_revision": vrow["source_revision"],
                "source_stale": source_stale,
                "subquestion_label": vrow["subquestion_label"],
                "subquestion_stem": vrow["subquestion_stem"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })

        derived_ids = {entry["id"] for entry in derived}
        extra_ids = sorted({
            v["subquestion_id"] for v in note_views
            if v["subquestion_id"] not in derived_ids
        })
        subquestions = []
        for entry in derived:
            subquestions.append({
                "id": entry["id"],
                "label": entry["label"],
                "stem": entry["stem"],
                "notes": [v for v in note_views
                          if v["subquestion_id"] == entry["id"]],
            })
        for extra_id in extra_ids:
            sample = next(v for v in note_views if v["subquestion_id"] == extra_id)
            subquestions.append({
                "id": extra_id,
                "label": sample["subquestion_label"] or extra_id,
                "stem": sample["subquestion_stem"],
                "notes": [v for v in note_views if v["subquestion_id"] == extra_id],
                "extra": True,
            })

        return {
            "schema": "qb_teaching_notes/v1",
            "item_id": item_id,
            "item_revision": revision,
            "current_revision": current,
            "subquestions": subquestions,
            "summary": self._notes_summary(subquestions, note_views),
            "capabilities": {
                "server_ffmpeg": ai.ffmpeg_available(),
                "asr_configured": ai.api_key() is not None,
                "llm_configured": ai.api_key() is not None,
            },
        }

    @staticmethod
    def _notes_summary(subquestions: list, note_views: list) -> dict[str, Any]:
        """缺项按派生小问的当前可用覆盖计算：只有来源不过期的 note 计入覆盖；
        过期内容单列（stale_notes），不掩盖缺口。单个 approach 缺项在卡片状态
        里单独展示，不并入小问缺项计数。"""
        derived_groups = [s for s in subquestions if not s.get("extra")]
        missing_explanation = sum(
            1 for s in derived_groups
            if not any(v["explanation_text"].strip() and not v["source_stale"]
                       for v in s["notes"])
        )
        missing_solution = sum(
            1 for s in derived_groups
            if not any(v["solution_text"].strip() and not v["source_stale"]
                       for v in s["notes"])
        )
        approved_pairs = sum(
            1 for v in note_views
            if v["review_status"] == "approved" and not v["source_stale"]
        )
        stale_notes = sum(1 for v in note_views if v["source_stale"])
        return {
            "note_count": len(note_views),
            "missing_explanation": missing_explanation,
            "missing_solution_count": missing_solution,
            "approved_pairs": approved_pairs,
            "stale_notes": stale_notes,
        }

    def _note_row(self, note_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM teaching_notes WHERE note_id = %s", (note_id,)
        ).fetchone()
        if row is None:
            raise QbError([
                error("NOTE_NOT_FOUND", f"讲解-解答对不存在: {note_id}",
                      item_key=note_id, retryable=False)
            ])
        return row

    def _live_note_row(self, note_id: str) -> dict:
        row = self._note_row(note_id)
        if row["deleted"]:
            raise QbError([
                error("NOTE_DELETED",
                      f"讲解-解答对已删除: {note_id}",
                      item_key=note_id, retryable=False)
            ])
        return row

    def _note_version_row(self, note_id: str, version: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM teaching_note_versions WHERE note_id = %s AND version = %s",
            (note_id, version),
        ).fetchone()
        if row is None:
            raise QbError([
                error("NOTE_VERSION_NOT_FOUND",
                      f"笔记版本不存在: {note_id} v{version}",
                      item_key=note_id, retryable=False)
            ])
        return row

    def _note_review_decision(self, note_id: str, version: int) -> str | None:
        row = self.conn.execute(
            "SELECT decision FROM teaching_note_reviews"
            " WHERE note_id = %s AND version = %s"
            " ORDER BY review_id DESC LIMIT 1",
            (note_id, version),
        ).fetchone()
        return row["decision"] if row else None

    def _check_note_base(self, row: dict, base_version: Any) -> int:
        if not isinstance(base_version, int) or base_version <= 0:
            raise QbError([
                error("SCHEMA_INVALID", "base_version 必须是正整数",
                      item_key=row["note_id"], retryable=False)
            ])
        current = row["current_version"]
        if current is None or base_version != current:
            raise ReceiptError([
                error(
                    "NOTE_VERSION_CONFLICT",
                    f"基线 v{base_version} 不是当前版本 v{current}",
                    item_key=row["note_id"],
                    fix_hint="刷新讲解/解答后按当前版本重试",
                    retryable=False,
                )
            ])
        return current

    def _next_note_id(self, item_id: str) -> str:
        rows = self.conn.execute(
            "SELECT note_id FROM teaching_notes WHERE item_id = %s", (item_id,)
        ).fetchall()
        numbers = []
        for row in rows:
            match = re.search(r"-n(\d+)$", row["note_id"])
            if match:
                numbers.append(int(match.group(1)))
        return f"{item_id}-n{(max(numbers) + 1) if numbers else 1}"

    def _snapshot_for(self, item_id: str, subquestion_id: str) -> dict[str, Any]:
        """取当前题目修订下该小问的快照（label/stem/fingerprint/source_revision）。"""
        item_row = self._item_row(item_id)
        current = item_row["current_revision"]
        if current is None:
            raise QbError([
                error("ITEM_NOT_FOUND", f"题目尚无修订: {item_id}",
                      item_key=item_id, retryable=False)
            ])
        content = self._content_of(item_id, current)
        for entry in derive_subquestions(content.stem):
            if entry["id"] == subquestion_id:
                return {
                    "source_revision": current,
                    "subquestion_label": entry["label"],
                    "subquestion_stem": entry["stem"],
                    "stem_fingerprint": stem_fingerprint(entry["stem"]),
                    "stem": content.stem,
                    "answer": content.answer,
                    "solution": content.solution,
                }
        raise QbError([
            error("SUBQUESTION_NOT_FOUND",
                  f"小问不存在于当前题目: {subquestion_id}",
                  item_key=item_id, retryable=False,
                  fix_hint="刷新讲解/解答视图后按当前小问列表选择")
        ])

    def _insert_note_version(
        self,
        note_row: dict,
        *,
        parent_version: int | None,
        base_version: int | None,
        snapshot: dict[str, Any],
        title: str,
        ordinal: int,
        explanation_text: str,
        explanation_source: str,
        transcript: str,
        audio_asset_id: str | None,
        solution_text: str,
        solution_source: str,
        operation_id: str,
        actor_kind: str,
        actor_id: str,
    ) -> int:
        version = (note_row["current_version"] or 0) + 1
        now = db_mod.utc_now()
        self.conn.execute(
            "INSERT INTO teaching_note_versions(note_id, version, parent_version,"
            " base_version, source_revision, subquestion_id, subquestion_label,"
            " subquestion_stem, stem_fingerprint, title, ordinal, explanation_text,"
            " explanation_source, transcript, audio_asset_id, solution_text,"
            " solution_source, actor_kind, actor_id, operation_id, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                note_row["note_id"], version, parent_version, base_version,
                snapshot["source_revision"], snapshot["subquestion_id"],
                snapshot["subquestion_label"], snapshot["subquestion_stem"],
                snapshot["stem_fingerprint"], title, ordinal,
                explanation_text, explanation_source, transcript, audio_asset_id,
                solution_text, solution_source, actor_kind, actor_id,
                operation_id, now,
            ),
        )
        self.conn.execute(
            "UPDATE teaching_notes SET current_version = %s, updated_at = %s"
            " WHERE note_id = %s",
            (version, now, note_row["note_id"]),
        )
        return version

    # ------------------------------------------------------------------
    # 创建 / 编辑 / 软删除
    # ------------------------------------------------------------------

    def create_note(
        self, item_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        operation_id = _validate_operation_id(request.get("operation_id"), "create_note")
        subquestion_id = str(request.get("subquestion_id") or "").strip()
        if not subquestion_id:
            raise QbError([
                error("SCHEMA_INVALID", "subquestion_id 不能为空",
                      item_key=item_id, retryable=False)
            ])
        title = str(request.get("title") or "").strip()
        request_hash = canonical_json({"item_id": item_id, **request})
        with self._write_lock:
            self.conn.begin_write()
            try:
                if self._operation_exists(operation_id):
                    self.conn.execute("COMMIT")
                    return self._replay_or_reject(operation_id, request_hash)
                snapshot = self._snapshot_for(item_id, subquestion_id)
                note_id = self._next_note_id(item_id)
                now = db_mod.utc_now()
                self.conn.execute(
                    "INSERT INTO teaching_notes(note_id, item_id, subquestion_id,"
                    " ordinal, title, current_version, deleted, created_at, updated_at)"
                    " VALUES (%s,%s,%s,%s,%s,0,0,%s,%s)",
                    (note_id, item_id, subquestion_id, 0, title, now, now),
                )
                row = self._note_row(note_id)
                version = self._insert_note_version(
                    row,
                    parent_version=None, base_version=None,
                    snapshot={**snapshot, "subquestion_id": subquestion_id},
                    title=title or "思路 1",
                    ordinal=0,
                    explanation_text="", explanation_source="manual",
                    transcript="", audio_asset_id=None,
                    solution_text="", solution_source="manual",
                    operation_id=operation_id,
                    actor_kind=actor_kind, actor_id=actor_id,
                )
                receipt = {
                    "operation_id": operation_id,
                    "note_id": note_id,
                    "item_id": item_id,
                    "subquestion_id": subquestion_id,
                    "version": version,
                    "status": "created",
                    "replayed": False,
                }
                self._insert_operation(
                    operation_id, request_hash, "create_note", actor_kind,
                    actor_id, item_id, None, None, receipt,
                )
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self._rollback_quiet()
                raise

    def update_note(
        self, note_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        operation_id = _validate_operation_id(request.get("operation_id"), "update_note")
        readapt = bool(request.get("readapt"))
        request_hash = canonical_json({"note_id": note_id, **request})
        with self._write_lock:
            self.conn.begin_write()
            try:
                if self._operation_exists(operation_id):
                    self.conn.execute("COMMIT")
                    return self._replay_or_reject(operation_id, request_hash)
                row = self._live_note_row(note_id)
                base = self._check_note_base(row, request.get("base_version"))
                current = self._note_version_row(note_id, base)
                title = current["title"]
                if request.get("title") is not None:
                    title = str(request["title"]).strip()
                ordinal = current["ordinal"]
                if request.get("ordinal") is not None:
                    ordinal = int(request["ordinal"])
                explanation_text = current["explanation_text"]
                if request.get("explanation_text") is not None:
                    explanation_text = str(request["explanation_text"]).strip()
                solution_text = current["solution_text"]
                if request.get("solution_text") is not None:
                    solution_text = str(request["solution_text"]).strip()
                explanation_source = current["explanation_source"]
                if (request.get("explanation_text") is not None
                        and request["explanation_text"].strip() != current["explanation_text"]):
                    explanation_source = "manual"
                solution_source = current["solution_source"]
                if (request.get("solution_text") is not None
                        and request["solution_text"].strip() != current["solution_text"]):
                    solution_source = "manual"

                if readapt:
                    # 显式确认适配当前题目：建立新来源版本（需重新审批）。
                    snapshot = self._snapshot_for(note_id.rsplit("-n", 1)[0], row["subquestion_id"])
                    snapshot["subquestion_id"] = row["subquestion_id"]
                else:
                    snapshot = {
                        "source_revision": current["source_revision"],
                        "subquestion_id": current["subquestion_id"],
                        "subquestion_label": current["subquestion_label"],
                        "subquestion_stem": current["subquestion_stem"],
                        "stem_fingerprint": current["stem_fingerprint"],
                    }
                changed = any([
                    title != current["title"],
                    ordinal != current["ordinal"],
                    explanation_text != current["explanation_text"],
                    solution_text != current["solution_text"],
                    readapt,
                ])
                if not changed:
                    receipt = {
                        "operation_id": operation_id, "note_id": note_id,
                        "status": "noop", "version": base, "replayed": False,
                    }
                    self._insert_operation(
                        operation_id, request_hash, "update_note", actor_kind,
                        actor_id, row["item_id"], base, base, receipt)
                    self.conn.execute("COMMIT")
                    return receipt
                version = self._insert_note_version(
                    row,
                    parent_version=base, base_version=base,
                    snapshot=snapshot,
                    title=title, ordinal=ordinal,
                    explanation_text=explanation_text,
                    explanation_source=explanation_source,
                    transcript=current["transcript"],
                    audio_asset_id=current["audio_asset_id"],
                    solution_text=solution_text,
                    solution_source=solution_source,
                    operation_id=operation_id,
                    actor_kind=actor_kind, actor_id=actor_id,
                )
                receipt = {
                    "operation_id": operation_id, "note_id": note_id,
                    "status": "saved", "base_version": base, "version": version,
                    "readapt": readapt, "replayed": False,
                }
                self._insert_operation(
                    operation_id, request_hash, "update_note", actor_kind,
                    actor_id, row["item_id"], base, version, receipt)
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self._rollback_quiet()
                raise

    def delete_note(
        self, note_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        operation_id = _validate_operation_id(request.get("operation_id"), "delete_note")
        request_hash = canonical_json({"note_id": note_id, **request})
        with self._write_lock:
            self.conn.begin_write()
            try:
                if self._operation_exists(operation_id):
                    self.conn.execute("COMMIT")
                    return self._replay_or_reject(operation_id, request_hash)
                row = self._live_note_row(note_id)
                base = self._check_note_base(row, request.get("base_version"))
                now = db_mod.utc_now()
                self.conn.execute(
                    "UPDATE teaching_notes SET deleted = 1, deleted_at = %s,"
                    " updated_at = %s WHERE note_id = %s",
                    (now, now, note_id),
                )
                receipt = {
                    "operation_id": operation_id, "note_id": note_id,
                    "status": "deleted", "base_version": base,
                    "version": base, "replayed": False,
                }
                self._insert_operation(
                    operation_id, request_hash, "delete_note", actor_kind,
                    actor_id, row["item_id"], base, base, receipt)
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self._rollback_quiet()
                raise

    def review_note(
        self, note_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        operation_id = _validate_operation_id(request.get("operation_id"), "review_note")
        decision = request.get("decision")
        if decision not in ("approved", "rejected"):
            raise QbError([
                error("SCHEMA_INVALID", "decision 必须是 approved 或 rejected",
                      item_key=note_id, retryable=False)
            ])
        note_text = str(request.get("note") or "")
        request_hash = canonical_json({"note_id": note_id, **request})
        with self._write_lock:
            self.conn.begin_write()
            try:
                if self._operation_exists(operation_id):
                    self.conn.execute("COMMIT")
                    return self._replay_or_reject(operation_id, request_hash)
                row = self._live_note_row(note_id)
                base = self._check_note_base(row, request.get("base_version"))
                current = self._note_version_row(note_id, base)
                if decision == "approved" and (
                    not current["explanation_text"].strip()
                    or not current["solution_text"].strip()
                ):
                    raise ReceiptError([
                        error("NOTE_APPROVAL_BLOCKED",
                              "讲解与解答都填写后才能批准并导出 blueprint",
                              item_key=note_id,
                              fix_hint="补齐缺失的讲解或解答正文后重试",
                              retryable=False)
                    ])
                self.conn.execute(
                    "INSERT INTO teaching_note_reviews(note_id, version, decision,"
                    " actor_kind, actor_id, note, operation_id, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (note_id, base, decision, actor_kind, actor_id, note_text,
                     operation_id, db_mod.utc_now()),
                )
                receipt = {
                    "operation_id": operation_id, "note_id": note_id,
                    "status": "reviewed", "decision": decision,
                    "version": base, "replayed": False,
                }
                self._insert_operation(
                    operation_id, request_hash, "review_note", actor_kind,
                    actor_id, row["item_id"], base, base, receipt)
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self._rollback_quiet()
                raise

    # ------------------------------------------------------------------
    # 模型辅助（转写/润色/生成）：先登记 ai_operations 再调用
    # ------------------------------------------------------------------

    def _operation_exists(self, operation_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM operations WHERE operation_id = %s", (operation_id,)
        ).fetchone() is not None

    def _replay_or_reject(self, operation_id: str, request_hash: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = %s", (operation_id,)
        ).fetchone()
        if row["request_hash"] != request_hash:
            raise ReceiptError([
                error("OPERATION_ID_REUSED", "相同 operation_id 携带不同载荷",
                      retryable=False)
            ])
        receipt = json.loads(row["receipt"])
        receipt["replayed"] = True
        return receipt

    def _begin_ai_operation(
        self, operation_id: str, kind: str, target: str,
        request_hash: str, input_snapshot: dict[str, Any],
    ) -> dict | None:
        """登记调用状态；返回已存在的同 ID 行（重放/失败/未知），None 表示可执行。"""
        with self._write_lock:
            self.conn.begin_write()
            try:
                existing = self.conn.execute(
                    "SELECT * FROM ai_operations WHERE operation_id = %s",
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    self.conn.execute("COMMIT")
                    # 先核验载荷：异载荷同 ID 一律拒绝（无论状态）。
                    if existing["request_hash"] != request_hash:
                        raise ReceiptError([
                            error("OPERATION_ID_REUSED",
                                  "相同 operation_id 携带不同载荷", retryable=False)
                        ])
                    if existing["state"] == "running":
                        raise _ai_error(
                            "AI_OPERATION_UNKNOWN",
                            f"操作 {operation_id} 的远端结果未知（进程可能中断）；"
                            "请检查笔记内容后用新的操作重试，不会自动重新调用付费接口",
                        )
                    return existing
                self.conn.execute(
                    "INSERT INTO ai_operations(operation_id, request_hash, kind,"
                    " target, input_snapshot, state, owner, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,'running',%s,%s)",
                    (operation_id, request_hash, kind, target,
                     canonical_json(input_snapshot), self._ai_owner_id(),
                     db_mod.utc_now()),
                )
                self.conn.execute("COMMIT")
                return None
            except Exception:
                self._rollback_quiet()
                raise

    def _finish_ai_operation(
        self, operation_id: str, state: str, *,
        result: str | None = None, error_text: str | None = None,
        meta: dict[str, Any] | None = None, applied: bool = False,
    ) -> None:
        with self._write_lock:
            # 可能在调用方写事务内执行：仅当没有活动事务时自管提交。
            own_txn = not self.conn.in_transaction
            if own_txn:
                self.conn.begin_write()
            try:
                calls = (meta or {}).get("calls")
                last = calls[-1] if calls else (meta or {})
                usage_value = (meta or {}).get("usage")
                if usage_value is None:
                    usage_value = ({"calls": calls} if calls else None)
                self.conn.execute(
                    "UPDATE ai_operations SET state = %s, result = %s, error = %s,"
                    " provider = %s, model = %s, usage = %s, applied = %s, completed_at = %s"
                    " WHERE operation_id = %s",
                    (
                        state, result, error_text,
                        last.get("provider"),
                        last.get("model"),
                        canonical_json(usage_value)
                        if usage_value is not None else None,
                        1 if applied else 0, db_mod.utc_now(), operation_id,
                    ),
                )
                if own_txn:
                    self.conn.execute("COMMIT")
            except Exception:
                if own_txn:
                    self._rollback_quiet()
                raise

    def _replay_ai_outcome(self, row: dict) -> dict[str, Any]:
        if row["state"] == "running":
            raise _ai_error(
                "AI_OPERATION_UNKNOWN",
                f"操作 {row['operation_id']} 的远端结果未知（进程可能中断）；"
                "请检查笔记内容后用新的操作重试，不会自动重新调用付费接口",
            )
        receipt = {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "state": row["state"],
            "replayed": True,
        }
        if row["error"]:
            receipt["errors"] = [json.loads(row["error"])] if row["error"].startswith("{") else [
                {"code": "ai_failed", "message": row["error"]}
            ]
        return receipt

    def save_recording(
        self, note_id: str, base_version: Any, operation_id: Any,
        raw: bytes, content_type: str, *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        """上传录音：登记音频资产 → 转写（模型）→ 写入新版本。

        重放优先：同 ID 同载荷（含当时的 base_version）直接返回原回执，
        不再校验当前版本。ASR 失败时音频已登记且版本已更新（只带音频不带
        转写稿），错误如实返回；之后可通过 polish 重读录音重试转写。
        """
        operation_id = _validate_operation_id(operation_id, "save_recording")
        info = self.assets.register_audio(raw, content_type)
        with self._write_lock:
            self.conn.begin_write()
            try:
                self.conn.execute(
                    "INSERT INTO assets(asset_id, sha256, path, mime,"
                    " size_bytes, width, height, source_kind, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
                    (info["asset_id"], info["sha256"], info["path"], info["mime"],
                     info["size_bytes"], None, None, "recording", db_mod.utc_now()),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self._rollback_quiet()
                raise
        request_hash = self._model_request_hash({
            "note_id": note_id, "base_version": base_version,
            "asset": info["asset_id"], "content_type": info["mime"],
        })
        replayed = self._replay_operation_receipt(operation_id, request_hash)
        if replayed is not None:
            return replayed

        row = self._live_note_row(note_id)
        base = self._check_note_base(row, base_version)
        existing = self._begin_ai_operation(
            operation_id, "transcribe", note_id, request_hash,
            {"note_id": note_id, "base_version": base, "asset_id": info["asset_id"]},
        )
        if existing is not None:
            return self._replay_ai_outcome(existing)

        transcript = ""
        asr_error: str | None = None
        meta: dict[str, Any] = {}
        try:
            transcript = ai.transcribe_audio(raw, content_type, meta)
        except ai.AiAssistError as exc:
            asr_error = f"{exc.code}: {exc}"
            self._finish_ai_operation(
                operation_id, "failed", error_text=asr_error, meta=meta)

        with self._write_lock:
            self.conn.begin_write()
            try:
                fresh = self._note_row(note_id)
                if fresh["current_version"] != base or fresh["deleted"]:
                    self._finish_ai_operation(
                        operation_id, "conflict", result=json.dumps(
                            {"transcript": transcript}, ensure_ascii=False),
                        meta=meta)
                    self.conn.execute("COMMIT")
                    raise ReceiptError([
                        error("NOTE_VERSION_CONFLICT",
                              f"录音期间笔记已变化（当前 v{fresh['current_version']}"
                              f"{'，已删除' if fresh['deleted'] else ''}）；"
                              "转写结果已保留为未应用，请刷新后重试",
                              item_key=note_id, retryable=False)
                    ])
                current = self._note_version_row(note_id, base)
                snapshot = {
                    "source_revision": current["source_revision"],
                    "subquestion_id": current["subquestion_id"],
                    "subquestion_label": current["subquestion_label"],
                    "subquestion_stem": current["subquestion_stem"],
                    "stem_fingerprint": current["stem_fingerprint"],
                }
                version = self._insert_note_version(
                    fresh,
                    parent_version=base, base_version=base,
                    snapshot=snapshot,
                    title=current["title"], ordinal=current["ordinal"],
                    explanation_text=current["explanation_text"],
                    explanation_source="recorded",
                    transcript=transcript,
                    audio_asset_id=info["asset_id"],
                    solution_text=current["solution_text"],
                    solution_source=current["solution_source"],
                    operation_id=operation_id,
                    actor_kind="ai" if asr_error is None else actor_kind,
                    actor_id=actor_id,
                )
                receipt = {
                    "operation_id": operation_id, "note_id": note_id,
                    "status": "transcribed" if asr_error is None else "audio_saved",
                    "base_version": base, "version": version,
                    "asset_id": info["asset_id"],
                    "transcript": transcript,
                    "replayed": False,
                }
                if asr_error is not None:
                    receipt["errors"] = [{
                        "code": "asr_failed", "message":
                        f"音频已保存，转写失败：{asr_error}；可稍后点“润色讲解”重试转写",
                    }]
                if asr_error is None:
                    self._finish_ai_operation(
                        operation_id, "succeeded",
                        result=json.dumps({"transcript": transcript},
                                          ensure_ascii=False),
                        meta=meta, applied=True)
                self._insert_operation(
                    operation_id, request_hash,
                    "save_recording", actor_kind, actor_id,
                    fresh["item_id"], base, version, receipt,
                )
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self._rollback_quiet()
                raise

    def _replay_operation_receipt(
        self, operation_id: str, request_hash: str,
    ) -> dict[str, Any] | None:
        """operations 表重放：同 ID 同载荷返回原回执（先于任何版本检查）。"""
        row = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = %s", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ReceiptError([
                error("OPERATION_ID_REUSED", "相同 operation_id 携带不同载荷",
                      retryable=False)
            ])
        receipt = json.loads(row["receipt"])
        receipt["replayed"] = True
        return receipt


    def _model_request_hash(self, payload: dict[str, Any]) -> str:
        return canonical_json(payload)


    # ------------------------------------------------------------------
    # 润色 / 生成 / 一键补齐
    # ------------------------------------------------------------------

    def _apply_note_text(
        self,
        note_id: str,
        base_version: int,
        operation_id: str,
        request_hash: str,
        kind_name: str,
        apply_fields: dict[str, str],
        actor_kind: str,
        actor_id: str,
        snapshot_override: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        expected_item_revision: int | None = None,
    ) -> dict[str, Any]:
        """把模型产出写入新版本；期间笔记变化/删除/题目变版则保留结果为未应用。"""
        with self._write_lock:
            self.conn.begin_write()
            try:
                fresh = self._note_row(note_id)
                item_row = self._item_row(fresh["item_id"])
                drifted = (
                    fresh["deleted"]
                    or fresh["current_version"] != base_version
                    or (expected_item_revision is not None
                        and item_row["current_revision"] != expected_item_revision)
                )
                if drifted:
                    self._finish_ai_operation(
                        operation_id, "conflict",
                        result=json.dumps(apply_fields, ensure_ascii=False),
                        meta=meta)
                    self.conn.execute("COMMIT")
                    reasons = []
                    if fresh["deleted"]:
                        reasons.append("笔记已删除")
                    if fresh["current_version"] != base_version:
                        reasons.append(f"笔记已到 v{fresh['current_version']}")
                    if (expected_item_revision is not None
                            and item_row["current_revision"] != expected_item_revision):
                        reasons.append(
                            f"题目已到 r{item_row['current_revision']}"
                            f"（生成基于 r{expected_item_revision}）")
                    raise ReceiptError([
                        error("NOTE_VERSION_CONFLICT",
                              "生成/润色期间状态变化：" + "；".join(reasons)
                              + "。模型结果已保留为未应用，请刷新后重试",
                              item_key=note_id, retryable=False)
                    ])
                current = self._note_version_row(note_id, base_version)
                if snapshot_override is not None:
                    snapshot = snapshot_override
                else:
                    snapshot = {
                        "source_revision": current["source_revision"],
                        "subquestion_id": current["subquestion_id"],
                        "subquestion_label": current["subquestion_label"],
                        "subquestion_stem": current["subquestion_stem"],
                        "stem_fingerprint": current["stem_fingerprint"],
                    }
                version = self._insert_note_version(
                    fresh, parent_version=base_version, base_version=base_version,
                    snapshot=snapshot,
                    title=current["title"], ordinal=current["ordinal"],
                    explanation_text=apply_fields.get(
                        "explanation_text", current["explanation_text"]),
                    explanation_source=apply_fields.get(
                        "explanation_source", current["explanation_source"]),
                    transcript=apply_fields.get(
                        "transcript", current["transcript"]),
                    audio_asset_id=current["audio_asset_id"],
                    solution_text=apply_fields.get(
                        "solution_text", current["solution_text"]),
                    solution_source=apply_fields.get(
                        "solution_source", current["solution_source"]),
                    operation_id=operation_id,
                    actor_kind="ai", actor_id=actor_id,
                )
                receipt = {
                    "operation_id": operation_id, "note_id": note_id,
                    "status": "applied", "base_version": base_version,
                    "version": version, "replayed": False,
                }
                for key in ("explanation_text", "solution_text", "transcript"):
                    if key in apply_fields:
                        receipt[key] = apply_fields[key]
                self._finish_ai_operation(
                    operation_id, "succeeded",
                    result=json.dumps(apply_fields, ensure_ascii=False),
                    meta=meta, applied=True)
                self._insert_operation(
                    operation_id, request_hash, kind_name, actor_kind, actor_id,
                    fresh["item_id"], base_version, version, receipt)
                self.conn.execute("COMMIT")
                return receipt
            except Exception:
                self._rollback_quiet()
                raise

    def _persist_transcript_only(
        self, note_id: str, base_version: int, transcript: str,
        audio_asset_id: str | None, operation_id: str,
        actor_kind: str, actor_id: str, meta: dict[str, Any] | None,
        expected_item_revision: int | None = None,
    ) -> int:
        """润色链路第一阶段：ASR 成功即持久化转写稿（独立版本）。

        润色失败时转写稿不丢，重试润色不会重复付费 ASR。
        """
        with self._write_lock:
            self.conn.begin_write()
            try:
                fresh = self._note_row(note_id)
                item_row = self._item_row(fresh["item_id"])
                drifted = (
                    fresh["deleted"]
                    or fresh["current_version"] != base_version
                    or (expected_item_revision is not None
                        and item_row["current_revision"] != expected_item_revision)
                )
                if drifted:
                    self._finish_ai_operation(
                        operation_id, "conflict",
                        result=json.dumps({"transcript": transcript},
                                          ensure_ascii=False),
                        meta=meta)
                    self.conn.execute("COMMIT")
                    reasons = []
                    if fresh["deleted"]:
                        reasons.append("笔记已删除")
                    if fresh["current_version"] != base_version:
                        reasons.append(f"笔记已到 v{fresh['current_version']}")
                    if (expected_item_revision is not None
                            and item_row["current_revision"] != expected_item_revision):
                        reasons.append(
                            f"题目已到 r{item_row['current_revision']}")
                    raise ReceiptError([
                        error("NOTE_VERSION_CONFLICT",
                              "转写期间状态变化：" + "；".join(reasons)
                              + "；转写结果已保留为未应用，请刷新后重试",
                              item_key=note_id, retryable=False)
                    ])
                current = self._note_version_row(note_id, base_version)
                snapshot = {
                    "source_revision": current["source_revision"],
                    "subquestion_id": current["subquestion_id"],
                    "subquestion_label": current["subquestion_label"],
                    "subquestion_stem": current["subquestion_stem"],
                    "stem_fingerprint": current["stem_fingerprint"],
                }
                return self._insert_note_version(
                    fresh, parent_version=base_version, base_version=base_version,
                    snapshot=snapshot,
                    title=current["title"], ordinal=current["ordinal"],
                    explanation_text=current["explanation_text"],
                    explanation_source=current["explanation_source"],
                    transcript=transcript,
                    audio_asset_id=audio_asset_id or current["audio_asset_id"],
                    solution_text=current["solution_text"],
                    solution_source=current["solution_source"],
                    operation_id=operation_id,
                    actor_kind="ai", actor_id=actor_id,
                )
            except Exception:
                self._rollback_quiet()
                raise
            finally:
                if self.conn.in_transaction:
                    self.conn.execute("COMMIT")

    def _note_ai_context(self, note_id: str) -> dict[str, Any]:
        """生成/润色的题目上下文：笔记来源修订的三块正文 + 小问快照。"""
        row = self._live_note_row(note_id)
        current = self._note_version_row(note_id, row["current_version"])
        content = self._content_of(row["item_id"], current["source_revision"])
        return {
            "stem": content.stem,
            "subquestion_label": current["subquestion_label"],
            "subquestion_stem": current["subquestion_stem"],
            "answer": content.answer,
            "solution": content.solution,
            "item_id": row["item_id"],
            "note_row": row,
            "version_row": current,
        }

    def polish_note(
        self, note_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        """转写稿 -> 润色讲解；无转写稿但有录音时先补转写（独立持久化阶段）。"""
        operation_id = _validate_operation_id(request.get("operation_id"), "polish")
        request_hash = self._model_request_hash({
            "note_id": note_id, "request": request,
        })
        replayed = self._replay_operation_receipt(operation_id, request_hash)
        if replayed is not None:
            return replayed
        row = self._live_note_row(note_id)
        base = self._check_note_base(row, request.get("base_version"))
        current = self._note_version_row(note_id, base)
        transcript = current["transcript"].strip()
        audio_info: dict[str, Any] | None = None
        if not transcript and current["audio_asset_id"]:
            arow = self.conn.execute(
                "SELECT * FROM assets WHERE asset_id = %s",
                (current["audio_asset_id"],),
            ).fetchone()
            if arow is None:
                raise _ai_error(
                    "ASSET_NOT_FOUND", f"录音资产未登记: {current['audio_asset_id']}")
            audio_info = {
                "mime": arow["mime"],
                "bytes": self.assets.read_audio_bytes(arow["mime"], arow["path"]),
            }
        if not transcript and audio_info is None:
            raise QbError([
                error("POLISH_NO_TRANSCRIPT",
                      "没有可润色的录音转写稿：请先录制讲解",
                      item_key=note_id, retryable=False)
            ])
        item_revision_at_call = self._item_row(row["item_id"])["current_revision"]
        existing = self._begin_ai_operation(
            operation_id, "polish", note_id, request_hash,
            {"note_id": note_id, "base_version": base,
             "transcript": transcript, "need_asr": audio_info is not None,
             "item_revision": item_revision_at_call},
        )
        if existing is not None:
            return self._replay_ai_outcome(existing)
        meta: dict[str, Any] = {"calls": []}
        ctx = self._note_ai_context(note_id)
        try:
            if not transcript:
                asr_meta: dict[str, Any] = {}
                transcript = ai.transcribe_audio(
                    audio_info["bytes"], audio_info["mime"], asr_meta)
                meta["calls"].append({"stage": "asr", **asr_meta})
                # ASR 成功立即持久化（独立阶段）：润色失败也不丢转写稿。
                base = self._persist_transcript_only(
                    note_id, base, transcript,
                    current["audio_asset_id"], operation_id,
                    actor_kind, actor_id, meta,
                    expected_item_revision=item_revision_at_call)
            polish_meta: dict[str, Any] = {}
            polished = ai.polish_explanation_text(
                {**ctx, "transcript": transcript}, polish_meta)
            meta["calls"].append({"stage": "polish", **polish_meta})
        except ai.AiAssistError as exc:
            self._finish_ai_operation(
                operation_id, "failed", error_text=f"{exc.code}: {exc}", meta=meta)
            raise _ai_error(exc.code, str(exc)) from exc
        except ReceiptError:
            raise
        return self._apply_note_text(
            note_id, base, operation_id, request_hash, "polish_note",
            {"explanation_text": polished, "explanation_source": "polished",
             "transcript": transcript},
            actor_kind, actor_id, meta=meta,
            expected_item_revision=item_revision_at_call,
        )

    def generate_note_text(
        self, note_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        """单对生成（讲解或解答的另一半）；基于当前题目修订建立新来源版本。"""
        operation_id = _validate_operation_id(request.get("operation_id"), "generate")
        request_hash = self._model_request_hash({
            "note_id": note_id, "request": request,
        })
        replayed = self._replay_operation_receipt(operation_id, request_hash)
        if replayed is not None:
            return replayed
        row = self._live_note_row(note_id)
        base = self._check_note_base(row, request.get("base_version"))
        kind = request.get("kind")
        if kind not in ("explanation", "solution"):
            raise QbError([
                error("SCHEMA_INVALID", "kind 必须是 explanation 或 solution",
                      item_key=note_id, retryable=False)
            ])
        current = self._note_version_row(note_id, base)
        item_row = self._item_row(row["item_id"])
        item_revision = item_row["current_revision"]
        content = self._content_of(row["item_id"], item_revision)
        snapshot = self._snapshot_for(row["item_id"], current["subquestion_id"])
        existing = self._begin_ai_operation(
            operation_id, f"generate_{kind}", note_id, request_hash,
            {"note_id": note_id, "base_version": base, "kind": kind,
             "item_revision": item_revision},
        )
        if existing is not None:
            return self._replay_ai_outcome(existing)
        meta: dict[str, Any] = {}
        ctx = {
            "stem": content.stem,
            "subquestion_label": snapshot["subquestion_label"],
            "subquestion_stem": snapshot["subquestion_stem"],
            "answer": content.answer,
            "solution": content.solution,
            "explanation": current["explanation_text"],
        }
        try:
            if kind == "explanation":
                text = ai.generate_explanation_text(ctx, meta)
                fields: dict[str, Any] = {
                    "explanation_text": text, "explanation_source": "generated"}
            else:
                text = ai.generate_solution_text(ctx, meta)
                fields = {"solution_text": text, "solution_source": "generated"}
        except ai.AiAssistError as exc:
            self._finish_ai_operation(
                operation_id, "failed", error_text=f"{exc.code}: {exc}", meta=meta)
            raise _ai_error(exc.code, str(exc)) from exc
        # 生成基于当前题目修订：应用时复核题目未变版（变版保留结果为未应用）。
        return self._apply_note_text(
            note_id, base, operation_id, request_hash, "generate_note_text",
            fields, actor_kind, actor_id,
            snapshot_override={
                **snapshot, "subquestion_id": current["subquestion_id"]},
            meta=meta, expected_item_revision=item_revision)




    def fill_missing(
        self, item_id: str, request: dict[str, Any], *,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        """一键补齐（老版语义）：冻结目标清单，只补空字段，逐项成功/失败。

        目标清单在调用前冻结并登记（ai_operations 父记录）；同 ID 重放按
        冻结清单返回原结果，不重新计算。子操作 ID 含 note_id 与基线版本，
        同小问多个 approach 不冲突。kind=explanation：为没有任何当前可用
        讲解的派生小问生成讲解（无可用 note 时建默认对；过期 note 不动，
        另建当前来源对）。kind=solution：小问有可用讲解但无任何可用解答时，
        为其中缺解答的当前来源对生成解答。
        """
        operation_id = _validate_operation_id(request.get("operation_id"), "fill")
        kind = request.get("kind")
        if kind not in ("explanation", "solution"):
            raise QbError([
                error("SCHEMA_INVALID", "kind 必须是 explanation 或 solution",
                      item_key=item_id, retryable=False)
            ])
        request_hash = self._model_request_hash({"item_id": item_id, "request": request})
        replayed = self._replay_operation_receipt(operation_id, request_hash)
        if replayed is not None:
            return replayed
        item_row = self._item_row(item_id)
        if item_row is None or item_row["current_revision"] is None:
            raise QbError([
                error("ITEM_NOT_FOUND", f"题目不存在或尚无修订: {item_id}",
                      item_key=item_id, retryable=False)
            ])
        item_revision = item_row["current_revision"]
        content = self._content_of(item_id, item_revision)
        derived = derive_subquestions(content.stem)

        # ---- 冻结目标清单（覆盖口径：仅当前来源不过期的 note 计入覆盖）----
        notes = self.conn.execute(
            "SELECT * FROM teaching_notes WHERE item_id = %s AND deleted = 0",
            (item_id,),
        ).fetchall()
        live: dict[str, list[dict[str, Any]]] = {entry["id"]: [] for entry in derived}
        by_note: dict[str, dict[str, Any]] = {}
        for note in notes:
            version_row = self._note_version_row(note["note_id"], note["current_version"])
            subq = next((e for e in derived if e["id"] == version_row["subquestion_id"]), None)
            stale = (
                version_row["source_revision"] != item_revision
                or subq is None
                or version_row["stem_fingerprint"] != stem_fingerprint(subq["stem"])
            )
            entry = {
                "note_id": note["note_id"], "version": note["current_version"],
                "subquestion_id": version_row["subquestion_id"],
                "explanation": version_row["explanation_text"],
                "solution": version_row["solution_text"],
                "stale": stale,
            }
            by_note[note["note_id"]] = entry
            if not stale:
                live.setdefault(version_row["subquestion_id"], []).append(entry)
        targets: list[dict[str, Any]] = []
        for subq in derived:
            entries = live.get(subq["id"], [])
            if kind == "explanation":
                if any(e["explanation"].strip() for e in entries):
                    continue
                target_note = next((e for e in entries), None)
                targets.append({
                    "subquestion_id": subq["id"],
                    "subquestion_label": subq["label"],
                    "subquestion_stem": subq["stem"],
                    "note_id": target_note["note_id"] if target_note else None,
                    "note_version": target_note["version"] if target_note else None,
                })
            else:
                if any(e["solution"].strip() for e in entries):
                    continue
                if not any(e["explanation"].strip() for e in entries):
                    continue
                target_note = next(
                    (e for e in entries
                     if e["explanation"].strip() and not e["solution"].strip()), None)
                if target_note is None:
                    continue  # 有讲解的对都已带解答但覆盖仍缺：不强行新建
                targets.append({
                    "subquestion_id": subq["id"],
                    "subquestion_label": subq["label"],
                    "subquestion_stem": subq["stem"],
                    "note_id": target_note["note_id"],
                    "note_version": target_note["version"],
                })
        frozen = {
            "item_id": item_id, "kind": kind, "item_revision": item_revision,
            "targets": targets,
        }
        parent = self._begin_ai_operation(
            operation_id, f"fill_{kind}", item_id, request_hash, frozen)
        if parent is not None:
            return self._replay_ai_outcome(parent)

        results: list[dict[str, Any]] = []
        for target in targets:
            sub_op = None
            sub_op = self._derive_sub_operation_id(
                operation_id,
                f"{target['subquestion_id']}:{target['note_id'] or 'new'}"
                f":{target['note_version'] or 0}")
            try:
                if target["note_id"] is None:
                    create = self.create_note(item_id, {
                        "subquestion_id": target["subquestion_id"],
                        "title": "补齐讲解",
                        "operation_id": self._derive_sub_operation_id(
                            operation_id,
                            f"new:{target['subquestion_id']}"),
                    })
                    note_id = create["note_id"]
                    note_base = create["version"]
                else:
                    note_id = target["note_id"]
                    note_base = target["note_version"]
                ctx = {
                    "stem": content.stem,
                    "subquestion_label": target["subquestion_label"],
                    "subquestion_stem": target["subquestion_stem"],
                    "answer": content.answer,
                    "solution": content.solution,
                    "explanation": (by_note.get(note_id) or {}).get("explanation", ""),
                }
                meta: dict[str, Any] = {}
                child_hash = self._model_request_hash({
                    "parent": operation_id, "target": target, "kind": kind})
                existing = self._begin_ai_operation(
                    sub_op, f"fill_{kind}", note_id, child_hash,
                    {"item_id": item_id, "target": target, "kind": kind,
                     "item_revision": item_revision})
                if existing is not None:
                    outcome = self._replay_ai_outcome(existing)
                    results.append({
                        "subquestion_id": target["subquestion_id"],
                        "note_id": note_id, "status": "replayed",
                        **({"errors": outcome["errors"]}
                           if "errors" in outcome else {}),
                    })
                    continue
                if kind == "explanation":
                    text = ai.generate_explanation_text(ctx, meta)
                    fields = {"explanation_text": text,
                              "explanation_source": "generated"}
                else:
                    text = ai.generate_solution_text(ctx, meta)
                    fields = {"solution_text": text,
                              "solution_source": "generated"}
                receipt = self._apply_note_text(
                    note_id, note_base, sub_op, child_hash, "fill_missing",
                    fields, actor_kind, actor_id,
                    snapshot_override={
                        "source_revision": item_revision,
                        "subquestion_id": target["subquestion_id"],
                        "subquestion_label": target["subquestion_label"],
                        "subquestion_stem": target["subquestion_stem"],
                        "stem_fingerprint": stem_fingerprint(
                            target["subquestion_stem"]),
                    },
                    meta=meta, expected_item_revision=item_revision)
                results.append({
                    "subquestion_id": target["subquestion_id"],
                    "note_id": note_id, "status": "generated",
                    "version": receipt["version"],
                })
            except (QbError, ai.AiAssistError) as exc:
                errors = exc.errors if isinstance(exc, QbError) else [
                    {"code": getattr(exc, "code", "ai_failed"), "message": str(exc)}
                ]
                # 只终结仍处 running 的子调用；_apply_note_text 已提交的
                # conflict（含未应用模型结果与调用元数据）不得被空值覆盖。
                sub_op_id = locals().get("sub_op")
                status = "failed"
                if sub_op_id:
                    existing_row = self.conn.execute(
                        "SELECT state FROM ai_operations WHERE operation_id = %s",
                        (sub_op_id,),
                    ).fetchone()
                    if existing_row is None:
                        self._finish_ai_operation(
                            sub_op_id, "failed",
                            error_text="; ".join(
                                f"{e.get('code')}: {e.get('message')}"
                                for e in errors))
                    elif existing_row["state"] == "running":
                        self._finish_ai_operation(
                            sub_op_id, "failed",
                            error_text="; ".join(
                                f"{e.get('code')}: {e.get('message')}"
                                for e in errors))
                    else:
                        status = existing_row["state"]
                results.append({
                    "subquestion_id": target["subquestion_id"],
                    "note_id": target.get("note_id"),
                    "status": status, "errors": errors,
                })
        receipt = {
            "operation_id": operation_id, "item_id": item_id, "kind": kind,
            "targets": [f"{t['subquestion_id']}" for t in targets],
            "results": results,
            "generated": sum(1 for r in results if r["status"] == "generated"),
            "replayed": False,
        }
        self._finish_ai_operation(
            operation_id, "succeeded",
            result=json.dumps(receipt, ensure_ascii=False), applied=True)
        with self._write_lock:
            self.conn.begin_write()
            try:
                self._insert_operation(
                    operation_id, request_hash, "fill_missing", actor_kind,
                    actor_id, item_id, None, None, receipt)
                self.conn.execute("COMMIT")
            except Exception:
                self._rollback_quiet()
                raise
        return receipt

    @staticmethod
    def _derive_sub_operation_id(parent: str, suffix: str) -> str:
        base = parent if len(parent) <= 56 else "h" + hashlib.sha256(
            parent.encode("utf-8")).hexdigest()[:20]
        digest = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:8]
        sub = f"{base}-f{digest}"
        assert OPERATION_ID_PATTERN.match(sub)
        return sub

    # ------------------------------------------------------------------
    # blueprint 导出：本地不可变快照 + 配置外部投递
    # ------------------------------------------------------------------

    def export_paper_blueprints(
        self, paper_id: str, request: dict[str, Any] | None = None, *,
        teaching_tools_root: str | Path | None = None,
        actor_kind: str = "human", actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        """把该试卷全部可导出的讲解-解答对重建为 teaching-tools 候选批次。

        - 快照一致：候选绑定 (item_id, item_revision, note_id, note_version)，
          投递前逐项复核当前状态（题目修订、笔记版本/删除、双方批准），
          漂移即报冲突不投递。
        - 重放：同 ID 同载荷返回原回执，不重算重投。
        - 所有权：外部目标文件已存在但内容不是本功能产出（source/taskId
          不符或不可解析）时拒绝覆盖/删除，报 external failed。
        - 零候选也处理外部当前文件（在所有权允许时删除），不残留旧候选。
        """
        request = request or {}
        import time as _time
        operation_id = _validate_operation_id(
            request.get("operation_id")
            or f"bp-export-{_time.strftime('%Y%m%dT%H%M%S')}", "export")
        paper_row = self.conn.execute(
            "SELECT * FROM papers WHERE paper_id = %s", (paper_id,)
        ).fetchone()
        if paper_row is None:
            raise QbError([
                error("PAPER_NOT_FOUND", f"试卷不存在: {paper_id}",
                      item_key=paper_id, retryable=False)
            ])

        # ---- 冻结快照：一次读取全部可导出绑定 ----
        snapshot: list[dict[str, Any]] = []
        items = self.conn.execute(
            "SELECT item_id, question_number, current_revision FROM items"
            " WHERE paper_id = %s ORDER BY question_number", (paper_id,),
        ).fetchall()
        pending_item_review: list[str] = []
        for item in items:
            current = item["current_revision"]
            if current is None:
                continue
            content = self._content_of(item["item_id"], current)
            derived = {
                entry["id"]: entry for entry in derive_subquestions(content.stem)
            }
            notes = self.conn.execute(
                "SELECT * FROM teaching_notes WHERE item_id = %s AND deleted = 0",
                (item["item_id"],),
            ).fetchall()
            for note in notes:
                version_row = self._note_version_row(
                    note["note_id"], note["current_version"])
                if self._note_review_decision(
                        note["note_id"], note["current_version"]) != "approved":
                    continue
                subq = derived.get(version_row["subquestion_id"])
                stale = (
                    version_row["source_revision"] != current
                    or subq is None
                    or version_row["stem_fingerprint"] != stem_fingerprint(subq["stem"])
                )
                if stale:
                    continue  # 来源过期：保留批准事实，但不进当前有效候选
                if not self.is_approved(item["item_id"], current):
                    pending_item_review.append(item["item_id"])
                    continue
                snapshot.append({
                    "item_id": item["item_id"], "item_revision": current,
                    "note_id": note["note_id"], "note_version": note["current_version"],
                    "subquestion": subq, "content": content,
                    "version_row": version_row,
                })
        frozen_ids = [
            {"item_id": s["item_id"], "item_revision": s["item_revision"],
             "note_id": s["note_id"], "note_version": s["note_version"]}
            for s in snapshot
        ]
        # 重放按客户端请求哈希（不含快照）：导出后内容变化不改变同一请求的
        # 重放语义；首次冻结的快照已持久化在回执与 blueprint_batches。
        request_hash = self._model_request_hash({
            "paper_id": paper_id, "operation_id": operation_id,
            "request": request,
        })
        replayed = self._replay_operation_receipt(operation_id, request_hash)
        if replayed is not None:
            return replayed

        self._verify_export_snapshot(frozen_ids)

        candidates: list[dict[str, Any]] = []
        assignments: set[str] = set([])
        from question_bank import export as export_mod
        for s in snapshot:
            export = export_mod.export_revision(self, s["item_id"], s["item_revision"])
            teacher_abs = str(
                (self.root / export["path"] / "teacher.assignment.yaml").resolve())
            assignments.add(teacher_abs)
            candidate_id = (
                f"{paper_id}:{s['item_id']}:{s['subquestion']['id']}"
                f":{s['note_id']}:v{s['note_version']}"
            )
            solution_steps = [
                {"title": "", "content": paragraph.strip()}
                for paragraph in s["content"].solution.split("\n\n") if paragraph.strip()
            ] or [{"title": "", "content": s["content"].solution}]
            candidates.append({
                "id": candidate_id,
                "promptData": {
                    "promptLatex": s["subquestion"]["stem"] or s["content"].stem,
                    "fullStemLatex": s["content"].stem,
                    "subquestionLabel": s["subquestion"]["label"],
                    "itemId": s["item_id"],
                    "explanationLatex": s["version_row"]["explanation_text"],
                    "solutionLatex": s["version_row"]["solution_text"],
                    "approvedAt": self._note_approved_at(
                        s["note_id"], s["note_version"]),
                },
                "answerKey": {
                    "answerLatex": s["content"].answer,
                    "solutionSteps": solution_steps,
                },
                "metadata": {
                    "source": "reviewed-bank-import",
                    "assignments": [teacher_abs],
                    "tags": [],
                    "difficulty": "",
                    "itemRevision": s["item_revision"],
                    "noteId": s["note_id"],
                    "noteVersion": s["note_version"],
                },
            })
        batch = {
            "taskId": paper_id,
            "engineKind": "topic-practice",
            "contentId": f"topic-practice.{paper_id}.v1",
            "version": "1",
            "source": "reviewed-bank-import",
            "assignments": sorted(assignments),
            "candidates": candidates,
        }
        # 投递前二次复核：候选构建（含导出教师 yaml）期间状态可能变化
        self._verify_export_snapshot(frozen_ids)
        manifest_hash = "sha256:" + hashlib.sha256(
            canonical_json(batch).encode("utf-8")).hexdigest()
        slug = re.sub(r"[^0-9A-Za-z._-]+", "-", paper_id).strip("-") or "bank"
        local_dir = self.root / "exports" / "blueprints" / paper_id
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / f"{manifest_hash[7:23]}.candidates.json"
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=local_dir, delete=False, suffix=".tmp")
        try:
            json.dump(batch, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.close()
            os.replace(handle.name, local_path)
        finally:
            handle.close()
            if os.path.exists(handle.name):
                os.unlink(handle.name)

        external_path = None
        external_status = "not_configured"
        external_error = None
        root_path = teaching_tools_root or os.environ.get("QB_TEACHING_TOOLS_ROOT")
        if root_path:
            out_dir = Path(root_path) / "authoring" / "tmp" / "reviewed-bank-import"
            target = out_dir / f"{slug}.candidates.json"
            external_path = str(target)
            try:
                if target.exists() and not self._owns_external_file(target, paper_id):
                    external_status = "failed"
                    external_error = (
                        f"外部文件已存在且不是本功能产出（{target}）；拒绝覆盖/删除"
                    )
                else:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    if candidates:
                        handle = tempfile.NamedTemporaryFile(
                            "w", encoding="utf-8", dir=out_dir,
                            delete=False, suffix=".tmp")
                        try:
                            json.dump(batch, handle, ensure_ascii=False, indent=2)
                            handle.write("\n")
                            handle.close()
                            os.replace(handle.name, target)
                        finally:
                            handle.close()
                            if os.path.exists(handle.name):
                                os.unlink(handle.name)
                    elif target.exists():
                        target.unlink()
                    external_status = "delivered"
            except OSError as exc:
                external_status = "failed"
                external_error = str(exc)

        receipt = {
            "operation_id": operation_id,
            "paper_id": paper_id,
            "batch_path": str(local_path),
            "manifest_hash": manifest_hash,
            "candidate_count": len(candidates),
            "item_count": len({c["promptData"]["itemId"] for c in candidates}),
            "external_path": external_path,
            "external_status": external_status,
            **({"external_error": external_error} if external_error else {}),
            "pending_item_review": sorted(set(pending_item_review)),
            "replayed": False,
        }
        with self._write_lock:
            self.conn.begin_write()
            try:
                self.conn.execute(
                    "INSERT INTO blueprint_batches(paper_id,"
                    " manifest_hash, local_path, external_path, external_status,"
                    " candidate_count, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
                    (paper_id, manifest_hash, str(local_path), external_path,
                     external_status, len(candidates), db_mod.utc_now()),
                )
                batch_row = self.conn.execute(
                    "SELECT batch_id FROM blueprint_batches WHERE paper_id = %s"
                    " AND manifest_hash = %s", (paper_id, manifest_hash),
                ).fetchone()
                for candidate in candidates:
                    self.conn.execute(
                        "INSERT INTO blueprint_candidates(batch_id,"
                        " candidate_id, item_id, item_revision, note_id,"
                        " note_version, content_hash, payload)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
                        (batch_row["batch_id"], candidate["id"],
                         candidate["promptData"]["itemId"],
                         candidate["metadata"]["itemRevision"],
                         candidate["metadata"]["noteId"],
                         candidate["metadata"]["noteVersion"],
                         manifest_hash,
                         canonical_json(candidate)),
                    )
                self._insert_operation(
                    operation_id, request_hash, "blueprint_export",
                    actor_kind, actor_id, None, None, None, receipt)
                self.conn.execute("COMMIT")
            except Exception:
                self._rollback_quiet()
                raise
        return receipt

    def _verify_export_snapshot(self, frozen_ids: list) -> None:
        """复核冻结清单仍与当前数据库状态一致；漂移即拒绝投递。"""
        drifted: list[str] = []
        for binding in frozen_ids:
            item_row = self._item_row(binding["item_id"])
            note_row = self._note_row(binding["note_id"])
            inconsistent = (
                item_row is None
                or item_row["current_revision"] != binding["item_revision"]
                or note_row["deleted"]
                or note_row["current_version"] != binding["note_version"]
                or not self.is_approved(binding["item_id"], binding["item_revision"])
                or self._note_review_decision(
                    binding["note_id"], binding["note_version"]) != "approved"
            )
            if inconsistent:
                drifted.append(
                    f"{binding['item_id']}@r{binding['item_revision']}"
                    f":{binding['note_id']}@v{binding['note_version']}")
        if drifted:
            raise ReceiptError([
                error("EXPORT_SNAPSHOT_STALE",
                      f"导出期间状态变化：{'、'.join(drifted[:3])}"
                      f"（共 {len(drifted)} 项）；未投递任何文件，请刷新后重试",
                      item_key=drifted[0].split(":")[0], retryable=False)
            ])

    @staticmethod
    def _owns_external_file(target: Path, paper_id: str) -> bool:
        """外部目标文件是否本功能产出：可解析且 source/taskId 与本功能一致。"""
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("source") == "reviewed-bank-import"
            and payload.get("taskId") == paper_id
        )


    def _note_approved_at(self, note_id: str, version: int) -> str | None:
        row = self.conn.execute(
            "SELECT created_at FROM teaching_note_reviews"
            " WHERE note_id = %s AND version = %s AND decision = 'approved'"
            " ORDER BY review_id DESC LIMIT 1",
            (note_id, version),
        ).fetchone()
        return row["created_at"] if row else None
