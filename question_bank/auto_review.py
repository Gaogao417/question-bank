"""Revision-bound automatic acceptance, separate from human review facts."""

from __future__ import annotations

import hashlib
from typing import Any

from question_bank import db
from question_bank.canonical import canonical_json
from question_bank.errors import QbError
from question_bank.review_routing import is_image_issue, ai_repair_needed

AUTO_REVIEW_POLICY = "no-open-issues-v1"


class AutoReviewMixin:
    def _auto_accept_in_transaction(self, item_id: str, revision: int) -> bool:
        if not self.conn.in_transaction:
            raise RuntimeError("automatic acceptance requires a write transaction")
        item = self._item_row(item_id)
        if not item or item["current_revision"] != revision:
            return False
        # Never overwrite a review decision, especially a human rejection.
        if self.latest_review_decision(item_id, revision) is not None:
            return False
        if self._has_unreviewed_ai_change(item_id, revision):
            return False
        try:
            content = self._content_of(item_id, revision)
            if any(issue.state == "open" for issue in content.issues):
                return False
            if self.approval_blockers(item_id, revision):
                return False
        except (QbError, ValueError):
            # Invalid legacy content stays pending; another item can still pass.
            return False
        operation_id = (
            "auto-"
            + hashlib.sha256(
                f"{AUTO_REVIEW_POLICY}:{item_id}:{revision}".encode("utf-8")
            ).hexdigest()[:40]
        )
        note = (
            "系统自动通过：当前修订无未决疑点，且满足结构与答案检查；不表示人工审阅。"
        )
        cursor = self.conn.execute(
            "INSERT INTO reviews(item_id, revision, decision, actor_kind, actor_id,"
            " note, operation_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING review_id",
            (
                item_id,
                revision,
                "approved",
                "system",
                AUTO_REVIEW_POLICY,
                note,
                operation_id,
                db.utc_now(),
            ),
        )
        receipt = {
            "operation_id": operation_id,
            "item_id": item_id,
            "revision": revision,
            "decision": "approved",
            "status": "reviewed",
            "review_id": cursor.fetchone()["review_id"],
            "approval_source": "system",
            "policy": AUTO_REVIEW_POLICY,
            "replayed": False,
        }
        self._insert_operation(
            operation_id,
            canonical_json(
                {"item_id": item_id, "revision": revision, "policy": AUTO_REVIEW_POLICY}
            ),
            "auto_review",
            "system",
            AUTO_REVIEW_POLICY,
            item_id,
            revision,
            revision,
            receipt,
        )
        return True

    def auto_accept_current_items(self) -> int:
        """Apply the authorized policy once at startup, without rewriting history."""
        with self._write_lock:
            self.conn.begin_write()
            try:
                # These are the same two early exits as the per-item gate. Do
                # not substitute the issues table for the content JSON here:
                # legacy content must still pass the original content checks.
                rows = self.conn.execute(
                    "SELECT i.item_id, i.current_revision FROM items i"
                    " WHERE i.current_revision IS NOT NULL"
                    " AND NOT EXISTS (SELECT 1 FROM reviews j"
                    " WHERE j.item_id=i.item_id AND j.revision=i.current_revision)"
                    " AND NOT EXISTS (SELECT 1 FROM revisions r"
                    " WHERE r.item_id=i.item_id AND r.revision<=i.current_revision"
                    " AND r.actor_kind='ai' AND r.parent_revision IS NOT NULL"
                    " AND r.revision>COALESCE((SELECT MAX(j.revision) FROM reviews j"
                    " WHERE j.item_id=i.item_id AND j.revision<i.current_revision),0))"
                    " ORDER BY i.item_id"
                ).fetchall()
                count = sum(
                    self._auto_accept_in_transaction(
                        row["item_id"], row["current_revision"]
                    )
                    for row in rows
                )
                self.conn.execute("COMMIT")
                return count
            except Exception:
                self._rollback_quiet()
                raise

    @staticmethod
    def ai_repair_needed(detail):
        return ai_repair_needed(detail)

    def _has_unreviewed_ai_change(self, item_id, revision):
        reviewed = self.conn.execute(
            "SELECT MAX(revision) AS revision FROM reviews WHERE item_id = %s AND revision < %s",
            (item_id, revision),
        ).fetchone()["revision"]
        return bool(
            self.conn.execute(
                "SELECT 1 FROM revisions WHERE item_id = %s AND revision > %s AND revision <= %s"
                " AND actor_kind = 'ai' AND parent_revision IS NOT NULL LIMIT 1",
                (item_id, reviewed or 0, revision),
            ).fetchone()
        )

    def review_metadata(self, item_id: str, revision: int | None) -> dict[str, Any]:
        review = self.conn.execute(
            "SELECT decision, actor_kind, actor_id FROM reviews WHERE item_id = %s AND revision = %s"
            " ORDER BY review_id DESC LIMIT 1",
            (item_id, revision),
        ).fetchone()
        open_issues = self.conn.execute(
            "SELECT COUNT(*) AS count FROM issues WHERE item_id = %s AND revision = %s AND state = 'open'",
            (item_id, revision),
        ).fetchone()["count"]
        source = review["actor_kind"] if review else None
        if review:
            workflow = (
                "auto_approved"
                if (
                    review["decision"] == "approved"
                    and source == "system"
                    and review["actor_id"] == AUTO_REVIEW_POLICY
                )
                else review["decision"]
            )
        else:
            reviewed = self.conn.execute(
                "SELECT 1 FROM reviews WHERE item_id = %s AND revision < %s LIMIT 1",
                (item_id, revision),
            ).fetchone()
            workflow = (
                "ai_revised"
                if self._has_unreviewed_ai_change(item_id, revision)
                else "modified"
                if reviewed
                else "unreviewed"
            )
        issues = (
            [i for i in self._content_of(item_id, revision).issues if i.state == "open"]
            if revision
            else []
        )
        manual_images = [i.issue_id for i in issues if is_image_issue(i)]
        ai_issues = [i.issue_id for i in issues if not is_image_issue(i)]
        task = (
            "image"
            if manual_images
            else "ai"
            if ai_issues
            else "review_ai"
            if workflow == "ai_revised"
            else "review"
        )
        return {
            "review_state": review["decision"] if review else None,
            "review_workflow": workflow,
            "approval_source": source,
            "open_issues": open_issues,
            "has_open_issues": bool(open_issues),
            "manual_image_issues": manual_images,
            "ai_issue_count": len(ai_issues),
            "review_task": task,
        }
