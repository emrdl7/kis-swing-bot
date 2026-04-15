"""LLM 클라이언트 — claude CLI 우선, 실패 시 gemini CLI 자동 fallback."""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_TIMEOUT = 120.0
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"


def _claude_bin() -> str:
    return str(os.getenv("CLAUDE_BIN", "claude")).strip() or "claude"


def _gemini_bin() -> str:
    return str(os.getenv("GEMINI_BIN", "gemini")).strip() or "gemini"


class LLMClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2000,
        timeout: float = DEFAULT_TIMEOUT,
        gemini_model: str = DEFAULT_GEMINI_MODEL,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.gemini_model = gemini_model

    def chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        """system + user 프롬프트로 단일 응답 반환."""
        return self._call(user, system=system)

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

    # ── 호출 ────────────────────────────────────────────────────────────

    def _call(self, prompt: str, system: str = "") -> str:
        # 1차: Claude
        text = self._call_claude(prompt, system)
        if text:
            return text
        # 2차: Gemini fallback
        if shutil.which(_gemini_bin()):
            log.warning("Claude 실패 → Gemini fallback 시도 (model=%s)", self.gemini_model)
            text = self._call_gemini(prompt, system)
            if text:
                return text
            log.error("Gemini fallback 도 실패")
        else:
            log.error("Gemini 바이너리 미설치 → fallback 불가")
        return ""

    def _call_claude(self, prompt: str, system: str) -> str:
        cmd = [_claude_bin(), "-p", prompt, "--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout,
                )
                if result.returncode != 0:
                    err = (result.stderr or "").strip()
                    log.warning("claude rc=%d: %s", result.returncode, err[:200])
                    return ""  # 즉시 fallback (재시도는 gemini로)
                text = (result.stdout or "").strip()
                if not text:
                    log.warning("claude 빈 응답")
                    return ""
                return text
            except subprocess.TimeoutExpired:
                log.warning("claude 타임아웃 (attempt %d)", attempt + 1)
                if attempt == 0:
                    time.sleep(2)
            except Exception as e:
                log.warning("claude 예외: %s", e)
                return ""
        return ""

    @staticmethod
    def _strip_gemini_noise(raw: str) -> str:
        """gemini CLI 출력에서 deprecation·credential·hook 등 노이즈 라인 제거."""
        noise_starts = (
            "(node:", "(Use `node", "Loaded cached", "Hook registry",
            "DeprecationWarning", "YOLO mode", "WARNING:", "[33m", "[31m",
        )
        cleaned = []
        for line in raw.splitlines():
            stripped = line.lstrip()
            if any(stripped.startswith(p) for p in noise_starts):
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    def _call_gemini(self, prompt: str, system: str) -> str:
        # gemini CLI 는 system prompt 별도 옵션 없음 → 본문에 합성
        full_prompt = f"[SYSTEM]\n{system}\n\n[USER]\n{prompt}" if system else prompt
        cmd = [_gemini_bin(), "-p", full_prompt, "--model", self.gemini_model,
               "--output-format", "text"]
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout,
                )
                if result.returncode != 0:
                    err = (result.stderr or "").strip()
                    log.warning("gemini rc=%d: %s", result.returncode, err[:200])
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    return ""
                text = self._strip_gemini_noise(result.stdout or "")
                if not text:
                    log.warning("gemini 빈 응답")
                    return ""
                log.info("Gemini fallback 응답 길이=%d", len(text))
                return text
            except subprocess.TimeoutExpired:
                log.warning("gemini 타임아웃 (attempt %d)", attempt + 1)
                if attempt == 0:
                    time.sleep(2)
            except Exception as e:
                log.warning("gemini 예외: %s", e)
                return ""
        return ""
