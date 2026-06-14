"""LLM 클라이언트 — Codex/Gemini primary 선택 + 교차 fallback."""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"
DEFAULT_CODEX_MODEL = ""  # ChatGPT 구독 계정에서는 -m 생략이 필요


def _gemini_bin() -> str:
    return str(os.getenv("GEMINI_BIN", "gemini")).strip() or "gemini"


def _codex_bin() -> str:
    return str(os.getenv("CODEX_BIN", "codex")).strip() or "codex"


class LLMClient:
    def __init__(
        self,
        max_tokens: int = 2000,
        timeout: float = DEFAULT_TIMEOUT,
        gemini_model: str = DEFAULT_GEMINI_MODEL,
        codex_model: str = DEFAULT_CODEX_MODEL,
        primary: str = "codex",  # "codex" | "gemini"
    ):
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.gemini_model = gemini_model
        self.codex_model = codex_model
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
        return self._call_codex_first(prompt, system)

    def _call_codex_first(self, prompt: str, system: str) -> str:
        """Codex 우선, 실패 시 Gemini."""
        text = self._call_codex(prompt, system)
        if text:
            return text
        gbin = _gemini_bin()
        if self._bin_available(gbin):
            log.warning("Codex 실패 → Gemini fallback 시도")
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
            log.warning("Gemini 실패 → Codex fallback 시도")
        else:
            log.warning("Gemini 바이너리 없음 → Codex로 직접 시도")
        text = self._call_codex(prompt, system)
        if text:
            return text
        log.error("Codex fallback 도 실패")
        return ""

    def _call_codex(self, prompt: str, system: str) -> str:
        """Codex CLI(`codex exec`) 비대화형 호출.

        - codex는 `--append-system-prompt` 같은 시스템 프롬프트 옵션이 없어
          system을 본문 상단에 합성. (gemini와 동일 패턴)
        - launchd 환경에서 PATH 격리 대응을 위해 /usr/local/bin 명시.
        - 응답에서 codex CLI 내부 메타라인(thinking, usage 등) 제거.
        """
        full_prompt = f"[SYSTEM]\n{system}\n\n[USER]\n{prompt}" if system else prompt
        cmd = [_codex_bin(), "exec", "--skip-git-repo-check"]
        # codex_model 비어있으면 -m 생략 → ChatGPT 계정 default 모델 사용
        # (ChatGPT 계정은 'gpt-5' 등 명시 모델명 거부함)
        if self.codex_model:
            cmd += ["-m", self.codex_model]
        cmd += [full_prompt]
        env = os.environ.copy()
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout, env=env,
                )
                if result.returncode != 0:
                    err = (result.stderr or "").strip()
                    log.warning("codex rc=%d: %s", result.returncode, err[:200])
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    return ""
                text = self._strip_codex_noise(result.stdout or "")
                if not text:
                    log.warning("codex 빈 응답")
                    return ""
                log.info("Codex 응답 길이=%d", len(text))
                return text
            except subprocess.TimeoutExpired:
                log.warning("codex 타임아웃 (attempt %d)", attempt + 1)
                if attempt == 0:
                    time.sleep(2)
            except Exception as e:
                log.warning("codex 예외: %s", e)
                return ""
        return ""

    @staticmethod
    def _strip_codex_noise(raw: str) -> str:
        """codex exec 출력의 메타라인(타임스탬프, OpenAI 사용량 등) 제거.

        codex exec는 보통 stderr에 메타정보를 보내고 stdout에 본문만 출력하지만,
        일부 버전에서 stdout 머리·꼬리에 [INFO]/[USAGE]/timestamps 같은 라인을
        섞어내는 경우가 있어 안전하게 거름.
        """
        noise_starts = (
            "[INFO]", "[WARN]", "[ERROR]", "[DEBUG]", "[USAGE]", "[TOOL_USE]",
            "OpenAI usage:", "tokens used:", "Model:", "Reasoning:",
        )
        cleaned = []
        for line in raw.splitlines():
            stripped = line.lstrip()
            if any(stripped.startswith(p) for p in noise_starts):
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()

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
        # launchd 환경에서 /usr/local/bin(node) 경로 보장
        env = os.environ.copy()
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        for attempt in range(2):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout, env=env,
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
