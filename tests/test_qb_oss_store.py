"""OSS 直存链路测试：monkeypatch 内存假 OSS，不触碰真实 bucket。"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from question_bank import oss_store


class FakeOSS:
    """内存 dict 假对象存储：key=path，记录 content_type。"""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.calls: list[str] = []

    # 与 oss_store 同名 API
    def enabled(self):
        return True

    def describe(self):
        return {"bucket": "fake-bucket", "endpoint": "fake-internal",
                "public_endpoint": "fake-public"}

    def put_object(self, key, raw, content_type):
        self.calls.append(f"put:{key}")
        self.objects[key] = (raw, content_type)

    def get_object(self, key):
        self.calls.append(f"get:{key}")
        if key not in self.objects:
            from question_bank.errors import QbError, error
            raise QbError([error("ASSET_FILE_MISSING", f"OSS 对象缺失: {key}",
                                 retryable=False)])
        return self.objects[key][0]

    def object_exists(self, key):
        self.calls.append(f"exists:{key}")
        return key in self.objects

    def presigned_get(self, key, expires_seconds=3600):
        self.calls.append(f"presign:{key}")
        return f"https://fake-public/{key}?expires={expires_seconds}"


@pytest.fixture
def fake_oss(monkeypatch):
    fake = FakeOSS()
    monkeypatch.setattr(oss_store, "_config", lambda: {"bucket": "fake-bucket"} | fake.describe())
    monkeypatch.setattr(oss_store, "enabled", fake.enabled)
    monkeypatch.setattr(oss_store, "describe", fake.describe)
    monkeypatch.setattr(oss_store, "put_object", fake.put_object)
    monkeypatch.setattr(oss_store, "get_object", fake.get_object)
    monkeypatch.setattr(oss_store, "object_exists", fake.object_exists)
    monkeypatch.setattr(oss_store, "presigned_get", fake.presigned_get)
    return fake


def _png_bytes(size=(40, 30), color=(255, 0, 0)) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


WAV_BYTES = (
    b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
    + b"\x01\x00\x01\x00" + (8000).to_bytes(4, "little") * 2
    + b"\x01\x00\x08\x00" + b"data" + (0).to_bytes(4, "little")
)


def test_register_uploads_to_oss_not_local(isolated_bank, fake_oss, tmp_path):
    from question_bank.service import QuestionBankService

    svc = QuestionBankService(tmp_path / "root")
    try:
        info = svc.register_asset(_png_bytes(), source_kind="manual")
        key = info["path"]
        assert key in fake_oss.objects, "资产应上传到 OSS"
        raw, ctype = fake_oss.objects[key]
        assert ctype == "image/png", "PUT 时写 content-type"
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == info["sha256"]
        assert not (svc.root / key).is_file(), "本地不应落字节"
        # 幂等：重复登记同 hash 不重复 put
        svc.register_asset(_png_bytes(), source_kind="manual")
        assert fake_oss.calls.count(f"put:{key}") == 1
    finally:
        svc.close()


def test_render_and_dimensions_via_oss(isolated_bank, fake_oss, tmp_path):
    from question_bank.service import QuestionBankService

    svc = QuestionBankService(tmp_path / "root")
    try:
        info = svc.register_asset(_png_bytes(), source_kind="manual")
        rendered = svc.assets.render(info["asset_id"], crop_px=[5, 5, 20, 20])
        assert rendered[:8] == b"\x89PNG\r\n\x1a\n"
        assert svc.assets.dimensions(info["asset_id"]) == (40, 30)
        assert any(c.startswith("get:") for c in fake_oss.calls)
    finally:
        svc.close()


def test_audio_endpoint_redirects_to_presigned(isolated_bank, fake_oss, tmp_path):
    from question_bank import db as db_mod
    from question_bank.server import create_app
    from question_bank.service import QuestionBankService

    root = tmp_path / "root"
    svc = QuestionBankService(root)
    info = svc.assets.register_audio(WAV_BYTES, "audio/wav")
    with svc._write_lock:
        svc.conn.begin_write()
        svc.conn.execute(
            "INSERT INTO assets(asset_id, sha256, path, mime, size_bytes,"
            " width, height, source_kind, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (info["asset_id"], info["sha256"], info["path"], info["mime"],
             info["size_bytes"], None, None, "recording", db_mod.utc_now()))
        svc.conn.execute("COMMIT")
    svc.close()

    client = TestClient(create_app(root))
    resp = client.get(f"/api/assets/{info['asset_id']}/audio",
                      follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://fake-public/")
    assert info["path"] in resp.headers["location"]
    assert resp.headers["cache-control"] == "no-store"
    # 文件端点同样重定向
    resp2 = client.get(f"/api/assets/{info['asset_id']}/file",
                       follow_redirects=False)
    assert resp2.status_code == 302
    assert "expires=3600" in resp2.headers["location"]
    # OSS 读回：讲解润色路径走 get_object
    assert fake_oss.objects[info["path"]][1] == "audio/wav"


def test_backup_verify_restore_with_oss(isolated_bank, fake_oss, tmp_path):
    from question_bank import backup as backup_mod
    from question_bank.service import QuestionBankService

    root = tmp_path / "root"
    svc = QuestionBankService(root)
    try:
        info = svc.register_asset(_png_bytes(), source_kind="manual")
        dest = tmp_path / "snapshot"
        result = backup_mod.backup(root, dest)
        assert result["oss_assets"] >= 1
        assert not (dest / "assets").exists(), "OSS 模式快照不含 assets 字节"
        import json
        snapshot = json.loads((dest / "database.json").read_text())
        assert snapshot["oss"]["bucket"] == "fake-bucket"
        # verify：object_exists 快速校验
        counts = backup_mod.verify(dest)
        assert counts["items"] >= 0
        # 对象被删后 verify 报缺失
        del fake_oss.objects[info["path"]]
        from question_bank.errors import QbError
        with pytest.raises(QbError):
            backup_mod.verify(dest)
        fake_oss.objects[info["path"]] = (_png_bytes(), "image/png")
    finally:
        svc.close()


def test_restore_legacy_local_snapshot_uploads_to_oss(isolated_bank, monkeypatch, tmp_path):
    """旧本地快照（无 oss 段）在 OSS 模式下恢复：assets 上传 OSS。"""
    from question_bank import backup as backup_mod
    from question_bank.service import QuestionBankService

    # 1) 先在非 OSS 模式造本地快照
    monkeypatch.setattr(oss_store, "_config", lambda: None)
    monkeypatch.setattr(oss_store, "enabled", lambda: False)

    def _describe_disabled():
        return None
    monkeypatch.setattr(oss_store, "describe", _describe_disabled)

    root = tmp_path / "root1"
    svc = QuestionBankService(root)
    try:
        info = svc.register_asset(_png_bytes(), source_kind="manual")
        dest = tmp_path / "legacy-snapshot"
        backup_mod.backup(root, dest)
        assert (dest / "assets").exists()
    finally:
        svc.close()

    # 2) 开启 OSS 模式 restore 旧快照
    fake = FakeOSS()
    monkeypatch.setattr(oss_store, "_config", lambda: {"bucket": "fake-bucket"} | fake.describe())
    monkeypatch.setattr(oss_store, "enabled", fake.enabled)
    monkeypatch.setattr(oss_store, "describe", fake.describe)
    monkeypatch.setattr(oss_store, "put_object", fake.put_object)
    monkeypatch.setattr(oss_store, "get_object", fake.get_object)
    monkeypatch.setattr(oss_store, "object_exists", fake.object_exists)
    monkeypatch.setattr(oss_store, "presigned_get", fake.presigned_get)

    target = tmp_path / "root2"
    result = backup_mod.restore(dest, target)
    assert result["oss"] == "fake-bucket"
    assert info["path"] in fake.objects, "旧快照资产应上传到 OSS"
    raw, ctype = fake.objects[info["path"]]
    assert ctype == "image/png"
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == info["sha256"]
    # 新根经 OSS 可读
    svc2 = QuestionBankService(target)
    try:
        assert svc2.assets.dimensions(info["asset_id"]) == (40, 30)
    finally:
        svc2.close()


def test_assetstore_explicit_oss_false(isolated_bank, fake_oss, tmp_path):
    """显式 oss=False（agent 预检临时根）：写本地、不碰 OSS。"""
    from question_bank.assets import AssetStore

    store = AssetStore(tmp_path / "offline-root", oss=False)
    info = store.register_bytes(_png_bytes())
    assert (store.root / info["path"]).is_file()
    assert not fake_oss.objects, "不得往 OSS 写任何对象"
