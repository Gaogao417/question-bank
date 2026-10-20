"""UI e2e 辅助：在临时管理根播种固定候选（不调用模型）。"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from tests.test_qb_manage_service import make_package  # noqa: E402
from question_bank.service import QuestionBankService  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1])
    staging = Path(sys.argv[2])
    package = make_package(staging, submission_id="ui-e2e-001")
    service = QuestionBankService(root)
    try:
        receipt = service.submit_candidate(package)
    finally:
        service.close()
    print(receipt["status"], receipt["item_id"], "r" + str(receipt["revision"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
