#!/usr/bin/env python3
"""Normalize a DOC/DOCX source, extract Word media, and render PDF pages.

Text and formulas are transcribed from the PDF rendered pages; Word media
images are extracted only for diagram prompts (几何图/统计图/照片等) that would
lose resolution or transparency if re-rasterized from the PDF.

Paragraph stream (段落流) is the authoritative source for image attribution:
each embedded media is bound to its containing paragraph by OOXML structure,
then classified into prompt/solution buckets per question via a paragraph
state machine. Confidence flags mark cases needing human review.

Output directory layout:
  <output_dir>/
    source.docx|source.doc       # original file copy
    normalized.docx               # OOXML version (DOC→DOCX conversion if needed)
    word-source.yaml              # paragraph stream + image attribution + media + PDF pages
    media/                        # original embedded images and formula objects (WMF/EMF/PNG)
    ooxml/
      document.xml
      document.xml.rels
    rendered.pdf                  # PDF rendered by soffice (formula images baked in)
    pages/
      001.png, 002.png, ...       # rendered PDF pages as PNG (text/formula transcription)

The PDF pages are the authoritative source for all text and formula transcription.
Word media images are the authoritative source for diagram prompts.
Paragraph stream is the authoritative source for image attribution.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image
import yaml


NAMESPACES = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "o": "urn:schemas-microsoft-com:office:office",
    "v": "urn:schemas-microsoft-com:vml",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"

# Paragraph markers that separate stem from solution in teacher-edition DOCX.
SOLUTION_MARKERS = re.compile(r"【分析】|【详解】|【小问\d*详解】|【解答】|【解析】")
# Question number at paragraph start, e.g. "1．" / "12." (full/half-width period).
QUESTION_NUMBER = re.compile(r"^(\d+)[．.]")
# Stem wording that declares a figure, e.g. 如图 / 图1 / 下图.
FIGURE_DECLARATION = re.compile(r"如图|下图|上图|图中|图\d{1,2}")

# --- 媒体三态分类（公式碎片治理，2026-09 审查 P0-2）---------------------------
# 旧卷公式常失去 OLE 绑定、仅剩行内预览小图，被当内容图塞进题面（缺陷 A7）
# 或堆成解答碎片风暴（A4）。分类为「疑似碎片」的媒体保留文件与溯源、进入
# needs_review，绝不默认进 prompt。尺寸单独不判定；EMF/WMF 未绑定仍按图处理。
FRAGMENT_MAX_EDGE_PX = 50
FRAGMENT_DENSE_PARAGRAPH_IMAGES = 3
# 选项段落保护：A．/B．/C．/D． 或 （A）… 形式的选项行即使多图也是真图。
OPTION_PARAGRAPH = re.compile(r"[（(][A-D][）)]|[A-D][．.、]\s*\S")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def relationship_map(xml_bytes: bytes) -> dict[str, str]:
    """Parse word/_rels/document.xml.rels into {rId: media target}."""
    root = ET.fromstring(xml_bytes)
    return {
        node.attrib["Id"]: node.attrib["Target"]
        for node in root.findall(f"{{{RELATIONSHIP_NAMESPACE}}}Relationship")
        if "Id" in node.attrib and "Target" in node.attrib
    }


def paragraph_records(document_xml: bytes, relationships: dict[str, str]) -> list[dict]:
    """Extract the paragraph stream from OOXML.

    Each paragraph records its index, text, and the media files embedded in it.
    Empty paragraphs are dropped; the rest keep previous/next non-empty text for
    context (used to locate question numbers when image paragraphs are empty).
    """
    root = ET.fromstring(document_xml)
    records: list[dict] = []
    rel_key = f"{{{NAMESPACES['r']}}}embed"
    vml_rel_key = f"{{{NAMESPACES['r']}}}id"
    for index, paragraph in enumerate(root.findall(".//w:p", NAMESPACES)):
        text = "".join(
            node.text or "" for node in paragraph.findall(".//w:t", NAMESPACES)
        ).strip()
        relation_ids = [
            node.attrib.get(rel_key)
            for node in paragraph.findall(".//a:blip", NAMESPACES)
        ]
        relation_ids.extend(
            node.attrib.get(vml_rel_key)
            for node in paragraph.findall(".//v:imagedata", NAMESPACES)
        )
        images: list[str] = []
        for relation_id in relation_ids:
            target = relationships.get(str(relation_id), "")
            if target.startswith("media/") and target not in images:
                images.append(target)
        records.append({"index": index, "text": text, "images": images})

    previous_text = ""
    for record in records:
        record["previous_text"] = previous_text
        if record["text"]:
            previous_text = record["text"]
    next_text = ""
    for record in reversed(records):
        record["next_text"] = next_text
        if record["text"]:
            next_text = record["text"]
    return [record for record in records if record["text"] or record["images"]]


def ole_formula_bindings(
    document_xml: bytes, relationships: dict[str, str]
) -> dict[str, dict]:
    """Return deterministic media-preview -> embedded OLE formula evidence.

    Word stores many MathType/Equation objects as a ``w:object`` containing
    both a VML preview image and an ``o:OLEObject`` relationship. The preview's
    extension is not authoritative: an unbound EMF/WMF can be a real diagram,
    while an OLE-bound PNG can be a formula preview.
    """

    root = ET.fromstring(document_xml)
    rel_key = f"{{{NAMESPACES['r']}}}id"
    bindings: dict[str, dict] = {}
    for obj in root.findall(".//w:object", NAMESPACES):
        ole = obj.find(".//o:OLEObject", NAMESPACES)
        if ole is None:
            continue
        ole_rid = ole.attrib.get(rel_key)
        object_path = relationships.get(str(ole_rid), "")
        prog_id = ole.attrib.get("ProgID")
        for preview in obj.findall(".//v:imagedata", NAMESPACES):
            preview_rid = preview.attrib.get(rel_key)
            media_target = relationships.get(str(preview_rid), "")
            if not media_target.startswith("media/"):
                continue
            bindings[media_target] = {
                "embedded": True,
                "relationship_id": ole_rid,
                "object_path": object_path or None,
                "prog_id": prog_id,
            }
    return bindings


def classify_media(
    paragraphs: list[dict],
    ole_bindings: dict[str, dict],
    media_records: list[dict] | None = None,
) -> dict[str, dict]:
    """媒体三态分类：formula / standalone / fragment（疑似公式碎片）。

    判据组合（单一尺寸/扩展名不决定分类）：
    - formula：OLE 绑定（确定性公式预览，沿用 ole_formula_bindings）。
    - fragment：栅格图且已知尺寸，min 边 < FRAGMENT_MAX_EDGE_PX，
      且至少一个佐证信号（同段图片数 ≥2 / 全文出现 ≥2 / 同段有正文），
      且所在段落不是选项行（四图选项保护）。
      ——对应 2026-09 审查发现的 38×18px「±√2」类碎片（A7）与
      同段公式群（A4 碎片风暴的单段形态）。
    - standalone：其余全部（含无尺寸信息的 EMF/WMF 未绑定媒体——按原语义
      视为可能的真实图，交给既有 confidence/needs_review 机制）。
    """
    sizes = {
        r["path"]: (r.get("width_px"), r.get("height_px"))
        for r in (media_records or [])
    }
    density: dict[int, int] = {}
    has_text: dict[int, bool] = {}
    occurrences: dict[str, int] = {}
    for rec in paragraphs:
        density[rec["index"]] = len(rec["images"])
        has_text[rec["index"]] = bool(rec["text"].strip())
        for target in rec["images"]:
            occurrences[target] = occurrences.get(target, 0) + 1

    kinds: dict[str, dict] = {}
    for rec in paragraphs:
        for target in rec["images"]:
            if target in kinds:
                continue
            if ole_bindings.get(target, {}).get("embedded"):
                kinds[target] = {"kind": "formula", "evidence": {"ole": True}}
                continue
            width, height = sizes.get(target, (None, None))
            min_edge = min(width, height) if width and height else None
            evidence = {
                "min_edge_px": min_edge,
                "paragraph_images": density[rec["index"]],
                "paragraph_has_text": has_text[rec["index"]],
                "occurrences": occurrences[target],
            }
            tiny = min_edge is not None and min_edge < FRAGMENT_MAX_EDGE_PX
            corroborated = (
                density[rec["index"]] >= 2
                or occurrences[target] >= 2
                or has_text[rec["index"]]
            )
            option_paragraph = bool(
                OPTION_PARAGRAPH.search(rec["text"]) and density[rec["index"]] >= 2
            )
            if tiny and corroborated and not option_paragraph:
                kinds[target] = {"kind": "fragment", "evidence": evidence}
            else:
                kinds[target] = {"kind": "standalone", "evidence": evidence}
    return kinds


def _is_prompt_media(
    target: str,
    ole_bindings: dict[str, dict],
    media_kinds: dict[str, dict] | None = None,
) -> bool:
    """A media target is content unless formula-bound or a suspected fragment.

    With ``media_kinds`` (from :func:`classify_media`), OLE-bound formulas and
    suspected formula fragments are both excluded from prompt/solution buckets;
    fragments are re-emitted separately with bucket ``fragment`` for review.
    """

    if media_kinds is not None:
        kind = media_kinds.get(target, {}).get("kind", "standalone")
        return kind == "standalone"
    return not bool(ole_bindings.get(target, {}).get("embedded"))


def attribute_images(
    paragraphs: list[dict],
    ole_bindings: dict[str, dict] | None = None,
    media_kinds: dict[str, dict] | None = None,
) -> list[dict]:
    """Classify each non-formula image into a question bucket with confidence.

    Walks the paragraph stream with a state machine:
      - QUESTION_NUMBER at paragraph start opens a new question (strictly
        increasing within each explicit question/answer section).
      - Explicit answer headings reset numbering; scored sequences ignore bare
        numbered solution steps. Ambiguous scored restarts fail closed.
      - SOLUTION_MARKERS switch the bucket from prompt to solution.

    With ``media_kinds`` (from :func:`classify_media`), suspected formula
    fragments never enter prompt/solution buckets: they are emitted with
    bucket ``fragment`` (confidence ``low``) carrying the classifier evidence,
    so downstream routes them to needs_review instead of the question prompt.

    Confidence:
      high   - image sits inside one question's stem region and the stem wording
               declares a figure whose count matches the image count.
      medium - image sits inside a question region but the stem figure-declaration
               count disagrees with the image count (e.g. a composite figure, or a
               multi-subquestion figure that the state machine cannot split).
      low    - the image appears in multiple paragraphs, the question numbering is
               not strictly increasing at its location, or it falls outside any
               question region (orphan).
    """
    ole_bindings = ole_bindings or {}

    # An answer section is a second question sequence, not numbered solution
    # steps. End the previous range before resetting, so heading images cannot
    # inherit the final question's identity.
    answer_heading = re.compile(r"^(?:参考答案(?:与试题解析|及解析)?|答案与解析|试题解析|参考解答)[：:]?$")
    scored_question = re.compile(r"^\d+[．.]\s*[（(]\s*\d+(?:\.\d+)?\s*分[）)]")
    question_starts: list[tuple[int, int | None]] = []
    prev_q = 0
    scored_sequence = False
    for record in paragraphs:
        text = record["text"].strip()
        if answer_heading.fullmatch(text):
            question_starts.append((record["index"], None))
            prev_q = 0
            scored_sequence = False
            continue
        m = QUESTION_NUMBER.match(text)
        if not m:
            continue
        n = int(m.group(1))
        scored = bool(scored_question.match(text))
        # In scored papers, bare numbered lines are solution steps.
        if scored_sequence and not scored:
            continue
        if scored and prev_q and n <= prev_q:
            raise ValueError(
                f"DOCX question-number state lost at paragraph {record['index']}: "
                f"unexpected scored question restart {n}; answer section heading required."
            )
        if n == prev_q + 1:
            question_starts.append((record["index"], n))
            prev_q = n
            scored_sequence = scored_sequence or scored
        elif n > prev_q + 1:
            raise ValueError(
                "DOCX question-number state lost at paragraph "
                f"{record['index']}: expected question {prev_q + 1}, "
                f"found question {n}. Refusing image attribution; "
                "do not infer question-image mappings from media filenames."
            )
    # Append sentinel.
    question_starts.append((paragraphs[-1]["index"] + 1 if paragraphs else 0, None))

    # Build per-question stem/solution paragraph ranges keyed by question_no.
    # paragraphs may have gaps in index (empties dropped); map index->position.
    index_to_pos = {rec["index"]: pos for pos, rec in enumerate(paragraphs)}

    attributions: list[dict] = []
    for qi in range(len(question_starts) - 1):
        start_idx, qno = question_starts[qi]
        end_idx, _ = question_starts[qi + 1]
        if qno is None:
            continue
        # Slice paragraph positions belonging to this question.
        positions = [
            pos for rec in paragraphs
            if start_idx <= rec["index"] < end_idx
            for pos in [index_to_pos[rec["index"]]]
        ]
        if not positions:
            continue
        # Find solution boundary inside this question.
        sol_pos = None
        for pos in positions:
            if SOLUTION_MARKERS.search(paragraphs[pos]["text"]):
                sol_pos = pos
                break
        stem_positions = positions if sol_pos is None else [p for p in positions if p < sol_pos]
        sol_positions = [] if sol_pos is None else [p for p in positions if p >= sol_pos]

        stem_text = "".join(paragraphs[p]["text"] for p in stem_positions)
        declared_fig_nums = {int(x) for x in re.findall(r"图(\d{1,2})", stem_text)}
        has_figure_word = bool(FIGURE_DECLARATION.search(stem_text))
        declared_count = max(len(declared_fig_nums), 1 if has_figure_word and not declared_fig_nums else 0)

        def _emit(positions_list: list[int], bucket: str) -> None:
            seen_media: dict[str, int] = {}
            for pos in positions_list:
                for target in paragraphs[pos]["images"]:
                    if (
                        media_kinds is not None
                        and media_kinds.get(target, {}).get("kind") == "fragment"
                    ):
                        # 疑似公式碎片：保留溯源单独发射，不进 prompt/solution。
                        attributions.append({
                            "media": target,
                            "question_number": qno,
                            "bucket": "fragment",
                            "paragraph_index": paragraphs[pos]["index"],
                            "confidence": "low",
                            "kind_evidence": media_kinds[target].get("evidence", {}),
                        })
                        continue
                    if not _is_prompt_media(target, ole_bindings, media_kinds):
                        continue
                    seen_media[target] = seen_media.get(target, 0) + 1
                    # Confidence.
                    if bucket == "prompt":
                        if declared_count and seen_media[target] == 1:
                            # Compare total prompt image count to declaration below.
                            conf = "medium"
                        else:
                            conf = "medium"
                    else:
                        conf = "high"
                    attributions.append({
                        "media": target,
                        "question_number": qno,
                        "bucket": bucket,
                        "paragraph_index": paragraphs[pos]["index"],
                        "confidence": conf,
                    })

        range_start = len(attributions)
        _emit(stem_positions, "prompt")
        _emit(sol_positions, "solution")

        # Refine prompt confidence now that the full prompt set is known.
        prompt_media = [a for a in attributions[range_start:] if a["bucket"] == "prompt"]
        prompt_unique = {a["media"] for a in prompt_media}
        # Mark duplicates low.
        for a in prompt_media:
            occ = sum(1 for pos in positions if a["media"] in paragraphs[pos]["images"])
            if occ > 1:
                a["confidence"] = "low"
        if prompt_media:
            if not declared_count and not has_figure_word:
                # Stem does not declare any figure but images present: suspect.
                for a in prompt_media:
                    a["confidence"] = "medium"
            elif declared_count and len(prompt_unique) == declared_count:
                for a in prompt_media:
                    if a["confidence"] != "low":
                        a["confidence"] = "high"
            elif declared_count and len(prompt_unique) != declared_count:
                for a in prompt_media:
                    if a["confidence"] != "low":
                        a["confidence"] = "medium"

    # Reuse by different questions is ambiguous even if each local range looks
    # valid. Never promote such a mapping into the agent whitelist.
    owners: dict[str, set[int]] = {}
    for attr in attributions:
        owners.setdefault(attr["media"], set()).add(attr["question_number"])
    for attr in attributions:
        if len(owners[attr["media"]]) > 1:
            attr["confidence"] = "low"

    # Orphan images: media that never appeared in any question region.
    attributed_media = {a["media"] for a in attributions}
    for record in paragraphs:
        for target in record["images"]:
            if (
                media_kinds is not None
                and media_kinds.get(target, {}).get("kind") == "fragment"
                and target not in attributed_media
            ):
                # 区域外的碎片同样保留溯源（bucket=fragment，无题号）。
                attributed_media.add(target)
                attributions.append({
                    "media": target,
                    "question_number": None,
                    "bucket": "fragment",
                    "paragraph_index": record["index"],
                    "confidence": "low",
                    "kind_evidence": media_kinds[target].get("evidence", {}),
                })
                continue
            if (
                _is_prompt_media(target, ole_bindings, media_kinds)
                and target not in attributed_media
            ):
                attributions.append({
                    "media": target,
                    "question_number": None,
                    "bucket": "orphan",
                    "paragraph_index": record["index"],
                    "confidence": "low",
                })
                attributed_media.add(target)

    # Sort by paragraph index for stable output.
    attributions.sort(key=lambda a: (a["paragraph_index"], a["media"]))
    return attributions


def attribute_images_with_status(
    paragraphs: list[dict],
    ole_bindings: dict[str, dict] | None = None,
    media_kinds: dict[str, dict] | None = None,
) -> tuple[list[dict], str, dict | None]:
    """Run strict image attribution without aborting unrelated extraction.

    A state-machine error invalidates the *entire* attribution result: returning
    a partial prefix would make later images appear trustworthy when their
    question join is not. Media extraction and rendered pages remain useful for
    text/formula transcription, so callers receive an empty attribution list
    plus a structured failure instead of an exception.
    """

    try:
        return attribute_images(paragraphs, ole_bindings, media_kinds), "complete", None
    except ValueError as exc:
        return (
            [],
            "failed",
            {
                "code": "question_number_state_lost",
                "detail": str(exc),
            },
        )


def find_soffice(soffice_arg: str | None) -> str:
    """Find soffice executable, raising clear error if missing."""
    executable = soffice_arg or shutil.which("soffice")
    if executable is None:
        raise ValueError(
            "soffice (LibreOffice) is required for DOC→DOCX normalization "
            "and DOCX→PDF rendering. Pass --soffice or add it to PATH."
        )
    return executable


def normalize_doc(source: Path, temporary: Path, soffice: str | None) -> Path:
    """Convert DOC to DOCX via soffice; for DOCX, just copy."""
    if source.suffix.lower() == ".docx":
        target = temporary / "normalized.docx"
        shutil.copy2(source, target)
        return target

    if soffice is None:
        raise ValueError("DOC input requires soffice for normalization")
    profile = temporary / "soffice-profile"
    profile.mkdir()
    conversion_dir = temporary / "converted"
    conversion_dir.mkdir()
    subprocess.run(
        [
            soffice,
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(conversion_dir),
            str(source),
        ],
        check=True,
    )
    candidates = sorted(conversion_dir.glob("*.docx"))
    if len(candidates) != 1:
        raise ValueError(f"soffice produced {len(candidates)} DOCX files; expected one")
    target = temporary / "normalized.docx"
    candidates[0].replace(target)
    return target


def render_pdf(docx_path: Path, output_dir: Path, soffice: str, dpi: int) -> tuple[Path, list[dict]]:
    """Convert DOCX to PDF via soffice, then render PDF pages as PNG.

    Returns (pdf_path, page_records).
    """
    # Step 1: DOCX → PDF
    profile_dir = output_dir.parent / ".soffice-pdf-profile"
    profile_dir.mkdir(exist_ok=True)
    pdf_dir = output_dir.parent / ".pdf-temp"
    pdf_dir.mkdir(exist_ok=True)

    subprocess.run(
        [
            soffice,
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(pdf_dir),
            str(docx_path),
        ],
        check=True,
    )
    pdf_candidates = sorted(pdf_dir.glob("*.pdf"))
    if len(pdf_candidates) != 1:
        raise ValueError(f"soffice produced {len(pdf_candidates)} PDF files; expected one")
    pdf_path = output_dir / "rendered.pdf"
    pdf_candidates[0].replace(pdf_path)
    shutil.rmtree(pdf_dir, ignore_errors=True)

    # Step 2: PDF → PNG pages via pdftoppm
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        raise ValueError("pdftoppm is required for PDF page rendering but was not found")

    pages_dir = output_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".pdf-pages-", dir=output_dir.parent) as tmp:
        subprocess.run(
            [
                pdftoppm,
                "-png",
                "-r",
                str(dpi),
                str(pdf_path),
                str(Path(tmp) / "page"),
            ],
            check=True,
        )

        def page_number(p: Path) -> int:
            m = re.search(r"-(\d+)\.png$", p.name)
            return int(m.group(1)) if m else 0

        rendered = sorted(Path(tmp).glob("page-*.png"), key=page_number)
        if not rendered:
            raise ValueError("pdftoppm produced no pages from PDF")

        page_records = []
        for index, source_page in enumerate(rendered, start=1):
            target = pages_dir / f"{index:03d}.png"
            source_page.replace(target)
            with Image.open(target) as img:
                w, h = img.size
            page_records.append({
                "path": f"pages/{target.name}",
                "sha256": sha256(target),
                "width_px": w,
                "height_px": h,
            })

    return pdf_path, page_records


def read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError as exc:
        raise ValueError(f"normalized DOCX is missing {name}") from exc


def extract(source: Path, output_dir: Path, soffice_arg: str | None, dpi: int, no_pdf: bool) -> dict:
    source = source.resolve()
    output_dir = output_dir.resolve()
    if not source.is_file():
        raise ValueError(f"Word source not found: {source}")
    if source.suffix.lower() not in {".doc", ".docx"}:
        raise ValueError(f"input must be DOC or DOCX: {source}")
    if output_dir.exists():
        raise ValueError(f"output directory already exists; refusing overwrite: {output_dir}")

    needs_soffice = source.suffix.lower() == ".doc" or not no_pdf
    soffice = find_soffice(soffice_arg) if needs_soffice else None

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}-", dir=output_dir.parent) as name:
        temporary = Path(name)
        normalized = normalize_doc(source, temporary, soffice)
        with zipfile.ZipFile(normalized) as archive:
            document_xml = read_zip_member(archive, "word/document.xml")
            rels_xml = read_zip_member(archive, "word/_rels/document.xml.rels")
            media_names = sorted(
                name for name in archive.namelist() if name.startswith("word/media/")
            )

            output_dir.mkdir()
            try:
                source_copy = output_dir / f"source{source.suffix.lower()}"
                shutil.copy2(source, source_copy)
                normalized_copy = output_dir / "normalized.docx"
                shutil.copy2(normalized, normalized_copy)
                ooxml_dir = output_dir / "ooxml"
                ooxml_dir.mkdir()
                (ooxml_dir / "document.xml").write_bytes(document_xml)
                (ooxml_dir / "document.xml.rels").write_bytes(rels_xml)
                media_dir = output_dir / "media"
                media_dir.mkdir()
                relationships = relationship_map(rels_xml)
                formula_bindings = ole_formula_bindings(
                    document_xml, relationships
                )
                media_records = []
                for archive_name in media_names:
                    target = media_dir / Path(archive_name).name
                    target.write_bytes(archive.read(archive_name))
                    width = height = None
                    try:
                        with Image.open(target) as image:
                            image.verify()
                        with Image.open(target) as image:
                            width, height = image.size
                    except OSError:
                        pass
                    media_path = f"media/{target.name}"
                    suffix = target.suffix.lower()
                    ole_binding = None
                    if suffix in {".emf", ".wmf"} or media_path in formula_bindings:
                        ole_binding = formula_bindings.get(
                            media_path, {"embedded": False}
                        )
                    record = {
                        "path": media_path,
                        "sha256": sha256(target),
                        "width_px": width,
                        "height_px": height,
                    }
                    if ole_binding is not None:
                        record["ole_binding"] = ole_binding
                        record["emf_class"] = (
                            "formula"
                            if ole_binding["embedded"]
                            else "diagram"
                        )
                    media_records.append(record)

                # Paragraph stream + image attribution (from OOXML structure).
                paragraphs = paragraph_records(document_xml, relationships)
                media_classification = classify_media(
                    paragraphs, formula_bindings, media_records
                )
                (
                    image_attributions,
                    image_attribution_status,
                    image_attribution_error,
                ) = attribute_images_with_status(
                    paragraphs, formula_bindings, media_classification
                )

                # Render PDF for formula transcription (unless --no-pdf)
                page_records: list[dict] = []
                pdf_info = None
                if not no_pdf:
                    pdf_path, page_records = render_pdf(normalized_copy, output_dir, soffice, dpi)
                    pdf_info = {
                        "path": "rendered.pdf",
                        "sha256": sha256(pdf_path),
                        "dpi": dpi,
                    }

                manifest = {
                    "schema": "math_word_source_extract/v1",
                    "source": {
                        "path": source_copy.name,
                        "format": source.suffix.lower().lstrip("."),
                        "sha256": sha256(source_copy),
                    },
                    "normalized_docx": {
                        "path": normalized_copy.name,
                        "sha256": sha256(normalized_copy),
                    },
                    "media": media_records,
                    "paragraphs": paragraphs,
                    "media_classification": media_classification,
                    "image_attribution_status": image_attribution_status,
                    "image_attribution": image_attributions,
                }
                if image_attribution_error is not None:
                    manifest["image_attribution_error"] = (
                        image_attribution_error
                    )
                if pdf_info:
                    manifest["rendered_pdf"] = pdf_info
                    manifest["rendered_pages"] = page_records
                (output_dir / "word-source.yaml").write_text(
                    yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=1000),
                    encoding="utf-8",
                )
            except BaseException:
                shutil.rmtree(output_dir, ignore_errors=True)
                raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--soffice", help="path to soffice (LibreOffice) executable")
    parser.add_argument("--dpi", type=int, default=180, help="PDF rendering DPI (default: 180)")
    parser.add_argument("--no-pdf", action="store_true",
                        help="skip PDF rendering (formulas will need manual transcription)")
    args = parser.parse_args()
    try:
        manifest = extract(args.source, args.output_dir, args.soffice, args.dpi, args.no_pdf)
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    pages = len(manifest.get("rendered_pages", []))
    pdf_note = f" pdf_pages={pages}" if pages else " (no PDF)"
    paragraphs_n = len(manifest.get("paragraphs", []))
    attributions = manifest.get("image_attribution", [])
    attribution_status = manifest.get("image_attribution_status", "complete")
    high = sum(1 for a in attributions if a.get("confidence") == "high")
    med = sum(1 for a in attributions if a.get("confidence") == "medium")
    low = sum(1 for a in attributions if a.get("confidence") == "low")
    print(
        f"WORD SOURCE EXTRACTED: media={len(manifest['media'])}"
        f" paragraphs={paragraphs_n}"
        f" attribution_status={attribution_status}"
        f" attributions={len(attributions)} (high={high} medium={med} low={low})"
        f"{pdf_note} output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
