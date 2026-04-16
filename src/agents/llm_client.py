"""LLM 클라이언트 — primary LLM 선택 + 교차 fallback.

primary="claude" → Claude 시도 → Gemini fallback
primary="gemini" → Gemini 시도 → Claude fallback
"""
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
        primary: str = "claude",  # "claude" or "gemini"
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.gemini_model = gemini_model
        self.primary = primary

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

    @staticmethod
    def _bin_available(path_or_name: str) -> bool:
        """절대경로면 파일 존재 + 실행권한 확인, 아니면 PATH 탐색."""
        if not path_or_name:
            return False
        if os.path.isabs(path_or_name):
            return os.path.isfile(path_or_name) and os.access(path_or_name, os.X_OK)
        return shutil.which(path_or_name) is not None

    def _call(self, prompt: str, system: str = "") -> str:
        if self.primary == "gemini":
            return self._call_gemini_first(prompt, system)
        return self._call_claude_first(prompt, system)

    def _call_claude_first(self, prompt: str, system: str) -> str:
        text = self._call_claude(prompt, system)
        if text:
            return text
        gbin = _gemini_bin()
        if self._bin_available(gbin):
            log.warning("Claude 실패 → Gemini fallback 시도")
            text = self._call_gemini(prompt, system)
            if text:
                return text
            log.error("Gemini fallback 도 실패")
        else:
            log.error("Gemini 바이너리 없음 (%s) — fallback 불가", gbin)
        return ""

    def _call_gemini_first(self, prompt: str, system: str) -> str:
        gbin = _gemini_bin()
        if self._bin_available(gbin):
            text = self._call_gemini(prompt, system)
            if text:
                return text
            log.warning("Gemini 실패 → Claude fallback 시도")
        else:
            log.warning("Gemini 바이너리 없음 → Claude로 직접 시도")
        text = self._call_claude(prompt, system)
        if text:
            return text
        log.error("Claude fallback 도 실패")
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
