"""试卷目录：元信息确定性解析与目录聚合查询。

复刻老版 Review UI「查卷」体验（筛选 / 检索 / 进度）在数据库侧的数据来源：
- 年份、区、考试类型没有独立 schema 字段，从 paper_id 与 title 做确定性解析；
  解析不出就是 None（未知），不猜默认年级或考试类型（设计约定：宁缺勿滥）。
- 联合命题卷（如 2012-BAOSHAN-JIADING-ERMO）保留全部区，不截取第一个。
- 统计全部基于当前修订：pending / approved / rejected / stale 互斥；
  blocked（未决阻断疑点或结构检查未过）与 published_current 为重叠维度。
  旧修订批准、新修订未审核 → stale，不再计入 approved。
- 按命中卷读取当前状态，一次 SQL，不传输全库历史，不逐题解析修订正文。
"""

from __future__ import annotations

import re
from question_bank import db as db_mod
from typing import Any

# 上海区名：paper_id 拼音 → 展示名。ZHABEI（闸北）2015 并入静安，历史卷仍按原区名。
DISTRICT_TOKENS = {
    "HUANGPU": "黄浦",
    "XUHUI": "徐汇",
    "CHANGNING": "长宁",
    "JINGAN": "静安",
    "PUTUO": "普陀",
    "HONGKOU": "虹口",
    "YANGPU": "杨浦",
    "MINHANG": "闵行",
    "BAOSHAN": "宝山",
    "JIADING": "嘉定",
    "PUDONG": "浦东",
    "PUDONGXIN": "浦东",
    "JINSHAN": "金山",
    "SONGJIANG": "松江",
    "QINGPU": "青浦",
    "FENGXIAN": "奉贤",
    "CHONGMING": "崇明",
    "ZHABEI": "闸北",
    "SHANGHAI": "上海市",
}

EXAM_TYPE_TOKENS = {
    "YIMO": "一模",
    "ERMO": "二模",
    "SANMO": "三模",
    "QIZHONG": "期中",
    "QIMO": "期末",
    "ZHONGKAO": "中考",
    "LIANKAO": "联考",
    "YUEKAO": "月考",
}

_YEAR_TOKEN = re.compile(r"^(?:19|20)\d{2}$")
_YEAR_IN_TEXT = re.compile(r"(?:19|20)\d{2}")
_GRADE_PATTERNS = [
    (re.compile(r"初三|九年级"), "初三"),
    (re.compile(r"初二|八年级"), "初二"),
    (re.compile(r"初一|七年级"), "初一"),
    (re.compile(r"高三"), "高三"),
    (re.compile(r"高二"), "高二"),
    (re.compile(r"高一"), "高一"),
    (re.compile(r"六年级|预初"), "六年级"),
]

_CHINESE_DISTRICTS = sorted(
    {name for name in DISTRICT_TOKENS.values() if name != "上海市"},
    key=len,
    reverse=True,
)
_EXAM_TYPE_NAMES = sorted(set(EXAM_TYPE_TOKENS.values()), key=len, reverse=True)


