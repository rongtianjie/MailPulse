from __future__ import annotations

from typing import Protocol

import httpx

from .types import (
    ContentPart,
    EvidencePart,
    GenerationRequest,
    GenerationResult,
    ImagePart,
    MarkdownPart,
    ModelProfile,
    TextPart,
    encode_image_part,
    parse_json_text,
)


class ModelProvider(Protocol):
    def test_connection(self) -> None: ...

    def get_model_name(self) -> str: ...

    def generate(self, request: GenerationRequest) -> GenerationResult: ...


class OpenAICompatibleProvider:
    def __init__(self, profile: ModelProfile):
        self.profile = profile

    @property
    def _base_url(self) -> str:
        return self.profile.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key:
            headers["Authorization"] = f"Bearer {self.profile.api_key}"
        return headers

    def test_connection(self) -> None:
        response = httpx.get(
            f"{self._base_url}/models",
            headers=self._headers(),
            timeout=10,
        )
        response.raise_for_status()

    def get_model_name(self) -> str:
        if self.profile.model_name:
            return self.profile.model_name
        response = httpx.get(
            f"{self._base_url}/models",
            headers=self._headers(),
            timeout=10,
        )
        response.raise_for_status()
        models = response.json().get("data", [])
        if not models:
            raise RuntimeError("AI 服务没有可用模型")
        return str(models[0]["id"])

    def generate(self, request: GenerationRequest) -> GenerationResult:
        model_name = self.get_model_name()
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": self._content(request.content_parts)}],
            "temperature": 0.1,
            "max_tokens": request.max_output_tokens,
        }
        if request.response_schema and self.profile.capabilities.structured_output:
            payload["response_format"] = {"type": "json_object"}
        response = httpx.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=request.timeout,
        )
        if response.status_code == 400 and "response_format" in payload:
            payload.pop("response_format")
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=request.timeout,
            )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"].get("content", "")
        return GenerationResult(
            text=text,
            parsed_json=parse_json_text(text),
            model_name=model_name,
            usage=data.get("usage", {}),
        )

    @staticmethod
    def _content(parts: list[ContentPart]) -> list[dict[str, object]]:
        content: list[dict[str, object]] = []
        for part in parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
            elif isinstance(part, MarkdownPart):
                content.append({"type": "text", "text": f"### {part.source_name}\n{part.text}"})
            elif isinstance(part, EvidencePart):
                content.append(
                    {
                        "type": "text",
                        "text": f"视觉证据：{[item.model_dump() for item in part.evidence]}",
                    }
                )
            elif isinstance(part, ImagePart):
                content.append({"type": "image_url", "image_url": {"url": encode_image_part(part)}})
        return content
