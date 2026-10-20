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
