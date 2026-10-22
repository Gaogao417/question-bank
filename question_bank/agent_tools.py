"""Deterministic tools for a Codex/Claude Code agent; no model runtime.

prepare -> new -> agent edits three .md files and meta.json -> check -> submit.
All business acceptance rules are reused from QuestionBankService.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from question_bank.models import CandidateEnvelope, CandidateIssue, KEY_PATTERN, marker_keys
from question_bank.service import QuestionBankService
from question_bank.errors import QbError
from question_bank import db as db_mod


ATTRIBUTION_VERSION = "section-aware-v2"


TASK_FILES = ("stem.md", "answer.md", "solution.md", "meta.json")


class ToolError(ValueError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def within(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ToolError(f"File missing or outside source directory: {relative}")
    return path


def verified(entry):
    path = Path(entry["path"])
    if not path.is_file() or digest(path) != entry["sha256"]:
        raise ToolError(f"Source changed or missing; prepare materials again: {path}")
    return path


def image_size(path):
    with Image.open(path) as im:
        im.load()
        if im.format not in {"PNG", "JPEG", "WEBP"}:
            raise ToolError("Not a directly usable raster image")
        return list(im.size)


def prepare(word_dirs, paper_id, out):
    """Read existing extractor output, preserve source identities, never crop."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", paper_id):
        raise ToolError("paper_id must use letters, numbers, '-' or '_'")
    out = Path(out).resolve()
    if out.exists():
        raise ToolError("Output already exists; use a new materials directory")
    result = {"schema": "qb_agent_materials/v1", "paper_id": paper_id,
              "sources": [], "pages": {}, "illustrations": {}, "excluded": [],
              "attribution_version": ATTRIBUTION_VERSION}
    paragraphs = []
    for index, folder in enumerate(word_dirs, 1):
        root = Path(folder).resolve()
        manifest_path = root / "word-source.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        sid = f"src{index}"
        result["sources"].append({"id": sid, "path": str(manifest_path),
                                  "sha256": digest(manifest_path), "source": manifest.get("source", {})})
        pages = manifest.get("rendered_pages", [])
        if not pages:
            raise ToolError(f"No rendered pages in {manifest_path}; run extract_docx_source.py first")
        for number, record in enumerate(pages, 1):
            path = within(root, record["path"])
            size = image_size(path)
            result["pages"][f"{sid}-p{number:03d}"] = {
                "path": str(path), "sha256": digest(path), "size_px": size,
                "source_id": sid, "page_number": number, "global_page_number": len(result["pages"]) + 1}
        # Recompute legacy manifests in memory: old attribution may incorrectly
        # attach an entire answer section to the final question. Preserve source
        # bytes and illustration IDs; never migrate active material directories.
        from question_bank import docx_source as extractor
        source_paragraphs = manifest.get("paragraphs")
        if source_paragraphs:
            bindings = {m["path"]: m.get("ole_binding", {}) for m in manifest.get("media", [])}
            attrs, attribution_status, attribution_error = extractor.attribute_images_with_status(source_paragraphs, bindings)
        else:
            attrs, attribution_status, attribution_error = [], "failed", {"code": "missing_paragraphs"}
        result["sources"][-1].update(image_attribution_status=attribution_status,
                                      image_attribution_error=attribution_error)
        hints = {}
        for attr in attrs:
            hints.setdefault(str(attr.get("media", "")).replace("\\", "/"), []).append(attr)
        for number, record in enumerate(manifest.get("media", []), 1):
            relative = record["path"]
            reason = None
            matches = hints.get(relative.replace("\\", "/"), [])
            try:
                path = within(root, relative)
                size = image_size(path)
                if (record.get("ole_binding") or {}).get("embedded") or record.get("emf_class") == "formula":
                    reason = "formula"
                elif min(size) < 64:
                    reason = "small_fragment"
                elif attribution_status != "complete":
                    reason = "uncertain_attribution"
                elif not any(h.get("confidence") == "high" and h.get("question_number") for h in matches):
                    reason = "uncertain_illustration"
            except (OSError, ValueError) as exc:
                reason = f"unusable: {exc}"
            if reason:
                result["excluded"].append({"source_id": sid, "file": relative, "reason": reason})
                continue
            result["illustrations"][f"{sid}-fig{number:03d}"] = {
                "path": str(path), "sha256": digest(path), "size_px": size,
                "source_id": sid, "original_filename": Path(relative).name, "hints": matches}
        paragraphs.append(f"# {sid}: {root}\n\n辅助定位文本；公式和正文必须对照原页。\n\n" +
                          "\n".join(str(p.get("text", "")) for p in manifest.get("paragraphs", [])))
    out.mkdir(parents=True)
    write_json(out / "materials.json", result)
    (out / "paragraphs.md").write_text("\n\n".join(paragraphs), encoding="utf-8")
    lines = [f"# {paper_id} agent materials", "", "原页用于读题与来源证据，不得自行截图/裁图。缺图请标待人工补图。", "", "## Pages"]
    for key, page in result["pages"].items():
        lines.append(f"- `{key}`: {page['path']}")
    lines += ["", "## Usable original illustrations", "只复用下列完整原图；不确定的图不提供。无需使用全部图片。"]
    for key, media in result["illustrations"].items():
        lines += [f"- `{key}` ({media['size_px']}): {media['path']}",
                  f"  original_filename: {media['original_filename']}; sha256: {media['sha256']}",
                  f"  hints: {json.dumps(media['hints'], ensure_ascii=False)}"]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"materials": str(out / "materials.json"), "pages": len(result["pages"]),
            "usable_illustrations": len(result["illustrations"]), "excluded": len(result["excluded"])}


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    illustration: str
    role: str


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_number: int = Field(ge=1, le=200)
    question_type: str
    points: int = Field(ge=0, le=200)
    section_title: str = ""
    base_revision: int | None = None
    question_pages: list[str] = Field(default_factory=list)
    solution_pages: list[str] = Field(default_factory=list)
    images: list[Ref] = Field(default_factory=list)
    issues: list[CandidateIssue] = Field(default_factory=list)


