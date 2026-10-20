#!/usr/bin/env python3
"""讲解/解答 AI 辅助（数据库版 Review UI，2026-09 由老版移植适配）。

沿用老版（.codex/skills/math-topic-question-bank/scripts/explanations_ai.py，
该文件保持原样不动）的固定 DashScope OpenAI 兼容调用与提示词，上下文改为
三块正文（stem/answer/solution）+ 派生小问：

- ``transcribe_audio``: 教师口述录音 -> 转写稿（qwen3-asr-flash，
  浏览器录音经 ffmpeg 转 16 kHz 单声道 WAV）。
- ``polish_explanation_text``: 转写稿 -> 书面讲解（qwen-plus）。
- ``generate_explanation_text`` / ``generate_solution_text``: 从另一半补齐。

设计约定（docs/question-bank-management-design.md §12）：
- 固定的 ASR/润色/生成 HTTP 直调；不扩展为 agent 运行时或通用调度。
- 模块级函数便于测试 monkeypatch；失败抛 :class:`AiAssistError`（code 稳定）。
- 每次调用可经 ``meta`` 字典回收 provider/model/usage（仅实际返回时非空），
  供 ai_operations 记录；调用方不得据此声称未实际发生的计费。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_ASR_MODEL = "qwen3-asr-flash"
DEFAULT_LLM_MODEL = "qwen-plus"
PROVIDER = "dashscope"
MAX_AUDIO_BYTES = 50 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 180.0

AUDIO_SUFFIXES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}

SYSTEM_PROMPT = (
    "你是资深初中数学教师编辑，负责把教师的讲解整理成学生可读的书面讲解。"
    "数学表达式一律用 LaTeX 行内公式 $...$ 书写，三角形写 $\\triangle ABC$，相似写 $\\triangle ABC\\sim\\triangle DEF$。"
    "直接输出正文文字，不要输出 JSON、代码围栏或任何前后缀说明。"
)

POLISH_PROMPT = """请把下面这段教师口头讲解的转写稿润色成书面讲解。

要求：
1. 保留教师原始思路、讲解顺序和所有数学结论，不得新增、删改数学事实。
2. 修正口语中的重复、停顿、口误和语病，改写为通顺的书面中文。
3. 以学生动作为主线（先看什么、再找什么、然后算什么），适合初中生自主阅读。
4. 数学内容用 LaTeX 行内公式书写，分句清晰，每句以句号或分号结束。

题目（大题题干）：
{stem}

{subquestion_line}参考答案：
{answer}

已有解答（供对照，不要照抄）：
{solution}

教师讲解转写稿：
{transcript}

直接输出润色后的讲解正文。
"""

GENERATE_EXPLANATION_PROMPT = """这道题目前有解答但没有讲解。请依据题目和解答写出这一小问的讲解。

要求：
1. 讲解"思路从何而来"：识别什么模型/结构、条件如何转化、为什么选这条路，而不是复述解题步骤。
2. 以学生动作为主线，适合初中生阅读；数学内容用 LaTeX 行内公式书写。
3. 只使用题目和解答中出现的数学事实，不得引入未经验证的新结论。

题目（大题题干）：
{stem}

{subquestion_line}参考答案：
{answer}

分步解答：
{solution}

直接输出讲解正文。
"""

GENERATE_SOLUTION_PROMPT = """这道题已有讲解但没有配套解答。请依据讲解的思路写出这一小问的完整解答。

要求：
1. 解答第一行以"解："开头，随后按讲解的思路分步书写，每步一行，行末用句号。
2. 依据说明充分（判定依据、比例式来源都要写明），数学内容用 LaTeX 行内公式书写。
3. 不得出现界面用语（点击、按钮、系统等），最终结论与参考答案一致。

题目（大题题干）：
{stem}

{subquestion_line}参考答案：
{answer}

已批准的讲解：
{explanation}

