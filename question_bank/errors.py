"""统一错误模型：code / item_key / field_path / message / fix_hint / retryable。

错误对象按题定位，不把一题失败扩大为整卷失败。
"""

from __future__ import annotations

from typing import Any


class QbError(Exception):
    """带结构化错误列表的业务异常。

    errors: list[dict]，每项包含 code、item_key、field_path、message、
    fix_hint、retryable。抛出时调用方应把 errors 原样放进回执或 HTTP 响应。
    """

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = [normalize_error(err) for err in errors]
        super().__init__(self.summary())

    def summary(self) -> str:
        parts = []
        for err in self.errors:
            location = err.get("field_path") or err.get("item_key") or ""
            parts.append(
                f"{err['code']}({location}): {err['message']}"
            )
        return "; ".join(parts)


class ReceiptError(QbError):
    """回执级错误（幂等冲突、操作 ID 复用等），不伴随内容校验失败。

    conflict: 可选的乐观并发冲突负载（当前修订、变化字段等），供 HTTP 409
    响应携带；UI 保留未保存修改并展示比较。
    """

    def __init__(self, errors, conflict: dict[str, Any] | None = None):
        self.conflict = conflict
        super().__init__(errors)


def normalize_error(err: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(err.get("code", "UNKNOWN")),
        "item_key": err.get("item_key") or "",
        "field_path": err.get("field_path") or "",
        "message": str(err.get("message", "")),
        "fix_hint": str(err.get("fix_hint", "")),
        "retryable": bool(err.get("retryable", False)),
    }


def error(
    code: str,
    message: str,
    *,
    item_key: str = "",
    field_path: str = "",
    fix_hint: str = "",
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "item_key": item_key,
        "field_path": field_path,
        "message": message,
        "fix_hint": fix_hint,
        "retryable": retryable,
    }