def new_task(out, number, question_type, points):
    out = Path(out)
    if out.exists():
        raise ToolError("Task already exists; refusing to overwrite agent work")
    task = Task(question_number=number, question_type=question_type, points=points)
    out.mkdir(parents=True)
    write_json(out / "meta.json", task.model_dump())
    for name in ("stem", "answer", "solution"):
        (out / f"{name}.md").write_text("", encoding="utf-8")
    return {"task": str(out.resolve()), "edit": list(TASK_FILES),
            "next_step": "Read all four files before editing (read --task); use fill with the returned sha256 when replacing existing content"}


def read_task(task_dir):
    return {"task": str(Path(task_dir).resolve()), "files": {
        name: {"content": (data := within(task_dir, name).read_bytes()).decode("utf-8"),
               "sha256": hashlib.sha256(data).hexdigest()} for name in TASK_FILES}}


def fill_task(task_dir, name, content, expected_sha256=None):
    """Atomically replace one candidate file, serializing cooperating writers."""
    if name not in TASK_FILES:
        raise ToolError("Only the four candidate files may be filled")
    path = within(task_dir, name)
    if name == "meta.json":
        Task.model_validate(json.loads(content))
    lock = Path(task_dir) / ".fill.lock"
    try:
        handle = lock.open("x")
    except FileExistsError:
        raise ToolError("Candidate is being filled; retry after the active writer finishes")
    temporary = None
    try:
        with handle:
            current = path.read_bytes()
            actual = hashlib.sha256(current).hexdigest()
            if expected_sha256 is None:
                if current:
                    raise ToolError("Candidate file is not empty; read it and provide --expected-sha256")
            elif actual != expected_sha256:
                raise ToolError("Candidate changed since read; refusing overwrite. Read again and reconcile changes")
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fill-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return {"ok": True, "file": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()


def build_package(materials, task_dir, out):
    materials_path = Path(materials).resolve()
    m = read_json(materials_path)
    if m.get("attribution_version") != ATTRIBUTION_VERSION:
        raise ToolError("Outdated image attribution; prepare materials again in a new directory and review image references")
    if m.get("schema") != "qb_agent_materials/v1":
        raise ToolError("Unsupported materials schema")
    # The source manifest fingerprint must still match before using its whitelist.
    for source in m["sources"]:
        verified(source)
    task_dir = Path(task_dir)
    task = Task.model_validate(read_json(task_dir / "meta.json"))
    body = {name: (task_dir / f"{name}.md").read_text(encoding="utf-8") for name in ("stem", "answer", "solution")}
    if not task.question_pages:
        raise ToolError("question_pages must cite the original stem pages")
    if (body["answer"].strip() or body["solution"].strip()) and not task.solution_pages:
        raise ToolError("solution_pages must cite the original answer/solution pages")
    if len({im.key for im in task.images}) != len(task.images):
        raise ToolError("Duplicate image binding key")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / "media").mkdir()

    def copy(entry):
        src = verified(entry)
        relative = f"media/{entry['sha256']}{src.suffix.lower()}"
        shutil.copyfile(src, out / relative)
        return {"ref": relative, "sha256": "sha256:" + entry["sha256"]}

    evidence = []
    for role, ids in (("question", task.question_pages), ("solution", task.solution_pages)):
        if len(ids) != len(set(ids)):
            raise ToolError(f"Duplicate {role} page IDs")
        groups = {}
        for pid in ids:
            if pid not in m["pages"]:
                raise ToolError(f"Unknown source page: {pid}")
            page = m["pages"][pid]
            groups.setdefault(page["source_id"], []).append(page["page_number"])
            evidence.append({"role": role, "asset": copy(page), "page_number": page["global_page_number"]})
        for nums in groups.values():
            if sorted(nums) != list(range(min(nums), max(nums) + 1)):
                raise ToolError(f"{role} evidence must include every intermediate page")
    images = []
    for ordinal, ref in enumerate(task.images):
        if not KEY_PATTERN.fullmatch(ref.key) or ref.role not in ("stem", "solution"):
            raise ToolError("Invalid image key/role")
        if ref.illustration not in m["illustrations"]:
            raise ToolError("Only whitelisted complete illustrations may be used; no page crops")
        entry = m["illustrations"][ref.illustration]
        bucket = "prompt" if ref.role == "stem" else "solution"
        if not any(h.get("question_number") == task.question_number and h.get("bucket") == bucket and
                   h.get("confidence") == "high" for h in entry["hints"]):
            raise ToolError("Illustration not confidently attributed to this question/role; leave for manual UI correction")
        if ref.key not in marker_keys(body[ref.role]):
            raise ToolError(f"Image {ref.key} must appear in {ref.role}.md")
        images.append({"key": ref.key, "role": ref.role, "asset": copy(entry), "ordinal": ordinal})
    for name, text in body.items():
        # External/inline images and malformed asset markers are not an escape hatch.
        markers = re.findall(r"!\[[^\]]*\]\([^)]*\)", text)
        if text.count("![") != len(markers) or len(markers) != len(marker_keys(text)) or re.search(r"<\s*(img|svg|iframe|object)\b|data:image", text, re.I):
            raise ToolError(f"Only ![label](asset:key) images allowed in {name}.md")
        allowed = {ref.key for ref in task.images if ref.role == name}
        if any(key not in allowed for key in marker_keys(text)):
            raise ToolError(f"Unbound or wrong-role image in {name}.md")
    issues = [i.model_dump() for i in task.issues]
    if any(i["state"] != "open" or i["resolution"] is not None for i in issues):
        raise ToolError("Agent submissions may declare open issues, not human resolutions")
    payload = {"schema": "question_bank_candidate/v1", "submission_id": "pending",
               "base_revision": task.base_revision,
               "paper": {"paper_id": m["paper_id"], "source_archive": str(materials_path)},
               "item": {"question_number": task.question_number, "question_type": task.question_type,
                        "points": task.points, "section_title": task.section_title, **body},
               "images": images, "evidence": evidence, "issues": issues}
    # Stable for exact retries; editing the candidate produces a new request ID.
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    payload["submission_id"] = "agent-" + fingerprint
    CandidateEnvelope.model_validate(payload)
    write_json(out / "candidate.json", payload)
    write_json(out / "agent-provenance.json", {"materials": str(materials_path), "materials_sha256": digest(materials_path),
                                              "source_pages": {"question": task.question_pages, "solution": task.solution_pages}})
    return payload


