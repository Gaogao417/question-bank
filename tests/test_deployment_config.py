"""Boot checks must fail closed without revealing credentials or touching DB."""
import os
import subprocess
import sys

from question_bank.deployment_check import validate


def test_oss_boot_requires_its_credentials_and_authentication():
    env = {"QB_DATABASE_URL": "postgresql://user:private@db/test", "QB_OSS_BUCKET": "test"}
    failures = validate(env)
    assert len(failures) == 3
    assert "private" not in " ".join(failures)
    env.update(QB_UI_PASSWORD="test-password", ALIBABA_CLOUD_ACCESS_KEY_ID="test-id",
               ALIBABA_CLOUD_ACCESS_KEY_SECRET="test-secret")
    assert validate(env) == []


def test_public_entrypoint_refuses_missing_password_without_db_connection():
    env = os.environ.copy()
    env.pop("QB_UI_PASSWORD", None)
    env.pop("QB_DATABASE_URL", None)
    process = subprocess.run([sys.executable, "-c", "import serve_public"], env=env,
                             capture_output=True, text=True, timeout=15)
    assert process.returncode != 0
    assert "QB_UI_PASSWORD is required" in process.stderr


def test_config_errors_do_not_echo_invalid_database_url():
    failures = validate({"QB_UI_PASSWORD": "test", "QB_DATABASE_URL": "https://user:secret@example.com/db"})
    assert len(failures) == 1
    assert "secret" not in failures[0]


def test_public_unauthorized_response_is_complete_asgi_bytes(tmp_path, monkeypatch):
    import importlib.util
    from fastapi.testclient import TestClient
    monkeypatch.setenv("QB_UI_PASSWORD", "isolated-test-password")
    monkeypatch.setenv("QB_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location("qb_boot_auth_test", "serve_public.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    with TestClient(entry.app) as client:
        response = client.post("/api/submissions", content=b"not uploaded")
        assert response.status_code == 401
        assert response.content == b"401 Unauthorized"
        assert not (tmp_path / "inputs").exists()
