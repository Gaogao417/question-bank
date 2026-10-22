"""OSS 媒体存储封装。

启用条件：环境变量 QB_OSS_BUCKET 已设置。key 与 DB assets.path 字段完全一致
（assets/xx/hash.ext），不做前缀拼接；凭证优先 QB_OSS_ACCESS_KEY_ID/SECRET，
回落 ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET。服务器读写走内网 endpoint
（QB_OSS_ENDPOINT），浏览器签名 URL 走公网 endpoint（QB_OSS_PUBLIC_ENDPOINT）。
"""
from __future__ import annotations

import os
from functools import lru_cache

from question_bank.errors import QbError, error

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30


@lru_cache(maxsize=1)
def _config() -> dict | None:
    bucket = os.environ.get("QB_OSS_BUCKET", "").strip()
    if not bucket:
        return None
    return {
        "bucket": bucket,
        "endpoint": os.environ.get("QB_OSS_ENDPOINT", "").strip() or "https://oss-cn-shanghai-internal.aliyuncs.com",
        "public_endpoint": os.environ.get("QB_OSS_PUBLIC_ENDPOINT", "").strip()
        or "https://oss-cn-shanghai.aliyuncs.com",
        "key_id": os.environ.get("QB_OSS_ACCESS_KEY_ID", "").strip()
        or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip(),
        "key_secret": os.environ.get("QB_OSS_ACCESS_KEY_SECRET", "").strip()
        or os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", ""),
    }


def enabled() -> bool:
    return _config() is not None


def describe() -> dict | None:
    """当前 OSS 配置摘要（不含密钥），写入备份快照用于自描述。"""
    cfg = _config()
    if cfg is None:
        return None
    return {"bucket": cfg["bucket"], "endpoint": cfg["endpoint"],
            "public_endpoint": cfg["public_endpoint"]}


@lru_cache(maxsize=1)
def _bucket():
    import oss2

    cfg = _config()
    if cfg is None:
        raise RuntimeError("OSS not enabled: set QB_OSS_BUCKET")
    if not cfg["key_id"] or not cfg["key_secret"]:
        raise RuntimeError("OSS credentials missing: set QB_OSS_ACCESS_KEY_ID/SECRET or ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET")
    auth = oss2.Auth(cfg["key_id"], cfg["key_secret"])
    # enable_crc=False 规避 crcmod C 扩展依赖；完整性由 HTTPS 传输 + DB sha256 校验兜底。
    return oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"],
                       enable_crc=False, connect_timeout=CONNECT_TIMEOUT)


def _public_bucket():
    import oss2

    cfg = _config()
    auth = oss2.Auth(cfg["key_id"], cfg["key_secret"])
    return oss2.Bucket(auth, cfg["public_endpoint"], cfg["bucket"], enable_crc=False)


def _wrap(exc: Exception) -> QbError:
    return QbError([
        error("OSS_ERROR", f"OSS 操作失败: {type(exc).__name__}: {exc}", retryable=True)
    ])


def put_object(key: str, raw: bytes, content_type: str) -> None:
    """上传对象；key 内容寻址不可变，重复 put 幂等。失败抛错阻断 DB 登记。"""
    import oss2

    headers = {"Content-Type": content_type}
    try:
        _bucket().put_object(key, raw, headers=headers)
    except oss2.exceptions.OssError as exc:
        raise _wrap(exc) from exc


def get_object(key: str) -> bytes:
    import oss2

    try:
        return _bucket().get_object(key).read()
    except oss2.exceptions.NoSuchKey as exc:
        raise QbError([
            error("ASSET_FILE_MISSING", f"OSS 对象缺失: {key}", retryable=False)
        ]) from exc
    except oss2.exceptions.OssError as exc:
        raise _wrap(exc) from exc


def object_exists(key: str) -> bool:
    import oss2

    try:
        return bool(_bucket().object_exists(key))
    except oss2.exceptions.OssError as exc:
        raise _wrap(exc) from exc


def presigned_get(key: str, expires_seconds: int = 3600) -> str:
    """生成公网签名 GET URL（默认 1 小时，避免播放中过期）。"""
    import oss2

    try:
        return _public_bucket().sign_url("GET", key, expires_seconds, slash_safe=True)
    except oss2.exceptions.OssError as exc:
        raise _wrap(exc) from exc
