"""UI e2e 播种（老版查卷/连续审核复刻）：多卷、多种审核状态，不调用模型。

- 2014-HUANGPU-YIMO：3 题，Q001 已批准，Q002/Q003 待审。
- 2015-SONGJIANG-ERMO：3 题全部待审（全卷通过的目标卷）。
- 2016-XUHUI-YIMO：2 题，Q002 带阻断疑点（全卷通过部分失败的目标卷）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from tests.test_qb_manage_service import make_package  # noqa: E402
from question_bank.service import QuestionBankService  # noqa: E402

PLANS = [
    ("2014-HUANGPU-YIMO", 1, "seed-hp-1", []),
    ("2014-HUANGPU-YIMO", 2, "seed-hp-2", []),
    ("2014-HUANGPU-YIMO", 3, "seed-hp-3", []),
    ("2015-SONGJIANG-ERMO", 1, "seed-sj-1", []),
    ("2015-SONGJIANG-ERMO", 2, "seed-sj-2", []),
    ("2015-SONGJIANG-ERMO", 3, "seed-sj-3", []),
    ("2016-XUHUI-YIMO", 1, "seed-xh-1", []),
    ("2016-XUHUI-YIMO", 2, "seed-xh-2", [
        {
            "issue_id": "iss-crop",
            "type": "needs_human_crop",
            "target": "images[fig-01]",
            "reason": "边界不清，需人工补图",
            "blocking": True,
        }
    ]),
]


def main() -> int:
    root = Path(sys.argv[1])
    staging = Path(sys.argv[2])
    service = QuestionBankService(root)
    try:
        for paper_id, question_number, submission_id, issues in PLANS:
            receipt = service.submit_candidate(make_package(
                staging, submission_id=submission_id, paper_id=paper_id,
                title="", grade="九年级", question_number=question_number,
                issues=issues,
            ))
            assert receipt["status"] in ("accepted", "accepted_with_issues"), receipt
        service.record_review("2014-HUANGPU-YIMO-Q001", {
            "decision": "approved", "note": "", "base_revision": 1,
            "operation_id": "seed-rv-hp1",
        })
    finally:
        service.close()
    print("seeded 3 papers / 8 items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
