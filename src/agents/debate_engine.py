"""멀티 에이전트 토론 엔진 — 2라운드 토론 후 모더레이터 최종 결정.

흐름:
  Round 0: 각 에이전트가 종목명/코드/근거만 추천 (가격 없음)
  [가격 조회]: Round 0 추천 종목에 대해 KIS로 현재가·기술지표 실시간 조회
  Round 1: 실제 가격 데이터를 받아 진입가/목표가/손절가 설정
  Round 2: 모더레이터 최종 결정
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

from src.agents.base_agent import BaseAgent, extract_json
from src.agents.llm_client import LLMClient
from src.core.config import ScreeningConfig
from src.core.models import AgentOpinion, DebateResult, SwingCandidate

log = logging.getLogger(__name__)

_AGENT_LABEL = {
    "news_agent":      "📰 뉴스 에이전트",
    "theme_agent":     "🏷 테마/공시 에이전트",
    "technical_agent": "📊 기술적 분석 에이전트",
}

# Round 0: 종목 추천만 (가격 없음)
_PRELIM_SCHEMA = """[
  {
    "symbol": "종목코드(6자리)",
    "name": "종목명",
    "conviction": 0.0~1.0,
    "rationale": "추천 이유 (2-3문장)",
    "tags": ["태그1", "태그2"]
  }
]"""


class DebateEngine:
    def __init__(
        self,
        agents: list[BaseAgent],
        llm: LLMClient,
        screening_cfg: ScreeningConfig,
        num_rounds: int = 2,
        price_fetcher=None,   # callable(symbols: list[str]) -> dict[str, dict]
    ):
        self.agents = agents
        self.llm = llm
        self.screening_cfg = screening_cfg
        self.num_rounds = num_rounds
        self.price_fetcher = price_fetcher  # KIS 현재가+기술지표 조회 함수
        self._transcript: list[str] = []

    # ── 공개 API ──────────────────────────────────────────────────────────

    def run(self, context: dict) -> tuple[list[SwingCandidate], str, list[SwingCandidate]]:
        """토론 실행 → (candidates, transcript, reserves)."""
        self._transcript = []
        self._budget_text = context.get("budget_text", "")
        today = context.get("today", datetime.now().strftime("%Y-%m-%d"))
        self._log(f"# {today} 스윙봇 종목발굴 토론 보고서\n")

        # ── Round 0: 종목 추천 (가격 없음) ───────────────────────────────
        self._log("## ▶ Round 0 — 독립 분석 (종목 선정)\n각 에이전트가 독립적으로 종목을 추천합니다.\n")
        log.info("=== Round 0: 독립 종목 추천 ===")
        prelim: dict[str, list[dict]] = {}
        for agent in self.agents:
            ops = self._round0_analyze(agent, context)
            prelim[agent.name] = ops
            self._log_prelim(agent.name, ops)
            log.info("  [%s] %d개 추천", agent.name, len(ops))

        # ── 가격 조회 ─────────────────────────────────────────────────────
        all_symbols = list({op["symbol"] for ops in prelim.values() for op in ops if op.get("symbol")})
        price_ctx: dict[str, dict] = {}
        if all_symbols and self.price_fetcher:
            log.info("KIS 실시간 가격 조회: %s", all_symbols)
            price_ctx = self.price_fetcher(all_symbols)
            self._log("## 📈 실시간 주가 데이터\n")
            for sym, d in price_ctx.items():
                self._log(
                    f"**{d.get('name', sym)} ({sym})**\n"
                    f"- 현재가: {int(d.get('price', 0)):,}원  전일대비: {d.get('chg_pct', 0):+.2f}%\n"
                    f"- MA5: {int(d.get('ma5') or 0):,}  MA20: {int(d.get('ma20') or 0):,}  "
                    f"ATR14: {int(d.get('atr14') or 0):,}  RSI14: {d.get('rsi14', '-')}\n"
                    f"- 거래량: {d.get('last_volume', 0):,}  (20일평균: {d.get('volume_avg20', 0):,})\n"
                )
        else:
            log.warning("가격 조회 불가 — price_fetcher 미설정")

        price_text = _format_price_ctx(price_ctx)

        # ── Round 1: 실제 가격 기반으로 진입가/목표가/손절가 설정 ──────────
        self._log("## ▶ Round 1 — 가격 기반 재검토\n실제 주가 데이터를 바탕으로 진입가/목표가/손절가를 설정합니다.\n")
        log.info("=== Round 1: 가격 기반 의견 수렴 ===")
        all_opinions: dict[str, list[AgentOpinion]] = {}
        for agent in self.agents:
            ops = self._round1_price_review(agent, prelim, price_text)
            all_opinions[agent.name] = ops
            self._log_opinions(agent.name, ops, "Round 1")
            log.info("  [%s] %d개 의견", agent.name, len(ops))

        # ── Round 2: 모더레이터 최종 결정 ────────────────────────────────
        self._log("## ▶ Round 2 — 모더레이터 최종 결정\n")
        log.info("=== Round 2: 모더레이터 결정 ===")
        debate_results = self._moderate(all_opinions, today, price_text)

        self._log("## ✅ 최종 선정 종목\n")
        for r in debate_results:
            tp_pct = (r.target_price / r.entry_high - 1) * 100 if r.entry_high else 0
            sl_pct = (1 - r.stop_price / r.entry_low) * 100 if r.entry_low else 0
            cur = price_ctx.get(r.symbol, {}).get("price", 0)
            self._log(
                f"### {r.name} ({r.symbol})\n"
                f"- 현재가: {int(cur):,}원\n"
                f"- 신뢰도: {r.consensus_score:.0%}  |  찬성: {', '.join(r.supporting_agents)}\n"
                f"- 진입 구간: {int(r.entry_low):,} ~ {int(r.entry_high):,}원\n"
                f"- 목표가: {int(r.target_price):,}원 (+{tp_pct:.1f}%)\n"
                f"- 손절가: {int(r.stop_price):,}원 (-{sl_pct:.1f}%)\n"
                f"- 선정 근거: {r.final_rationale}\n"
            )

        max_primary = self.screening_cfg.max_candidates
        all_candidates = self._to_candidates(debate_results)
        candidates = all_candidates[:max_primary]
        reserves = all_candidates[max_primary:]
        log.info("최종 후보: %d개 (예비: %d개)", len(candidates), len(reserves))
        return candidates, "\n".join(self._transcript), reserves

    # ── Round 0: 종목만 추천 ────────────────────────────────────────────

    def _round0_analyze(self, agent: BaseAgent, context: dict) -> list[dict]:
        """가격 없이 종목명/코드/근거만 추천."""
        news_text = context.get("news_text", "")
        dart_text = context.get("dart_text", "")
        today = context.get("today", "")

        budget_text = context.get("budget_text", "")

        user_msg = f"""[{today}] 오늘 뉴스와 공시를 분석하여 스윙 트레이딩 유망 종목을 추천하십시오.
