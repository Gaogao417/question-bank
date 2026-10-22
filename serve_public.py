"""Review UI 入口：在 0.0.0.0 上对外提供服务，并加 HTTP Basic Auth 保护。

用法: QB_UI_PASSWORD=... uvicorn serve_public:app --host 0.0.0.0 --port 8890
"""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path

from question_bank.server import create_app

ROOT = Path(os.environ.get("QB_ROOT", str(Path(__file__).parent / "data"))).expanduser().resolve()
UI_USER = os.environ.get("QB_UI_USER", "admin")
UI_PASSWORD = os.environ.get("QB_UI_PASSWORD", "")
REALM = b'Basic realm="question-bank"'


class BasicAuthMiddleware:
    """纯 ASGI 中间件，避免 BaseHTTPMiddleware 对流式响应的影响。"""

    def __init__(self, app):
        if not UI_PASSWORD:
            raise RuntimeError("QB_UI_PASSWORD is required for the public Question Bank service")
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"")
        if auth.startswith(b"Basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")
            except Exception:
                user, pw = "", ""
            if hmac.compare_digest(user, UI_USER) and hmac.compare_digest(pw, UI_PASSWORD):
                return await self.app(scope, receive, send)
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [(b"www-authenticate", REALM), (b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": b"401 Unauthorized"})


app = BasicAuthMiddleware(create_app(ROOT))