def process(materials, task_dir, root=None, actor="client-agent", *, server=None, user=None, password=None, timeout=120):
    if server and root is not None:
        raise ToolError("--root conflicts with --server/QB_SERVER_URL; unset QB_SERVER_URL for direct mode")
    if server:
        return process_remote(materials, task_dir, server, user=user, password=password, timeout=timeout)
    if root is not None and not db_mod.is_initialized():
        raise ToolError("Management database missing; initialize explicitly using existing cli init")
    with tempfile.TemporaryDirectory(prefix="qb-agent-") as tmp:
        package = Path(tmp) / "package"
        payload = build_package(materials, task_dir, package)
        m = read_json(materials)
        task = Task.model_validate(read_json(Path(task_dir) / "meta.json"))
        figures = [{"figure_id": ref.illustration, "key": ref.key, "role": ref.role,
                    "question_number": task.question_number,
                    "original_filename": Path(m["illustrations"][ref.illustration]["path"]).name,
                    "sha256": m["illustrations"][ref.illustration]["sha256"]} for ref in task.images]
        feedback = {"preflight_only": True, "submitted": False, "content_approved": False,
                    "open_blocking_issues": sum(i["state"] == "open" and i["blocking"] for i in payload["issues"]),
                    "figures": figures,
                    "note": "Temporary database structural preflight only; does not verify transcription correctness, target database acceptance, or publication approval"}
        # Full current A/B checks in an isolated temporary DB, never writes target DB.
        with db_mod.temporary_schema() as schema:
            service = QuestionBankService(Path(tmp) / "check-db", database_schema=schema, oss=False)
            try:
                result = service.submit_candidate(package, actor_id="agent-preflight")
            finally:
                service.close()
        if result["status"] not in {"accepted", "accepted_with_issues", "noop"}:
            return {**feedback, "ok": False, "phase": "check", "receipt": result}
        if root is None:
            return {**feedback, "ok": True, "phase": "check", "submission_id": payload["submission_id"],
                    "preflight_status": result["status"], "content_status": result["status"]}
        # Retain the exact submitted package before touching the management DB.
        archive = Path(task_dir) / "submissions" / payload["submission_id"]
        if archive.exists():
            if read_json(archive / "candidate.json") != payload:
                raise ToolError("Existing archived submission differs; refusing overwrite")
            for ref in [x["asset"] for x in payload["images"] + payload["evidence"]]:
                if "sha256:" + digest(within(archive, ref["ref"])) != ref["sha256"]:
                    raise ToolError("Archived submission media changed")
        else:
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package, archive)
            shutil.copyfile(materials, archive / "materials.snapshot.json")
        service = QuestionBankService(root)
        try:
            receipt = service.submit_candidate(archive, actor_id=actor)
        finally:
            service.close()
        # Local receipts are append-only, including repeated/conflicting attempts.
        receipts = Path(task_dir) / "receipts"
        receipts.mkdir(exist_ok=True)
        import uuid
        write_json(receipts / f"{uuid.uuid4().hex}.json", receipt)
        accepted = receipt["status"] in {"accepted", "accepted_with_issues", "noop"}
        return {**feedback, "ok": accepted, "phase": "submit", "receipt": receipt,
                "preflight_only": False, "submitted": accepted,
                "note": "Target database receipt; accepted_with_issues remains pending review. This command does not approve content or publish"}


