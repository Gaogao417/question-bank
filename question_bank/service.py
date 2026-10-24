"""业务规则层：接收、保存、疑点处置、复核、发布与查询。

所有写路径遵循：
- 幂等：submission_id / operation_id 先查回执，同载荷重放、异载荷拒绝；
- 乐观并发：写事务（PostgreSQL transaction + advisory lock）内复核 base_revision；
- 修订只追加：人工修改产生新修订，原始誊录保持不变；
- 状态分离：接收/结构检查/复核/发布分别记录，不合成单一 complete。
"""

from __future__ import annotations

import hashlib
import json
import re
from question_bank import db as db_mod
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from typing import Any

import yaml

from question_bank import db as db_mod
from question_bank import catalog
from question_bank import export as export_mod
from question_bank.assets import AssetStore
from question_bank.auto_review import AutoReviewMixin
from question_bank.teaching import TeachingNotesMixin
from question_bank.canonical import (
    CONTENT_SCHEMA,
    HASH_VERSION,
    canonical_json,
    content_hash_payload,
)
from question_bank.errors import QbError, ReceiptError, error
from question_bank.models import (
    CandidateEnvelope,
    ContentPatch,
    ItemContent,
    Issue,
    OPERATION_ID_PATTERN,
    marker_keys,
    item_id_for,
    item_key_for,
)

VALIDATOR_NAME = "qb_content_gate"
VALIDATOR_VERSION = "1"

# 空 answer / solution 必须显式声明为待审疑点，不允许静默缺失。
ANSWER_ISSUE_TYPES = {"missing_answer", "unknown_answer", "suspected_wrong_answer"}
SOLUTION_ISSUE_TYPES = {"missing_solution"}
SOLUTION_REQUIRED_TYPES = {"problem", "short_answer", "fillin"}
CHOICE_ANSWER = re.compile(r"^[A-D]$")


