from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from markitdown import MarkItDown
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Attachment


@dataclass(slots=True)
class ConvertedAttachment:
    attachment_id: int
    markdown_path: str | None
    markdown_content: str
    image_assets: list[dict[str, str | int]]
    warnings: list[str]
    status: str
    converter_version: str


class MarkItDownAttachmentConverter:
    converter_version = "markitdown-0.1"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.converter = MarkItDown(enable_plugins=False)

    def convert(self, session: Session, attachment: Attachment) -> ConvertedAttachment:
        warnings: list[str] = []
        if not attachment.storage_path:
            return self._finish(session, attachment, "failed", "附件本地内容不存在")
        source = Path(attachment.storage_path)
        if not source.is_file():
            return self._finish(session, attachment, "failed", "附件文件不存在")
        if source.stat().st_size > self.settings.max_attachment_bytes:
            return self._finish(session, attachment, "too_large", "附件超过单文件大小限制")

        try:
            result = self.converter.convert(source)
            markdown = result.markdown or ""
        except Exception as exc:  # MarkItDown plugins have heterogeneous parser errors.
            return self._finish(
                session, attachment, "failed", f"MarkItDown 转换失败: {type(exc).__name__}"
            )

        if not markdown.strip():
            warnings.append("MarkItDown 未提取到有效 Markdown 文本")
        image_assets = self._collect_image_assets(source, attachment.mime_type, markdown, warnings)
        target_dir = (
            self.settings.conversions_dir
            / str(attachment.message.owner_user_id)
            / str(attachment.message_id)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{attachment.id}.md"
        target.write_text(markdown, encoding="utf-8")

        attachment.markdown_path = str(target)
        attachment.image_assets = image_assets
        attachment.conversion_warnings = warnings
        attachment.converter_version = self.converter_version
        attachment.conversion_status = "converted"
        session.flush()
        return ConvertedAttachment(
            attachment_id=attachment.id,
            markdown_path=str(target),
            markdown_content=markdown,
            image_assets=image_assets,
            warnings=warnings,
            status="converted",
            converter_version=self.converter_version,
        )

    def _collect_image_assets(
        self,
        source: Path,
        mime_type: str,
        markdown: str,
        warnings: list[str],
    ) -> list[dict[str, str | int]]:
        assets: list[dict[str, str | int]] = []
        if mime_type.startswith("image/"):
            assets.append({"path": str(source), "mime_type": mime_type, "image_index": 0})
        for index, reference in enumerate(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)):
            candidate = Path(reference)
            if not candidate.is_absolute():
                candidate = source.parent / candidate
            if candidate.is_file():
                assets.append(
                    {"path": str(candidate), "mime_type": "image/*", "image_index": index}
                )
            else:
                warnings.append(f"Markdown 图片资源不存在: {reference}")
        return assets

    def _finish(self, session: Session, attachment: Attachment, status: str, warning: str):
        attachment.conversion_status = status
        attachment.conversion_warnings = [warning]
        attachment.converter_version = self.converter_version
        session.flush()
        return ConvertedAttachment(
            attachment_id=attachment.id,
            markdown_path=None,
            markdown_content="",
            image_assets=[],
            warnings=[warning],
            status=status,
            converter_version=self.converter_version,
        )
