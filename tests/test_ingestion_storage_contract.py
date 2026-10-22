"""Offline storage acceptance: no database connections or real OSS requests."""
import hashlib
import json

import pytest

from question_bank import backup, oss_store
from question_bank.assets import AssetStore
from question_bank.errors import QbError, error
from tests.test_qb_oss_store import FakeOSS, _png_bytes


@pytest.fixture
def storage(monkeypatch, tmp_path):
    fake = FakeOSS()
    for name in ("put_object", "get_object", "object_exists"):
        monkeypatch.setattr(oss_store, name, getattr(fake, name))
    return AssetStore(tmp_path, oss=True), fake


def test_repeated_bytes_keep_identity_and_avoid_second_upload(storage):
    store, fake = storage
    first = store.register_bytes(_png_bytes())
    assert store.register_bytes(_png_bytes()) == first
    assert fake.calls.count("put:" + first["path"]) == 1
    assert "sha256:" + hashlib.sha256(fake.objects[first["path"]][0]).hexdigest() == first["sha256"]


def test_upload_failure_does_not_return_success(storage, monkeypatch):
    store, fake = storage
    def fail(*args):
        raise QbError([error("OSS_ERROR", "injected upload failure", retryable=True)])
    monkeypatch.setattr(oss_store, "put_object", fail)
    with pytest.raises(QbError):
        store.register_bytes(_png_bytes())
    assert fake.objects == {}


def test_changed_content_uses_new_key_and_preserves_old_bytes(storage):
    store, fake = storage
    first = store.register_bytes(_png_bytes())
    original = fake.objects[first["path"]]
    second = store.register_bytes(_png_bytes(color=(0, 255, 0)))
    assert first["path"] != second["path"]
    assert fake.objects[first["path"]] == original


def test_missing_object_read_is_rejected(storage):
    store, fake = storage
    first = store.register_bytes(_png_bytes())
    del fake.objects[first["path"]]
    with pytest.raises(QbError):
        store._load_bytes(first["path"])


def test_preflight_local_storage_makes_no_oss_calls(storage, tmp_path):
    _, fake = storage
    local = AssetStore(tmp_path / "preflight", oss=False)
    first = local.register_bytes(_png_bytes())
    assert (local.root / first["path"]).read_bytes() == _png_bytes()
    assert fake.calls == []


def test_full_snapshot_verification_detects_corrupt_object(storage, tmp_path):
    store, fake = storage
    first = store.register_bytes(_png_bytes())
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    tables = {name: [] for name in backup.TABLES}
    tables["assets"] = [first]
    (snapshot / "database.json").write_text(json.dumps({
        "format": "question-bank-postgresql/v1", "oss": fake.describe(),
        "files": {}, "tables": tables,
    }))
    fake.objects[first["path"]] = (_png_bytes(color=(0, 255, 0)), "image/png")
    assert backup.verify(snapshot)["items"] == 0  # existence is not integrity
    with pytest.raises(QbError):
        backup.verify(snapshot, full=True)


def test_registration_rejects_existing_object_with_wrong_hash(storage):
    store, fake = storage
    first = store.register_bytes(_png_bytes())
    fake.objects[first["path"]] = (_png_bytes(color=(0, 255, 0)), "image/png")
    with pytest.raises(QbError):
        store.register_bytes(_png_bytes())


def test_read_rejects_object_with_wrong_hash(storage):
    store, fake = storage
    first = store.register_bytes(_png_bytes())
    fake.objects[first["path"]] = (_png_bytes(color=(0, 255, 0)), "image/png")
    with pytest.raises(QbError):
        store._load_bytes(first["path"])


@pytest.mark.parametrize("mutation", ["replace", "delete"])
def test_warm_cache_does_not_hide_storage_damage(storage, mutation):
    store, fake = storage
    asset = store.register_bytes(_png_bytes())
    store._load_bytes(asset["path"], cache=True)
    if mutation == "delete":
        del fake.objects[asset["path"]]
    else:
        fake.objects[asset["path"]] = (_png_bytes(color=(0, 255, 0)), "image/png")
    with pytest.raises(QbError):
        store._load_bytes(asset["path"], cache=True)


def test_audio_registration_and_read_reject_wrong_hash(storage, monkeypatch):
    store, fake = storage
    monkeypatch.setattr(store, "_probe_audio", lambda *args: ("ok", 1.0, ""))
    raw = b"RIFF" + b"original audio"
    asset = store.register_audio(raw, "audio/wav")
    fake.objects[asset["path"]] = (b"RIFFdifferent audio", "audio/wav")
    with pytest.raises(QbError):
        store.register_audio(raw, "audio/wav")
    with pytest.raises(QbError):
        store.read_audio_bytes("audio/wav", asset["path"])


def test_reuse_checks_hash_without_head_and_does_not_overwrite_on_read_error(storage, monkeypatch):
    store, fake = storage
    info = store.register_bytes(_png_bytes())
    fake.calls.clear()
    store.register_bytes(_png_bytes())
    assert fake.calls == ["get:" + info["path"]]
    def fail(key):
        raise QbError([error("OSS_ERROR", "injected read timeout", retryable=True)])
    monkeypatch.setattr(oss_store, "get_object", fail)
    with pytest.raises(QbError):
        store.register_bytes(_png_bytes())
    assert not any(call.startswith("put:") for call in fake.calls)


def test_validation_decodes_and_checks_crop_without_encoding_png(storage, monkeypatch):
    from PIL import Image
    from question_bank.models import ImageBinding
    store, fake = storage
    asset = store.register_bytes(_png_bytes())
    binding = ImageBinding(key='fig', asset_id=asset['asset_id'], role='stem',
                           crop_px=[0, 0, 10, 10], rotate=90, ordinal=0)
    def forbidden(*args, **kwargs):
        raise AssertionError('validation must not encode a preview')
    with monkeypatch.context() as m:
        m.setattr(Image.Image, 'save', forbidden)
        store.validate_binding_renderable(binding)
        invalid = binding.model_copy(update={'crop_px': [0, 0, 1000, 1000]})
        with pytest.raises(QbError) as exc:
            store.validate_binding_renderable(invalid)
        assert exc.value.errors[0]['code'] == 'CROP_OUT_OF_BOUNDS'
    # Actual user-facing preview still encodes the transformed image.
    rendered = store.render(asset['asset_id'], crop_px=[0, 0, 10, 10], rotate=90)
    assert rendered.startswith(b'\x89PNG')
