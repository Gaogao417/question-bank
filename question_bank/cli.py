"""题库管理 CLI：init / submit / receipt / status / export / backup / serve。

CLI 只是 AI↔程序接口的客户端；业务规则全部在 service 层。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from question_bank import backup as backup_mod
from question_bank import export as export_mod
from question_bank.errors import QbError, ReceiptError
from question_bank.service import QuestionBankService


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_init(args: argparse.Namespace) -> int:
    QuestionBankService(args.root).close()
    _print({"ok": True, "root": str(Path(args.root).resolve())})
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    service = QuestionBankService(args.root)
    try:
        receipt = service.submit_candidate(args.package, actor_id=args.actor)
    finally:
        service.close()
    _print(receipt)
    return 0 if receipt["status"] in ("accepted", "accepted_with_issues", "noop") else 1


def cmd_receipt(args: argparse.Namespace) -> int:
    service = QuestionBankService(args.root)
    try:
        _print(service.submission_receipt(args.submission_id))
    finally:
        service.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    service = QuestionBankService(args.root)
    try:
        _print({"root": str(service.root), "items": service.list_items(paper_id=args.paper_id)})
    finally:
        service.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    service = QuestionBankService(args.root)
    try:
        _print(export_mod.export_revision(
            service, args.item_id, args.revision, allow_draft=args.allow_draft
        ))
    finally:
        service.close()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    _print(backup_mod.backup(args.root, args.dest))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _print(backup_mod.verify(args.root))
    return 0


def cmd_oss_sync(args: argparse.Namespace) -> int:
    """存量本地媒体一次性回填 OSS（key=path）；已存在的对象跳过。"""
    from question_bank import oss_store

    if not oss_store.enabled():
        _print({"ok": False, "errors": [{"code": "OSS_ERROR",
                                         "message": "Set QB_OSS_BUCKET to enable OSS",
                                         "retryable": False}]})
        return 2
    root = Path(args.root).resolve()
    assets_dir = root / "assets"
    if not assets_dir.is_dir():
        _print({"ok": True, "uploaded": 0, "skipped": 0, "note": "no local assets dir"})
        return 0
    mime_by_path = {}
    try:
        service = QuestionBankService(args.root)
        try:
            rows = service.conn.execute("SELECT path, mime FROM assets").fetchall()
            mime_by_path = {r["path"]: r["mime"] for r in rows}
        finally:
            service.close()
    except Exception:
        pass  # DB 不可用时仍可按扩展名回填
    suffix_mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
                   ".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
                   ".mp3": "audio/mpeg", ".wav": "audio/wav"}
    uploaded = skipped = 0
    for p in sorted(assets_dir.rglob("*")):
        if not p.is_file():
            continue
        key = str(p.relative_to(root)).replace("\\", "/")
        if oss_store.object_exists(key):
            skipped += 1
            continue
        mime = mime_by_path.get(key) or suffix_mime.get(p.suffix.lower(), "application/octet-stream")
        oss_store.put_object(key, p.read_bytes(), mime)
        uploaded += 1
    _print({"ok": True, "uploaded": uploaded, "skipped": skipped,
            "bucket": oss_store.describe()["bucket"]})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from question_bank.server import create_app

    uvicorn.run(
        create_app(args.root),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m question_bank.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化管理根（数据库与目录）")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("submit", help="接收 AI 候选包")
    p.add_argument("--root", required=True)
    p.add_argument("--package", required=True)
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("receipt", help="按 submission_id 查询回执")
    p.add_argument("--root", required=True)
    p.add_argument("--submission-id", required=True)
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("status", help="逐题状态列表")
    p.add_argument("--root", required=True)
    p.add_argument("--paper-id", help="Only list this paper; omit to list the whole bank")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="导出指定修订")
    p.add_argument("--root", required=True)
    p.add_argument("--item-id", required=True)
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--allow-draft", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("backup", help="备份管理根（数据库 + 媒体 + 输入）")
    p.add_argument("--root", required=True)
    p.add_argument("--dest", required=True)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("verify", help="恢复一致性验证")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("oss-sync", help="存量本地媒体一次性回填 OSS（key=path）")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_oss_sync)

    p = sub.add_parser("serve", help="启动 Review UI 服务")
    p.add_argument("--root", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8890)
    p.add_argument("--log-level", default="info")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("restore", help="Restore snapshot into an empty PostgreSQL bank")
    p.add_argument("--source", required=True)
    p.add_argument("--root", required=True)
    p.set_defaults(func=lambda args: (_print(backup_mod.restore(args.source,args.root)) or 0))
    p = sub.add_parser("import-sqlite", help="Read-only import from an existing SQLite bank")
    p.add_argument("--source", required=True)
    p.add_argument("--root", required=True)
    from question_bank.import_sqlite import import_sqlite
    p.set_defaults(func=lambda args: (_print(import_sqlite(args.source,args.root)) or 0))
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (QbError, ReceiptError) as exc:
        if isinstance(exc, ReceiptError) and getattr(exc, "conflict", None):
            _print({"ok": False, "conflict": exc.conflict, "errors": exc.errors})
        else:
            _print({"ok": False, "errors": exc.errors})
        return 2


if __name__ == "__main__":
    sys.exit(main())