이 단계에서는 종목명과 추천 이유만 작성하고, 가격은 입력하지 마십시오.
{budget_text}

=== 오늘 뉴스 ===
{news_text[:1500]}

=== DART 공시 ===
{dart_text[:800]}

출력 형식 (JSON, 2~4개):
{_PRELIM_SCHEMA}"""

        raw = self.llm.chat(agent.system_prompt, user_msg)
        data = extract_json(raw)
        if not data:
            return []
        if isinstance(data, dict):
            data = [data]
        result = []
        for item in data:
            if item.get("symbol") and item.get("name"):
                result.append({
                    "symbol": str(item["symbol"]).strip(),
                    "name": str(item["name"]).strip(),
                    "conviction": float(item.get("conviction", 0.5)),
                    "rationale": str(item.get("rationale", "")),
                    "tags": list(item.get("tags", [])),
                })
        return result

    # ── Round 1: 실제 가격 기반 진입가·목표가·손절가 설정 ────────────────

    def _round1_price_review(
        self,
        agent: BaseAgent,
        prelim: dict[str, list[dict]],
        price_text: str,
    ) -> list[AgentOpinion]:
        """실제 주가 데이터를 보고 가격 목표를 설정."""
        # 전체 Round0 추천 요약
        all_prelim_text = ""
        for name, ops in prelim.items():
            all_prelim_text += f"\n[{_AGENT_LABEL.get(name, name)}]\n"
            for op in ops:
                all_prelim_text += f"  {op['name']}({op['symbol']}) conv={op['conviction']:.2f}: {op['rationale'][:80]}\n"

        my_ops = prelim.get(agent.name, [])
        my_text = "\n".join(f"  - {op['name']}({op['symbol']}): {op['rationale'][:100]}" for op in my_ops)

        user_msg = f"""아래 실시간 주가 데이터를 참고하여, 당신이 추천한 종목의 진입가/목표가/손절가를 설정하십시오.
