"""题库管理 HTTP API 测试：真实 FastAPI 应用 + 独立重开服务验证重启一致性。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from question_bank.server import create_app  # noqa: E402
from tests.test_qb_manage_service import (  # noqa: E402
    ANSWER_TEXT,
    STEM_TEXT,
    SOLUTION_TEXT,
    make_package,
)


def _png_bytes(size=(190, 110), color=(200, 90, 30)) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "manage-root"


@pytest.fixture()
def client(root: Path) -> TestClient:
    app = create_app(root)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def submitted(root: Path, tmp_path: Path) -> dict:
    from question_bank.service import QuestionBankService

    service = QuestionBankService(root)
    try:
        return service.submit_candidate(make_package(tmp_path))
    finally:
        service.close()


@pytest.fixture()
def client_with_item(root: Path, submitted: dict) -> TestClient:
    app = create_app(root)
    with TestClient(app) as test_client:
        yield test_client


class TestApiClosedLoop:
    def test_bootstrap_and_detail(self, client_with_item, submitted):
        response = client_with_item.get("/api/bootstrap")
        assert response.status_code == 200
        data = response.json()
        assert data["papers"][0]["paper_id"] == "2026-DEMO"
        detail = client_with_item.get("/api/items/2026-DEMO-Q021").json()
        assert detail["revision"] == 1
        assert detail["body"]["stem"] == STEM_TEXT
        assert [image["key"] for image in detail["images"]] == ["fig-01", "fig-sol"]
        assert len(detail["evidence"]) == 2

    def test_asset_render_and_crop(self, client_with_item, submitted):
        detail = client_with_item.get("/api/items/2026-DEMO-Q021").json()
        binding = detail["images"][0]
        source = binding["resolved_source"] or binding["asset_id"]
        response = client_with_item.get(
            f"/api/assets/{source}/render?l=10&t=10&r=200&b=140&rotate=90"
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        import io

        image = Image.open(io.BytesIO(response.content))
        assert image.size == (130, 190)  # 裁 190x130 后旋转 90°

    def test_save_text_edit_then_restart_server_state_consistent(
        self, root, submitted
    ):
        app = create_app(root)
        with TestClient(app) as first:
            patch = {
                "base_revision": 1,
                "operation_id": "op-ui-1",
                "stem": STEM_TEXT.replace("求函数的对称轴", "求该函数图像的对称轴"),
            }
            receipt = first.patch(
                "/api/items/2026-DEMO-Q021/content", json=patch
            ).json()
            assert receipt["status"] == "saved" and receipt["revision"] == 2
        # “重启”：全新应用实例 + 新连接，只依赖磁盘数据库
        app2 = create_app(root)
        with TestClient(app2) as second:
            detail = second.get("/api/items/2026-DEMO-Q021").json()
            assert detail["revision"] == 2
            assert "该函数图像的对称轴" in detail["body"]["stem"]
            history = [r["revision"] for r in detail["revisions"]]
            assert history == [1, 2]

    def test_image_upload_and_binding_update(self, client_with_item, submitted):
        upload = client_with_item.post(
            "/api/items/2026-DEMO-Q021/assets",
            content=_png_bytes(),
            headers={"content-type": "image/png"},
        )
        assert upload.status_code == 200
        asset_id = upload.json()["asset_id"]
        detail = client_with_item.get("/api/items/2026-DEMO-Q021").json()
        images = []
        for binding in detail["images"]:
            payload = {k: binding[k] for k in (
                "key", "asset_id", "role", "source_asset_id", "crop_px",
                "rotate", "width", "ordinal")}
            if binding["key"] == "fig-01":
                payload.update({
                    "asset_id": asset_id, "source_asset_id": asset_id,
                    "crop_px": [5, 5, 185, 105], "rotate": 180,
                })
            images.append(payload)
        receipt = client_with_item.patch(
            "/api/items/2026-DEMO-Q021/content",
            json={
                "base_revision": 1, "operation_id": "op-ui-img", "images": images,
            },
        ).json()
        assert receipt["revision"] == 2
        updated = {
            b["key"]: b
            for b in client_with_item.get("/api/items/2026-DEMO-Q021").json()["images"]
        }
        assert updated["fig-01"]["asset_id"] == asset_id
        assert updated["fig-01"]["rotate"] == 180

    def test_conflict_returns_409_with_payload(self, client_with_item, submitted):
        first = client_with_item.patch(
            "/api/items/2026-DEMO-Q021/content",
            json={"base_revision": 1, "operation_id": "op-a", "answer": "修改一"},
        )
        assert first.status_code == 200
        second = client_with_item.patch(
            "/api/items/2026-DEMO-Q021/content",
            json={"base_revision": 1, "operation_id": "op-b", "answer": "并发修改"},
        )
        assert second.status_code == 409
        detail = second.json()["detail"]
        assert detail["conflict"]["current_revision"] == 2
        assert "body.answer" in detail["conflict"]["changed_fields"]

    def test_operation_receipt_query(self, client_with_item, submitted):
        receipt = client_with_item.patch(
            "/api/items/2026-DEMO-Q021/content",
            json={"base_revision": 1, "operation_id": "op-q", "points": 14},
        ).json()
        queried = client_with_item.get("/api/operations/op-q").json()
        assert queried["operation_id"] == "op-q"
        assert queried["revision"] == receipt["revision"]

    def test_invalid_edit_returns_field_errors(self, client_with_item, submitted):
        response = client_with_item.patch(
            "/api/items/2026-DEMO-Q021/content",
            json={"base_revision": 1, "operation_id": "op-bad", "answer": ""},
        )
        assert response.status_code == 400
        codes = [e["code"] for e in response.json()["detail"]["errors"]]
        assert "ANSWER_UNDECLARED_EMPTY" in codes

    def test_review_publish_export_flow(self, root, tmp_path):
        from question_bank.service import QuestionBankService

        service = QuestionBankService(root)
        try:
            service.submit_candidate(make_package(
                tmp_path, submission_id="sub-flow",
                answer_placeholder=True,
                issues=[{
                    "issue_id": "iss-1", "type": "missing_answer",
                    "target": "body.answer", "reason": "缺答案", "blocking": True,
                }],
            ))
        finally:
            service.close()

        app = create_app(root)
        with TestClient(app) as client:
            item = "/api/items/2026-DEMO-Q021"
            # 未解决疑点前不能批准
            blocked = client.post(f"{item}/review", json={
                "decision": "approved", "base_revision": 1,
                "operation_id": "op-appr-early",
            })
            assert blocked.status_code == 400
            assert blocked.json()["detail"]["errors"][0]["code"] == "APPROVAL_BLOCKED"
            # 同一事务：补答案 + 解决疑点
            fixed = client.patch(f"{item}/content", json={
                "base_revision": 1, "operation_id": "op-fix",
                "answer": ANSWER_TEXT,
                "issue_ops": [{"issue_id": "iss-1", "state": "resolved",
                               "note": "人工补齐"}],
            }).json()
            assert fixed["revision"] == 2
            approved = client.post(f"{item}/review", json={
                "decision": "approved", "base_revision": 2,
                "operation_id": "op-appr",
            }).json()
            assert approved["decision"] == "approved"
            exported = client.post(f"{item}/exports", json={
                "revision": 2,
            }).json()
            assert exported["status"] == "ready"
            export_dir = root / exported["path"]
            assert (export_dir / "teacher.assignment.yaml").is_file()
            assert (export_dir / "student.assignment.yaml").is_file()
            assert (export_dir / "manifest.json").is_file()
            published = client.post(f"{item}/publication", json={
                "revision": 2, "expected_previous": None,
                "operation_id": "op-pub",
            }).json()
            assert published["status"] == "published"
            # r3 草稿保存后：发布指针不变、r3 待复核
            edited = client.patch(f"{item}/content", json={
                "base_revision": 2, "operation_id": "op-edit3",
                "solution": SOLUTION_TEXT + "\n（人工补充）",
            }).json()
            assert edited["revision"] == 3
            detail = client.get(item).json()
            assert detail["publication"]["revision"] == 2
            assert detail["review_state"] is None
            withdrawn = client.post(f"{item}/withdrawal", json={
                "expected_previous": published["publication_id"],
                "operation_id": "op-wd",
            }).json()
            assert withdrawn["status"] == "withdrawn"
            assert client.get(item).json()["publication"] is None

    def test_healthz(self, client, root):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_index_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "题库管理" in response.text
