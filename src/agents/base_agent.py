"""에이전트 기반 클래스."""
from __future__ import annotations
import json
import logging
import re
from typing import Optional

from src.agents.llm_client import LLMClient
from src.core.models import AgentOpinion

log = logging.getLogger(__name__)

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```")


def extract_json(text: str) -> Optional[list | dict]:
    """텍스트에서 JSON 블록 추출."""
    m = JSON_BLOCK_RE.search(text)
    raw = m.group(1) if m else text.strip()
    try:
        return json.loads(raw)
    except Exception:
        # json 블록이 여러개인 경우 fallback
        for block in JSON_BLOCK_RE.findall(text):
            try:
                return json.loads(block)
            except Exception:
                continue
    return None


class BaseAgent:
    """모든 스윙 에이전트의 공통 기반."""

    name: str = "base_agent"

    def __init__(self, llm: LLMClient):
        self.llm = llm

    @property
    def system_prompt(self) -> str:
        raise NotImplementedError

    def analyze(self, context: dict) -> list[AgentOpinion]:
        """컨텍스트를 분석하여 종목 추천 의견 반환."""
        raise NotImplementedError

    def _parse_opinions(self, text: str) -> list[AgentOpinion]:
        """LLM 응답에서 AgentOpinion 목록 파싱."""
        data = extract_json(text)
        if data is None:
            log.warning("[%s] JSON 파싱 실패:\n%s", self.name, text[:300])
            return []

        if isinstance(data, dict):
            data = [data]

        opinions = []
        for item in data:
            try:
                opinions.append(AgentOpinion(
                    agent_name=self.name,
                    symbol=str(item.get("symbol", "")),
                    name=str(item.get("name", "")),
                    conviction=float(item.get("conviction", 0.5)),
                    rationale=str(item.get("rationale", "")),
                    entry_low=float(item.get("entry_low", 0)),
                    entry_high=float(item.get("entry_high", 0)),
                    target_price=float(item.get("target_price", 0)),
                    stop_price=float(item.get("stop_price", 0)),
                    tags=list(item.get("tags", [])),
                ))
            except Exception as e:
                log.warning("[%s] 의견 파싱 오류: %s | %s", self.name, e, item)
        return opinions

    def _opinion_json_schema(self) -> str:
        return """[
  {
    "symbol": "종목코드(6자리)",
    "name": "종목명",
    "conviction": 0.0~1.0,
    "rationale": "추천 이유 (2-3문장)",
    "entry_low": 진입하단가(숫자),
    "entry_high": 진입상단가(숫자),
    "target_price": 목표가(숫자),
    "stop_price": 손절가(숫자),
    "tags": ["태그1", "태그2"]
  }
]"""