반드시 현재가를 기준으로 현실적인 가격을 설정하고, 현재가에서 크게 벗어난 수치는 절대 사용하지 마십시오.

=== 실시간 주가 데이터 ===
{price_text}

=== 모든 에이전트 Round0 추천 ===
{all_prelim_text}

=== 내 Round0 추천 ===
{my_text if my_text else "없음"}

진입 기준:
- 진입 구간: 현재가 ±2% 이내
- 목표가: ATR14의 3~5배 위 (현재가 대비 +3~8%)
- 손절가: ATR14의 1.5~2배 아래 (현재가 대비 -2~4%)
- 다른 에이전트가 추천한 종목 중 동의하는 것도 포함 가능

출력 형식 (JSON):
{agent._opinion_json_schema()}"""

        raw = self.llm.chat(agent.system_prompt, user_msg)
        opinions = agent._parse_opinions(raw)
        return opinions

    # ── 모더레이터 ──────────────────────────────────────────────────────

    def _moderate(
        self,
        all_opinions: dict[str, list[AgentOpinion]],
        today: str,
        price_text: str,
    ) -> list[DebateResult]:
        summary = self._opinions_to_text(all_opinions)

        system = """당신은 한국 주식 스윙 트레이딩 전문 심판관(Moderator)입니다.
여러 분석가의 의견을 종합하여 최종 투자 종목을 선정하십시오.

선정 기준:
1. 2인 이상 에이전트가 동의한 종목 우선
2. 평균 conviction 0.6 이상인 종목만 선정
3. 최대 5개 종목을 신뢰도 순으로 선정 (상위 3개는 정규 후보, 나머지는 예비 후보)
4. 진입가/목표가/손절가는 반드시 실시간 현재가 기준으로 현실적인 값 사용
5. 동일 섹터 종목이 많으면 가장 유망한 1개만 선택

반드시 JSON 형식으로만 응답하십시오."""

        user_msg = f"""[{today}] 최종 종목 선정
{self._budget_text}

=== 실시간 주가 데이터 ===
{price_text}

=== 분석가 의견 ===
{summary}

위 의견을 종합하여 최종 투자 종목을 선정하십시오.
진입가/목표가/손절가는 위 실시간 주가를 기준으로 설정하십시오.

