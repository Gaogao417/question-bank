"""Compare a read-only snapshot with immutable historical audit evidence.

Historical prose and classification keyword matches remain claims, never facts.
No attempt totals, acceptance or content corrections are inferred from prose.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path


def load_evidence(directory, relative):
    path = Path(directory).resolve() / relative
    raw = path.read_bytes()
    return json.loads(raw), {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def reconcile_report(report: dict, audit_dir) -> dict:
    snapshots, provenance = load_evidence(audit_dir, "inventory/reports-snapshot.json")
    rows, row_provenance = load_evidence(
        audit_dir, "inventory/report-question-rows.json"
    )
    known = {p["paper_id"] for p in report["papers"]}
    reports = []
    for old in snapshots:
        path = Path(old["path"])
        current_hash = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        reports.append(
            {
                "paper_id": old["paper_id"],
                "path": old["path"],
                "audit_sha256": old["sha256"],
                "source_sha256": current_hash,
                "source_state": "missing"
                if current_hash is None
                else "unchanged"
                if current_hash == old["sha256"]
                else "changed_after_audit",
                "paper_in_snapshot": old["paper_id"] in known,
            }
        )
    report_papers = {r["paper_id"] for r in reports}
    item_by_key = {(i["paper_id"], i["question_number"]): i for i in report["items"]}
    submissions_by_item = {}
    for s in report["submissions"]:
        if s["item_id"]:
            submissions_by_item.setdefault(s["item_id"], []).append(s)
    differences = []
    for old in rows:
        key = (old["paper_id"], old["question_number"])
        item = item_by_key.get(key)
        if item is None:
            # Reports outside the selected root/scope are retained in coverage,
            # and must not be counted as missing questions in this root.
            if old["paper_id"] in known:
                differences.append(
                    {
                        "kind": "reported_question_not_in_database",
                        "historical": old,
                        "database_state": "missing",
                    }
                )
            continue
        submissions = submissions_by_item.get(item["item_id"], [])
        recorded_statuses = sorted({s["status"] for s in submissions})
        if old.get("status") and old["status"] not in recorded_statuses:
            differences.append(
                {
                    "kind": "status_claim_not_supported_by_persisted_submissions",
                    "item_id": item["item_id"],
                    "base_revision": item["base_revision"],
                    "historical": old,
                    "persisted_statuses": recorded_statuses,
                    "interpretation": "Claim mismatch; historical attempts may be unrecorded.",
                }
            )
        old_types = set(old.get("types", []))
        new_types = {i["type"] for i in item["issues"] if i["state"] == "open"}
        if old_types != new_types:
            differences.append(
                {
                    "kind": "historical_issue_types_differ_from_current_open_types",
                    "item_id": item["item_id"],
                    "base_revision": item["base_revision"],
                    "historical": old,
                    "current_open_types": sorted(new_types),
                    "interpretation": "Current revision may differ from the historical report.",
                }
            )
    missing = [p for p in report["papers"] if p["paper_id"] not in report_papers]
    return {
        "schema": "qb_reconciliation/v1",
        "root": report["root"],
        "run_id": report["run_id"],
        "captured_at": report["captured_at"],
        "snapshot_hash": report["snapshot_hash"],
        "evidence": [provenance, row_provenance],
        "historical_reports": reports,
        "coverage": {
            "historical_report_files": len(reports),
            "database_papers": len(known),
            "database_papers_with_canonical_report": len(known & report_papers),
            "database_papers_without_canonical_report": len(missing),
            "items_in_papers_without_canonical_report": sum(
                p["received_unique_items"] for p in missing
            ),
            "papers_without_canonical_report": [p["paper_id"] for p in missing],
            "historical_report_papers_outside_root": sorted(report_papers - known),
        },
        "difference_counts": dict(
            sorted(Counter(d["kind"] for d in differences).items())
        ),
        "differences": differences,
        "limits": [
            "Reports outside this root are not summed into this bank.",
            "Canonical report absence is documentation coverage, not proof of absent content.",
            "Historical prose is not a submission ledger or correctness certificate.",
        ],
    }
