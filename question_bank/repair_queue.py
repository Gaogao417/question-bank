"""Traceable repair tasks. A candidate, check or keyword match is not a repair."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from question_bank.reconcile import load_evidence
from question_bank.reporting import digest
from question_bank.content_rules import (
    ANSWER_ISSUE_TYPES,
    SOLUTION_ISSUE_TYPES,
    SOLUTION_REQUIRED_TYPES,
)


def build_queue(report: dict, *, audit_dir=None) -> dict:
    tasks = []
    by_key = {(i["paper_id"], i["question_number"]): i for i in report["items"]}
    reports_by_paper = {}
    rows_by_key = {}
    local_candidates = {}
    evidence = []
    if audit_dir:
        snapshots, p = load_evidence(audit_dir, "inventory/reports-snapshot.json")
        evidence.append(p)
        for row in snapshots:
            reports_by_paper.setdefault(row["paper_id"], []).append(row)
        rows, p = load_evidence(audit_dir, "inventory/report-question-rows.json")
        evidence.append(p)
        for row in rows:
            rows_by_key.setdefault(
                (row["paper_id"], row["question_number"]), []
            ).append(row)
        # Read only the candidate files beside the historical report; report
        # itself, media and the business database are never modified.
        for key, item in by_key.items():
            old = reports_by_paper.get(key[0], [])
            if not old:
                continue
            task_dir = Path(old[0]["path"]).parent / f"Q{key[1]:03d}"
            fields = {}
            raw_fields = {}
            controls = []
            sources = []
            for field in ("stem", "answer", "solution"):
                path = task_dir / (field + ".md")
                if path.is_file():
                    raw = path.read_bytes()
                    raw_text = raw.decode("utf-8")
                    # Scan decoded original text before strip/newline normalization:
                    # Python strip also removes illegal C0 at either boundary.
                    controls.extend(
                        {
                            "field": field,
                            "offset": n,
                            "codepoint": f"U+{ord(c):04X}",
                            "path": str(path),
                            "line": raw_text[:n].count("\n") + 1,
                        }
                        for n, c in enumerate(raw_text)
                        if ord(c) < 32 and c not in "\n\r\t"
                    )
                    raw_fields[field] = hashlib.sha256(raw).hexdigest()
                    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
                    fields[field] = hashlib.sha256(text.encode()).hexdigest()
                    sources.append(
                        {
                            "path": str(path),
                            "line": 1,
                            "field": field,
                            "sha256": raw_fields[field],
                            "normalized_sha256": fields[field],
                        }
                    )
            if sources:
                local_candidates[key] = {
                    "candidate_hash": digest(raw_fields),
                    "normalized_candidate_hash": digest(fields),
                    "source_locations": sources,
                    "raw_field_hashes": raw_fields,
                    "normalized_field_hashes": fields,
                    "field_hashes": fields,
                    "control_characters": controls,
                    "raw_different_fields": [
                        k
                        for k, v in raw_fields.items()
                        if v != item["body_hashes"].get(k)
                    ],
                    "different_fields": [
                        k for k, v in fields.items() if v != item["body_hashes"].get(k)
                    ],
                    "acceptance": "unknown",
                }

    def add(
        kind,
        *,
        item=None,
        paper_id=None,
        source=None,
        details=None,
        confidence="database_fact",
    ):
        paper_id = item["paper_id"] if item else paper_id
        loc = (
            [
                {"path": r["path"], "line": r["line"], "raw": r["raw"]}
                for r in rows_by_key.get((paper_id, item["question_number"]), [])
            ]
            if item
            else []
        )
        if source:
            loc.append(source)
        candidate = (
            local_candidates.get((paper_id, item["question_number"])) if item else None
        )
        if candidate:
            loc.extend(candidate["source_locations"])
        state = (
            {
                "current_revision": item["base_revision"],
                "content_hash": item["content_hash"],
                "review_state": item["review_state"],
                "publication": item["publication"],
                "proposals": item["proposals"],
                "open_issues": [i for i in item["issues"] if i["state"] == "open"],
            }
            if item
            else "unknown"
        )
        task = {
            "type": kind,
            "root": report["root"],
            "paper_id": paper_id,
            "item_id": item["item_id"] if item else None,
            "base_revision": item["base_revision"] if item else None,
            "database_state": state,
            "source_locations": loc,
            "candidate_hash": candidate["candidate_hash"] if candidate else None,
            "candidate_acceptance": "unknown",
            "local_candidate": candidate,
            "source_page_review": "pending_visual_verification",
            "pages_to_verify": item["evidence"] if item else [],
            "confidence": confidence,
            "details": details or {},
            "repair_state": "pending",
        }
        task["task_id"] = "repair-" + digest(task)[:24]
        tasks.append(task)

    for item in report["items"]:
        candidate = local_candidates.get((item["paper_id"], item["question_number"]))
        if candidate and candidate["control_characters"]:
            add(
                "local_candidate_control_character_corruption",
                item=item,
                details={
                    "positions": candidate["control_characters"],
                    "scope": "local_candidate_file_only",
                    "raw_candidate_hash": candidate["candidate_hash"],
                    "normalized_candidate_hash": candidate["normalized_candidate_hash"],
                },
                confidence="local_candidate_fact",
            )
        if candidate and candidate["different_fields"]:
            add(
                "local_candidate_differs_from_database",
                item=item,
                details=candidate,
                confidence="difference_only_not_content_verdict",
            )
        for issue in item["issues"]:
            if issue["state"] == "open":
                add("open_issue", item=item, details=issue)
        if item["control_characters"]:
            add(
                "control_character_corruption",
                item=item,
                details={"positions": item["control_characters"]},
            )
        for p in item["proposals"]:
            if p["effective_status"] in {"open", "stale", "expired", "rejected"}:
                add("proposal_not_accepted", item=item, details=p)
                tasks[-1]["candidate_hash"] = hashlib.sha256(
                    p["patch"].encode()
                ).hexdigest()
                tasks[-1]["candidate_acceptance"] = p["effective_status"]
        # Choice empty solution is deliberately not a defect or repair task.
        if "answer" in item["empty_fields"] and not any(
            i["type"] in ANSWER_ISSUE_TYPES and i["state"] == "open"
            for i in item["issues"]
        ):
            add("undeclared_empty_answer", item=item)
        if (
            "solution" in item["empty_fields"]
            and item["question_type"] in SOLUTION_REQUIRED_TYPES
            and not any(
                i["type"] in SOLUTION_ISSUE_TYPES and i["state"] == "open"
                for i in item["issues"]
            )
        ):
            add("undeclared_empty_solution", item=item)
    for p in report["papers"]:
        if audit_dir and p["paper_id"] not in reports_by_paper:
            add(
                "canonical_report_missing",
                paper_id=p["paper_id"],
                details={"received_unique_items": p["received_unique_items"]},
            )
        if p["missing_question_numbers"]:
            for number in p["missing_question_numbers"]:
                add(
                    "expected_question_missing",
                    paper_id=p["paper_id"],
                    details={
                        "question_number": number,
                        "answer_action": "Do not invent an answer; locate original source.",
                    },
                )
    for s in report["submissions"]:
        if s["status"] not in {"rejected", "conflict"}:
            continue
        item = next((i for i in report["items"] if i["item_id"] == s["item_id"]), None)
        if item is None and "#q" in s["item_key"]:
            pid, number = s["item_key"].rsplit("#q", 1)
            if number.isdigit():
                item = by_key.get((pid, int(number)))
        add("rejected_submission", item=item, paper_id=s.get("paper_id"), details=s)
        # Legacy rejected request_hash may contain a textual JSON surrogate,
        # rather than a cryptographic hash. Preserve it in details only.
        tasks[-1]["candidate_acceptance"] = s["status"]
    if audit_dir:
        classes, p = load_evidence(audit_dir, "analysis/classification-candidates.json")
        evidence.append(p)
        for kind, group in sorted(classes.items()):
            for r in group.get("reports", []):
                for match in r.get("matches", []):
                    # A regex match creates an investigation task, including negated
                    # statements. It never establishes a positive defect.
                    numbers = sorted(
                        {
                            int(n)
                            for n in re.findall(r"\bQ0*(\d+)\b", match["raw"], re.I)
                        }
                    )
                    targets = [
                        by_key[(r["paper_id"], n)]
                        for n in numbers
                        if (r["paper_id"], n) in by_key
                    ]
                    for item in targets or [None]:
                        add(
                            "historical_claim_to_verify",
                            item=item,
                            paper_id=r["paper_id"],
                            source={"path": r["path"], **match},
                            details={
                                "classification_candidate": kind,
                                "claim": match["raw"],
                                "not_confirmed_by_keyword": True,
                            },
                            confidence="unknown",
                        )
        # Load the full audit risk evidence as claims, preserving exact JSON pointer.
        risks, p = load_evidence(audit_dir, "source/content-risk-samples.json")
        evidence.append(p)
        for n, target in enumerate(risks.get("targets", [])):
            item = next(
                (i for i in report["items"] if i["item_id"] == target.get("item_id")),
                None,
            )
            add(
                "audited_content_sample_to_verify",
                item=item,
                source={
                    "path": p["path"],
                    "line": None,
                    "json_pointer": f"/targets/{n}",
                },
                details={
                    "audit_revision": target.get("current_revision"),
                    "audit_candidate_hash": digest(target),
                },
                confidence="unknown",
            )
    unique = {t["task_id"]: t for t in tasks}
    tasks = sorted(
        unique.values(),
        key=lambda t: (
            t["paper_id"] or "",
            t["item_id"] or "",
            t["type"],
            t["task_id"],
        ),
    )
    return {
        "schema": "qb_repair_queue/v1",
        "run_id": report["run_id"],
        "root": report["root"],
        "captured_at": report["captured_at"],
        "snapshot_hash": report["snapshot_hash"],
        "task_count": len(tasks),
        "counts_by_type": dict(sorted(Counter(t["type"] for t in tasks).items())),
        "unique_referenced_database_items": len(
            {t["item_id"] for t in tasks if t["item_id"]}
        ),
        "unconfirmed_claim_tasks": sum(t["confidence"] == "unknown" for t in tasks),
        "local_candidates": [
            {"paper_id": key[0], "item_id": by_key[key]["item_id"], **candidate}
            for key, candidate in sorted(local_candidates.items())
        ],
        "tasks": tasks,
        "audit_evidence": evidence,
        "rules": [
            "Local candidates are not accepted corrections; only database revisions and receipts establish acceptance.",
            "Tasks are multiple-source work items, not a count of unique defects or affected questions.",
            "Local candidate raw C0 is scanned before normalization; raw and normalized hashes are distinct provenance.",
            "Keyword classifications are unconfirmed claims and may contain negation.",
            "Choice empty solution is legal under current rules.",
            "Missing original papers remain blocked; never fabricate answers or count them as successful.",
            "Unknown historical attempts or absent report details remain unknown.",
        ],
    }
