"""멀티 에이전트 토론 엔진 — 2라운드 토론 후 모더레이터 최종 결정."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

from src.agents.base_agent import BaseAgent, extract_json
from src.agents.llm_client import LLMClient
from src.core.config import ScreeningConfig
from src.core.models import AgentOpinion, DebateResult, SwingCandidate

log = logging.getLogger(__name__)

_AGENT_LABEL = {
    "news_agent": "📰 뉴스 에이전트",
    "theme_agent": "🏷 테마/공시 에이전트",
    "technical_agent": "📊 기술적 분석 에이전트",
}


class DebateEngine:
    """
    토론 진행 방식:
      Round 0: 각 에이전트가 독립적으로 종목 추천
      Round 1: 서로의 의견을 보고 재검토 (cross-review)
      Round 2: 모더레이터가 최종 종목 결정
    """

    def __init__(
        self,
        agents: list[BaseAgent],
        llm: LLMClient,
        screening_cfg: ScreeningConfig,
        num_rounds: int = 2,
    ):
        self.agents = agents
        self.llm = llm
        self.screening_cfg = screening_cfg
        self.num_rounds = num_rounds
        self._transcript: list[str] = []

    # ── 공개 API ──────────────────────────────────────────────────────────

    def run(self, context: dict) -> tuple[list[SwingCandidate], str]:
        """토론 실행.

        Returns:
            (candidates, transcript_text)
        """
        self._transcript = []
        today = context.get("today", datetime.now().strftime("%Y-%m-%d"))
        self._log(f"# {today} 스윙봇 종목발굴 토론 보고서\n")

        # Round 0: 독립적 분석
        self._log("## ▶ Round 0 — 독립 분석\n각 에이전트가 독립적으로 종목을 추천합니다.\n")
        log.info("=== 토론 Round 0: 독립 분석 시작 ===")
        all_opinions: dict[str, list[AgentOpinion]] = {}
        for agent in self.agents:
            ops = agent.analyze(context)
            all_opinions[agent.name] = ops
            log.info("  [%s] %d개 의견", agent.name, len(ops))
            self._log_opinions(agent.name, ops)

        if self.num_rounds >= 1:
            # Round 1: cross-review
            self._log("## ▶ Round 1 — 교차 검토\n다른 에이전트의 의견을 본 후 재검토합니다.\n")
            log.info("=== 토론 Round 1: 교차 검토 시작 ===")
            all_opinions = self._cross_review(all_opinions, context)

        # Round 2: 모더레이터 최종 결정
        self._log("## ▶ Round 2 — 모더레이터 최종 결정\n")
        log.info("=== 토론 Round 2: 모더레이터 결정 ===")
        debate_results = self._moderate(all_opinions, today)

        # 최종 결과 기록
        self._log("## ✅ 최종 선정 종목\n")
        for r in debate_results:
            tp_pct = (r.target_price / r.entry_high - 1) * 100
            sl_pct = (1 - r.stop_price / r.entry_low) * 100
            self._log(
                f"### {r.name} ({r.symbol})\n"
                f"- 신뢰도: {r.consensus_score:.0%}  |  찬성: {', '.join(r.supporting_agents)}\n"
                f"- 진입 구간: {int(r.entry_low):,} ~ {int(r.entry_high):,}원\n"
                f"- 목표가: {int(r.target_price):,}원 (+{tp_pct:.1f}%)\n"
                f"- 손절가: {int(r.stop_price):,}원 (-{sl_pct:.1f}%)\n"
                f"- 선정 근거: {r.final_rationale}\n"
            )

        candidates = self._to_candidates(debate_results)
        log.info("최종 후보: %d개", len(candidates))
        return candidates, "\n".join(self._transcript)

    # ── 내부 메서드 ────────────────────────────────────────────────────────

    def _cross_review(
        self,
        round0: dict[str, list[AgentOpinion]],
        context: dict,
    ) -> dict[str, list[AgentOpinion]]:
        updated: dict[str, list[AgentOpinion]] = {}

        for agent in self.agents:
            others_text = []
            for name, ops in round0.items():
                if name != agent.name:
                    others_text.append(f"\n[{_AGENT_LABEL.get(name, name)} 의견]\n" + self._opinions_mini(ops))
            other_views = "\n".join(others_text)

            user_msg = f"""다른 분석가들의 의견을 검토한 후, 당신의 최종 추천을 업데이트하십시오.

