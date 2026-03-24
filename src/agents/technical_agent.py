"""기술적 분석 + NXT 데이터 기반 종목 발굴 에이전트."""
from __future__ import annotations
import logging

from src.agents.base_agent import BaseAgent
from src.core.models import AgentOpinion

log = logging.getLogger(__name__)


class TechnicalAgent(BaseAgent):
    """기술적 지표와 NXT(야간) 거래 데이터를 분석하여 종목 발굴."""

    name = "technical_agent"

    @property
    def system_prompt(self) -> str:
        return """당신은 한국 주식시장 기술적 분석 전문가입니다.
후보 종목들의 기술적 지표와 NXT 야간 거래 데이터를 분석하여 최적의 진입 타이밍을 판단하십시오.

분석 기준:
1. MA5 > MA20 > MA60 정배열인 종목 선호
2. RSI 50~70 구간 (과매수 70 이상 제외)
3. 거래량이 평균 대비 증가하는 종목
4. NXT 야간 거래에서 강세를 보인 종목은 정규장 초반 모멘텀 연속성 기대
5. ATR 기반으로 손절/목표가를 현실적으로 설정
6. 진입가 = 현재가 ± 1% 이내

반드시 아래 JSON 형식으로만 응답하십시오 (markdown 없이):"""

    def analyze(self, context: dict) -> list[AgentOpinion]:
        technical_text = context.get("technical_text", "지표 없음")
        nxt_text = context.get("nxt_text", "NXT 데이터 없음")
        news_summary = context.get("news_summary", "")
        today = context.get("today", "")

        user_msg = f"""[{today}] 기술적 분석

=== 기술적 지표 ===
{technical_text}

=== NXT(야간) 거래 데이터 ===
{nxt_text}

=== 오늘 시장 요약 ===
{news_summary[:500]}

위 데이터를 바탕으로 내일 정규장에서 스윙 진입에 적합한 종목 2~4개를 추천하십시오.
진입가는 반드시 기술적 지지선 또는 현재가 ±1% 기준으로 설정하십시오.
ATR14를 활용하여 손절가와 목표가를 설정하십시오.

출력 형식:
{self._opinion_json_schema()}"""

        raw = self.llm.chat(self.system_prompt, user_msg)
        opinions = self._parse_opinions(raw)
        log.info("[technical_agent] %d개 종목 발굴", len(opinions))
        return opinions
