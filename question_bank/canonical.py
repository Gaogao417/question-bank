"""规范化序列化与内容 hash。

content_hash 基于“规范内容 + 来源证据 + 引用媒体 hash”，排除绝对路径、
时间戳、审批信息与内部 item_id；算法随 hash_version 版本化。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTENT_SCHEMA = "qb_question_content/v1"
HASH_VERSION = 1


def canonical_json(value: Any) -> str:
    """稳定序列化：排序键、紧凑分隔符、保留非 ASCII 字符。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_canonical(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def content_hash_payload(content: dict[str, Any]) -> str:
    """计算修订 content_hash。

    输入是完整规范内容 dict，接受 body 包装形状或平铺形状
    （ItemContent.model_dump 为平铺：stem/answer/solution 顶层字段）。
    issues 不参与内容 hash：疑点处置可以只产生新修订而不改变内容。
    """
    if "body" in content:
        body = content["body"]
    else:
        body = {
            "stem": content.get("stem"),
            "answer": content.get("answer"),
            "solution": content.get("solution"),
        }
    payload = {
        "schema": content["schema"],
        "identity": content["identity"],
        "body": body,
        "images": content["images"],
        "evidence": content["evidence"],
    }
    return sha256_canonical(payload)