def parse_paper_meta(
    paper_id: str, title: str = "", *, grade: str = ""
) -> dict[str, Any]:
    """确定性解析试卷元信息。

    优先级：paper_id 拼音 token → title 中文关键词 → None。
    返回 {year, districts, district_label, exam_type, grade}；解析不出为 None。
    """
    tokens = [token.strip().upper() for token in paper_id.split("-") if token.strip()]

    year = None
    year_token_idx = None
    for index, token in enumerate(tokens):
        if _YEAR_TOKEN.match(token):
            year = int(token)
            year_token_idx = index
            break

    exam_type = None
    exam_token_idx = None
    for index, token in enumerate(tokens):
        if token in EXAM_TYPE_TOKENS:
            exam_type = EXAM_TYPE_TOKENS[token]
            exam_token_idx = index

    districts: list[str] = []
    for index, token in enumerate(tokens):
        if index == year_token_idx or index == exam_token_idx:
            continue
        name = DISTRICT_TOKENS.get(token)
        if name and name not in districts:
            districts.append(name)

    # paper_id 解析不出的字段才用 title 兜底（中文关键词），仍解析不出保持 None。
    text = f"{title}"
    if year is None and title:
        match = _YEAR_IN_TEXT.search(title)
        if match:
            year = int(match.group(0))
    if not districts and title:
        for name in _CHINESE_DISTRICTS:
            if name in title and name not in districts:
                districts.append(name)
    if exam_type is None and title:
        for name in _EXAM_TYPE_NAMES:
            if name in title:
                exam_type = name
                break

    resolved_grade = grade.strip() or None
    if resolved_grade is None and title:
        for pattern, canonical in _GRADE_PATTERNS:
            if pattern.search(title):
                resolved_grade = canonical
                break

    return {
        "year": year,
        "districts": districts or None,
        "district_label": "·".join(districts) if districts else None,
        "exam_type": exam_type,
        "grade": resolved_grade,
    }


# ----------------------------------------------------------------------
# 批量状态查询（目录统计与单卷题目目录共用）
# ----------------------------------------------------------------------


def _state_rows(conn: db_mod.Connection, paper_ids: list[str]) -> list[dict[str, Any]]:
    """One round trip, current heads only, restricted to selected papers.

    Lateral lookups use existing item/revision indexes and never transfer review
    history to Python. EXISTS preserves stale semantics without fetching history.
    """
    if not paper_ids:
        return []
    return conn.execute(
        """
        SELECT i.item_id, i.paper_id, i.question_number,
               i.current_revision AS revision,
               CASE WHEN r.decision IN ('approved', 'rejected') THEN r.decision
                    WHEN EXISTS (SELECT 1 FROM reviews h WHERE h.item_id=i.item_id)
                    THEN 'stale' ELSE 'pending' END AS review_status,
               x.n AS open_blocking_issues, c.passed AS check_passed,
               (x.n > 0 OR COALESCE(c.passed = 0, FALSE)) AS blocked,
               CASE WHEN p.revision=i.current_revision THEN p.revision END AS published_revision
        FROM items i
        LEFT JOIN LATERAL (
            SELECT decision FROM reviews WHERE item_id=i.item_id
            AND revision=i.current_revision ORDER BY review_id DESC LIMIT 1
        ) r ON TRUE
        LEFT JOIN LATERAL (
            SELECT passed FROM checks WHERE item_id=i.item_id
            AND revision=i.current_revision ORDER BY check_id DESC LIMIT 1
        ) c ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS n FROM issues WHERE item_id=i.item_id
            AND revision=i.current_revision AND state='open' AND blocking=1
        ) x ON TRUE
        LEFT JOIN publication_heads h ON h.item_id=i.item_id
        LEFT JOIN publications p ON p.publication_id=h.publication_id
        WHERE i.paper_id=ANY(%s) AND i.current_revision IS NOT NULL
        ORDER BY i.paper_id, i.question_number
        """, (paper_ids,),
    ).fetchall()


