"""数据库题库 Review UI 服务端。

人 ↔ 程序接口：查看当前修订与原始证据、编辑三块正文、调整图片、
处置疑点、保存（base_revision 乐观并发）、复核、发布与导出。
不触碰旧文件题库（question_bank_review_server.py 保持原样）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from question_bank import export as export_mod
from question_bank.errors import QbError, ReceiptError
from question_bank.service import QuestionBankService

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _oss_redirect(key: str) -> Response:
    """302 到 OSS 公网签名 URL（1 小时）。no-store 防签名 URL 过期后仍被缓存复用。"""
    from question_bank import oss_store

    url = oss_store.presigned_get(key, expires_seconds=3600)
    return Response(
        status_code=302,
        headers={"Location": url, "Cache-Control": "no-store"},
    )


def create_app(root: str | Path) -> FastAPI:
    app = FastAPI(title="question-bank-manage", version="b1")
    state: dict[str, QuestionBankService] = {}

    service_lock = threading.Lock()

    def service() -> QuestionBankService:
        with service_lock:
            svc = state.get("service")
            if svc is None:
                svc = QuestionBankService(root)
                state["service"] = svc
            return svc

    @app.on_event("shutdown")
    def _close() -> None:
        svc = state.pop("service", None)
        if svc is not None:
            svc.close()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        try:
            service().conn.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        return {"ok": db_ok, "root": str(Path(root).resolve())}

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        svc = service()
        items = svc.list_items()
        papers: dict[str, dict[str, Any]] = {}
        for item in items:
            papers.setdefault(item["paper_id"], {
                "paper_id": item["paper_id"],
                "paper_title": item["paper_title"],
                "items": [],
            })["items"].append(item)
        return {"root": str(svc.root), "papers": list(papers.values())}

    @app.get("/api/papers")
    def list_papers(
        q: str | None = None,
        year: int | None = None,
        district: str | None = None,
        exam_type: str | None = None,
        grade: str | None = None,
    ) -> dict[str, Any]:
        """试卷目录：筛选 / 检索 / facets / 当前修订统计（老版查卷体验）。"""
        return service().paper_catalog(
            q=q, year=year, district=district, exam_type=exam_type, grade=grade
        )

    @app.get("/api/items")
    def list_items(paper_id: str | None = None) -> dict[str, Any]:
        svc = service()
        if paper_id is not None:
            try:
                return {"paper_id": paper_id, "items": svc.paper_item_directory(paper_id)}
            except QbError as exc:
                raise _http(exc, 404) from exc
        return {"items": svc.list_items()}

    @app.post("/api/papers/{paper_id}/review-all")
    async def review_paper_all(paper_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().review_paper_all(paper_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            not_found = exc.errors and all(
                e.get("code") in ("PAPER_NOT_FOUND",) for e in exc.errors
            )
            raise _http(exc, 404 if not_found else 400) from exc

    # ------------------------------------------------------------------
    # 教学内容（讲解/解答对）：老版 explanations 子系统的数据库版
    # ------------------------------------------------------------------

    @app.get("/api/items/{item_id}/notes")
    def notes_view(item_id: str, revision: int | None = None) -> dict[str, Any]:
        try:
            return service().notes_view(item_id, revision)
        except QbError as exc:
            raise _http(exc, 404) from exc

    @app.post("/api/items/{item_id}/notes")
    async def create_note(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().create_note(item_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.patch("/api/notes/{note_id}")
    async def update_note(note_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().update_note(note_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.delete("/api/notes/{note_id}")
    async def delete_note(note_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().delete_note(note_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.post("/api/notes/{note_id}/review")
    async def review_note(note_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().review_note(note_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.post("/api/notes/{note_id}/audio")
    async def save_recording(
        note_id: str, request: Request,
        base_version: int = Query(...),
        operation_id: str = Query(...),
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        raw = await request.body()
        try:
            return service().save_recording(
                note_id, base_version, operation_id, raw, content_type)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.post("/api/notes/{note_id}/polish")
    async def polish_note(note_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().polish_note(note_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.post("/api/notes/{note_id}/generate")
    async def generate_note_text(note_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().generate_note_text(note_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.post("/api/items/{item_id}/notes/fill")
    async def fill_missing(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().fill_missing(item_id, payload)
        except QbError as exc:
            if isinstance(exc, ReceiptError):
                raise _http(exc, 409) from exc
            raise _http(exc) from exc

    @app.post("/api/papers/{paper_id}/blueprint-export")
    async def export_blueprints(paper_id: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        payload = json.loads(body) if body else {}
        try:
            return service().export_paper_blueprints(paper_id, payload)
        except QbError as exc:
            not_found = exc.errors and all(
                e.get("code") in ("PAPER_NOT_FOUND",) for e in exc.errors
            )
            raise _http(exc, 404 if not_found else 400) from exc

    @app.get("/api/assets/{asset_id}/audio")
    def audio_file(asset_id: str):
        from question_bank.errors import error as _error

        svc = service()
        try:
            row = svc.conn.execute(
                "SELECT * FROM assets WHERE asset_id = %s", (asset_id,)
            ).fetchone()
            if row is None or not row["mime"].startswith("audio/"):
                raise QbError([
                    _error("AUDIO_ASSET_EXPECTED",
                           f"资产不是录音: {asset_id}", retryable=False)
                ])
            if svc.assets.oss:
                return _oss_redirect(row["path"])
            return FileResponse(svc.root / row["path"], media_type=row["mime"])
        except QbError as exc:
            raise _http(exc, 404) from exc
        except Exception as exc:  # OSS 签名/网络故障
            raise HTTPException(status_code=502, detail=f"audio fetch failed: {exc}") from exc

    @app.get("/api/items/{item_id}")
    def item_detail(item_id: str, revision: int | None = None) -> dict[str, Any]:
        try:
            return service().item_detail(item_id, revision)
        except QbError as exc:
            raise _http(exc, 404) from exc

    @app.patch("/api/items/{item_id}/content")
    async def save_content(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().save_edit(item_id, payload)
        except QbError as exc:
            raise _http(exc) from exc

    @app.post("/api/items/{item_id}/assets")
    async def upload_asset(item_id: str, request: Request) -> dict[str, Any]:
        """上传不可变媒体资产；仅登记，不自动改动题目内容。"""
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="请上传图片内容")
        raw = await request.body()
        try:
            info = service().register_asset(raw, source_kind="manual")
        except QbError as exc:
            raise _http(exc) from exc
        return {"asset_id": info["asset_id"], "width": info["width"],
                "height": info["height"], "mime": info["mime"]}

    @app.post("/api/revision-assist/transcribe")
    async def transcribe_revision(request: Request) -> dict[str, Any]:
        from question_bank import explanations_ai as ai
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > ai.MAX_AUDIO_BYTES:
                raise HTTPException(413, detail="录音超过 50MB 上限")
        try:
            transcript = await run_in_threadpool(
                ai.transcribe_audio, bytes(raw), request.headers.get("content-type", "audio/webm"))
            return {"transcript": transcript}
        except ai.AiAssistError as exc:
            raise HTTPException(400, detail={"errors": [{"code": exc.code, "message": str(exc)}]}) from exc

    @app.post("/api/items/{item_id}/revision-assist")
    async def revision_assist(item_id: str, request: Request) -> dict[str, Any]:
        from question_bank.revision_ai import generate_revision, MODEL
        import uuid
        payload = await request.json()
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8000:
            raise HTTPException(400, detail={"errors": [{"code": "INVALID_INSTRUCTION", "message": "请填写 1–8000 字修改意见"}]})
        try:
            detail = service().item_detail(item_id)
            if payload.get("base_revision") != detail["current_revision"]:
                raise HTTPException(409, detail={"errors": [{"code": "BASE_REVISION_STALE", "message": "题目版本已变化，请刷新后再生成"}]})
            patch = await run_in_threadpool(generate_revision, Path(root), detail, instruction.strip())
            return service().submit_proposal(item_id, {
                **patch, "base_revision": detail["current_revision"],
                "proposal_id": "revision-" + uuid.uuid4().hex,
                "reason": instruction.strip() + "\n\n" + patch["reason"],
            }, actor_id=MODEL)
        except QbError as exc:
            raise _http(exc) from exc
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(502, detail={"errors": [{"code": "REVISION_AI_FAILED", "message": str(exc)}]}) from exc

    @app.get("/api/items/{item_id}/proposals")
    def list_proposals(item_id: str) -> dict[str, Any]:
        try:
            return {"item_id": item_id,
                    "proposals": service().list_proposals(item_id)}
        except QbError as exc:
            raise _http(exc, 404) from exc

    @app.post("/api/items/{item_id}/proposals")
    async def submit_proposal(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().submit_proposal(item_id, payload)
        except QbError as exc:
            raise _http(exc, 409) from exc

    @app.post("/api/proposals/{proposal_id}/accept")
    async def accept_proposal(proposal_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().accept_proposal(proposal_id, payload)
        except QbError as exc:
            raise _http(exc, 409) from exc

    @app.post("/api/proposals/{proposal_id}/reject")
    async def reject_proposal(proposal_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().reject_proposal(proposal_id, payload)
        except QbError as exc:
            raise _http(exc, 409) from exc

    @app.post("/api/items/{item_id}/review")
    async def review_item(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().record_review(item_id, payload)
        except QbError as exc:
            raise _http(exc) from exc

    @app.post("/api/items/{item_id}/publication")
    async def publish_item(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().publish(item_id, payload)
        except QbError as exc:
            raise _http(exc) from exc

    @app.post("/api/items/{item_id}/withdrawal")
    async def withdraw_item(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return service().withdraw(item_id, payload)
        except QbError as exc:
            raise _http(exc) from exc

    @app.post("/api/items/{item_id}/exports")
    async def export_item(item_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            return export_mod.export_revision(
                service(), item_id, int(payload.get("revision", 0)),
                allow_draft=bool(payload.get("allow_draft", False)),
            )
        except QbError as exc:
            raise _http(exc) from exc

    @app.get("/api/operations/{operation_id}")
    def operation(operation_id: str) -> dict[str, Any]:
        try:
            return service().operation_receipt(operation_id)
        except QbError as exc:
            raise _http(exc, 404) from exc

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str):
        try:
            rel = service().asset_rel_path(asset_id)
        except QbError as exc:
            raise _http(exc, 404) from exc
        if service().assets.oss:
            try:
                return _oss_redirect(rel)
            except Exception as exc:  # OSS 签名/网络故障
                raise HTTPException(status_code=502, detail=f"asset fetch failed: {exc}") from exc
        return FileResponse(Path(root) / rel)

    @app.get("/api/assets/{asset_id}/render")
    def asset_render(
        asset_id: str,
        l: int | None = Query(default=None, ge=0),
        t: int | None = Query(default=None, ge=0),
        r: int | None = Query(default=None, ge=0),
        b: int | None = Query(default=None, ge=0),
        rotate: int = Query(default=0),
    ) -> Response:
        crop = [l, t, r, b] if None not in (l, t, r, b) else None
        try:
            svc = service()
            rel = svc.asset_rel_path(asset_id)
            data = svc.assets.render_rel(rel, crop_px=crop, rotate=rotate)
        except QbError as exc:
            raise _http(exc, 404) from exc
        return Response(content=data, media_type="image/png")

    def _http(exc: QbError, default_status: int = 400) -> HTTPException:
        """QbError → HTTPException；乐观并发冲突固定 409 并携带冲突负载。"""
        conflict = getattr(exc, "conflict", None)
        if isinstance(exc, ReceiptError) and conflict:
            return HTTPException(
                status_code=409,
                detail={"errors": exc.errors, "conflict": conflict},
            )
        return HTTPException(
            status_code=default_status, detail={"errors": exc.errors}
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="root")
    return app
