from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ActionItem(BaseModel):
    action: str
    owner: str | None = None
    due_at: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    verified: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_string_item(cls, value):
        if isinstance(value, str):
            return {"action": value}
        return value

    @field_validator("source_refs", mode="before")
    @classmethod
    def normalize_source_refs(cls, value):
        if not isinstance(value, list):
            return []
        return [
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in value
        ]


class SourceReference(BaseModel):
    message_id: int
    attachment_id: int | None = None
    page_number: int | None = None
    image_index: int | None = None
    quote: str | None = None


class MessageSummary(BaseModel):
    """A bounded, source-oriented summary for one input message."""

    message_id: int
    thread_key: str | None = None
    subject: str = ""
    received_at: str | None = None
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)


class MessageExtractionResponse(BaseModel):
    items: list[MessageSummary] = Field(default_factory=list)


class SummaryCoverage(BaseModel):
    input_message_count: int = 0
    summarized_message_count: int = 0
    omitted_message_ids: list[int] = Field(default_factory=list)
    truncated_message_ids: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    mode: Literal["direct", "two_stage", "degraded"] = "direct"


class StructuredSummary(BaseModel):
    category: str = "其他"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    summary: str = ""
    message_summaries: list[MessageSummary] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    attachment_status: list[str] = Field(default_factory=list)
    coverage: SummaryCoverage = Field(default_factory=SummaryCoverage)

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value):
        return {
            "低": "low",
            "普通": "normal",
            "中": "normal",
            "高": "high",
            "紧急": "urgent",
        }.get(value, value)


class VisualEvidence(BaseModel):
    message_id: int
    attachment_id: int
    page_number: int | None = None
    image_index: int | None = None
    extracted_text: str = ""
    tables: list[list[str]] = Field(default_factory=list)
    detected_objects: list[str] = Field(default_factory=list)
    key_fields: dict[str, str] = Field(default_factory=dict)
    confidence_advisory: float | None = Field(default=None, ge=0, le=1)
    evidence_status: Literal["verified", "uncertain", "failed"] = "verified"
    warnings: list[str] = Field(default_factory=list)


class VisualEvidenceResponse(BaseModel):
    evidence: list[VisualEvidence] = Field(default_factory=list)


@dataclass(slots=True)
class ModelCapabilities:
    text_input: bool = True
    image_input: bool = False
    structured_output: bool = False
    strict_json_schema: bool = False


@dataclass(slots=True)
class ModelRuntimePolicy:
    max_input_chars: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    max_images: int | None = None
    max_image_bytes: int | None = None


@dataclass(slots=True)
class ModelProfile:
    name: str
    base_url: str
    api_key: str | None
    model_name: str | None
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    policy: ModelRuntimePolicy = field(default_factory=ModelRuntimePolicy)


@dataclass(slots=True)
class TextPart:
    text: str


@dataclass(slots=True)
class MarkdownPart:
    text: str
    source_name: str


@dataclass(slots=True)
class ImagePart:
    path: Path
    mime_type: str = "image/png"
    source_name: str = ""


@dataclass(slots=True)
class EvidencePart:
    evidence: list[VisualEvidence]


ContentPart = TextPart | MarkdownPart | ImagePart | EvidencePart


@dataclass(slots=True)
class GenerationRequest:
    role: str
    content_parts: list[ContentPart]
    system_prompt: str | None = None
    response_schema: dict[str, Any] | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int = 1800
    timeout: float = 90.0
    retries: int = 0


@dataclass(slots=True)
class GenerationResult:
    text: str
    parsed_json: dict[str, Any] | None
    model_name: str
    usage: dict[str, Any] = field(default_factory=dict)


def encode_image_part(part: ImagePart) -> str:
    encoded = base64.b64encode(part.path.read_bytes()).decode("ascii")
    return f"data:{part.mime_type};base64,{encoded}"


def parse_json_text(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        # Some OpenAI-compatible local servers wrap a JSON object in a singleton array.
        value = value[0]
    return value if isinstance(value, dict) else None
