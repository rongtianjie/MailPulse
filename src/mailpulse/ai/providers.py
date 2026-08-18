from __future__ import annotations

import time
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
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": self._content(request.content_parts)})
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": request.max_output_tokens,
        }
        if request.response_schema and self.profile.capabilities.structured_output:
            if self.profile.capabilities.strict_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": _schema_name(request.role),
                        "strict": True,
                        "schema": request.response_schema,
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        response = self._post_with_retries(payload, request)
        if response.status_code == 400 and "response_format" in payload:
            if payload["response_format"].get("type") == "json_schema":
                payload["response_format"] = {"type": "json_object"}
            else:
                payload.pop("response_format")
            response = self._post_with_retries(payload, request)
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"].get("content", "")
        return GenerationResult(
            text=text,
            parsed_json=parse_json_text(text),
            model_name=model_name,
            usage=data.get("usage", {}),
        )

    def _post_with_retries(self, payload: dict[str, object], request: GenerationRequest):
        retryable_statuses = {408, 425, 429}
        last_error: Exception | None = None
        for attempt in range(request.retries + 1):
            try:
                response = httpx.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=request.timeout,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= request.retries:
                    raise
                time.sleep(self._retry_delay(attempt, None))
                continue
            if response.status_code not in retryable_statuses and response.status_code < 500:
                return response
            if attempt >= request.retries:
                return response
            time.sleep(self._retry_delay(attempt, response))
        if last_error is not None:
            raise last_error
        raise RuntimeError("AI 请求未返回响应")

    @staticmethod
    def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        try:
            if retry_after is not None:
                return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
        return min(0.25 * (2**attempt), 4.0)

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


def _schema_name(role: str) -> str:
    value = "".join(char if char.isalnum() else "_" for char in role).strip("_")
    return value or "mailpulse_response"