直接输出完整解答正文。
"""


class AiAssistError(RuntimeError):
    """AI 辅助调用失败；code 稳定供 API 层映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def base_url() -> str:
    return os.environ.get("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL


def asr_model() -> str:
    return os.environ.get("QUESTION_BANK_ASR_MODEL") or DEFAULT_ASR_MODEL


def llm_model() -> str:
    return os.environ.get("QUESTION_BANK_LLM_MODEL") or DEFAULT_LLM_MODEL


def api_key() -> str | None:
    value = os.environ.get("DASHSCOPE_API_KEY", "")
    return value.strip() or None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def audio_suffix_for(content_type: str) -> str:
    normalized = (content_type or "").split(";")[0].strip().lower()
    suffix = AUDIO_SUFFIXES.get(normalized)
    if suffix is None:
        raise AiAssistError(
            "unsupported_media_type",
            f"不支持的录音格式：{content_type or '未知'}（可用：{'、'.join(sorted(AUDIO_SUFFIXES))}）",
        )
    return suffix


def convert_audio_to_wav(data: bytes, suffix: str) -> bytes:
    """浏览器录音转 16 kHz 单声道 WAV；模块级函数便于测试替换。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AiAssistError("audio_convert_failed", "服务器未安装 ffmpeg，无法转换录音")
    with tempfile.TemporaryDirectory(prefix="qbank-asr-") as tmp:
        source = Path(tmp) / f"input{suffix}"
        source.write_bytes(data)
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0 or not completed.stdout:
            detail = completed.stderr.decode("utf-8", "replace").strip()[-400:]
            raise AiAssistError("audio_convert_failed", f"ffmpeg 转换录音失败：{detail or '无输出'}")
        return completed.stdout


def _post_chat(payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    key = api_key()
    if not key:
        raise AiAssistError(
            "no_api_key",
            "DASHSCOPE_API_KEY 未设置：录音转写与讲解润色不可用（音频已保存，可稍后重试）",
        )
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:400]
        raise AiAssistError("provider_failed", f"模型服务请求失败（{exc.response.status_code}）：{detail}") from exc
    except httpx.HTTPError as exc:
        raise AiAssistError("provider_failed", f"模型服务请求失败：{exc}") from exc


def _capture_meta(meta: dict[str, Any] | None, model: str, body: dict[str, Any]) -> None:
    if meta is None:
        return
    meta["provider"] = PROVIDER
    meta["model"] = model
    usage = body.get("usage") if isinstance(body, dict) else None
    meta["usage"] = usage if isinstance(usage, dict) else None


def _chat_text(
    messages: list[dict[str, Any]], model: str,
    meta: dict[str, Any] | None = None,
) -> str:
    body = _post_chat({"model": model, "messages": messages})
    _capture_meta(meta, model, body)
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AiAssistError("provider_failed", f"模型服务返回异常：{json.dumps(body, ensure_ascii=False)[:400]}") from exc
    # content 为 null/非字符串时不得 str() 成 "None" 伪正文。
    if not isinstance(content, str) or not content.strip():
        raise AiAssistError(
            "llm_bad_output",
            f"模型返回空正文（content={content!r}）："
            f"{json.dumps(body, ensure_ascii=False)[:200]}",
        )
    return content


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise AiAssistError(
        "llm_bad_output",
        f"模型未返回合法 JSON：{text[:200]}",
    )


def _extract_body_text(text: str, field: str) -> str:
    """正文直出为主；模型若仍包了 JSON（LaTeX 反斜杠可能不合法）则尽力取字段。"""
    cleaned = re.sub(r"```[a-z]*", "", text).strip().strip("`").strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            value = _extract_json_object(cleaned).get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        except AiAssistError:
            pass
    return cleaned


def transcribe_audio(
    data: bytes, content_type: str, meta: dict[str, Any] | None = None
) -> str:
    """录音字节 -> 中文转写文本。

    qwen3-asr-flash 的 OpenAI 兼容协议：``input_audio.data`` 只接受公网 URL 或
    Data URL（base64 需带 ``data:audio/<mime>;base64,`` 前缀，无 format 字段）。
    注意该端点不接受任何 system message；中文与数字规整通过 ``asr_options`` 开启。
    """
    if len(data) > MAX_AUDIO_BYTES:
        raise AiAssistError("audio_too_large", "录音超过 50MB 上限")
    if not data:
        raise AiAssistError("audio_empty", "录音内容为空")
    suffix = audio_suffix_for(content_type)
    wav = data if suffix == ".wav" else convert_audio_to_wav(data, suffix)
    encoded = base64.b64encode(wav).decode("ascii")
    body = _post_chat(
        {
            "model": asr_model(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:audio/wav;base64,{encoded}"},
                        }
                    ],
                }
            ],
            "asr_options": {"language": "zh", "enable_itn": True},
        }
    )
    _capture_meta(meta, asr_model(), body)
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AiAssistError("asr_failed", f"转写服务返回异常：{json.dumps(body, ensure_ascii=False)[:400]}") from exc
    if not isinstance(content, str) or not content.strip():
        raise AiAssistError(
            "asr_failed",
            f"转写服务返回空正文（content={content!r}）："
            f"{json.dumps(body, ensure_ascii=False)[:200]}",
        )
    return content.strip()


def _subquestion_line(ctx: dict[str, Any]) -> str:
    label = str(ctx.get("subquestion_label") or "").strip()
    sub_stem = str(ctx.get("subquestion_stem") or "").strip()
    if not label and not sub_stem:
        return ""
    shown = label or "整题"
    if sub_stem and sub_stem != str(ctx.get("stem") or "").strip():
        return f"当前小问（{shown}）：{sub_stem}\n\n"
    return f"当前处理整题（小问 {shown}）。\n\n"


def _required_context(ctx: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if not str(ctx.get(key) or "").strip()]
    if missing:
        raise AiAssistError("context_incomplete", f"生成上下文缺少字段：{'、'.join(missing)}")


def _render(template: str, ctx: dict[str, Any]) -> str:
    values = {
        "stem": str(ctx.get("stem") or "（无题干）"),
        "subquestion_line": _subquestion_line(ctx),
        "answer": str(ctx.get("answer") or "（无参考答案）"),
        "solution": str(ctx.get("solution") or "（无分步解答）"),
        "explanation": str(ctx.get("explanation") or "（无讲解）"),
        "transcript": str(ctx.get("transcript") or "（无转写稿）"),
    }
    return template.format(**values)


def polish_explanation_text(
    ctx: dict[str, Any], meta: dict[str, Any] | None = None
) -> str:
    """录音转写稿 -> 润色后的书面讲解。"""
    _required_context(ctx, ("stem", "transcript"))
    text = _chat_text(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _render(POLISH_PROMPT, ctx)},
        ],
        llm_model(),
        meta,
    )
    result = _extract_body_text(text, "explanation")
    if not result.strip():
        raise AiAssistError("llm_bad_output", "润色结果为空")
    return result


def generate_explanation_text(
    ctx: dict[str, Any], meta: dict[str, Any] | None = None
) -> str:
    """从题干 + 解答生成该小问的讲解。"""
    _required_context(ctx, ("stem",))
    text = _chat_text(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _render(GENERATE_EXPLANATION_PROMPT, ctx)},
        ],
        llm_model(),
        meta,
    )
    result = _extract_body_text(text, "explanation")
    if not result.strip():
        raise AiAssistError("llm_bad_output", "生成的讲解为空")
    return result


def generate_solution_text(
    ctx: dict[str, Any], meta: dict[str, Any] | None = None
) -> str:
    """从讲解生成配套完整解答。"""
    _required_context(ctx, ("stem", "explanation"))
    text = _chat_text(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _render(GENERATE_SOLUTION_PROMPT, ctx)},
        ],
        llm_model(),
        meta,
    )
    result = _extract_body_text(text, "solution")
    if not result.strip():
        raise AiAssistError("llm_bad_output", "生成的解答为空")
    return result
