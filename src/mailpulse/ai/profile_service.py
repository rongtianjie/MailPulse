from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import AIProviderProfile, ModelBinding
from ..security import decrypt_secret
from .providers import OpenAICompatibleProvider
from .types import ModelCapabilities, ModelProfile, ModelRuntimePolicy


@dataclass(slots=True)
class ResolvedProviders:
    primary: OpenAICompatibleProvider | None
    primary_image_input: bool
    vision: OpenAICompatibleProvider | None


class AIProfileService:
    def __init__(self, session: Session, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    def resolve_for(self, user_id: int, mailbox_id: int) -> ResolvedProviders:
        primary = self._resolve_profile("primary", user_id, mailbox_id)
        vision = self._resolve_profile("vision", user_id, mailbox_id)
        return ResolvedProviders(
            primary=self._provider(primary) if primary else None,
            primary_image_input=bool(primary and primary.capabilities.get("image_input")),
            vision=self._provider(vision) if vision else None,
        )

    def _resolve_profile(
        self, role: str, user_id: int, mailbox_id: int
    ) -> AIProviderProfile | None:
        bindings = list(
            self.session.scalars(
                select(ModelBinding)
                .where(ModelBinding.role == role)
                .order_by(ModelBinding.id.desc())
            )
        )
        candidates: list[tuple[int, AIProviderProfile]] = []
        for binding in bindings:
            if binding.mailbox_id is not None and binding.mailbox_id != mailbox_id:
                continue
            if binding.user_id is not None and binding.user_id != user_id:
                continue
            score = 0
            if binding.mailbox_id == mailbox_id:
                score = 3
            elif binding.user_id == user_id:
                score = 2
            elif binding.mailbox_id is None and binding.user_id is None:
                score = 1
            if score:
                profile = self.session.get(AIProviderProfile, binding.provider_profile_id)
                if profile and profile.is_enabled and profile.owner_user_id in {None, user_id}:
                    candidates.append((score, profile))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _provider(self, profile: AIProviderProfile) -> OpenAICompatibleProvider:
        if not self.settings.external_ai_allowed and not _is_local_url(profile.base_url):
            raise PermissionError("当前策略禁止向外部 AI 服务发送邮件内容")
        api_key = (
            decrypt_secret(profile.api_key_encrypted, self.settings)
            if profile.api_key_encrypted
            else None
        )
        capabilities = profile.capabilities or {}
        return OpenAICompatibleProvider(
            ModelProfile(
                name=profile.name,
                base_url=profile.base_url,
                api_key=api_key,
                model_name=profile.model_name,
                capabilities=ModelCapabilities(
                    text_input=bool(capabilities.get("text_input", True)),
                    image_input=bool(capabilities.get("image_input", False)),
                    structured_output=bool(capabilities.get("structured_output", False)),
                    strict_json_schema=bool(capabilities.get("strict_json_schema", False)),
                ),
                policy=_runtime_policy(profile.policy),
            )
        )

    def provider_for_profile(self, profile: AIProviderProfile) -> OpenAICompatibleProvider:
        """Build a configured provider for an administrator connection test."""
        return self._provider(profile)


def _is_local_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _runtime_policy(value: dict | None) -> ModelRuntimePolicy:
    value = value or {}
    return ModelRuntimePolicy(
        max_input_chars=_positive_int(value.get("max_input_chars")),
        max_output_tokens=_positive_int(value.get("max_output_tokens")),
        timeout_seconds=_positive_float(value.get("timeout_seconds")),
        max_retries=_bounded_int(value.get("max_retries"), 0, 5),
        max_images=_positive_int(value.get("max_images")),
        max_image_bytes=_positive_int(value.get("max_image_bytes")),
    )


def _positive_int(value) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _positive_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _bounded_int(value, minimum: int, maximum: int) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if minimum <= result <= maximum else None
