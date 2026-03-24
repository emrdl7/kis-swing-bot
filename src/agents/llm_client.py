"""LLM 클라이언트 — claude CLI 바이너리 호출 (기존 kis-auto-standalone 방식)."""
from __future__ import annotations
import logging
import os
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_TIMEOUT = 120.0


def _claude_bin() -> str:
    return str(os.getenv("CLAUDE_BIN", "claude")).strip() or "claude"


class LLMClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2000,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout

    def chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        """system + user 프롬프트로 단일 응답 반환."""
        prompt = user
        full_system = system
        return self._call(prompt, system=full_system)

    def chat_with_history(
        self,
        system: str,
        messages: list[dict],
        max_tokens: Optional[int] = None,
    ) -> str:
        """대화 이력을 단일 프롬프트로 직렬화하여 호출."""
        parts = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            parts.append(f"[{role.upper()}]\n{content}")
        prompt = "\n\n".join(parts)
        return self._call(prompt, system=system)

    def _call(self, prompt: str, system: str = "") -> str:
        cmd = [_claude_bin(), "-p", prompt, "--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                if result.returncode != 0:
                    err = (result.stderr or "").strip()
                    raise RuntimeError(f"claude_cli rc={result.returncode}: {err}")
                text = (result.stdout or "").strip()
                if not text:
                    raise RuntimeError("claude_cli_empty_response")
                return text
            except subprocess.TimeoutExpired as exc:
                last_exc = exc
                log.warning("LLM 타임아웃 (attempt %d)", attempt + 1)
                if attempt == 0:
                    time.sleep(2)
            except RuntimeError as e:
                log.error("LLM 호출 실패: %s", e)
                return ""

        log.error("LLM 최종 실패 (timeout x2)")
        return ""