=== 다른 분석가 의견 ===
{other_views}

위 의견들을 참고하여 당신의 기존 추천을 유지/수정/철회하고,
새로운 동의 종목이 있다면 추가하십시오.

출력 형식 (JSON):
{agent._opinion_json_schema()}"""

            raw = self.llm.chat(agent.system_prompt, user_msg)
            refined = agent._parse_opinions(raw)
            updated[agent.name] = refined if refined else round0[agent.name]
            log.info("  [%s] Round1 후 %d개 의견", agent.name, len(updated[agent.name]))
            self._log_opinions(agent.name, updated[agent.name], round_label="Round 1 재검토 후")

        return updated

    def _moderate(
        self,
        all_opinions: dict[str, list[AgentOpinion]],
        today: str,
    ) -> list[DebateResult]:
        summary = self._opinions_to_text(all_opinions)

        system = """당신은 한국 주식 스윙 트레이딩 전문 심판관(Moderator)입니다.
여러 분석가의 의견을 종합하여 최종 투자 종목을 선정하십시오.

선정 기준:
1. 2인 이상 에이전트가 동의한 종목 우선
2. 평균 conviction 0.6 이상인 종목만 선정
3. 최대 3개 종목 선정 (신중하게)
4. 진입가/목표가/손절가는 에이전트들의 평균값 또는 가장 보수적인 값 채택
5. 동일 섹터 종목이 많으면 가장 유망한 1개만 선택

반드시 JSON 형식으로만 응답하십시오."""

        user_msg = f"""[{today}] 분석가 의견 종합

{summary}

위 의견들을 종합하여 최종 투자 종목을 선정하십시오.
최대 3개, 신중하게 선택하십시오.

출력 형식 (JSON):
[
  {{
    "symbol": "종목코드",
    "name": "종목명",
    "consensus_score": 0.0~1.0 (에이전트 동의 수준),
    "final_rationale": "최종 선정 이유 (2-3문장)",
    "entry_low": 진입하단가,
    "entry_high": 진입상단가,
    "target_price": 목표가,
    "stop_price": 손절가,
    "supporting_agents": ["에이전트명1", "에이전트명2"],
    "tags": ["태그1"]
  }}
]"""

        raw = self.llm.chat(system, user_msg)
        data = extract_json(raw)
        if not data:
            log.error("모더레이터 응답 파싱 실패:\n%s", raw[:300])
            self._log(f"[모더레이터 오류] 파싱 실패\n{raw[:300]}\n")
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
                log.warning("모더레이터 결과 파싱 오류: %s | %s", e, item)
        return results

    def _to_candidates(self, results: list[DebateResult]) -> list[SwingCandidate]:
        max_cands = self.screening_cfg.max_candidates
        expiry_days = self.screening_cfg.entry_expiry_days
        now = datetime.now()
        candidates = []
        for r in results[:max_cands]:
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

    # ── 트랜스크립트 헬퍼 ─────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self._transcript.append(text)

    def _log_opinions(
        self,
        agent_name: str,
        ops: list[AgentOpinion],
        round_label: str = "",
    ) -> None:
        label = _AGENT_LABEL.get(agent_name, agent_name)
        suffix = f" ({round_label})" if round_label else ""
        self._log(f"### {label}{suffix}\n")
        if not ops:
            self._log("추천 종목 없음\n")
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

    def _opinions_mini(self, ops: list[AgentOpinion]) -> str:
        return "\n".join(
            f"  {op.name}({op.symbol}) conv={op.conviction:.2f}: {op.rationale[:80]}"
            for op in ops
        )