def process_remote(materials, task_dir, server, **options):
    from question_bank.remote_ingestion import package_bytes, request
    import uuid
    with tempfile.TemporaryDirectory(prefix="qb-remote-") as tmp:
        package = Path(tmp) / "package"
        payload = build_package(materials, task_dir, package)
        shutil.copyfile(materials, package / "materials.snapshot.json")
        sources = package / "sources"
        sources.mkdir()
        for index, entry in enumerate(read_json(materials)["sources"]):
            shutil.copyfile(verified(entry), sources / f"{index:04}.source")
        archive = Path(task_dir) / "submissions" / payload["submission_id"]
        if archive.exists():
            expected = {p.relative_to(package): digest(p) for p in package.rglob("*") if p.is_file()}
            actual = {p.relative_to(archive): digest(p) for p in archive.rglob("*") if p.is_file()}
            if expected != actual:
                raise ToolError("Archived package changed; refusing retry")
        else:
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package, archive)
        result = request(server, "/api/submissions", raw=package_bytes(archive), **options)
        receipts = Path(task_dir) / "receipts"
        receipts.mkdir(exist_ok=True)
        write_json(receipts / f"{uuid.uuid4().hex}.json", result)
        return {**result, "submission_id": payload["submission_id"], "phase": "submit",
                "preflight_only": False, "content_approved": False,
                "submitted": result.get("ok", False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--word-dir", action="append", required=True, help="Existing extractor output; repeat for separate answers")
    p.add_argument("--paper-id", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("new")
    p.add_argument("--out", required=True)
    p.add_argument("--number", type=int, required=True)
    p.add_argument("--type", choices=["choice", "fillin", "short_answer", "problem"], required=True)
    p.add_argument("--points", type=int, required=True)
    p = sub.add_parser("read")
    p.add_argument("--task", required=True)
    p = sub.add_parser("fill")
    p.add_argument("--task", required=True)
    p.add_argument("--file", choices=TASK_FILES, required=True)
    p.add_argument("--input", required=True, help="UTF-8 text file, or - for stdin")
    p.add_argument("--expected-sha256")
    for command in ("check", "submit"):
        p = sub.add_parser(command)
        p.add_argument("--materials", required=True)
        p.add_argument("--task", required=True)
        if command == "submit":
            target = p.add_mutually_exclusive_group()
            target.add_argument("--root")
            target.add_argument("--server", default=__import__("os").environ.get("QB_SERVER_URL"))
            p.add_argument("--user")
            p.add_argument("--password")
            p.add_argument("--timeout", type=float, default=120)
            p.add_argument("--actor", default="client-agent")
    p = sub.add_parser("upload", help="Upload a prepared candidate.json package without DB/OSS configuration")
    p.add_argument("--package", required=True)
    p.add_argument("--server", default=__import__("os").environ.get("QB_SERVER_URL"))
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--receipt-out")
    p = sub.add_parser("receipt")
    p.add_argument("--server", default=__import__("os").environ.get("QB_SERVER_URL"))
    p.add_argument("--submission-id", required=True)
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.word_dir, args.paper_id, args.out)
        elif args.command == "new":
            result = new_task(args.out, args.number, args.type, args.points)
        elif args.command == "read":
            result = read_task(args.task)
        elif args.command == "fill":
            content = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
            result = fill_task(args.task, args.file, content, args.expected_sha256)
        elif args.command == "upload":
            from question_bank.remote_ingestion import package_bytes, request
            if not args.server:
                raise ToolError("upload requires --server or QB_SERVER_URL")
            result = request(args.server, "/api/submissions", raw=package_bytes(args.package), user=args.user, password=args.password, timeout=args.timeout)
            if args.receipt_out:
                destination = Path(args.receipt_out)
                with destination.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
        elif args.command == "receipt":
            from question_bank.remote_ingestion import request
            from urllib.parse import quote
            if not args.server:
                raise ToolError("receipt requires --server or QB_SERVER_URL")
            result = request(args.server, "/api/submissions/" + quote(args.submission_id, safe=""), user=args.user, password=args.password, timeout=args.timeout)
        else:
            if args.command == "submit" and not (args.root or args.server):
                raise ToolError("submit requires --root or --server/QB_SERVER_URL")
            result = process(args.materials, args.task, getattr(args, "root", None), getattr(args, "actor", "client-agent"),
                             server=getattr(args, "server", None), user=getattr(args, "user", None),
                             password=getattr(args, "password", None), timeout=getattr(args, "timeout", 120))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except QbError as exc:
        print(json.dumps({"ok": False, "errors": exc.errors}, ensure_ascii=False))
        return 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "errors": [{"code": "AGENT_INPUT_INVALID", "message": str(exc),
                            "fix_hint": "Check materials/meta; use only full whitelisted originals, cite source pages; uncertain images go to Review UI"}]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
