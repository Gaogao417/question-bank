"""规范内容模型与候选包契约（pydantic）。

三块正文：stem（题目）、answer（答案）、solution（解答）。
图片只在正文中以 ![标签](asset:key) 标记出现；key 在 images 绑定表中
解析为受管资产与裁框/旋转变换。不强制 choices[]、subquestions[]、
solution_steps[]（设计文档 §9）。
"""

from __future__ import annotations

import re
import warnings
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# “schema” 字段名遮蔽 BaseModel.schema()（pydantic v2 已弃用方法），告警无实义。
warnings.filterwarnings("ignore", message='Field name "schema"')

from question_bank.canonical import CONTENT_SCHEMA

QUESTION_TYPES = ("choice", "fillin", "short_answer", "problem")
IMAGE_ROLES = ("stem", "solution", "evidence")
EVIDENCE_ROLES = ("question", "solution")
ISSUE_STATES = ("open", "resolved", "dismissed")

# 正文图片标记：![标签](asset:key)
IMAGE_MARKER = re.compile(r"!\[([^\]]*)\]\(asset:([A-Za-z0-9_\-]{1,64})\)")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")


def marker_keys(text: str) -> list[str]:
    """按出现顺序返回正文引用的图片 key（含重复）。"""
    return [match.group(2) for match in IMAGE_MARKER.finditer(text)]


def normalize_text(value: str) -> str:
    """统一换行、去首尾空白。正文是连贯数学文本，不做其他改写。"""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


class ImageBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    asset_id: str = Field(min_length=1)
    role: Literal["stem", "solution", "evidence"]
    # 裁框作用于 source_asset（缺省为 asset_id 本身），原图像素坐标 [l,t,r,b]。
    source_asset_id: str | None = None
    crop_px: list[int] | None = None
    rotate: int = 0
    width: str | None = None
    ordinal: int = 0

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if not KEY_PATTERN.match(value):
            raise ValueError("图片 key 只能包含字母、数字、下划线、连字符")
        return value

    @field_validator("crop_px")
    @classmethod
    def _check_crop(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if len(value) != 4 or any(v < 0 for v in value):
            raise ValueError("crop_px 必须是 [left, top, right, bottom] 且非负")
        if value[2] <= value[0] or value[3] <= value[1]:
            raise ValueError("crop_px 右/下边界必须大于左/上")
        return value

    @field_validator("rotate")
    @classmethod
    def _check_rotate(cls, value: int) -> int:
        if value not in (0, 90, 180, 270):
            raise ValueError("rotate 只支持 0/90/180/270")
        return value

    def resolved_source(self) -> str:
        return self.source_asset_id or self.asset_id


class EvidencePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["question", "solution"]
    asset_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str
    type: str = Field(min_length=1, max_length=64)
    target: str = ""
    reason: str = ""
    evidence: str = ""
    blocking: bool = True
    state: Literal["open", "resolved", "dismissed"] = "open"
    resolution: dict[str, Any] | None = None

    @field_validator("issue_id")
    @classmethod
    def _check_issue_id(cls, value: str) -> str:
        if not KEY_PATTERN.match(value):
            raise ValueError("issue_id 只能包含字母、数字、下划线、连字符")
        return value


class ItemIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str = Field(min_length=1, max_length=128)
    question_number: int = Field(ge=1, le=200)
    question_type: Literal["choice", "fillin", "short_answer", "problem"]
    points: int = Field(ge=0, le=200)
    section_title: str = ""

    @field_validator("paper_id")
    @classmethod
    def _check_paper_id(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.strip() != value:
            raise ValueError("paper_id 不能包含路径分隔符或首尾空白")
        return value


class ItemContent(BaseModel):
    """一道题的完整规范内容（一个修订的权威负载）。"""

    model_config = ConfigDict(extra="forbid")

    schema: Literal["qb_question_content/v1"] = CONTENT_SCHEMA
    identity: ItemIdentity
    stem: str
    answer: str
    solution: str
    images: list[ImageBinding] = Field(default_factory=list)
    evidence: list[EvidencePage] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @field_validator("stem", "answer", "solution")
    @classmethod
    def _normalize_body(cls, value: str) -> str:
        return normalize_text(value)

    def body(self) -> dict[str, str]:
        return {"stem": self.stem, "answer": self.answer, "solution": self.solution}

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def item_id_for(paper_id: str, question_number: int) -> str:
    """内部稳定 item_id：与 (paper_id, question_number) 一一对应。

    沿用库内既有命名惯例（如 2014-BAOSHAN-YIMO-Q001）。
    """
    return f"{paper_id}-Q{question_number:03d}"


def item_key_for(paper_id: str, question_number: int) -> str:
    return f"{paper_id}#q{question_number}"


# ---------------------------------------------------------------------------
# AI 候选包 envelope（question_bank_candidate/v1）
# ---------------------------------------------------------------------------


class CandidateAssetRef(BaseModel):
    """包内媒体引用：文件相对路径，或已登记的 asset_id（sha256:…）。"""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    sha256: str | None = None  # ref 为路径时必须提供

    @field_validator("ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        if ".." in value.replace("\\", "/").split("/"):
            raise ValueError("媒体引用不能包含 ..")
        if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
            raise ValueError("媒体引用必须是包内相对路径或 asset_id")
        return value


class CandidateImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    asset: CandidateAssetRef
    role: Literal["stem", "solution", "evidence"]
    source_asset: CandidateAssetRef | None = None
    crop_px: list[int] | None = None
    rotate: int = 0
    width: str | None = None
    ordinal: int = 0


class CandidateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["question", "solution"]
    asset: CandidateAssetRef
    page_number: int = Field(ge=1)


class CandidateIssue(Issue):
    pass


class CandidatePaper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str = ""
    grade: str = ""
    subject: str = ""
    source_archive: str = ""


class CandidateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_number: int = Field(ge=1, le=200)
    question_type: Literal["choice", "fillin", "short_answer", "problem"]
    points: int = Field(ge=0, le=200)
    section_title: str = ""
    stem: str
    answer: str = ""
    solution: str = ""


class CandidateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: Literal["question_bank_candidate/v1"] = "question_bank_candidate/v1"
    submission_id: str
    base_revision: int | None = None
    paper: CandidatePaper
    item: CandidateItem
    images: list[CandidateImage] = Field(default_factory=list)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    issues: list[CandidateIssue] = Field(default_factory=list)

    @field_validator("submission_id")
    @classmethod
    def _check_submission_id(cls, value: str) -> str:
        if not OPERATION_ID_PATTERN.match(value):
            raise ValueError("submission_id 只能包含字母、数字、下划线、连字符")
        return value

    @field_validator("item", mode="before")
    @classmethod
    def _normalize_item_body(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for field in ("stem", "answer", "solution"):
                if isinstance(value.get(field), str):
                    value[field] = normalize_text(value[field])
        return value

    def item_key(self) -> str:
        return item_key_for(self.paper.paper_id, self.item.question_number)


# ---------------------------------------------------------------------------
# UI 保存 patch（PATCH /api/items/{id}/content）
# ---------------------------------------------------------------------------


class ContentPatch(BaseModel):
    """编辑补丁：任何字段缺省表示不变。

    images 提交全量替换后的绑定数组（含各自 asset_id）；图片字节上传走
    独立资产接口，这里只出现资产引用与变换。
    """

    model_config = ConfigDict(extra="forbid")

    stem: str | None = None
    answer: str | None = None
    solution: str | None = None
    question_type: Literal["choice", "fillin", "short_answer", "problem"] | None = None
    points: int | None = Field(default=None, ge=0, le=200)
    section_title: str | None = None
    images: list[ImageBinding] | None = None
    evidence: list[EvidencePage] | None = None
    # 与正文/图片修改同一事务内应用的疑点处置。
    issue_ops: list[dict[str, Any]] | None = None

    @field_validator("stem", "answer", "solution", "section_title")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_text(value)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    note: str = ""
    base_revision: int
    operation_id: str

    @field_validator("operation_id")
    @classmethod
    def _check_operation_id(cls, value: str) -> str:
        if not OPERATION_ID_PATTERN.match(value):
            raise ValueError("operation_id 只能包含字母、数字、下划线、连字符")
        return value


class ReviewAllItemRef(BaseModel):
    """全卷审核清单项：用户所见的 item_id 与修订基线。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    base_revision: int


class ReviewAllRequest(BaseModel):
    """全卷一键通过（review-all）：部分成功语义，逐题返回结果。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewAllItemRef] = Field(min_length=1)
    note: str = ""
    operation_id: str

    @field_validator("operation_id")
    @classmethod
    def _check_operation_id(cls, value: str) -> str:
        if not OPERATION_ID_PATTERN.match(value):
            raise ValueError("operation_id 只能包含字母、数字、下划线、连字符")
        return value


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    expected_previous: int | None = None  # 当前发布 publication_id；首次发布为 null
    operation_id: str

    @field_validator("operation_id")
    @classmethod
    def _check_operation_id(cls, value: str) -> str:
        if not OPERATION_ID_PATTERN.match(value):
            raise ValueError("operation_id 只能包含字母、数字、下划线、连字符")
        return value


class SaveEditRequest(ContentPatch):
    model_config = ConfigDict(extra="forbid")

    base_revision: int
    operation_id: str

    @field_validator("operation_id")
    @classmethod
    def _check_operation_id(cls, value: str) -> str:
        if not OPERATION_ID_PATTERN.match(value):
            raise ValueError("operation_id 只能包含字母、数字、下划线、连字符")
        return value


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    allow_draft: bool = False
