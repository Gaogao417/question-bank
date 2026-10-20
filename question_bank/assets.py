"""不可变媒体资产存储。

资产按内容 hash 寻址（assets/<前2字符>/<hash>.<ext>），先写临时文件、
验证可解码，再原子落位；数据库事务只登记与引用已持久化的字节。
失败可能留下未引用文件（延迟清理），禁止产生引用缺失媒体的已提交修订。

图片（PIL 解码、裁框/旋转渲染）与录音（讲解音频，登记/回放，不做图片变换）
共用 hash 寻址与原子落盘，但校验与读取路径分开：图片接口拒绝音频，音频
接口不参与图片绑定校验。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from question_bank.canonical import sha256_bytes
from question_bank.errors import QbError, error
from question_bank.models import ImageBinding

ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

# 录音（讲解口述）：与老版 explanations_ai 一致的容器格式；字节为空/超限直接拒绝。
AUDIO_MIME = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}
AUDIO_MAX_BYTES = 50 * 1024 * 1024
AUDIO_MAX_SECONDS = 600.0

# 各容器格式文件头（magic bytes）；防 content-type 伪装。
_AUDIO_MAGIC: list[tuple[str, tuple[bytes, ...]]] = [
    (".wav", (b"RIFF",)),
    (".webm", (b"\x1a\x45\xdf\xa3",)),
    (".ogg", (b"OggS",)),
    (".mp4", (b"ftyp",)),   # 出现在 4..8 偏移
    (".m4a", (b"ftyp",)),
]


class AssetStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.dir = self.root / "assets"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------

    def register_bytes(
        self, raw: bytes, *, source_kind: str = "imported"
    ) -> dict:
        """校验并持久化媒体字节，返回资产描述 dict。

        幂等：同 hash 字节重复登记返回同一资产。
        """
        if not raw:
            raise QbError([error("ASSET_EMPTY", "媒体字节为空", retryable=False)])
        digest = sha256_bytes(raw)
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
            fmt = (image.format or "").upper()
        mime_by_format = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
        mime = mime_by_format.get(fmt)
        if mime is None:
            raise QbError([
                error(
                    "ASSET_FORMAT_UNSUPPORTED",
                    f"不支持的图片格式 {fmt or '未知'}，仅支持 PNG/JPEG/WebP",
                    retryable=False,
                )
            ])
        suffix = ALLOWED_MIME[mime]
        rel = f"{digest[7:9]}/{digest[7:]}{suffix}"
        target = self.dir / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "wb", dir=target.parent, delete=False, suffix=".tmp"
            )
            try:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                os.replace(handle.name, target)
            finally:
                handle.close()
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
        return {
            "asset_id": digest,
            "sha256": digest,
            "path": f"assets/{rel}".replace("\\", "/"),
            "mime": mime,
            "size_bytes": len(raw),
            "width": width,
            "height": height,
            "source_kind": source_kind,
        }

    def asset_path(self, asset_id: str) -> Path:
        return self.root / self.asset_rel(asset_id)

    # ------------------------------------------------------------------
    # 录音（讲解音频）：与图片共用 hash 寻址与原子落盘，校验路径独立。
    # ------------------------------------------------------------------

    def register_audio(self, raw: bytes, content_type: str) -> dict:
        """校验并持久化录音字节（讲解口述）；返回资产描述 dict。

        幂等：同 hash 字节重复登记返回同一资产。音频不做 PIL 解码；
        magic bytes 防容器伪装，时长上限经 ffprobe 检查（无 ffprobe 时放行，
        ASR 阶段的 ffmpeg 会给出明确失败）。width/height 为 NULL。
        """
        if not raw:
            raise QbError([error("AUDIO_EMPTY", "录音内容为空", retryable=False)])
        if len(raw) > AUDIO_MAX_BYTES:
            raise QbError([
                error("AUDIO_TOO_LARGE", f"录音超过 {AUDIO_MAX_BYTES // (1024*1024)}MB 上限",
                      retryable=False)
            ])
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        suffix = AUDIO_MIME.get(normalized_type)
        if suffix is None:
            raise QbError([
                error(
                    "AUDIO_FORMAT_UNSUPPORTED",
                    f"不支持的录音格式：{content_type or '未知'}"
                    f"（可用：{'、'.join(sorted(AUDIO_MIME))}）",
                    retryable=False,
                )
            ])
        self._assert_audio_magic(raw, suffix)
        probe = self._probe_audio(raw, suffix)
        # ffprobe 存在但探测失败 → 拒绝（损坏容器/无音轨不能进入模型转换路径）；
        # ffprobe 缺失 → 时长未知，允许登记（能力提示已如实报告 ffmpeg 状态）。
        if probe[0] == "failed":
            raise QbError([
                error("AUDIO_PROBE_FAILED",
                      f"录音探测失败（ffprobe 无法读取有效音轨）：{probe[2]}",
                      retryable=False)
            ])
        seconds = probe[1]
        if seconds is not None and seconds > AUDIO_MAX_SECONDS:
            raise QbError([
                error(
                    "AUDIO_TOO_LONG",
                    f"录音时长 {seconds:.0f}s 超过 {AUDIO_MAX_SECONDS:.0f}s 上限",
                    retryable=False,
                )
            ])
        digest = sha256_bytes(raw)
        rel = f"{digest[7:9]}/{digest[7:]}{suffix}"
        target = self.dir / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "wb", dir=target.parent, delete=False, suffix=".tmp"
            )
            try:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                os.replace(handle.name, target)
            finally:
                handle.close()
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
        return {
            "asset_id": digest,
            "sha256": digest,
            "path": f"assets/{rel}".replace("\\", "/"),
            "mime": normalized_type,
            "size_bytes": len(raw),
            "width": None,
            "height": None,
            "source_kind": "recording",
            "duration_seconds": seconds,
        }

    @staticmethod
    def _assert_audio_magic(raw: bytes, suffix: str) -> None:
        magics = [m for s, m in _AUDIO_MAGIC if s == suffix]
        if not magics:
            return
        if suffix in (".mp4", ".m4a"):
            ok = raw[4:8] == b"ftyp"
        else:
            ok = any(raw.startswith(m) for m in magics[0])
        if not ok:
            raise QbError([
                error("AUDIO_MAGIC_MISMATCH",
                      f"录音字节与声明的容器格式不符（{suffix}）",
                      retryable=False)
            ])

    @staticmethod
    def _probe_audio(raw: bytes, suffix: str) -> tuple[str, float | None, str]:
        """探测录音。返回 (状态, 时长秒|None, 说明)。

        状态：ok=有效音轨；unknown=ffprobe 缺失（时长未知）；
        failed=ffprobe 存在但读取失败（损坏容器/无音轨/超时/不可解析）。
        """
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return ("unknown", None, "ffprobe 不可用")
        with tempfile.TemporaryDirectory(prefix="qbank-audio-") as tmp:
            source = Path(tmp) / f"input{suffix}"
            source.write_bytes(raw)
            try:
                completed = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                    capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                return ("failed", None, "ffprobe 超时")
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", "replace").strip()[-200:]
                return ("failed", None, detail or "ffprobe 非零退出")
            try:
                return ("ok", float(
                    completed.stdout.decode("utf-8", "replace").strip()), "")
            except ValueError:
                return ("failed", None, "ffprobe 输出不可解析")

    def read_audio_bytes(self, mime: str, rel_path: str) -> bytes:
        """读取已登记录音字节（path 来自 assets 表；mime 校验由调用方连接提供）。"""
        if not (mime or "").startswith("audio/"):
            raise QbError([
                error("AUDIO_ASSET_EXPECTED",
                      f"资产不是录音（mime={mime}）", retryable=False)
            ])
        path = self.root / rel_path
        if not path.is_file():
            raise QbError([
                error("ASSET_FILE_MISSING",
                      f"录音记录存在但文件缺失: {rel_path}",
                      fix_hint="从备份恢复媒体目录，不要用重新录制的内容顶替",
                      retryable=False)
            ])
        return path.read_bytes()

    def asset_rel(self, asset_id: str) -> str:
        row_path = self._lookup_path(asset_id)
        if row_path is None:
            raise QbError([
                error("ASSET_NOT_FOUND", f"资产不存在: {asset_id}", retryable=False)
            ])
        path = self.root / row_path
        if not path.is_file():
            raise QbError([
                error(
                    "ASSET_FILE_MISSING",
                    f"资产记录存在但文件缺失: {asset_id}",
                    fix_hint="从备份恢复媒体目录，不要用重新生成的内容顶替",
                    retryable=False,
                )
            ])
        return row_path

    def _lookup_path(self, asset_id: str) -> str | None:
        # 由调用方（service）保证 assets 表可查；这里只做文件层兜底。
        for suffix in (".png", ".jpg", ".webp"):
            candidate = f"assets/{asset_id[7:9]}/{asset_id[7:]}{suffix}"
            if (self.root / candidate).is_file():
                return candidate
        return None

    # ------------------------------------------------------------------
    # 渲染（UI 预览与导出共用同一变换实现）
    # ------------------------------------------------------------------

    def render(
        self,
        asset_id: str,
        *,
        crop_px: list[int] | None = None,
        rotate: int = 0,
    ) -> bytes:
        """按裁框（原图像素 [l,t,r,b]）与旋转（顺时针 90 步进）渲染 PNG。"""
        return self.render_rel(self.asset_rel(asset_id), crop_px=crop_px, rotate=rotate)

    def render_rel(
        self,
        rel_path: str,
        *,
        crop_px: list[int] | None = None,
        rotate: int = 0,
    ) -> bytes:
        """渲染数据库登记路径（assets/…）指向的资产；与 render 共用变换。"""
        path = self.root / rel_path
        try:
            with Image.open(path) as image:
                image.load()
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA")
                if crop_px is not None:
                    left, top, right, bottom = crop_px
                    if right > image.width or bottom > image.height:
                        raise QbError([
                            error(
                                "CROP_OUT_OF_BOUNDS",
                                f"裁框 {crop_px} 超出原图 {image.width}x{image.height}",
                                field_path="crop_px",
                                retryable=False,
                            )
                        ])
                    image = image.crop((left, top, right, bottom))
                if rotate:
                    image = image.rotate(-rotate, expand=True)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                return buffer.getvalue()
        except QbError:
            raise
        except (OSError, UnidentifiedImageError) as exc:
            raise QbError([
                error("ASSET_UNREADABLE", f"无法读取资产 {rel_path}: {exc}",
                      retryable=False)
            ]) from exc

    def dimensions(self, asset_id: str) -> tuple[int, int]:
        path = self.root / self.asset_rel(asset_id)
        with Image.open(path) as image:
            return image.size

    def validate_binding_renderable(self, binding: ImageBinding) -> None:
        """接收/保存前验证绑定可渲染（媒体存在、裁框在界内、可解码）。"""
        self.render(
            binding.resolved_source(),
            crop_px=binding.crop_px,
            rotate=binding.rotate,
        )