출력 형식 (JSON):
[
  {{
    "symbol": "종목코드",
    "name": "종목명",
    "consensus_score": 0.0~1.0,
    "final_rationale": "최종 선정 이유 (2-3문장)",
    "entry_low": 진입하단가,
    "entry_high": 진입상단가,
    "target_price": 목표가,
    "stop_price": 손절가,
    "supporting_agents": ["에이전트명1"],
    "tags": ["태그"]
  }}
]"""

        raw = self.llm.chat(system, user_msg)
        data = extract_json(raw)
        if not data:
            log.error("모더레이터 파싱 실패:\n%s", raw[:300])
            self._log(f"[모더레이터 오류]\n{raw[:300]}\n")
            return []
        if isinstance(data, dict):
            data = [data]

        results = []
        for item in data:
            try:
                results.append(DebateResult(
                    symbol=str(item["symbol"]),
                    name=str(item["name"]),
                    consensus_score=float(item.get("consensus_score", 0.5)),
                    final_rationale=str(item.get("final_rationale", "")),
                    entry_low=float(item["entry_low"]),
                    entry_high=float(item["entry_high"]),
                    target_price=float(item["target_price"]),
                    stop_price=float(item["stop_price"]),
                    supporting_agents=list(item.get("supporting_agents", [])),
                    tags=list(item.get("tags", [])),
                ))
            except Exception as e:
                log.warning("모더레이터 파싱 오류: %s | %s", e, item)
        return results

    # ── 변환 ────────────────────────────────────────────────────────────

    def _to_candidates(self, results: list[DebateResult]) -> list[SwingCandidate]:
        expiry_days = self.screening_cfg.entry_expiry_days
        now = datetime.now()
        candidates = []
        for r in results:
            candidates.append(SwingCandidate(
                symbol=r.symbol,
                name=r.name,
                entry_low=r.entry_low,
                entry_high=r.entry_high,
                target_price=r.target_price,
                stop_price=r.stop_price,
                consensus_score=r.consensus_score,
                rationale=r.final_rationale,
                tags=r.tags,
                discovered_at=now,
                expires_at=now + timedelta(days=expiry_days),
            ))
        return candidates

    # ── 트랜스크립트 ────────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self._transcript.append(text)

    def _log_prelim(self, agent_name: str, ops: list[dict]) -> None:
        label = _AGENT_LABEL.get(agent_name, agent_name)
        self._log(f"### {label}\n")
        if not ops:
            self._log("추천 없음\n")
            return
        for op in ops:
            self._log(
                f"**{op['name']} ({op['symbol']})**  conviction: {op['conviction']:.0%}\n"
                f"- 근거: {op['rationale']}\n"
            )

    def _log_opinions(self, agent_name: str, ops: list[AgentOpinion], label: str = "") -> None:
        alabel = _AGENT_LABEL.get(agent_name, agent_name)
        self._log(f"### {alabel} ({label})\n")
        if not ops:
            self._log("추천 없음\n")
            return
        for op in ops:
            tp_pct = (op.target_price / op.entry_high - 1) * 100 if op.entry_high else 0
            sl_pct = (1 - op.stop_price / op.entry_low) * 100 if op.entry_low else 0
            self._log(
                f"**{op.name} ({op.symbol})**  conviction: {op.conviction:.0%}\n"
                f"- 진입: {int(op.entry_low):,} ~ {int(op.entry_high):,}원\n"
                f"- 목표: {int(op.target_price):,}원 (+{tp_pct:.1f}%)  "
                f"손절: {int(op.stop_price):,}원 (-{sl_pct:.1f}%)\n"
                f"- 근거: {op.rationale}\n"
            )

    def _opinions_to_text(self, all_opinions: dict[str, list[AgentOpinion]]) -> str:
        lines = []
        for agent_name, ops in all_opinions.items():
            lines.append(f"\n[{agent_name}]")
            for op in ops:
                lines.append(
                    f"  {op.name}({op.symbol}) conviction={op.conviction:.2f}\n"
                    f"    진입: {int(op.entry_low):,}~{int(op.entry_high):,}  "
                    f"목표: {int(op.target_price):,}  손절: {int(op.stop_price):,}\n"
                    f"    근거: {op.rationale}"
                )
        return "\n".join(lines)


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def _format_price_ctx(price_ctx: dict[str, dict]) -> str:
    if not price_ctx:
        return "실시간 가격 데이터 없음"
    lines = []
    for sym, d in price_ctx.items():
        lines.append(
            f"{d.get('name', sym)}({sym}): 현재가 {int(d.get('price', 0)):,}원  "
            f"전일대비 {d.get('chg_pct', 0):+.2f}%  "
            f"MA5={int(d.get('ma5') or 0):,}  MA20={int(d.get('ma20') or 0):,}  "
            f"ATR14={int(d.get('atr14') or 0):,}  RSI={d.get('rsi14', '-')}  "
            f"거래량={d.get('last_volume', 0):,}"
        )
    return "\n".join(lines)
