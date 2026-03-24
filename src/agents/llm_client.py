"""Anthropic Claude API 래퍼."""
from __future__ import annotations
import logging
from typing import Optional

import anthropic

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, api_key: str, model: str = "claude-opus-4-6", max_tokens: int = 2000):
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key)

    def chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        """단순 텍스트 응답."""
        mt = max_tokens or self.max_tokens
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=mt,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return msg.content[0].text if msg.content else ""
        except Exception as e:
            log.error("LLM 호출 실패: %s", e)
            return ""

    def chat_with_history(
        self,
        system: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> str:
        """대화 이력이 있는 멀티턴 호출."""
        mt = max_tokens or self.max_tokens
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=mt,
                system=system,
                messages=messages,
            )
            return msg.content[0].text if msg.content else ""
        except Exception as e:
            log.error("LLM 멀티턴 호출 실패: %s", e)
            return ""
