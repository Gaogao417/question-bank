"""指定修订导出：teacher/student assignment + manifest。

- 导出固定到明确 revision；teacher/student 指向同一修订；
- 学生版剥离答案/解答与解答图（与 derive_student_assignment 的
  TEACHER_ONLY_KEYS 语义一致：answer/solution_steps/解答图不进学生版）；
- 文件先写入版本目录，manifest.json 最后落盘作为 ready 标记；
  失败登记 failed，人工修订仍在数据库，可按同一修订重试；
- 与旧 question-bank.yaml 消费链路的兼容适配属阶段 C，本导出不声明
  旧校验器通过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from question_bank import db as db_mod
from question_bank.canonical import canonical_json, sha256_bytes
from question_bank.errors import QbError, ReceiptError, error
from question_bank.models import ItemContent, marker_keys

EXPORTER_VERSION = "qb_assignment_exporter/1"
TEACHER_ONLY_BLOCK_KEYS = {"answer", "solution_steps", "solution_notes"}


def used_keys_by_body(content: ItemContent) -> dict[str, set[str]]:
    """按正文位置统计图片引用：学生端可见性由引用位置决定。

    只有题目正文引用的图是 clean（学生可见）；仅被答案/解答引用的图
    一律 teacher_only，即使声明的 role 是 stem。
    """
    return {
        "stem": set(marker_keys(content.stem)),
        "answer": set(marker_keys(content.answer)),
        "solution": set(marker_keys(content.solution)),
    }


def _block_images(content: ItemContent, used_keys: set[str]) -> list[dict[str, Any]]:
    stem_keys = set(marker_keys(content.stem))
    images = []
    for binding in content.images:
        if binding.key not in used_keys:
            continue
        clean = binding.key in stem_keys
        images.append({
            "key": binding.key,
            "image_path": f"media/{binding.key}.png",
            "width": binding.width,
            "variant": "prompt" if clean else "solution",
            "disclosure_policy": "clean" if clean else "teacher_only",
        })
    return images


def _teacher_assignment(detail: dict[str, Any], content: ItemContent) -> dict[str, Any]:
    # 教师版包含三块正文引用的全部图片（题目/答案/解答）。
    used = (
        set(marker_keys(content.stem))
        | set(marker_keys(content.answer))
        | set(marker_keys(content.solution))
    )
    block: dict[str, Any] = {
        "type": content.identity.question_type,
        "id": detail["item_id"],
        "points": content.identity.points,
        "stem_latex": content.stem,
        "images": _block_images(content, used),
    }
    if content.answer:
        block["answer"] = content.answer
    if content.solution:
        block["solution_steps"] = [content.solution]
    paper = detail.get("paper") or {}
    title = (paper.get("title") or detail["item_id"]) + (
        f" 第 {content.identity.question_number} 题"
    )
    return {
        "schema": "qb_assignment_export/v1",
        "meta": {
            "title": title,
            "version": "teacher",
            "show_answers": True,
            "item_id": detail["item_id"],
            "paper_id": content.identity.paper_id,
            "question_number": content.identity.question_number,
            "revision": detail["revision"],
            "content_hash": detail["content_hash"],
        },
        "render": {"template": "exam-zh-practice"},
        "sections": [{
            "id": "question",
            "title": content.identity.section_title or "题目",
            "type": "practice",
            "visibility": "both",
            "blocks": [block],
        }],
    }


def derive_student(teacher: dict[str, Any]) -> dict[str, Any]:
    """从 teacher 派生 student：剥离答案/解答与 teacher_only 图片。"""
    student = json.loads(json.dumps(teacher, ensure_ascii=False))
    student["meta"]["version"] = "student"
    student["meta"]["show_answers"] = False
    kept_sections = []
    for section in student.get("sections", []):
        if section.get("visibility") == "teacher":
            continue
        blocks = []
        for block in section.get("blocks", []):
            block = {
                key: value for key, value in block.items()
                if key not in TEACHER_ONLY_BLOCK_KEYS
            }
            block["images"] = [
                image for image in block.get("images", [])
                if image.get("disclosure_policy") != "teacher_only"
            ]
            blocks.append(block)
        section["blocks"] = blocks
        section["visibility"] = "student"
        kept_sections.append(section)
    student["sections"] = kept_sections
    return student


def export_revision(
    service,
    item_id: str,
    revision: int,
    *,
    allow_draft: bool = False,
) -> dict[str, Any]:
    """导出指定修订；返回 exports 行视图。幂等：ready 时重试返回既有结果。"""
    conn = service.conn
    detail = service.item_detail(item_id, revision)
    approved = service.is_approved(item_id, revision)
    if not approved and not allow_draft:
        raise ReceiptError([
            error(
                "EXPORT_NOT_APPROVED",
                f"修订 r{revision} 未获批准；草稿导出需显式 allow_draft",
                item_key=item_id, retryable=False,
            )
        ])
    content = service._content_of(item_id, revision)

    row = conn.execute(
        "SELECT * FROM exports WHERE item_id = %s AND revision = %s"
        " AND exporter_version = %s AND options_hash = ''",
        (item_id, revision, EXPORTER_VERSION),
    ).fetchone()
    export_id = row["export_id"] if row else None
    now = db_mod.utc_now()
    if export_id is None:
        cursor = conn.execute(
            "INSERT INTO exports(item_id, revision, exporter_version, options_hash,"
            " status, created_at) VALUES (%s,%s,%s,%s, 'pending', %s) RETURNING export_id",
            (item_id, revision, EXPORTER_VERSION, "", now),
        )
        export_id = cursor.fetchone()["export_id"]
    else:
        if row["status"] == "ready":
            return {
                "export_id": export_id,
                "status": "ready",
                "path": row["path"],
                "manifest_hash": row["manifest_hash"],
                "revision": revision,
                "replayed": True,
            }
        conn.execute(
            "UPDATE exports SET status = 'pending', error = NULL,"
            " completed_at = NULL WHERE export_id = %s",
            (export_id,),
        )
    conn.commit()

    out_dir = service.root / "exports" / item_id / f"r{revision}"
    tmp_dir = out_dir.parent / f".tmp-{item_id}-r{revision}"
    try:
        if tmp_dir.exists():
            _remove_tree(tmp_dir)
        (tmp_dir / "media").mkdir(parents=True, exist_ok=True)

        teacher = _teacher_assignment(detail, content)
        student = derive_student(teacher)

        (tmp_dir / "teacher.assignment.yaml").write_text(
            yaml.safe_dump(teacher, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        (tmp_dir / "student.assignment.yaml").write_text(
            yaml.safe_dump(student, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        used = (
            set(marker_keys(content.stem))
            | set(marker_keys(content.answer))
            | set(marker_keys(content.solution))
        )
        media_records = []
        for binding in content.images:
            if binding.key not in used:
                continue
            raw = service.assets.render_rel(
                service.asset_rel_path(binding.resolved_source()),
                crop_px=binding.crop_px,
                rotate=binding.rotate,
            )
            media_path = tmp_dir / "media" / f"{binding.key}.png"
            media_path.write_bytes(raw)
            media_records.append({
                "key": binding.key,
                "file": f"media/{binding.key}.png",
                "sha256": sha256_bytes(raw),
                "role": binding.role,
                "crop_px": binding.crop_px,
                "rotate": binding.rotate,
            })

        _assert_student_clean(student, content)

        manifest = {
            "schema": "qb_export_manifest/v1",
            "exporter_version": EXPORTER_VERSION,
            "item_id": item_id,
            "revision": revision,
            "content_hash": detail["content_hash"],
            "draft": not approved,
            "files": {
                "teacher": "teacher.assignment.yaml",
                "student": "student.assignment.yaml",
            },
            "media": media_records,
            "created_at": now,
        }
        manifest_bytes = canonical_json(manifest).encode("utf-8")
        (tmp_dir / "manifest.json").write_bytes(manifest_bytes)

        if out_dir.exists():
            _remove_tree(out_dir)
        tmp_dir.rename(out_dir)
        rel = str(out_dir.relative_to(service.root)).replace("\\", "/")
        conn.execute(
            "UPDATE exports SET status = 'ready', path = %s,"
            " manifest_hash = %s, completed_at = %s WHERE export_id = %s",
            (rel, sha256_bytes(manifest_bytes), db_mod.utc_now(), export_id),
        )
        conn.commit()
        return {
            "export_id": export_id,
            "status": "ready",
            "path": rel,
            "manifest_hash": sha256_bytes(manifest_bytes),
            "revision": revision,
            "replayed": False,
        }
    except Exception as exc:
        conn.execute(
            "UPDATE exports SET status = 'failed', error = %s, completed_at = %s"
            " WHERE export_id = %s",
            (str(exc), db_mod.utc_now(), export_id),
        )
        conn.commit()
        if tmp_dir.exists():
            _remove_tree(tmp_dir)
        if isinstance(exc, QbError):
            raise
        raise QbError([
            error("EXPORT_FAILED", f"导出失败: {exc}", item_key=item_id,
                  retryable=True)
        ]) from exc


def _assert_student_clean(student: dict[str, Any], content: ItemContent) -> None:
    """学生端泄漏边界：结构化剥离 + 长文本兜底。

    结构层：学生版 block 不得有 answer/solution_steps 字段，图片不得含
    teacher_only；答案值（如单字母 A、数字）可能合法出现在题干选项中，
    不做全文匹配。
    兜底层：较长的解答文本（>=20 字符）不得出现在学生版任何正文中。
    """
    leaks: list[str] = []
    for section in student.get("sections", []):
        for block in section.get("blocks", []):
            if "answer" in block:
                leaks.append("answer 字段")
            if "solution_steps" in block:
                leaks.append("solution_steps 字段")
            for image in block.get("images", []):
                if image.get("disclosure_policy") == "teacher_only":
                    leaks.append(f"teacher_only 图片 {image.get('key')}")
    if not leaks and len(content.solution.strip()) >= 20:
        student_text = yaml.safe_dump(student, allow_unicode=True)
        if content.solution.strip() in student_text:
            leaks.append("解答正文")
    if leaks:
        raise QbError([
            error(
                "STUDENT_LEAK",
                f"学生版导出泄漏了 {leaks}",
                retryable=False,
            )
        ])


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)