def _paper_states(conn: db_mod.Connection, paper_ids: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for row in _state_rows(conn, paper_ids):
        if row["check_passed"] is not None:
            row["check_passed"] = bool(row["check_passed"])
        result.setdefault(row["paper_id"], {})[row["item_id"]] = row
    return result


def _stats(states: dict[str, dict[str, Any]]) -> dict[str, int]:
    stats = {
        "total_received": 0,
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "stale": 0,
        "blocked": 0,
        "published_current": 0,
    }
    for state in states.values():
        stats["total_received"] += 1
        stats[state["review_status"]] += 1
        if state["blocked"]:
            stats["blocked"] += 1
        if state["published_revision"] is not None:
            stats["published_current"] += 1
    return stats


def paper_catalog(
    conn: db_mod.Connection,
    *,
    q: str | None = None,
    year: int | None = None,
    district: str | None = None,
    exam_type: str | None = None,
    grade: str | None = None,
) -> dict[str, Any]:
    """筛选后的试卷目录 + facets。返回 {papers, facets}。

    paper 数量级为几百：解析与筛选在 Python 侧完成，状态统计用批量 SQL。
    """
    paper_rows = conn.execute(
        "SELECT * FROM papers ORDER BY paper_id"
    ).fetchall()
    entries: list[dict[str, Any]] = []
    facet_years: dict[int, int] = {}
    facet_districts: dict[str, int] = {}
    facet_exam_types: dict[str, int] = {}
    facet_grades: dict[str, int] = {}

    for row in paper_rows:
        meta = parse_paper_meta(
            row["paper_id"], row["title"], grade=row["grade"]
        )
        entry = {
            "paper_id": row["paper_id"],
            "title": row["title"],
            "grade": row["grade"],
            "subject": row["subject"],
            "source_archive": row["source_archive"],
            "expected_state": row["expected_state"],
            "meta": meta,
        }
        # facets 统计全量（不受当前筛选影响），与老版 /api/banks/facets 语义一致。
        if meta["year"] is not None:
            facet_years[meta["year"]] = facet_years.get(meta["year"], 0) + 1
        for name in meta["districts"] or []:
            facet_districts[name] = facet_districts.get(name, 0) + 1
        if meta["exam_type"] is not None:
            facet_exam_types[meta["exam_type"]] = (
                facet_exam_types.get(meta["exam_type"], 0) + 1
            )
        if meta["grade"] is not None:
            facet_grades[meta["grade"]] = facet_grades.get(meta["grade"], 0) + 1

        if year is not None and meta["year"] != year:
            continue
        if district and district not in (meta["districts"] or []):
            continue
        if exam_type and meta["exam_type"] != exam_type:
            continue
        if grade and meta["grade"] != grade:
            continue
        if q:
            haystack = " ".join(
                str(part) for part in (
                    row["paper_id"], row["title"], meta["year"],
                    meta["district_label"], meta["exam_type"], meta["grade"],
                ) if part
            ).lower()
            if q.strip().lower() not in haystack:
                continue
        entries.append(entry)

    # Filter cheap metadata first; facets still describe the entire catalog.
    paper_ids = [entry["paper_id"] for entry in entries]
    states = _paper_states(conn, paper_ids)
    expected_rows = conn.execute(
        "SELECT paper_id, COUNT(*) AS expected,"
        " SUM(CASE WHEN item_id IS NULL THEN 1 ELSE 0 END) AS missing"
        " FROM paper_items WHERE paper_id=ANY(%s) GROUP BY paper_id",
        (paper_ids,),
    ).fetchall() if paper_ids else []
    expected = {row["paper_id"]: row for row in expected_rows}
    for entry in entries:
        paper_states = states.get(entry["paper_id"], {})
        counts = expected.get(entry["paper_id"], {})
        entry.update(stats=_stats(paper_states),
                     expected_count=counts.get("expected"),
                     missing_count=counts.get("missing"),
                     first_item_id=next(iter(paper_states), None))

    return {
        "papers": entries,
        "facets": {
            "years": sorted(facet_years, reverse=True),
            "districts": sorted(facet_districts),
            "exam_types": sorted(facet_exam_types),
            "grades": sorted(facet_grades),
        },
    }


def paper_item_directory(
    conn: db_mod.Connection, paper_id: str
) -> list[dict[str, Any]]:
    """单卷题目目录（轻量，不含正文）：供切卷与连续审核使用。"""
    directory = _state_rows(conn, [paper_id])
    for row in directory:
        row.pop("paper_id")
        if row["check_passed"] is not None:
            row["check_passed"] = bool(row["check_passed"])
    return directory


def paper_stats(conn: db_mod.Connection, paper_id: str) -> dict[str, int]:
    """单卷当前修订统计（review-all 回执复用）。"""
    directory = paper_item_directory(conn, paper_id)
    return _stats({entry["item_id"]: entry for entry in directory})
