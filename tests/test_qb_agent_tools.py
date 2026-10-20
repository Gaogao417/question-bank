"""Client-agent tools: conservative media, no crops, A/B acceptance integration."""
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image

from question_bank.agent_tools import prepare, new_task, process, read_json, write_json, ToolError, main
from question_bank.service import QuestionBankService


@pytest.fixture
def work(tmp_path):
    word = tmp_path / "word"
    (word / "pages").mkdir(parents=True)
    (word / "media").mkdir()
    for n in range(1, 4):
        Image.new("RGB", (400, 600), (n, 10, 20)).save(word / "pages" / f"{n:03}.png")
    media = []
    attrs = []
    for name, size, confidence, ole in [("good", (180, 100), "high", False),
                                       ("formula", (180, 100), "high", True),
                                       ("fragment", (20, 20), "high", False),
                                       ("wide-formula", (180, 24), "high", False),
                                       ("uncertain", (180, 100), "medium", False)]:
        Image.new("RGB", size, (20, 30, 40)).save(word / "media" / f"{name}.png")
        media.append({"path": f"media/{name}.png", "ole_binding": {"embedded": ole}})
        attrs.append({"media": f"media/{name}.png", "question_number": 1, "bucket": "prompt", "confidence": confidence})
    manifest = {"rendered_pages": [{"path": f"pages/{n:03}.png"} for n in range(1, 4)],
                "paragraphs": [
                    {"index": 0, "text": "1. 如图", "images": ["media/good.png", "media/formula.png"]},
                    {"index": 1, "text": "2. 第二题", "images": ["media/fragment.png", "media/wide-formula.png", "media/uncertain.png"]}],
                "media": media, "image_attribution": attrs, "image_attribution_status": "complete"}
    (word / "word-source.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    out = tmp_path / "materials"
    prepare([word], "TEST-PAPER", out)
    task = tmp_path / "q1"
    new_task(task, 1, "problem", 4)
    (task / "stem.md").write_text("求面积。![题图](asset:fig1)", encoding="utf-8")
    (task / "answer.md").write_text("$4$", encoding="utf-8")
    (task / "solution.md").write_text("面积为 $2 \\times 2=4$。", encoding="utf-8")
    meta = read_json(task / "meta.json")
    meta.update(question_pages=["src1-p001"], solution_pages=["src1-p002"],
                images=[{"key": "fig1", "illustration": "src1-fig001", "role": "stem"}])
    write_json(task / "meta.json", meta)
    return tmp_path, word, out / "materials.json", task


def test_conservative_whitelist(work):
    _, _, materials, _ = work
    m = read_json(materials)
    assert list(m["illustrations"]) == ["src1-fig001"]
    assert {x["reason"] for x in m["excluded"]} == {"formula", "small_fragment", "uncertain_illustration"}


def test_check_submit_retry_and_human_protection(work):
    base, _, materials, task = work
    checked = process(materials, task)
    assert checked["ok"]
    root = base / "bank"
    assert not root.exists()
    QuestionBankService(root).close()
    first = process(materials, task, root)["receipt"]
    repeated = process(materials, task, root)["receipt"]
    assert first["revision"] == 1 and repeated["replayed"]
    service = QuestionBankService(root)
    try:
        detail = service.item_detail(first["item_id"])
        assert detail
        from question_bank.models import SaveEditRequest
        service.save_edit(first["item_id"], SaveEditRequest(base_revision=1, operation_id="human-edit", stem="人工修订").model_dump(exclude_none=True), actor_id="test-human")
    finally:
        service.close()
    (task / "answer.md").write_text("$5$", encoding="utf-8")
    conflict = process(materials, task, root)
    assert not conflict["ok"]
    service = QuestionBankService(root)
    try:
        assert service.conn.execute("SELECT current_revision FROM items").fetchone()["current_revision"] == 2
    finally:
        service.close()


@pytest.mark.parametrize("change", ["crop", "page_image", "external", "gap", "wrong_question", "resolved"])
def test_reject_invalid_agent_input(work, change):
    _, _, materials, task = work
    meta = read_json(task / "meta.json")
    if change == "crop":
        meta["images"][0]["crop_px"] = [0, 0, 50, 50]
    elif change == "page_image":
        meta["images"][0]["illustration"] = "src1-p001"
    elif change == "external":
        (task / "stem.md").write_text("![图](file:///screenshot.png)", encoding="utf-8")
    elif change == "gap":
        meta["question_pages"] = ["src1-p001", "src1-p003"]
    elif change == "wrong_question":
        meta["question_number"] = 2
    else:
        meta["issues"] = [{"issue_id": "fake", "type": "missing_image", "state": "resolved"}]
    write_json(task / "meta.json", meta)
    with pytest.raises(ValueError):
        process(materials, task)


def test_changed_source_is_rejected(work):
    _, word, materials, task = work
    (word / "pages/001.png").write_bytes(b"changed")
    with pytest.raises(ToolError, match="Source changed"):
        process(materials, task)


def test_missing_image_and_answer_can_enter_review(work):
    _, _, materials, task = work
    meta = read_json(task / "meta.json")
    meta.update(images=[], issues=[{"issue_id": "missing-fig", "type": "missing_image", "reason": "请在原页手动截图"},
                                  {"issue_id": "missing-answer", "type": "missing_answer", "reason": "来源缺答案"}])
    write_json(task / "meta.json", meta)
    (task / "stem.md").write_text("如图，求面积。", encoding="utf-8")
    (task / "answer.md").write_text("", encoding="utf-8")
    result = process(materials, task)
    assert result["ok"] and result["content_status"] == "accepted_with_issues"
    assert result["open_blocking_issues"] == 2
    assert result["preflight_only"] and not result["submitted"]


def test_separate_sources_and_no_overwrite(work):
    base, word, _, task = work
    result = prepare([word, word], "SEPARATE", base / "separate")
    m = read_json(result["materials"])
    assert m["pages"]["src2-p001"]["source_id"] == "src2"
    assert len(m["pages"]) == 6
    with pytest.raises(ToolError):
        new_task(task, 1, "problem", 4)


def test_cli_invalid_meta_is_json(work, capsys):
    _, _, materials, task = work
    write_json(task / "meta.json", {"crop_px": [1, 2, 3, 4]})
    assert main(["check", "--materials", str(materials), "--task", str(task)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_prepare_recomputes_wrong_legacy_hints_without_changing_source(work):
    base, word, _, _ = work
    source = word / "word-source.yaml"
    m = yaml.safe_load(source.read_text(encoding="utf-8"))
    for a in m["image_attribution"]:
        a.update(question_number=25, bucket="solution", confidence="high")
    source.write_text(yaml.safe_dump(m), encoding="utf-8")
    original = source.read_bytes()
    out = base / "reprepared"
    prepare([word], "TEST-PAPER", out)
    hints = read_json(out / "materials.json")["illustrations"]["src1-fig001"]["hints"]
    assert hints[0]["question_number"] == 1 and hints[0]["bucket"] == "prompt"
    assert source.read_bytes() == original


def test_prepare_does_not_trust_hints_without_paragraphs(work):
    base, word, _, _ = work
    source = word / "word-source.yaml"
    m = yaml.safe_load(source.read_text(encoding="utf-8"))
    del m["paragraphs"]
    source.write_text(yaml.safe_dump(m), encoding="utf-8")
    out = base / "no-paragraphs"
    prepare([word], "TEST-PAPER", out)
    result = read_json(out / "materials.json")
    assert result["illustrations"] == {}
    assert result["sources"][0]["image_attribution_error"]["code"] == "missing_paragraphs"


def test_old_materials_are_rejected_before_check_or_submit(work):
    _, _, materials, task = work
    m = read_json(materials)
    del m["attribution_version"]
    write_json(materials, m)
    with pytest.raises(ToolError, match="Outdated image attribution"):
        process(materials, task)


def test_preflight_feedback_and_target_receipt(work):
    base, _, materials, task = work
    result = process(materials, task)
    assert result["preflight_only"] and not result["submitted"] and not result["content_approved"]
    assert result["open_blocking_issues"] == 0
    assert result["figures"][0]["original_filename"] == "good.png"
    assert result["figures"][0]["sha256"] == read_json(materials)["illustrations"]["src1-fig001"]["sha256"]
    root = base / "bank"
    QuestionBankService(root).close()
    result = process(materials, task, root)
    assert result["submitted"] and not result["preflight_only"] and not result["content_approved"]


def test_protected_fill_rejects_overwrite_and_stale_reads(tmp_path):
    from question_bank.agent_tools import read_task, fill_task
    task = tmp_path / "q1"
    new_task(task, 1, "problem", 4)
    snapshot = read_task(task)
    assert set(snapshot["files"]) == {"stem.md", "answer.md", "solution.md", "meta.json"}
    fill_task(task, "stem.md", "first")
    with pytest.raises(ToolError, match="not empty"):
        fill_task(task, "stem.md", "overwrite")
    with pytest.raises(ToolError, match="changed since read"):
        fill_task(task, "stem.md", "stale", snapshot["files"]["stem.md"]["sha256"])
    assert (task / "stem.md").read_text() == "first"
    fresh = read_task(task)
    fill_task(task, "stem.md", "second", fresh["files"]["stem.md"]["sha256"])
    assert (task / "stem.md").read_text() == "second"
    with pytest.raises(ValueError):
        fill_task(task, "meta.json", "{}", fresh["files"]["meta.json"]["sha256"])
    assert read_task(task)["files"]["meta.json"] == fresh["files"]["meta.json"]
    (task / ".fill.lock").touch()
    with pytest.raises(ToolError, match="active writer"):
        fill_task(task, "answer.md", "answer")
    assert (task / "answer.md").read_bytes() == b""


def test_status_filters_paper_and_preserves_default(work, capsys):
    from question_bank.cli import main as cli_main
    base, _, materials, task = work
    root = base / "bank"
    QuestionBankService(root).close()
    process(materials, task, root)
    other = read_json(materials)
    other["paper_id"] = "OTHER"
    other_materials = base / "other.json"
    write_json(other_materials, other)
    process(other_materials, task, root)
    assert cli_main(["status", "--root", str(root), "--paper-id", "TEST-PAPER"]) == 0
    assert [i["paper_id"] for i in json.loads(capsys.readouterr().out)["items"]] == ["TEST-PAPER"]
    cli_main(["status", "--root", str(root)])
    assert len(json.loads(capsys.readouterr().out)["items"]) == 2
    cli_main(["status", "--root", str(root), "--paper-id", "' OR 1=1 --"])
    assert json.loads(capsys.readouterr().out)["items"] == []


def test_fill_cli_failure_is_json_and_preserves_file(work, capsys):
    _, _, _, task = work
    original = (task / "stem.md").read_bytes()
    assert main(["fill", "--task", str(task), "--file", "stem.md", "--input", str(task / "answer.md")]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert (task / "stem.md").read_bytes() == original