def _write_locked(method):
    """串行化写路径：多线程（FastAPI 线程池）下保护显式事务边界。"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return method(self, *args, **kwargs)

    return wrapper


class QuestionBankService(AutoReviewMixin, TeachingNotesMixin):
    def __init__(self, root: str | Path, *, database_schema: str | None = None,
                 oss: bool | None = None):
        self.root = Path(root).expanduser().resolve()
        self._local = threading.local()
        self._database_schema = database_schema
        self._connections = []
        self._connections_lock = threading.Lock()
        self.conn  # Initialize the PostgreSQL schema before use.
        # oss=None 跟随环境变量；预检/临时根显式 False，不往真实 bucket 写。
        self.assets = AssetStore(self.root, oss=oss)
        self._write_lock = threading.RLock()
        # 上次进程中断留下的 running 调用标记为 unknown（刚启动的进程
        # 不可能有在途调用）；不自动重试付费接口。
        self.recover_interrupted_ai_operations()

    @property
    def conn(self) -> db_mod.Connection:
        """每线程独立 PostgreSQL 连接；事务级 advisory lock 串行化业务写入。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db_mod.connect(self.root, database_schema=self._database_schema)
            with self._connections_lock:
                self._connections.append(conn)
            self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        """Release only the background worker's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with self._connections_lock:
                if conn in self._connections:
                    self._connections.remove(conn)
                conn.close()
            del self._local.conn

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                conn.close()
            self._connections.clear()
        self._local = threading.local()

    # ==================================================================
    # AI 候选接收
    # ==================================================================

    def submit_candidate(
        self, package_dir: str | Path, *, actor_id: str = "cli"
    ) -> dict[str, Any]:
        """接收候选包并返回幂等回执。

        Media staging/validation is request-local and can overlap across calls.
        Thread-local connections and the schema's transaction advisory lock
        serialize authoritative revision/receipt decisions, including replays.
        """
        package = Path(package_dir).expanduser().resolve()
        envelope_path = None
        for name in ("candidate.json", "candidate.yaml", "candidate.yml"):
            candidate = package / name
            if candidate.is_file():
                envelope_path = candidate
                break
        if envelope_path is None:
            raise QbError([
                error(
                    "PACKAGE_MISSING_ENVELOPE",
                    f"候选包缺少 candidate.json/candidate.yaml: {package}",
                    fix_hint="包内必须包含 candidate.json 或 candidate.yaml",
                    retryable=False,
                )
            ])
        raw_text = envelope_path.read_text(encoding="utf-8")
        payload = (
            json.loads(raw_text)
            if envelope_path.suffix == ".json"
            else yaml.safe_load(raw_text)
        )
        try:
            envelope = CandidateEnvelope.model_validate(payload)
        except Exception as exc:  # pydantic ValidationError
            raise QbError([
                error(
                    "SCHEMA_INVALID",
                    f"候选包 schema 不合法: {exc}",
                    field_path="envelope",
                    retryable=False,
                )
            ]) from exc

        item_key = envelope.item_key()
        item_id = item_id_for(envelope.paper.paper_id, envelope.item.question_number)

        # ---- 包内媒体解析与持久化（字节落盘 + 资产行登记都在校验前完成）----
        registered: dict[str, dict[str, Any]] = {}

        def resolve_ref(ref, *, what: str) -> dict[str, Any]:
            return self._resolve_asset_ref(
                package, ref, registered, item_key=item_key, what=what
            )

        images = []
        for image in envelope.images:
            asset = resolve_ref(image.asset, what=f"images[{image.key}]")
            source = (
                resolve_ref(image.source_asset, what=f"images[{image.key}].source")
                if image.source_asset is not None
                else None
            )
            binding = {
                "key": image.key,
                "asset_id": asset["asset_id"],
                "role": image.role,
                "source_asset_id": source["asset_id"] if source else None,
                "crop_px": image.crop_px,
                "rotate": image.rotate,
                "width": image.width,
                "ordinal": image.ordinal,
            }
            images.append(binding)

        evidence = []
        for entry in envelope.evidence:
            asset = resolve_ref(entry.asset, what=f"evidence[{entry.role}]")
            evidence.append(
                {
                    "role": entry.role,
                    "asset_id": asset["asset_id"],
                    "page_number": entry.page_number,
                }
            )

        issues = [issue.model_dump() for issue in envelope.issues]

        # 责任边界：AI 不代人工裁图。候选只允许整图引用；裁框/旋转属于
        # Review UI 的人工调整（可声明 needs_human_crop 类疑点交人工处理）。
        ai_crop_errors = []
        for image in envelope.images:
            if image.crop_px is not None or image.rotate:
                what = []
                if image.crop_px is not None:
                    what.append("crop_px")
                if image.rotate:
                    what.append("rotate")
                ai_crop_errors.append(error(
                    "AI_CROP_NOT_ALLOWED",
                    f"AI 候选不允许携带 {'/'.join(what)}（代人工裁图）",
                    item_key=item_key,
                    field_path=f"images[{image.key}]",
                    fix_hint="提交整图引用并按需声明 needs_human_crop 疑点；裁框/旋转由人工在 Review UI 调整",
                    retryable=False,
                ))
        if ai_crop_errors:
            return self._persist_rejected_submission(
                envelope, item_key, ai_crop_errors, registered
            )

        # 资产行先入库（幂等），结构校验才能确认证据与媒体可引用。
        self._insert_asset_rows(registered.values())

        try:
            content = ItemContent.model_validate(
                {
                    "schema": CONTENT_SCHEMA,
                    "identity": {
                        "paper_id": envelope.paper.paper_id,
                        "question_number": envelope.item.question_number,
                        "question_type": envelope.item.question_type,
                        "points": envelope.item.points,
                        "section_title": envelope.item.section_title,
                    },
                    "stem": envelope.item.stem,
                    "answer": envelope.item.answer,
                    "solution": envelope.item.solution,
                    "images": images,
                    "evidence": evidence,
                    "issues": issues,
                }
            )
        except Exception as exc:
            raise QbError([
                error(
                    "SCHEMA_INVALID",
                    f"候选内容不合法: {exc}",
                    item_key=item_key,
                    retryable=False,
                )
            ]) from exc

        validation_errors = self.validate_content(content, item_key=item_key)
        if validation_errors:
            # 硬结构错误：拒绝且不创建半题。确定性拒绝持久化回执。
            receipt = self._persist_rejected_submission(
                envelope, item_key, validation_errors, registered
            )
            return receipt

        content_hash = content_hash_payload(content.to_dict())
        request_hash = self._submission_request_hash(envelope, registered)

        # ---- 写事务 ----
        self.conn.begin_write()
        try:
            existing = self.conn.execute(
                "SELECT * FROM submissions WHERE submission_id = %s",
                (envelope.submission_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] == request_hash:
                    self.conn.execute("COMMIT")
                    receipt = json.loads(existing["receipt"])
                    receipt["replayed"] = True
                    return receipt
                self.conn.execute("COMMIT")
                raise ReceiptError([
                    error(
                        "SUBMISSION_ID_REUSED",
                        "相同 submission_id 携带不同载荷，原回执保持不变",
                        item_key=item_key,
                        fix_hint="修正载荷后使用新的 submission_id 重试",
                        retryable=False,
                    )
                ])

            item_row = self._item_row(item_id)
            registered_list = list(registered.values())
            if item_row is None:
                self._create_item_from_candidate(
                    envelope, content, content_hash, registered_list, actor_id,
                    request_hash=request_hash,
                )
            else:
                # noop / conflict / rejected 已在 _resubmit_against_existing
                # 内持久化为 submissions 回执。
                self._resubmit_against_existing(
                    envelope, item_row, content, content_hash, registered_list,
                    actor_id, request_hash=request_hash,
                )

            row = self.conn.execute(
                "SELECT receipt FROM submissions WHERE submission_id = %s",
                (envelope.submission_id,),
            ).fetchone()
            self.conn.execute("COMMIT")
            receipt = json.loads(row["receipt"])
            receipt["replayed"] = False
            return receipt
        except Exception:
            self._rollback_quiet()
            raise

    def _resolve_asset_ref(
        self,
        package: Path,
        ref,
        registered: dict[str, dict[str, Any]],
        *,
        item_key: str,
        what: str,
    ) -> dict[str, Any]:
        cache_key = ref.ref
        if cache_key in registered:
            return registered[cache_key]
        if ref.ref.startswith("sha256:"):
            row = self.conn.execute(
                "SELECT * FROM assets WHERE asset_id = %s", (ref.ref,)
            ).fetchone()
            if row is None:
                raise QbError([
                    error(
                        "ASSET_NOT_REGISTERED",
                        f"{what} 引用的资产未登记: {ref.ref}",
                        item_key=item_key,
                        field_path=what,
                        retryable=False,
                    )
                ])
            info = dict(row)
        else:
            path = (package / ref.ref).resolve()
            if not path.is_relative_to(package) or not path.is_file():
                raise QbError([
                    error(
                        "ASSET_MISSING",
                        f"{what} 引用的包内文件不存在: {ref.ref}",
                        item_key=item_key,
                        field_path=what,
                        retryable=False,
                    )
                ])
            raw = path.read_bytes()
            info = self.assets.register_bytes(raw)
            if ref.sha256 is not None and info["sha256"] != ref.sha256:
                raise QbError([
                    error(
                        "HASH_MISMATCH",
                        f"{what} 声明 hash 与实际文件不符: {ref.ref}",
                        item_key=item_key,
                        field_path=what,
                        retryable=False,
                    )
                ])
        registered[cache_key] = info
        return info

    def _persist_rejected_submission(
        self,
        envelope: CandidateEnvelope,
        item_key: str,
        errors: list[dict[str, Any]],
        registered: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        receipt = {
            "submission_id": envelope.submission_id,
            "status": "rejected",
            "item_key": item_key,
            "item_id": None,
            "revision": None,
            "content_hash": None,
            "hash_version": None,
            "replayed": False,
            "review_url": None,
            "errors": errors,
        }
        request_hash = self._submission_request_hash(envelope, registered)
        self.conn.begin_write()
        try:
            existing = self.conn.execute(
                "SELECT * FROM submissions WHERE submission_id = %s",
                (envelope.submission_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] == request_hash:
                    self.conn.execute("COMMIT")
                    replay = json.loads(existing["receipt"])
                    replay["replayed"] = True
                    return replay
                raise ReceiptError([error(
                    "SUBMISSION_ID_REUSED",
                    "相同 submission_id 携带不同载荷，原回执保持不变",
                    item_key=item_key, retryable=False,
                )])
            self.conn.execute(
                "INSERT INTO submissions(submission_id, request_hash, item_key,"
                " status, receipt, errors, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    envelope.submission_id,
                    request_hash,
                    item_key,
                    "rejected",
                    canonical_json(receipt),
                    canonical_json(errors),
                    db_mod.utc_now(),
                ),
            )
            self.conn.execute("COMMIT")
        except db_mod.IntegrityError as exc:
            self.conn.execute("ROLLBACK")
            raise ReceiptError([
                error("SUBMISSION_ID_REUSED", str(exc), item_key=item_key,
                      retryable=False)
            ]) from exc
        except Exception:
            self._rollback_quiet()
            raise
        return receipt

    def _submission_request_hash(
        self,
        envelope: CandidateEnvelope,
        registered: dict[str, dict[str, Any]],
    ) -> str:
        """覆盖提交意图与完整语义载荷；与 content_hash（只定义内容）分开。"""
        return "sha256:" + hashlib.sha256(
            canonical_json(
                {
                    "base_revision": envelope.base_revision,
                    "paper": envelope.paper.model_dump(),
                    "item": envelope.item.model_dump(),
                    "images": [image.model_dump() for image in envelope.images],
                    "evidence": [entry.model_dump() for entry in envelope.evidence],
                    "issues": [issue.model_dump() for issue in envelope.issues],
                    "asset_hashes": {
                        key: info["sha256"] for key, info in registered.items()
                    },
                }
            ).encode("utf-8")
        ).hexdigest()

    # ==================================================================
    # 结构校验（接收/保存共用）
    # ==================================================================

    def validate_content(
        self, content: ItemContent, *, item_key: str = ""
    ) -> list[dict[str, Any]]:
        """轻量接收校验：正文存在、图片引用有效、可保存可呈现（设计 §9）。

        数学正确性与内容完整性由内容复核负责，不在这里检查。
        """
        errors: list[dict[str, Any]] = []
        if not content.stem.strip():
            errors.append(error("STEM_EMPTY", "题目正文不能为空",
                                item_key=item_key, field_path="body.stem"))

        open_issues = [i for i in content.issues if i.state == "open"]
        if not content.answer.strip():
            declared = any(i.type in ANSWER_ISSUE_TYPES for i in open_issues)
            if not declared:
                errors.append(error(
                    "ANSWER_UNDECLARED_EMPTY",
                    "答案为空且未声明缺答案疑点；不允许静默缺失",
                    item_key=item_key, field_path="body.answer",
                    fix_hint="补齐答案，或声明 missing_answer/unknown_answer 疑点",
                ))
        elif content.identity.question_type == "choice" and not CHOICE_ANSWER.match(
            content.answer.strip()
        ):
            errors.append(error(
                "ANSWER_FORMAT_CHOICE",
                "选择题答案必须是单个选项字母 A-D",
                item_key=item_key, field_path="body.answer",
            ))
        if not content.solution.strip() and (
            content.identity.question_type in SOLUTION_REQUIRED_TYPES
        ):
            declared = any(i.type in SOLUTION_ISSUE_TYPES for i in open_issues)
            if not declared:
                errors.append(error(
                    "SOLUTION_UNDECLARED_EMPTY",
                    "解答为空且未声明缺解答疑点",
                    item_key=item_key, field_path="body.solution",
                    fix_hint="补齐解答，或声明 missing_solution 疑点",
                ))

        binding_keys = [binding.key for binding in content.images]
        duplicates = {key for key in binding_keys if binding_keys.count(key) > 1}
        for key in sorted(duplicates):
            errors.append(error("IMAGE_KEY_DUPLICATE", f"图片 key 重复: {key}",
                                item_key=item_key, field_path=f"images[{key}]"))

        for field_name, text in (
            ("body.stem", content.stem),
            ("body.answer", content.answer),
            ("body.solution", content.solution),
        ):
            for key in marker_keys(text):
                if key not in binding_keys:
                    errors.append(error(
                        "IMAGE_MARKER_UNBOUND",
                        f"正文引用的图片未绑定: {key}",
                        item_key=item_key, field_path=field_name,
                        fix_hint="在 images 中补充该 key 的绑定，或修正正文标记",
                    ))

        def check_binding(binding):
            found = []
            try:
                self.assets.validate_binding_renderable(binding)
            except QbError as exc:
                for err in exc.errors:
                    err["item_key"] = item_key
                    err["field_path"] = f"images[{binding.key}]"
                    found.append(err)
            return found

        # Keep all SQL on the transaction's owning thread. Only independent
        # object reads/decoding run concurrently, bounded to four workers.
        evidence_paths = {}
        if content.evidence:
            rows = self.conn.execute(
                "SELECT asset_id, path FROM assets WHERE asset_id = ANY(%s)",
                ([page.asset_id for page in content.evidence],),
            ).fetchall()
            evidence_paths = {row['asset_id']: row['path'] for row in rows}

        def check_page(page):
            try:
                path = evidence_paths.get(page.asset_id)
                if path is None:
                    raise QbError([])
                self.assets._load_bytes(path)
            except QbError:
                return [error(
                    "EVIDENCE_ASSET_UNAVAILABLE",
                    f"证据页资产不可用: {page.asset_id}",
                    item_key=item_key, field_path=f"evidence[{page.role}]",
                    retryable=False,
                )]
            return []

        jobs = [(check_binding, binding) for binding in content.images]
        jobs.extend((check_page, page) for page in content.evidence)
        if self.assets.oss and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
                futures = [pool.submit(fn, value) for fn, value in jobs]
                # Preserve input order for deterministic validation receipts.
                for future in futures:
                    errors.extend(future.result())
        else:
            for fn, value in jobs:
                errors.extend(fn(value))
        return errors

    def _insert_asset_rows(self, asset_infos) -> None:
        for info in asset_infos:
            self.conn.execute(
                "INSERT INTO assets(asset_id, sha256, path, mime,"
                " size_bytes, width, height, source_kind, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
                (
                    info["asset_id"], info["sha256"], info["path"], info["mime"],
                    info["size_bytes"], info["width"], info["height"],
                    info.get("source_kind", "imported"), db_mod.utc_now(),
                ),
            )

    def _run_and_record_check(
        self,
        item_id: str,
        revision: int,
        content: ItemContent,
        *,
        registered_assets: list[dict[str, Any]] | None = None,
    ) -> None:
        """在写事务内登记资产、执行结构检查并写 checks 行。"""
        if registered_assets:
            for info in registered_assets:
                self.conn.execute(
                    "INSERT INTO assets(asset_id, sha256, path, mime,"
                    " size_bytes, width, height, source_kind, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
                    (
                        info["asset_id"],
                        info["sha256"],
                        info["path"],
                        info["mime"],
                        info["size_bytes"],
                        info["width"],
                        info["height"],
                        info.get("source_kind", "imported"),
                        db_mod.utc_now(),
                    ),
                )
        item_key = item_key_for(
            content.identity.paper_id, content.identity.question_number
        )
        errors = self.validate_content(content, item_key=item_key)
        warnings: list[dict[str, Any]] = []
        used = set(marker_keys(content.stem)) | set(marker_keys(content.solution))
        for binding in content.images:
            if binding.key not in used:
                warnings.append(error(
                    "IMAGE_BINDING_UNUSED",
                    f"图片绑定未被正文引用: {binding.key}",
                    item_key=item_key,
                    field_path=f"images[{binding.key}]",
                ))
        self.conn.execute(
            "INSERT INTO checks(item_id, revision, validator, validator_version,"
            " passed, errors, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                item_id,
                revision,
                VALIDATOR_NAME,
                VALIDATOR_VERSION,
                1 if not errors else 0,
                canonical_json({"errors": errors, "warnings": warnings}),
                db_mod.utc_now(),
            ),
        )

    def _create_item_from_candidate(
        self,
        envelope: CandidateEnvelope,
        content: ItemContent,
        content_hash: str,
        registered_assets: list[dict[str, Any]],
        actor_id: str,
        *,
        request_hash: str,
    ) -> str:
        """事务内首次创建题目与 r1；返回 accepted 状态。"""
        now = db_mod.utc_now()
        paper = envelope.paper
        existing_paper = self.conn.execute(
            "SELECT * FROM papers WHERE paper_id = %s", (paper.paper_id,)
        ).fetchone()
        if existing_paper is None:
            self.conn.execute(
                "INSERT INTO papers(paper_id, title, grade, subject, source_archive,"
                " expected_state, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    paper.paper_id,
                    paper.title,
                    paper.grade,
                    paper.subject,
                    paper.source_archive,
                    "unknown",
                    now,
                ),
            )
        else:
            # 只补空字段，不覆盖已有元信息。
            for field in ("title", "grade", "subject", "source_archive"):
                if not existing_paper[field] and getattr(paper, field):
                    self.conn.execute(
                        f"UPDATE papers SET {field} = %s WHERE paper_id = %s",
                        (getattr(paper, field), paper.paper_id),
                    )

        item_id = item_id_for(paper.paper_id, envelope.item.question_number)
        self.conn.execute(
            "INSERT INTO items(item_id, paper_id, question_number, current_revision,"
            " created_at) VALUES (%s,%s,%s,NULL,%s)",
            (item_id, paper.paper_id, envelope.item.question_number, now),
        )
        self.conn.execute(
            "INSERT INTO paper_items(paper_id, question_number, item_id,"
            " section_title) VALUES (%s,%s,%s,%s)",
            (
                paper.paper_id,
                envelope.item.question_number,
                item_id,
                envelope.item.section_title,
            ),
        )
        revision = 1
        self.conn.execute(
            "INSERT INTO revisions(item_id, revision, parent_revision, model_version,"
            " hash_version, content_hash, content, actor_kind, actor_id,"
            " origin_submission_id, origin_operation_id, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                item_id,
                revision,
                None,
                content.schema,
                HASH_VERSION,
                content_hash,
                canonical_json(content.to_dict()),
                "ai",
                actor_id,
                envelope.submission_id,
                None,
                now,
            ),
        )
        self.conn.execute(
            "UPDATE items SET current_revision = %s WHERE item_id = %s",
            (revision, item_id),
        )
        self._copy_bindings_and_issues(item_id, revision, content)
        self._run_and_record_check(
            item_id, revision, content, registered_assets=registered_assets
        )
        open_blocking = any(
            issue.state == "open" and issue.blocking for issue in content.issues
        )
        status = "accepted_with_issues" if open_blocking else "accepted"
        receipt = {
            "submission_id": envelope.submission_id,
            "status": status,
            "item_key": envelope.item_key(),
            "item_id": item_id,
            "revision": revision,
            "content_hash": content_hash,
            "hash_version": HASH_VERSION,
            "replayed": False,
            "review_url": None,
            "errors": [],
        }
        self._insert_submission(
            envelope, envelope.item_key(), status, item_id, revision,
            content_hash, receipt, request_hash=request_hash,
        )
        return status

    def _resubmit_against_existing(
        self,
        envelope: CandidateEnvelope,
        item_row: dict,
        content: ItemContent,
        content_hash: str,
        registered_assets: list[dict[str, Any]],
        actor_id: str,
        *,
        request_hash: str,
    ) -> str | None:
        """已有题的再次提交：noop / conflict / 拒绝（B 不做 AI 覆盖修订）。"""
        item_id = item_row["item_id"]
        current = item_row["current_revision"]
        if current is None:
            # items 行存在但无修订（不应发生）；按首次创建处理。
            return self._create_item_from_candidate(
                envelope, content, content_hash, registered_assets, actor_id,
                request_hash=request_hash,
            )
        current_content = self._content_of(item_id, current)
        item_key = envelope.item_key()

        def persist(status: str, errors: list[dict[str, Any]], *, revision=None,
                    content_hash_value=None, request_hash=None) -> None:
            receipt = {
                "submission_id": envelope.submission_id,
                "status": status,
                "item_key": item_key,
                "item_id": item_id,
                "revision": revision,
                "content_hash": content_hash_value,
                "hash_version": None if revision is None else HASH_VERSION,
                "replayed": False,
                "review_url": None,
                "errors": errors,
            }
            self._insert_submission(
                envelope, item_key, status, item_id, revision,
                content_hash_value, receipt, errors=errors,
                request_hash=request_hash,
            )

        same_content = content_hash_payload(current_content.to_dict()) == content_hash
        same_issues = canonical_json(
            [i.model_dump() for i in current_content.issues]
        ) == canonical_json([i.model_dump() for i in content.issues])

        if envelope.base_revision is None:
            persist(
                "rejected",
                [error(
                    "SUBMISSION_CONFLICT",
                    f"题目已存在（当前修订 r{current}），重复提交必须携带 base_revision",
                    item_key=item_key,
                    fix_hint=f"携带 base_revision={current} 重新提交",
                    retryable=False,
                )],
            )
            return None
        if envelope.base_revision != current:
            persist(
                "conflict",
                [error(
                    "BASE_REVISION_STALE",
                    f"base_revision={envelope.base_revision} 已过期，当前修订 r{current}",
                    item_key=item_key,
                    fix_hint=f"以 base_revision={current} 重新对比后提交",
                    retryable=False,
                )],
                request_hash=request_hash,
            )
            return None
        if same_content and same_issues:
            persist("noop", [], revision=current,
                    content_hash_value=content_hash, request_hash=request_hash)
            return None
        persist(
            "rejected",
            [error(
                "REVISION_REQUIRES_PROPOSAL",
                "已有题目的不同内容提交在阶段 B 不自动覆盖；人工修改请走 Review UI 保存",
                item_key=item_key,
                fix_hint="通过 Review UI 编辑保存，或等待 proposal 流程（阶段 D）",
                retryable=False,
            )],
            request_hash=request_hash,
        )
        return None

    def _insert_submission(
        self,
        envelope: CandidateEnvelope,
        item_key: str,
        status: str,
        item_id: str | None,
        revision: int | None,
        content_hash: str | None,
        receipt: dict[str, Any],
        *,
        errors: list[dict[str, Any]] | None = None,
        request_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO submissions(submission_id, request_hash, item_key, status,"
            " item_id, revision, content_hash, receipt, errors, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                envelope.submission_id,
                request_hash
                or "pending:" + canonical_json(
                    {"submission_id": envelope.submission_id, "status": status}
                ),
                item_key,
                status,
                item_id,
                revision,
                content_hash,
                canonical_json(receipt),
                canonical_json(errors or []),
                db_mod.utc_now(),
            ),
        )

    # ==================================================================
    # UI 保存（人工修订）
    # ==================================================================

    @_write_locked
    def save_edit(
        self,
        item_id: str,
        request: dict[str, Any],
        *,
        actor_kind: str = "human",
        actor_id: str = "review-ui",
        issue_resolution: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from question_bank.models import SaveEditRequest

        try:
            parsed = SaveEditRequest.model_validate(request)
        except Exception as exc:
            raise QbError([
                error("SCHEMA_INVALID", f"保存请求不合法: {exc}",
                      item_key=item_id, retryable=False)
            ]) from exc
        request_hash = canonical_json(
            {k: v for k, v in request.items() if k != "operation_id"}
        )

        self.conn.begin_write()
        try:
            existing_op = self.conn.execute(
                "SELECT * FROM operations WHERE operation_id = %s",
                (parsed.operation_id,),
            ).fetchone()
            if existing_op is not None:
                self.conn.execute("COMMIT")
                if existing_op["request_hash"] == request_hash:
                    receipt = json.loads(existing_op["receipt"])
                    receipt["replayed"] = True
                    return receipt
                raise ReceiptError([
                    error(
                        "OPERATION_ID_REUSED",
                        "相同 operation_id 携带不同载荷",
                        item_key=item_id,
                        retryable=False,
                    )
                ])

            item_row = self._item_row(item_id)
            if item_row is None or item_row["current_revision"] is None:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("ITEM_NOT_FOUND", f"题目不存在或尚无修订: {item_id}",
                          item_key=item_id, retryable=False)
                ])
            current = item_row["current_revision"]
            if parsed.base_revision != current:
                conflict = self._build_conflict(
                    item_id, parsed.base_revision, current, parsed
                )
                self.conn.execute("COMMIT")
                raise ReceiptError(conflict["errors"], conflict=conflict)

            base_content = self._content_of(item_id, current)
            new_data = base_content.to_dict()
            for field in ("stem", "answer", "solution", "question_type",
                          "points", "section_title", "images", "evidence"):
                value = getattr(parsed, field)
                if value is not None:
                    if field in ("question_type", "points", "section_title"):
                        new_data["identity"][field] = value
                    else:
                        new_data[field] = (
                            [v.model_dump() for v in value]
                            if field in ("images", "evidence")
                            else value
                        )
            issues_data = [i.model_dump() for i in base_content.issues]
            issue_changes = self._apply_issue_ops(issues_data, parsed.issue_ops,
                                                  item_id, issue_resolution=issue_resolution)
            new_data["issues"] = issues_data
            new_content = ItemContent.model_validate(new_data)
            validation_errors = self.validate_content(
                new_content, item_key=item_id
            )
            if validation_errors:
                raise QbError(validation_errors)

            old_hash = content_hash_payload(base_content.to_dict())
            new_hash = content_hash_payload(new_content.to_dict())
            changed = old_hash != new_hash or issue_changes > 0
            if not changed:
                receipt = {
                    "operation_id": parsed.operation_id,
                    "item_id": item_id,
                    "status": "noop",
                    "base_revision": current,
                    "revision": current,
                    "content_hash": old_hash,
                    "changed": False,
                    "replayed": False,
                }
                self._insert_operation(
                    parsed.operation_id, request_hash, "save_edit",
                    actor_kind, actor_id, item_id, current, current, receipt,
                )
                self.conn.execute("COMMIT")
                return receipt

            revision = current + 1
            now = db_mod.utc_now()
            self.conn.execute(
                "INSERT INTO revisions(item_id, revision, parent_revision,"
                " model_version, hash_version, content_hash, content, actor_kind,"
                " actor_id, origin_submission_id, origin_operation_id, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    item_id, revision, current, new_content.schema, HASH_VERSION,
                    new_hash, canonical_json(new_content.to_dict()),
                    actor_kind, actor_id, None, parsed.operation_id, now,
                ),
            )
            self.conn.execute(
                "UPDATE items SET current_revision = %s WHERE item_id = %s",
                (revision, item_id),
            )
            self._copy_bindings_and_issues(item_id, revision, new_content)
            self._run_and_record_check(item_id, revision, new_content)
            receipt = {
                "operation_id": parsed.operation_id,
                "item_id": item_id,
                "status": "saved",
                "base_revision": current,
                "revision": revision,
                "content_hash": new_hash,
                "changed": True,
                "replayed": False,
            }
            self._insert_operation(
                parsed.operation_id, request_hash, "save_edit", actor_kind,
                actor_id, item_id, current, revision, receipt,
            )
            self.conn.execute("COMMIT")
            return receipt
        except Exception:
            self._rollback_quiet()
            raise

    # ------------------------------------------------------------------
    # Proposal（阶段 D）：局部 AI/程序建议。建议独立保存，不直接覆盖当前
    # 内容；接受时校验 base_revision 并复用 save_edit 的写事务语义。
    # ------------------------------------------------------------------

    _PROPOSAL_FIELDS = ("stem", "answer", "solution")

    def submit_proposal(
        self,
        item_id: str,
        request: dict[str, Any],
        *,
        actor_kind: str = "ai",
        actor_id: str = "client-agent",
    ) -> dict[str, Any]:
        from question_bank.models import normalize_text

        proposal_id = str(request.get("proposal_id", "")).strip()
        if not OPERATION_ID_PATTERN.match(proposal_id):
            raise QbError([
                error("SCHEMA_INVALID", "proposal_id 只能包含字母、数字、下划线、连字符",
                      item_key=item_id, retryable=False)
            ])
        base_revision = request.get("base_revision")
        if not isinstance(base_revision, int) or base_revision <= 0:
            raise QbError([
                error("SCHEMA_INVALID", "base_revision 必须是正整数",
                      item_key=item_id, retryable=False)
            ])
        patch: dict[str, str] = {}
        for field in self._PROPOSAL_FIELDS:
            value = request.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise QbError([
                    error("SCHEMA_INVALID", f"{field} 必须是字符串",
                          item_key=item_id, retryable=False)
                ])
            patch[field] = normalize_text(value)
        if not patch:
            raise QbError([
                error("SCHEMA_INVALID", "建议至少包含 stem/answer/solution 之一",
                      item_key=item_id, retryable=False)
            ])
        reason = str(request.get("reason", "") or "")

        self.conn.begin_write()
        try:
            existing = self.conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = %s",
                (proposal_id,),
            ).fetchone()
            payload = {"item_id": item_id, "base_revision": base_revision,
                       "patch": patch, "reason": reason}
            if existing is not None:
                self.conn.execute("COMMIT")
                same = (existing["item_id"] == item_id
                        and existing["base_revision"] == base_revision
                        and json.loads(existing["patch"]) == patch
                        and existing["reason"] == reason)
                if not same:
                    raise QbError([
                        error("PROPOSAL_ID_REUSED",
                              "相同 proposal_id 携带不同载荷",
                              item_key=item_id, retryable=False)
                    ])
                return self._proposal_receipt(existing, replayed=True)

            item_row = self._item_row(item_id)
            if item_row is None or item_row["current_revision"] is None:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("ITEM_NOT_FOUND", f"题目不存在或尚无修订: {item_id}",
                          item_key=item_id, retryable=False)
                ])
            current = item_row["current_revision"]
            if base_revision != current:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("BASE_REVISION_STALE",
                          f"建议基于修订 {base_revision}，当前已是 {current}",
                          item_key=item_id, retryable=False,
                          fix_hint="重新读取当前修订后再提交建议")
                ])
            now = db_mod.utc_now()
            self.conn.execute(
                "INSERT INTO proposals(proposal_id, item_id, base_revision,"
                " patch, reason, created_by_kind, created_by, status, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,'open',%s)",
                (proposal_id, item_id, base_revision, canonical_json(patch),
                 reason, actor_kind, actor_id, now),
            )
            self.conn.execute("COMMIT")
            row = self.conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = %s",
                (proposal_id,),
            ).fetchone()
            return self._proposal_receipt(row)
        except Exception:
            self._rollback_quiet()
            raise

    def _proposal_receipt(self, row: dict, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "item_id": row["item_id"],
            "base_revision": row["base_revision"],
            "patch": json.loads(row["patch"]),
            "reason": row["reason"],
            "status": row["status"],
            "accepted_revision": row["accepted_revision"],
            "replayed": replayed,
        }

    def list_proposals(self, item_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE item_id = %s ORDER BY created_at, proposal_id",
            (item_id,),
        ).fetchall()
        return [self._proposal_receipt(r) for r in rows]

    def accept_proposal(self, proposal_id: str, request: dict[str, Any], *,
                        issue_resolution: dict[str, str] | None = None) -> dict[str, Any]:
        operation_id = str(request.get("operation_id", "")).strip()
        if not OPERATION_ID_PATTERN.match(operation_id):
            raise QbError([
                error("SCHEMA_INVALID", "operation_id 只能包含字母、数字、下划线、连字符",
                      item_key=proposal_id, retryable=False)
            ])
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id = %s", (proposal_id,)
        ).fetchone()
        if row is None:
            raise QbError([
                error("PROPOSAL_NOT_FOUND", f"建议不存在: {proposal_id}",
                      item_key=proposal_id, retryable=False)
            ])
        if row["status"] == "accepted":
            receipt = self._proposal_receipt(row)
            receipt["replayed"] = True
            return receipt
        if row["status"] != "open":
            raise QbError([
                error("PROPOSAL_NOT_OPEN",
                      f"建议当前状态为 {row['status']}，不能接受",
                      item_key=row["item_id"], retryable=False)
            ])
        item_id = row["item_id"]
        item_row = self._item_row(item_id)
        current = item_row["current_revision"] if item_row else None
        if current is None or current != row["base_revision"]:
            self.conn.begin_write()
            try:
                self.conn.execute(
                    "UPDATE proposals SET status = 'expired', decided_at = %s"
                    " WHERE proposal_id = %s AND status = 'open'",
                    (db_mod.utc_now(), proposal_id),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self._rollback_quiet()
                raise
            raise QbError([
                error("PROPOSAL_BASE_STALE",
                      f"建议基于修订 {row['base_revision']}，当前已是 {current}，"
                      "已标记过期，未覆盖当前内容",
                      item_key=item_id, retryable=False,
                      fix_hint="基于当前修订重新提交建议")
            ])

        save_request: dict[str, Any] = {"base_revision": current,
                                        "operation_id": operation_id}
        save_request.update(json.loads(row["patch"]))
        # 修改向导在采用建议时把疑点处置并入同一次保存（用户确认才算解决）。
        issue_ops = request.get("issue_ops")
        if isinstance(issue_ops, list) and issue_ops:
            save_request["issue_ops"] = issue_ops
        save_receipt = self.save_edit(
            item_id, save_request,
            actor_kind="ai", actor_id=f"proposal:{proposal_id}",
            issue_resolution=issue_resolution,
        )
        if save_receipt["status"] == "saved":
            new_status, accepted_rev = "accepted", save_receipt["revision"]
        else:
            new_status, accepted_rev = "accepted", save_receipt["revision"]
        self.conn.begin_write()
        try:
            self.conn.execute(
                "UPDATE proposals SET status = %s, accepted_revision = %s,"
                " accept_operation_id = %s, decided_at = %s"
                " WHERE proposal_id = %s AND status = 'open'",
                (new_status, accepted_rev, operation_id, db_mod.utc_now(),
                 proposal_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback_quiet()
            raise
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id = %s", (proposal_id,)
        ).fetchone()
        receipt = self._proposal_receipt(row)
        receipt["save_receipt"] = save_receipt
        return receipt

    def reject_proposal(self, proposal_id: str, request: dict[str, Any]) -> dict[str, Any]:
        reason = str(request.get("reason", "") or "")
        self.conn.begin_write()
        try:
            row = self.conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = %s", (proposal_id,)
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("PROPOSAL_NOT_FOUND", f"建议不存在: {proposal_id}",
                          item_key=proposal_id, retryable=False)
                ])
            if row["status"] != "open":
                self.conn.execute("COMMIT")
                raise QbError([
                    error("PROPOSAL_NOT_OPEN",
                          f"建议当前状态为 {row['status']}，不能驳回",
                          item_key=row["item_id"], retryable=False)
                ])
            self.conn.execute(
                "UPDATE proposals SET status = 'rejected', decided_at = %s"
                " WHERE proposal_id = %s",
                (db_mod.utc_now(), proposal_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback_quiet()
            raise
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id = %s", (proposal_id,)
        ).fetchone()
        return self._proposal_receipt(row)

    def _apply_issue_ops(
        self,
        issues_data: list[dict[str, Any]],
        issue_ops: list[dict[str, Any]] | None,
        item_id: str,
        *, issue_resolution: dict[str, str] | None = None,
    ) -> int:
        if not issue_ops:
            return 0
        by_id = {issue["issue_id"]: issue for issue in issues_data}
        changes = 0
        for op in issue_ops:
            issue_id = str(op.get("issue_id", ""))
            state = op.get("state")
            if issue_id not in by_id:
                raise QbError([
                    error("ISSUE_NOT_FOUND", f"疑点不存在: {issue_id}",
                          item_key=item_id, field_path=f"issues[{issue_id}]",
                          retryable=False)
                ])
            if state not in ("open", "resolved", "dismissed"):
                raise QbError([
                    error("ISSUE_STATE_INVALID",
                          f"疑点状态必须是 open/resolved/dismissed: {state}",
                          item_key=item_id, field_path=f"issues[{issue_id}]",
                          retryable=False)
                ])
            issue = by_id[issue_id]
            if issue["state"] != state or op.get("note"):
                changes += 1
            issue["state"] = state
            resolution = dict(issue.get("resolution") or {})
            if op.get("note"):
                resolution["note"] = str(op["note"])
            resolution["actor"] = (issue_resolution or {}).get("actor_kind", "human")
            if issue_resolution:
                resolution["actor_id"] = issue_resolution["actor_id"]
            resolution["at"] = db_mod.utc_now()
            issue["resolution"] = resolution
        return changes

    def _build_conflict(
        self, item_id: str, base: int, current: int, parsed
    ) -> dict[str, Any]:
        """构造 409 冲突负载：当前修订 + 基线后变化字段与本次编辑的交集。"""
        base_content = self._content_of(item_id, base)
        current_content = self._content_of(item_id, current)
        changed_fields: list[str] = []
        for field in ("stem", "answer", "solution"):
            if getattr(base_content, field) != getattr(current_content, field):
                changed_fields.append(f"body.{field}")
        for field in ("question_type", "points", "section_title"):
            if getattr(base_content.identity, field) != getattr(
                current_content.identity, field
            ):
                changed_fields.append(f"identity.{field}")
        if [b.model_dump() for b in base_content.images] != [
            b.model_dump() for b in current_content.images
        ]:
            changed_fields.append("images")
        if [e.model_dump() for e in base_content.evidence] != [
            e.model_dump() for e in current_content.evidence
        ]:
            changed_fields.append("evidence")
        attempted: list[str] = []
        for field in ("stem", "answer", "solution", "question_type", "points",
                      "section_title", "images", "evidence"):
            if getattr(parsed, field) is not None:
                attempted.append(field)
        return {
            "item_id": item_id,
            "base_revision": base,
            "current_revision": current,
            "current_content_hash": content_hash_payload(
                current_content.to_dict()
            ),
            "changed_fields": changed_fields,
            "attempted_fields": attempted,
            "conflict_fields": [
                f for f in changed_fields
                if f.split(".", 1)[-1] in attempted or f in attempted
            ],
            "errors": [error(
                "BASE_REVISION_CONFLICT",
                f"base_revision={base} 已过期，当前修订 r{current}",
                item_key=item_id,
                fix_hint="保留本地修改，刷新最新内容后基于新修订重新保存",
                retryable=False,
            )],
        }

    # ==================================================================
    # 复核与发布
    # ==================================================================

    @_write_locked
    def record_review(
        self,
        item_id: str,
        request: dict[str, Any],
        *,
        actor_kind: str = "human",
        actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        from question_bank.models import ReviewRequest

        try:
            parsed = ReviewRequest.model_validate(request)
        except Exception as exc:
            raise QbError([
                error("SCHEMA_INVALID", f"复核请求不合法: {exc}",
                      item_key=item_id, retryable=False)
            ]) from exc
        request_hash = canonical_json(
            {"item_id": item_id, **request, "operation_id": parsed.operation_id}
        )
        self.conn.begin_write()
        try:
            replay = self._replay_operation(parsed.operation_id, request_hash)
            if replay is not None:
                self.conn.execute("COMMIT")
                return replay
            item_row = self._item_row(item_id)
            if item_row is None or item_row["current_revision"] is None:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("ITEM_NOT_FOUND", f"题目不存在: {item_id}",
                          item_key=item_id, retryable=False)
                ])
            current = item_row["current_revision"]
            if parsed.base_revision != current:
                self.conn.execute("COMMIT")
                raise ReceiptError([
                    error(
                        "BASE_REVISION_CONFLICT",
                        f"复核基线 r{parsed.base_revision} 不是当前修订 r{current}",
                        item_key=item_id, retryable=False,
                    )
                ])
            if parsed.decision == "approved":
                blockers = self.approval_blockers(item_id, current)
                if blockers:
                    self.conn.execute("COMMIT")
                    raise ReceiptError([
                        error(
                            "APPROVAL_BLOCKED",
                            "当前修订不满足批准条件: " + "; ".join(blockers),
                            item_key=item_id,
                            fix_hint="先保存通过结构检查、解决阻断疑点并补齐答案",
                            retryable=False,
                        )
                    ])
            cursor = self.conn.execute(
                "INSERT INTO reviews(item_id, revision, decision, actor_kind,"
                " actor_id, note, operation_id, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING review_id",
                (item_id, current, parsed.decision, actor_kind, actor_id,
                 parsed.note, parsed.operation_id, db_mod.utc_now()),
            )
            receipt = {
                "operation_id": parsed.operation_id,
                "item_id": item_id,
                "status": "reviewed",
                "decision": parsed.decision,
                "revision": current,
                "review_id": cursor.fetchone()["review_id"],
                "replayed": False,
            }
            self._insert_operation(
                parsed.operation_id, request_hash, "review", actor_kind,
                actor_id, item_id, current, current, receipt,
            )
            self.conn.execute("COMMIT")
            return receipt
        except Exception:
            self._rollback_quiet()
            raise

    def latest_review_decision(self, item_id: str, revision: int) -> str | None:
        """目标修订的最新复核决定（approved 后又 rejected 视为未批准）。"""
        row = self.conn.execute(
            "SELECT decision FROM reviews WHERE item_id = %s AND revision = %s"
            " ORDER BY review_id DESC LIMIT 1",
            (item_id, revision),
        ).fetchone()
        return row["decision"] if row else None

    # ------------------------------------------------------------------
    # 全卷一键通过（老版 review-all 语义）：部分成功，逐题返回结果。
    # 不自动发布、不解决疑点、不替用户保存未提交编辑；服务端核对
    # 试卷归属与清单覆盖，逐题复用单题批准门槛。子操作 ID 派生自
    # 父 operation_id + item_id，丢回执重试不重复写审核记录。
    # ------------------------------------------------------------------

    @staticmethod
    def _sub_operation_id(parent: str, item_id: str) -> str:
        base = parent if len(parent) <= 64 else "h" + hashlib.sha256(
            parent.encode("utf-8")
        ).hexdigest()[:24]
        suffix = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:10]
        return f"{base}-{suffix}"

    @_write_locked
    def review_paper_all(
        self,
        paper_id: str,
        request: dict[str, Any],
        *,
        actor_kind: str = "human",
        actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        from question_bank.models import ReviewAllRequest

        try:
            parsed = ReviewAllRequest.model_validate(request)
        except Exception as exc:
            raise QbError([
                error("SCHEMA_INVALID", f"全卷审核请求不合法: {exc}",
                      item_key=paper_id, retryable=False)
            ]) from exc
        request_hash = canonical_json({"paper_id": paper_id, **request})

        # 顶层幂等：同载荷重放回执，异载荷拒绝。父操作行在整个循环完成后
        # 才写入；中途崩溃时重试会重新执行循环，已批准题按 skip 处理，
        # 不会重复写 reviews。
        existing = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = %s",
            (parsed.operation_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ReceiptError([
                    error("OPERATION_ID_REUSED", "相同 operation_id 携带不同载荷",
                          item_key=paper_id, retryable=False)
                ])
            receipt = json.loads(existing["receipt"])
            receipt["replayed"] = True
            return receipt

        paper_row = self.conn.execute(
            "SELECT * FROM papers WHERE paper_id = %s", (paper_id,)
        ).fetchone()
        if paper_row is None:
            raise QbError([
                error("PAPER_NOT_FOUND", f"试卷不存在: {paper_id}",
                      item_key=paper_id, retryable=False)
            ])

        listed_ids = [ref.item_id for ref in parsed.items]
        duplicates = sorted({i for i in listed_ids if listed_ids.count(i) > 1})
        if duplicates:
            raise QbError([
                error("REVIEW_ALL_DUPLICATE_ITEM",
                      f"清单包含重复题目: {', '.join(duplicates)}",
                      item_key=paper_id, retryable=False)
            ])

        rows = self.conn.execute(
            "SELECT item_id, current_revision FROM items WHERE paper_id = %s",
            (paper_id,),
        ).fetchall()
        current_of = {row["item_id"]: row["current_revision"] for row in rows}
        received = {i for i, rev in current_of.items() if rev is not None}
        listed = set(listed_ids)
        foreign = sorted(listed - set(current_of))
        if foreign:
            raise QbError([
                error("REVIEW_ALL_ITEM_FOREIGN",
                      f"清单包含不属于该试卷的题目: {', '.join(foreign[:5])}"
                      f"（共 {len(foreign)} 题）",
                      item_key=paper_id, retryable=False)
            ])
        uncovered = sorted(received - listed)
        if uncovered:
            raise QbError([
                error("REVIEW_ALL_COVERAGE",
                      f"清单未覆盖试卷全部已入库题目（缺 {len(uncovered)} 题，"
                      f"如 {', '.join(uncovered[:3])}）；请刷新目录后重试",
                      item_key=paper_id, retryable=False,
                      fix_hint="重新加载试卷目录，携带全部题目提交")
            ])

        approved: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for ref in parsed.items:
            item_id = ref.item_id
            current = current_of.get(item_id)
            if current is None:
                failed.append({"item_id": item_id, "errors": [error(
                    "ITEM_NOT_FOUND", f"题目尚无修订: {item_id}",
                    item_key=item_id, retryable=False)]})
                continue
            if ref.base_revision != current:
                failed.append({"item_id": item_id, "errors": [error(
                    "BASE_REVISION_CONFLICT",
                    f"清单基线 r{ref.base_revision} 不是当前修订 r{current}",
                    item_key=item_id, retryable=False,
                    fix_hint="刷新目录后按当前修订重试")]})
                continue
            if self.is_approved(item_id, current):
                skipped.append({"item_id": item_id, "revision": current})
                continue
            blockers = self.approval_blockers(item_id, current)
            if blockers:
                failed.append({"item_id": item_id, "errors": [error(
                    "APPROVAL_BLOCKED",
                    "当前修订不满足批准条件: " + "; ".join(blockers),
                    item_key=item_id, retryable=False,
                    fix_hint="先保存通过结构检查、解决阻断疑点并补齐答案")]})
                continue
            try:
                receipt = self.record_review(item_id, {
                    "decision": "approved",
                    "note": parsed.note,
                    "base_revision": current,
                    "operation_id": self._sub_operation_id(
                        parsed.operation_id, item_id),
                }, actor_kind=actor_kind, actor_id=actor_id)
                approved.append({"item_id": item_id, "revision": receipt["revision"]})
            except QbError as exc:
                failed.append({"item_id": item_id, "errors": exc.errors})

        receipt = {
            "operation_id": parsed.operation_id,
            "paper_id": paper_id,
            "status": "partial" if failed else "completed",
            "approved": approved,
            "skipped": skipped,
            "failed": failed,
            "counts": catalog.paper_stats(self.conn, paper_id),
            "replayed": False,
        }
        self.conn.begin_write()
        try:
            self._insert_operation(
                parsed.operation_id, request_hash, "review_all", actor_kind,
                actor_id, None, None, None, receipt,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback_quiet()
            raise
        return receipt

    def is_approved(self, item_id: str, revision: int) -> bool:
        return self.latest_review_decision(item_id, revision) == "approved"

    def approval_blockers(self, item_id: str, revision: int) -> list[str]:
        """批准门槛：结构检查通过、无未决阻断疑点、答案非空。"""
        blockers: list[str] = []
        check = self.conn.execute(
            "SELECT * FROM checks WHERE item_id = %s AND revision = %s"
            " ORDER BY check_id DESC LIMIT 1",
            (item_id, revision),
        ).fetchone()
        if check is None:
            blockers.append("缺少结构检查记录")
        elif not check["passed"]:
            errors = json.loads(check["errors"]).get("errors", [])
            blockers.append("结构检查未通过: " + "; ".join(
                e["code"] for e in errors
            ))
        content = self._content_of(item_id, revision)
        blocking = [
            issue.issue_id
            for issue in content.issues
            if issue.state == "open" and issue.blocking
        ]
        if blocking:
            blockers.append("存在未决阻断疑点: " + ", ".join(blocking))
        if not content.answer.strip():
            blockers.append("答案为空")
        return blockers

    @_write_locked
    def publish(
        self,
        item_id: str,
        request: dict[str, Any],
        *,
        actor_kind: str = "human",
        actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        from question_bank.models import PublishRequest

        try:
            parsed = PublishRequest.model_validate(request)
        except Exception as exc:
            raise QbError([
                error("SCHEMA_INVALID", f"发布请求不合法: {exc}",
                      item_key=item_id, retryable=False)
            ]) from exc
        request_hash = canonical_json(
            {"item_id": item_id, **request, "operation_id": parsed.operation_id}
        )
        self.conn.begin_write()
        try:
            replay = self._replay_operation(parsed.operation_id, request_hash)
            if replay is not None:
                self.conn.execute("COMMIT")
                return replay
            item_row = self._item_row(item_id)
            if item_row is None:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("ITEM_NOT_FOUND", f"题目不存在: {item_id}",
                          item_key=item_id, retryable=False)
                ])
            revision_row = self.conn.execute(
                "SELECT 1 FROM revisions WHERE item_id = %s AND revision = %s",
                (item_id, parsed.revision),
            ).fetchone()
            if revision_row is None:
                self.conn.execute("COMMIT")
                raise QbError([
                    error("REVISION_NOT_FOUND",
                          f"修订不存在: {item_id} r{parsed.revision}",
                          item_key=item_id, retryable=False)
                ])
            if not self.is_approved(item_id, parsed.revision):
                self.conn.execute("COMMIT")
                raise ReceiptError([
                    error(
                        "PUBLISH_NOT_APPROVED",
                        f"修订 r{parsed.revision} 未获有效批准（最新复核决定不是 approved），不能发布",
                        item_key=item_id, retryable=False,
                    )
                ])
            head = self.conn.execute(
                "SELECT publication_id FROM publication_heads WHERE item_id = %s",
                (item_id,),
            ).fetchone()
            current_head = head["publication_id"] if head else None
            if current_head != parsed.expected_previous:
                self.conn.execute("COMMIT")
                raise ReceiptError([
                    error(
                        "PUBLISH_CONFLICT",
                        "expected_previous 与当前发布指针不一致"
                        f"（当前: {current_head}）",
                        item_key=item_id,
                        fix_hint="刷新发布状态后按新的发布指针重试",
                        retryable=False,
                    )
                ])
            cursor = self.conn.execute(
                "INSERT INTO publications(item_id, revision, action,"
                " expected_previous, operation_id, actor_kind, actor_id,"
                " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)" " RETURNING publication_id",
                (item_id, parsed.revision, "publish", parsed.expected_previous,
                 parsed.operation_id, actor_kind, actor_id, db_mod.utc_now()),
            )
            publication_id = cursor.fetchone()["publication_id"]
            self.conn.execute(
                "INSERT INTO publication_heads(item_id, publication_id)"
                " VALUES (%s,%s)"
                " ON CONFLICT(item_id) DO UPDATE SET publication_id = excluded.publication_id",
                (item_id, publication_id),
            )
            receipt = {
                "operation_id": parsed.operation_id,
                "item_id": item_id,
                "status": "published",
                "revision": parsed.revision,
                "publication_id": publication_id,
                "replayed": False,
            }
            self._insert_operation(
                parsed.operation_id, request_hash, "publish", actor_kind,
                actor_id, item_id, parsed.revision, parsed.revision, receipt,
            )
            self.conn.execute("COMMIT")
            return receipt
        except Exception:
            self._rollback_quiet()
            raise

    @_write_locked
    def withdraw(
        self,
        item_id: str,
        request: dict[str, Any],
        *,
        actor_kind: str = "human",
        actor_id: str = "review-ui",
    ) -> dict[str, Any]:
        from question_bank.models import PublishRequest

        try:
            parsed = PublishRequest.model_validate(
                {**request, "revision": request.get("revision", 0) or 0}
            )
        except Exception as exc:
            raise QbError([
                error("SCHEMA_INVALID", f"撤回请求不合法: {exc}",
                      item_key=item_id, retryable=False)
            ]) from exc
        request_hash = canonical_json(
            {"item_id": item_id, "kind": "withdraw", **request,
             "operation_id": parsed.operation_id}
        )
        self.conn.begin_write()
        try:
            replay = self._replay_operation(parsed.operation_id, request_hash)
            if replay is not None:
                self.conn.execute("COMMIT")
                return replay
            head = self.conn.execute(
                "SELECT p.* FROM publication_heads h"
                " JOIN publications p ON p.publication_id = h.publication_id"
                " WHERE h.item_id = %s",
                (item_id,),
            ).fetchone()
            if head is None:
                self.conn.execute("COMMIT")
                raise ReceiptError([
                    error("NOT_PUBLISHED", f"题目当前没有有效发布: {item_id}",
                          item_key=item_id, retryable=False)
                ])
            if parsed.expected_previous != head["publication_id"]:
                self.conn.execute("COMMIT")
                raise ReceiptError([
                    error(
                        "PUBLISH_CONFLICT",
                        "expected_previous 与当前发布指针不一致",
                        item_key=item_id, retryable=False,
                    )
                ])
            self.conn.execute(
                "INSERT INTO publications(item_id, revision, action,"
                " expected_previous, operation_id, actor_kind, actor_id,"
                " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (item_id, head["revision"], "withdraw",
                 parsed.expected_previous, parsed.operation_id, actor_kind,
                 actor_id, db_mod.utc_now()),
            )
            self.conn.execute(
                "DELETE FROM publication_heads WHERE item_id = %s", (item_id,)
            )
            receipt = {
                "operation_id": parsed.operation_id,
                "item_id": item_id,
                "status": "withdrawn",
                "revision": head["revision"],
                "replayed": False,
            }
            self._insert_operation(
                parsed.operation_id, request_hash, "withdraw", actor_kind,
                actor_id, item_id, head["revision"], None, receipt,
            )
            self.conn.execute("COMMIT")
            return receipt
        except Exception:
            self._rollback_quiet()
            raise

    # ==================================================================
    # 查询
    # ==================================================================

    def paper_catalog(self, **filters: Any) -> dict[str, Any]:
        """试卷目录（筛选 + facets + 当前修订统计）；详见 catalog 模块。"""
        return catalog.paper_catalog(self.conn, **filters)

    def paper_item_directory(self, paper_id: str) -> list[dict[str, Any]]:
        """单卷题目目录（轻量，供连续审核跳题使用）。

        额外携带 notes_summary（缺讲解/缺解答/过期讲义徽标）：单卷规模下
        解析当前修订题干派生小问即可，不放大到全库目录。
        """
        row = self.conn.execute(
            "SELECT 1 FROM papers WHERE paper_id = %s", (paper_id,)
        ).fetchone()
        if row is None:
            raise QbError([
                error("PAPER_NOT_FOUND", f"试卷不存在: {paper_id}",
                      item_key=paper_id, retryable=False)
            ])
        directory = catalog.paper_item_directory(self.conn, paper_id)
        for entry in directory:
            entry["notes_summary"] = self._notes_summary_for_item(entry["item_id"])
        return directory

    def _notes_summary_for_item(self, item_id: str) -> dict[str, Any]:
        from question_bank.teaching import derive_subquestions, stem_fingerprint

        item_row = self._item_row(item_id)
        current = item_row["current_revision"] if item_row else None
        if current is None:
            return {"missing_explanation": 0, "missing_solution_count": 0,
                    "stale_notes": 0, "note_count": 0}
        content = self._content_of(item_id, current)
        derived = derive_subquestions(content.stem)
        prints = {e["id"]: stem_fingerprint(e["stem"]) for e in derived}
        notes = self.conn.execute(
            "SELECT n.note_id, n.current_version FROM teaching_notes n"
            " WHERE n.item_id = %s AND n.deleted = 0", (item_id,),
        ).fetchall()
        if not notes:
            return {"missing_explanation": len(derived), "missing_solution_count": 0,
                    "stale_notes": 0, "note_count": 0}
        grouped: dict[str, list] = {e["id"]: [] for e in derived}
        stale = 0
        for note in notes:
            vrow = self.conn.execute(
                "SELECT subquestion_id, explanation_text, solution_text,"
                " source_revision, stem_fingerprint FROM teaching_note_versions"
                " WHERE note_id = %s AND version = %s",
                (note["note_id"], note["current_version"]),
            ).fetchone()
            if vrow is None:
                continue
            subq_stale = (
                vrow["source_revision"] != current
                or vrow["subquestion_id"] not in prints
                or vrow["stem_fingerprint"] != prints.get(vrow["subquestion_id"])
            )
            if subq_stale:
                stale += 1
            else:
                grouped.setdefault(vrow["subquestion_id"], []).append(vrow)
        # 缺项按派生小问覆盖（仅当前来源 note 计入），与 notes_view 口径一致。
        missing_explanation = sum(
            1 for subq_id in prints
            if not any(v["explanation_text"].strip() for v in grouped.get(subq_id, []))
        )
        missing_solution = sum(
            1 for subq_id in prints
            if not any(v["solution_text"].strip() for v in grouped.get(subq_id, []))
        )
        return {
            "missing_explanation": missing_explanation,
            "missing_solution_count": missing_solution,
            "stale_notes": stale,
            "note_count": len(notes),
        }

    def list_items(self, paper_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT i.item_id, i.paper_id, i.question_number, i.current_revision,"
            " p.title AS paper_title FROM items i"
            " JOIN papers p ON p.paper_id = i.paper_id"
            + (" WHERE i.paper_id = %s" if paper_id is not None else "")
            + " ORDER BY i.paper_id, i.question_number",
            (paper_id,) if paper_id is not None else (),
        ).fetchall()
        result = []
        for row in rows:
            item_id = row["item_id"]
            revision = row["current_revision"]
            review = self.conn.execute(
                "SELECT decision, created_at FROM reviews"
                " WHERE item_id = %s AND revision = %s"
                " ORDER BY review_id DESC LIMIT 1",
                (item_id, revision),
            ).fetchone() if revision else None
            check = self.conn.execute(
                "SELECT passed FROM checks WHERE item_id = %s AND revision = %s"
                " ORDER BY check_id DESC LIMIT 1",
                (item_id, revision),
            ).fetchone() if revision else None
            head = self.conn.execute(
                "SELECT p.revision FROM publication_heads h"
                " JOIN publications p ON p.publication_id = h.publication_id"
                " WHERE h.item_id = %s",
                (item_id,),
            ).fetchone()
            blocking = 0
            if revision:
                content = self._content_of(item_id, revision)
                blocking = sum(
                    1 for i in content.issues
                    if i.state == "open" and i.blocking
                )
            result.append({
                "item_id": item_id,
                "paper_id": row["paper_id"],
                "paper_title": row["paper_title"],
                "question_number": row["question_number"],
                "revision": revision,
                "review_state": review["decision"] if review else None,
                "check_passed": bool(check["passed"]) if check else None,
                "open_blocking_issues": blocking,
                "published_revision": head["revision"] if head else None,
                **self.review_metadata(item_id, revision),
            })
        return result

    def item_detail(self, item_id: str, revision: int | None = None) -> dict[str, Any]:
        item_row = self._item_row(item_id)
        if item_row is None:
            raise QbError([
                error("ITEM_NOT_FOUND", f"题目不存在: {item_id}",
                      item_key=item_id, retryable=False)
            ])
        current = item_row["current_revision"]
        revision = revision or current
        if revision is None:
            raise QbError([
                error("ITEM_NOT_FOUND", f"题目尚无修订: {item_id}",
                      item_key=item_id, retryable=False)
            ])
        rev_row = self.conn.execute(
            "SELECT * FROM revisions WHERE item_id = %s AND revision = %s",
            (item_id, revision),
        ).fetchone()
        if rev_row is None:
            raise QbError([
                error("REVISION_NOT_FOUND",
                      f"修订不存在: {item_id} r{revision}",
                      item_key=item_id, retryable=False)
            ])
        content = self._content_of(item_id, revision)
        referenced_assets = set()
        for binding in content.images:
            referenced_assets.update({binding.asset_id, binding.resolved_source()})
        referenced_assets.update(page.asset_id for page in content.evidence)
        assets_by_id = {}
        for asset_id in sorted(referenced_assets):
            row = self.conn.execute(
                "SELECT * FROM assets WHERE asset_id = %s", (asset_id,)
            ).fetchone()
            if row:
                assets_by_id[asset_id] = dict(row)

        images_view = []
        for binding in content.images:
            info = assets_by_id.get(binding.asset_id, {})
            source_info = assets_by_id.get(binding.resolved_source(), {})
            images_view.append({
                **binding.model_dump(),
                "resolved_source": binding.resolved_source(),
                "asset": {
                    "mime": info.get("mime"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                },
                "source_dimensions": (
                    [source_info.get("width"), source_info.get("height")]
                    if source_info else None
                ),
            })
        evidence_view = []
        for page in content.evidence:
            info = assets_by_id.get(page.asset_id, {})
            evidence_view.append({
                **page.model_dump(),
                "asset": {
                    "mime": info.get("mime"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                },
            })
        checks = self.conn.execute(
            "SELECT * FROM checks WHERE item_id = %s AND revision = %s"
            " ORDER BY check_id DESC LIMIT 1",
            (item_id, revision),
        ).fetchone()
        reviews = self.conn.execute(
            "SELECT * FROM reviews WHERE item_id = %s AND revision = %s"
            " ORDER BY review_id DESC",
            (item_id, revision),
        ).fetchall()
        head = self.conn.execute(
            "SELECT p.* FROM publication_heads h"
            " JOIN publications p ON p.publication_id = h.publication_id"
            " WHERE h.item_id = %s",
            (item_id,),
        ).fetchone()
        revisions = self.conn.execute(
            "SELECT revision, content_hash, actor_kind, origin_submission_id,"
            " origin_operation_id, created_at FROM revisions"
            " WHERE item_id = %s ORDER BY revision",
            (item_id,),
        ).fetchall()
        paper = self.conn.execute(
            "SELECT * FROM papers WHERE paper_id = %s", (item_row["paper_id"],)
        ).fetchone()
        detail = {
            "item_id": item_id,
            "paper": dict(paper) if paper else None,
            "current_revision": current,
            "revision": revision,
            "is_current": revision == current,
            "identity": content.identity.model_dump(),
            "body": content.body(),
            "images": images_view,
            "evidence": evidence_view,
            "issues": [i.model_dump() for i in content.issues],
            "check": (
                {
                    "passed": bool(checks["passed"]),
                    "validator": checks["validator"],
                    "validator_version": checks["validator_version"],
                    **json.loads(checks["errors"]),
                }
                if checks else None
            ),
            "reviews": [dict(r) for r in reviews],
            "review_state": reviews[0]["decision"] if reviews else None,
            "publication": (
                {
                    "publication_id": head["publication_id"],
                    "revision": head["revision"],
                    "published_at": head["created_at"],
                }
                if head else None
            ),
            "approval_blockers": self.approval_blockers(item_id, revision),
            "content_hash": rev_row["content_hash"],
            "hash_version": rev_row["hash_version"],
            "revisions": [dict(r) for r in revisions],
        }
        detail.update(self.review_metadata(item_id, revision))
        return detail

    def submission_receipt(self, submission_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM submissions WHERE submission_id = %s",
            (submission_id,),
        ).fetchone()
        if row is None:
            raise QbError([
                error("SUBMISSION_NOT_FOUND",
                      f"提交不存在: {submission_id}", retryable=False)
            ])
        return json.loads(row["receipt"])

    def operation_receipt(self, operation_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = %s", (operation_id,)
        ).fetchone()
        if row is None:
            raise QbError([
                error("OPERATION_NOT_FOUND",
                      f"操作不存在: {operation_id}", retryable=False)
            ])
        return json.loads(row["receipt"])

    # ==================================================================
    # 资产辅助（服务端上传/渲染用）
    # ==================================================================

    def asset_rel_path(self, asset_id: str) -> str:
        """返回资产在管理根下的登记路径（数据库为准）。"""
        row = self.conn.execute(
            "SELECT path FROM assets WHERE asset_id = %s", (asset_id,)
        ).fetchone()
        if row is None:
            raise QbError([
                error("ASSET_NOT_FOUND", f"资产不存在: {asset_id}",
                      retryable=False)
            ])
        return row["path"]

    @_write_locked
    def register_asset(
        self, raw: bytes, *, source_kind: str = "manual"
    ) -> dict[str, Any]:
        """登记（或复用）不可变媒体资产并写入 assets 表。"""
        info = self.assets.register_bytes(raw, source_kind=source_kind)
        self.conn.execute(
            "INSERT INTO assets(asset_id, sha256, path, mime,"
            " size_bytes, width, height, source_kind, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
            (
                info["asset_id"], info["sha256"], info["path"], info["mime"],
                info["size_bytes"], info["width"], info["height"],
                info["source_kind"], db_mod.utc_now(),
            ),
        )
        self.conn.commit()
        return info

    def asset_bytes(self, asset_id: str) -> tuple[str, bytes]:
        rel = self.asset_rel_path(asset_id)
        return rel, self.assets._load_bytes(rel)

    # ==================================================================
    # 内部工具
    # ==================================================================

    def _rollback_quiet(self) -> None:
        """仅在实际持有写事务时回滚（COMMIT 后抛错的路径没有活动事务）。"""
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        except db_mod.OperationalError:
            pass

    def _item_row(self, item_id: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE item_id = %s", (item_id,)
        ).fetchone()

    def _content_of(self, item_id: str, revision: int) -> ItemContent:
        row = self.conn.execute(
            "SELECT content FROM revisions WHERE item_id = %s AND revision = %s",
            (item_id, revision),
        ).fetchone()
        if row is None:
            raise QbError([
                error("REVISION_NOT_FOUND",
                      f"修订不存在: {item_id} r{revision}", retryable=False)
            ])
        return ItemContent.model_validate(json.loads(row["content"]))

    def _copy_bindings_and_issues(
        self, item_id: str, revision: int, content: ItemContent
    ) -> None:
        for binding in content.images:
            self.conn.execute(
                "INSERT INTO revision_assets(item_id, revision, binding_key,"
                " asset_id, role, source_asset_id, crop_px, rotate, width,"
                " ordinal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    item_id, revision, binding.key, binding.asset_id,
                    binding.role, binding.source_asset_id,
                    canonical_json(binding.crop_px) if binding.crop_px else None,
                    binding.rotate, binding.width, binding.ordinal,
                ),
            )
        for issue in content.issues:
            self.conn.execute(
                "INSERT INTO issues(item_id, revision, issue_id, type, target,"
                " reason, evidence, blocking, state, resolution)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    item_id, revision, issue.issue_id, issue.type, issue.target,
                    issue.reason, issue.evidence, int(issue.blocking),
                    issue.state,
                    canonical_json(issue.resolution) if issue.resolution else None,
                ),
            )

    def _replay_operation(
        self, operation_id: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = %s", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ReceiptError([
                error("OPERATION_ID_REUSED",
                      "相同 operation_id 携带不同载荷", retryable=False)
            ])
        receipt = json.loads(row["receipt"])
        receipt["replayed"] = True
        return receipt

    def _insert_operation(
        self,
        operation_id: str,
        request_hash: str,
        kind: str,
        actor_kind: str,
        actor_id: str,
        item_id: str | None,
        base_revision: int | None,
        result_revision: int | None,
        receipt: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO operations(operation_id, request_hash, kind, actor_kind,"
            " actor_id, item_id, base_revision, result_revision, receipt,"
            " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                operation_id, request_hash, kind, actor_kind, actor_id, item_id,
                base_revision, result_revision, canonical_json(receipt),
                db_mod.utc_now(),
            ),
        )
