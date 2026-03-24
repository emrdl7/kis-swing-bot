"""테마/공시 기반 종목 발굴 에이전트."""
from __future__ import annotations
import logging

from src.agents.base_agent import BaseAgent
from src.core.models import AgentOpinion

log = logging.getLogger(__name__)


class ThemeAgent(BaseAgent):
    """당일 테마 흐름과 DART 공시를 분석하여 종목 발굴."""

    name = "theme_agent"

    @property
    def system_prompt(self) -> str:
        return """당신은 한국 주식시장의 테마/섹터 전문 분석가입니다.
DART 전자공시와 시장 테마 흐름을 분석하여 스윙 트레이딩 기회를 발굴하십시오.

분석 기준:
1. 실적 개선 공시(잠정실적, 영업이익 증가) → 높은 conviction
2. 자사주 매입, 유상증자 취소, 기술이전 공시 → 단기 모멘텀
3. 오늘 강한 테마와 연관된 저평가 종목 발굴
4. 공시 내용이 시장에 아직 충분히 반영되지 않은 종목 선호
5. 인버스/레버리지 ETF 제외

반드시 아래 JSON 형식으로만 응답하십시오 (markdown 없이):"""

    def analyze(self, context: dict) -> list[AgentOpinion]:
        dart_text = context.get("dart_text", "공시 없음")
        news_text = context.get("news_text", "뉴스 없음")
        today = context.get("today", "")

        user_msg = f"""[{today}] 테마/공시 분석

=== DART 주요 공시 ===
{dart_text}

=== 시장 뉴스 (테마 참고) ===
{news_text[:1000]}

위 공시와 테마를 바탕으로 스윙 트레이딩 유망 종목 2~4개를 추천하십시오.
인버스·레버리지 ETF는 절대 추천하지 마십시오.

출력 형식:
{self._opinion_json_schema()}"""

        raw = self.llm.chat(self.system_prompt, user_msg)
        opinions = self._parse_opinions(raw)
        log.info("[theme_agent] %d개 종목 발굴", len(opinions))
        return opinions
