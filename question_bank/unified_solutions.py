"""Legacy question editing uses the bank's one Storage solution owner.

Question revisions remain immutable snapshots. No asset bytes are moved here.
The owner binding is provisioned by Teaching Storage; a standalone old bank keeps
its existing behavior until that explicit binding exists.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from psycopg import sql


class SolutionConflict(ValueError):
    def __init__(self, expected, actual):
        super().__init__("解法已被其他操作修改，请刷新后重试。")
        self.expected, self.actual = expected, actual


def owner_schema(conn):
    exists = conn.execute("SELECT to_regclass(current_schema() || '.teaching_solution_owner') AS owner").fetchone()
    if not exists or exists["owner"] is None:
        return None
    row = conn.execute("SELECT storage_schema FROM teaching_solution_owner WHERE singleton").fetchone()
    if row is None:
        return None
    owner = row["storage_schema"]
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", owner):
        raise ValueError("Invalid canonical solution owner")
    return owner


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reference_id(question):
    identity = "\0".join((question["source"], question["question_id"], str(question["revision"]), question["content_hash"]))
    return "reference-" + hashlib.sha256(identity.encode()).hexdigest()[:48]


def content_hash(question, name, text, reasoning_path=None, files=None):
    payload = {"question": question, "name": name, "text": text, "reasoning_path": reasoning_path, "files": files or []}
    return "sha256:" + hashlib.sha256(canonical(payload).encode()).hexdigest()


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _lock(conn, owner):
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("teaching-storage:" + owner,))


def _insert(conn, owner, saved):
    q = saved["question"]
    conn.execute(sql.SQL("INSERT INTO {}.question_references(source,question_id,revision,content_hash,original_question_id) VALUES('question-bank',%s,%s,%s,%s) ON CONFLICT DO NOTHING").format(sql.Identifier(owner)),
        (q["question_id"], q["revision"], q["content_hash"], q["question_id"]))
    conn.execute(sql.SQL("INSERT INTO {}.question_solutions(solution_id,revision,content_hash,question_source,question_id,question_revision,question_hash,data) VALUES(%s,%s,%s,'question-bank',%s,%s,%s,%s::jsonb)").format(sql.Identifier(owner)),
        (saved["solution_id"], saved["revision"], saved["content_hash"], q["question_id"], q["revision"], q["content_hash"], canonical(saved)))


def ensure_reference(conn, item_id, revision):
    owner = owner_schema(conn)
    if owner is None:
        return None
    with conn.transaction():
        _lock(conn, owner)
        row = conn.execute("SELECT content_hash,content,actor_id,actor_kind,created_at FROM revisions WHERE item_id=%s AND revision=%s FOR SHARE", (item_id, revision)).fetchone()
        if row is None:
            raise ValueError("Exact question revision missing")
        q = {"source": "question-bank", "question_id": item_id, "revision": revision, "content_hash": row["content_hash"]}
        sid = reference_id(q)
        current = conn.execute(sql.SQL("SELECT data FROM {}.question_solutions WHERE solution_id=%s ORDER BY revision DESC LIMIT 1").format(sql.Identifier(owner)), (sid,)).fetchone()
        if current:
            return _json(current["data"])
        text = str(_json(row["content"]).get("solution") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return None
        name = "原参考解法"
        saved = {"solution_id": sid, "revision": 1, "question": q, "name": name, "text": text,
            "reasoning_path": None, "files": [], "content_hash": content_hash(q, name, text),
            "actor": {"id": row["actor_id"], "kind": row["actor_kind"]},
            "created_at": row["created_at"], "is_reference": True}
        _insert(conn, owner, saved)
        return saved


def save_reference_text(conn, item_id, revision, text, expected_revision, actor_kind, actor_id):
    owner = owner_schema(conn)
    if owner is None:
        return None
    with conn.transaction():
        _lock(conn, owner)
        current = ensure_reference(conn, item_id, revision)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if current is not None and normalized == current["text"]:
            return current
        actual = current["revision"] if current else None
        # Old r1 clients can edit their original snapshot; later independent
        # solution edits require the explicit CAS supplied by the current UI.
        if expected_revision is None and actual == 1:
            expected_revision = 1
        if expected_revision != actual:
            raise SolutionConflict(expected_revision, actual)
        if current is None:
            row = conn.execute("SELECT content_hash FROM revisions WHERE item_id=%s AND revision=%s", (item_id, revision)).fetchone()
            q = {"source": "question-bank", "question_id": item_id, "revision": revision, "content_hash": row["content_hash"]}
            current = {"solution_id": reference_id(q), "question": q, "name": "原参考解法"}
        q = current["question"]
        saved = {"solution_id": current["solution_id"], "revision": (actual or 0) + 1, "question": q,
            "name": current["name"], "text": normalized, "reasoning_path": None, "files": [],
            "content_hash": content_hash(q, current["name"], normalized),
            "actor": {"id": actor_id, "kind": actor_kind}, "created_at": datetime.now(timezone.utc).isoformat(), "is_reference": True}
        _insert(conn, owner, saved)
        return saved
