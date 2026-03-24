"""뉴스 기반 종목 발굴 에이전트."""
from __future__ import annotations
import logging

from src.agents.base_agent import BaseAgent
from src.core.models import AgentOpinion

log = logging.getLogger(__name__)


class NewsAgent(BaseAgent):
    """당일 뉴스 헤드라인을 분석하여 수혜 종목을 발굴."""

    name = "news_agent"

    @property
    def system_prompt(self) -> str:
        return """당신은 한국 주식시장 전문 뉴스 분석가입니다.
당일 뉴스 헤드라인과 요약을 분석하여 단기(1~5일) 스윙 트레이딩에 유망한 종목을 발굴하십시오.

분석 기준:
1. 직접 수혜 종목 우선 (간접 수혜는 낮은 conviction)
2. 정책/규제 발표, 실적 서프라이즈, M&A, 신제품 출시 등 이벤트 드리븐 기회
3. 단기 모멘텀이 지속 가능한지 판단
4. 이미 급등한 종목보다는 아직 반응이 덜한 종목 선호

반드시 아래 JSON 형식으로만 응답하십시오 (markdown 없이):"""

    def analyze(self, context: dict) -> list[AgentOpinion]:
        news_text = context.get("news_text", "뉴스 없음")
        today = context.get("today", "")

        user_msg = f"""[{today}] 당일 뉴스 분석

=== 뉴스 ===
{news_text}

위 뉴스를 바탕으로 스윙 트레이딩에 유망한 종목 2~4개를 추천하십시오.
종목코드는 한국 거래소 6자리 숫자를 사용하십시오.
진입가/목표가/손절가는 현실적인 가격대로 설정하십시오.

출력 형식:
{self._opinion_json_schema()}"""

        raw = self.llm.chat(self.system_prompt, user_msg)
        opinions = self._parse_opinions(raw)
        log.info("[news_agent] %d개 종목 발굴", len(opinions))
        return opinions
