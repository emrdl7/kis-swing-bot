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
    "risk_agent":      "⚠️ 리스크 에이전트",
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
            if agent.name == "risk_agent":
                prelim[agent.name] = []  # R0 스킵 — R1에서 다른 추천에 대해 리스크 평가
                log.info("  [%s] R0 스킵 (R1에서 리스크 평가)", agent.name)
                continue
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

            # A-1: 시총/거래대금 미달 종목 필터링
            min_cap = self.screening_cfg.min_market_cap_bn
            min_trade = self.screening_cfg.min_trade_amount
            filtered_out = []
            for sym in list(price_ctx.keys()):
                d = price_ctx[sym]
                mcap = d.get("market_cap_bn", 0)
                tr_amt = d.get("acml_tr_pbmn", 0)
                if mcap and mcap < min_cap:
                    filtered_out.append(f"{d.get('name', sym)}({sym}) 시총 {mcap:,}억 < {min_cap:,}억")
                    del price_ctx[sym]
                elif tr_amt and tr_amt < min_trade:
                    filtered_out.append(f"{d.get('name', sym)}({sym}) 거래대금 {tr_amt/1e8:.0f}억 < {min_trade/1e8:.0f}억")
                    del price_ctx[sym]
            if filtered_out:
                log.info("시총/거래대금 미달 제외: %s", filtered_out)
                self._log("## ⛔ 시총/거래대금 미달 제외\n" + "\n".join(f"- {f}" for f in filtered_out) + "\n")

            self._log("## 📈 실시간 주가 데이터\n")
            for sym, d in price_ctx.items():
                sector_str = f"  [{d.get('sector', '')}]" if d.get("sector") else ""
                mcap_str = f"  시총 {d.get('market_cap_bn', 0):,}억" if d.get("market_cap_bn") else ""
                per_str = f"  PER={d.get('per', 0):.1f}" if d.get("per") else ""
                self._log(
                    f"**{d.get('name', sym)} ({sym})**{sector_str}{mcap_str}{per_str}\n"
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

        # ── R1 가격 검증: 현재가 대비 ±20% 벗어난 가격은 보정 ──────────────
        for agent_name, ops in all_opinions.items():
            if agent_name == "risk_agent":
                continue
            for op in ops:
                cur = price_ctx.get(op.symbol, {}).get("price", 0)
                if cur <= 0:
                    continue
                margin = cur * 0.20
                clamped = False
                if op.entry_low < cur - margin:
                    op.entry_low = round(cur * 0.97)
                    clamped = True
                if op.entry_high > cur + margin:
                    op.entry_high = round(cur * 1.03)
                    clamped = True
                if op.target_price > cur * 1.20:
                    op.target_price = round(cur * 1.08)
                    clamped = True
                if op.stop_price < cur * 0.80:
                    op.stop_price = round(cur * 0.96)
                    clamped = True
                if clamped:
                    log.warning(
                        "[%s] %s 가격 보정 (현재가 %d): entry=%d~%d target=%d stop=%d",
                        agent_name, op.symbol, cur, int(op.entry_low), int(op.entry_high),
                        int(op.target_price), int(op.stop_price),
                    )

        # ── Round 2: 모더레이터 최종 결정 ────────────────────────────────
        self._log("## ▶ Round 2 — 모더레이터 최종 결정\n")
        log.info("=== Round 2: 모더레이터 결정 ===")
        debate_results = self._moderate(all_opinions, today, price_text)

        # 모더레이터가 0개 반환했지만 Round 1 의견이 있으면 fallback 선정
        # (risk_agent 의견은 매수 추천이 아니므로 제외)
        buy_opinions = {k: v for k, v in all_opinions.items() if k != "risk_agent"}
        has_opinions = any(ops for ops in buy_opinions.values())
        if not debate_results and has_opinions:
            log.warning("모더레이터 0개 반환 → Round 1 최고 conviction 종목 fallback 선정")
            self._log("⚠️ 모더레이터 선정 없음 — conviction 최고 종목 자동 선정\n")
            best_op = max(
                (op for ops in buy_opinions.values() for op in ops),
                key=lambda o: o.conviction,
            )
            debate_results = [DebateResult(
                symbol=best_op.symbol,
                name=best_op.name,
                consensus_score=best_op.conviction * 0.5,
                final_rationale=f"[자동선정] {best_op.rationale}",
                entry_low=best_op.entry_low,
                entry_high=best_op.entry_high,
                target_price=best_op.target_price,
                stop_price=best_op.stop_price,
                supporting_agents=[best_op.agent_name],
                tags=best_op.tags,
            )]

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
        all_candidates = self._to_candidates(debate_results, price_ctx)
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

        perf_text = context.get("perf_text", "")

        user_msg = f"""[{today}] 오늘 뉴스와 공시를 분석하여 스윙 트레이딩 유망 종목을 추천하십시오.
이 단계에서는 종목명과 추천 이유만 작성하고, 가격은 입력하지 마십시오.
{budget_text}
{perf_text}

=== 오늘 뉴스 ===
{news_text[:1500]}

=== DART 공시 ===
{dart_text[:800]}

출력 형식 (JSON, 2~4개):
{_PRELIM_SCHEMA}"""

        raw = self.llm.chat(agent.system_prompt, user_msg)
        data = extract_json(raw)
        if data is None:
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
            if name == "risk_agent":
                continue  # 리스크 에이전트의 R0 경고는 별도 처리
            all_prelim_text += f"\n[{_AGENT_LABEL.get(name, name)}]\n"
            for op in ops:
                all_prelim_text += f"  {op['name']}({op['symbol']}) conv={op['conviction']:.2f}: {op['rationale'][:80]}\n"

        my_ops = prelim.get(agent.name, [])
        my_text = "\n".join(f"  - {op['name']}({op['symbol']}): {op['rationale'][:100]}" for op in my_ops)

        # 리스크 에이전트는 다른 에이전트 추천을 평가하는 별도 프롬프트 사용
        if agent.name == "risk_agent":
            user_msg = f"""아래 실시간 주가 데이터와 다른 에이전트들의 추천 종목을 분석하여 리스크를 평가하십시오.

=== 실시간 주가 데이터 ===
{price_text}

=== 다른 에이전트들의 추천 ===
{all_prelim_text}

각 추천 종목에 대해 매수 반대 관점에서 리스크를 분석하십시오.
- conviction이 높을수록 해당 종목의 리스크가 크다는 의미입니다
- conviction 0.7 이상: 강한 반대 (매수 금지 권고)
- conviction 0.4~0.6: 주의 필요
- conviction 0.3 이하: 큰 문제 없음
- rationale에 구체적 리스크 사유를 명시하십시오
- entry_low/high/target_price/stop_price는 현재가 기준으로 현실적으로 설정

출력 형식 (JSON):
{agent._opinion_json_schema()}"""
        else:
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

분석가 구성:
- 뉴스/테마/기술적 에이전트: 매수 추천 관점
- 리스크 에이전트(risk_agent): 매수 반대 관점 — conviction이 높을수록 해당 종목 리스크가 큼

선정 기준 (우선순위):
1. 매수 에이전트 2인 이상 동의 종목 우선 (risk_agent는 동의 카운트에서 제외)
2. 매수 에이전트 conviction 0.6 이상 우선
3. 리스크 에이전트의 conviction이 0.7 이상인 종목은 선정 제외 또는 consensus_score 대폭 하향
4. 최대 5개 종목을 신뢰도 순으로 선정 (상위 3개는 정규 후보, 나머지는 예비 후보)
5. 진입가/목표가/손절가는 반드시 실시간 현재가 기준으로 현실적인 값 사용
6. 동일 섹터 종목이 많으면 가장 유망한 1개만 선택

consensus_score 계산 가이드:
- (매수 동의 에이전트 수 / 전체 매수 에이전트 수) × 평균 conviction
- 리스크 에이전트 conviction 0.5 이상이면 위 점수에서 0.1~0.2 차감

NXT(프리장) 데이터 활용:
- 주가 데이터에 [NXT 갭 +X%] 표시가 있으면 프리장 갭 상승/하락 신호임
- NXT 갭 +5% 이상: 과열 가능성 → 진입가 상향 또는 선정 제외 검토
- NXT 갭 -1% 이하: 야간 악재 → 손절가 타이트하게 설정
- NXT 거래대금이 크면 신뢰도 높은 신호

중요: 매수 에이전트 의견이 존재하는 한 최소 1개 종목은 반드시 선정하십시오.
2인 이상 동의 종목이 없으면 단독 추천 중 conviction이 가장 높은 종목을 consensus_score 0.5 미만으로 선정하십시오.
에이전트 의견이 모두 비어있을 때만 빈 배열 []을 반환하십시오.

반드시 JSON 배열 형식으로만 응답하십시오. JSON 외 텍스트를 포함하지 마십시오."""

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
        if data is None:
            log.warning("모더레이터 파싱 실패 (1차) — 재시도")
            raw = self.llm.chat(system, user_msg + "\n\n⚠️ 반드시 JSON 배열만 출력하십시오. 설명 텍스트 없이 [ ... ] 형태로만 응답.")
            data = extract_json(raw)
        if data is None:
            log.error("모더레이터 파싱 실패 (2차):\n%s", raw[:300])
            self._log(f"[모더레이터 오류]\n{raw[:300]}\n")
            return []
        if isinstance(data, list) and len(data) == 0:
            log.info("모더레이터 판정: 선정 종목 없음 (빈 배열 반환)")
            self._log("[모더레이터] 선정 종목 없음\n")
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

    def _to_candidates(self, results: list[DebateResult],
                        price_ctx: dict[str, dict] | None = None) -> list[SwingCandidate]:
        expiry_days = self.screening_cfg.entry_expiry_days
        now = datetime.now()
        price_ctx = price_ctx or {}
        candidates = []
        for r in results:
            d = price_ctx.get(r.symbol, {})
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
                nxt_close=d.get("price") if d.get("nxt_gap_pct") is not None else None,
                prev_close=d.get("prev_close"),
                nxt_gap_pct=d.get("nxt_gap_pct"),
                nxt_trade_amount_bn=d.get("nxt_trade_amount_bn"),
            ))
        return candidates

    # ── 모더레이터 재평가 (저녁 초벌 → 아침 최종) ────────────────────────────

    def moderator_reevaluate(
        self,
        prelim_candidates: list[SwingCandidate],
        overnight_delta: dict,
    ) -> list[SwingCandidate]:
        """저녁 초벌 후보 + 밤사이 변화 → Moderator 1회로 최종 후보 확정.

        - R0/R1은 실행하지 않음 (비용 절감 핵심)
        - 초벌에 없던 종목은 절대 추가하지 않음
        - 실패 시 prelim_candidates 전체를 그대로 반환 (폴백)
        """
        from src.data.overnight import format_us_market, format_nxt_prices

        today = datetime.now().strftime("%Y-%m-%d")
        prelim_symbol_set = {c.symbol for c in prelim_candidates}

        # 초벌 후보 요약
        prelim_text = "\n".join(
            f"- {c.name}({c.symbol}) 신뢰:{c.consensus_score:.0%} "
            f"진입:{int(c.entry_low):,}~{int(c.entry_high):,} "
            f"목표:{int(c.target_price):,} 손절:{int(c.stop_price):,}\n"
            f"  근거: {c.rationale[:120]}"
            for c in prelim_candidates
        )

        # 밤사이 변화 요약
        us_text = format_us_market(overnight_delta.get("us_market"))
        nxt_text = format_nxt_prices(overnight_delta.get("nxt_prices", {}), prelim_candidates)
        news_items = overnight_delta.get("fresh_news", [])
        news_text = "\n".join(
            f"- {n.get('title', '')} ({n.get('published_at', '')[:16]})"
            for n in news_items[:20]
        ) or "조간 뉴스 없음"

        system = """당신은 한국 주식 스윙 트레이딩 전문 심판관(Moderator)입니다.
전날 저녁에 선정한 초벌 후보 종목들을 밤사이 변화를 반영하여 재평가하십시오.

판정 기준:
1. 초벌 후보 중 악재·갭다운으로 당일 진입 부적합한 종목은 제외
2. 과도한 갭업(+5% 이상)은 consensus_score 감점 또는 제외
3. 초벌에 없던 종목을 새로 추가하지 말 것 (유니버스 확장 금지)
4. 미국 시장 하락(-1.5% 이상)이면 전반적 리스크 가중
5. 선정된 종목은 기존 스키마와 동일한 형식으로 출력

반드시 JSON 배열 형식으로만 응답하십시오."""

        user_msg = f"""[{today}] 아침 재평가

[전일 선정 초벌 후보]
{prelim_text}

[밤사이 변화]
- 미국 시장 마감: {us_text}
- 조간 뉴스 (06:00 이후):
{news_text}
- NXT 프리장 가격:
{nxt_text}

위 초벌 후보 중 당일 진입 적합한 종목만 아래 형식으로 선정하십시오.
초벌에 없던 종목을 추가하지 마십시오.

출력 형식 (JSON):
[
  {{
    "symbol": "종목코드",
    "name": "종목명",
    "consensus_score": 0.0~1.0,
    "final_rationale": "재평가 사유 (1-2문장)",
    "entry_low": 진입하단가,
    "entry_high": 진입상단가,
    "target_price": 목표가,
    "stop_price": 손절가,
    "supporting_agents": ["reeval"],
    "tags": ["태그"]
  }}
]"""

        log.info("[재평가] Moderator 호출 (초벌 %d개)", len(prelim_candidates))
        raw = self.llm.chat(system, user_msg)
        data = extract_json(raw)
        if data is None:
            log.warning("[재평가] 파싱 실패 (1차) — 재시도")
            raw = self.llm.chat(system, user_msg + "\n\n⚠️ 반드시 JSON 배열만 출력하십시오.")
            data = extract_json(raw)
        if data is None:
            log.error("[재평가] 파싱 실패 (2차) — 초벌 전체 폴백\n%s", raw[:200])
            return prelim_candidates
        if isinstance(data, dict):
            data = [data]

        results = []
        for item in data:
            try:
                sym = str(item["symbol"])
                if sym not in prelim_symbol_set:
                    log.warning("[재평가] 초벌에 없는 종목 %s 제외 (유니버스 확장 금지)", sym)
                    continue
                results.append(DebateResult(
                    symbol=sym,
                    name=str(item["name"]),
                    consensus_score=float(item.get("consensus_score", 0.5)),
                    final_rationale=str(item.get("final_rationale", "")),
                    entry_low=float(item["entry_low"]),
                    entry_high=float(item["entry_high"]),
                    target_price=float(item["target_price"]),
                    stop_price=float(item["stop_price"]),
                    supporting_agents=list(item.get("supporting_agents", ["reeval"])),
                    tags=list(item.get("tags", [])),
                ))
            except Exception as e:
                log.warning("[재평가] 파싱 오류: %s | %s", e, item)

        if not results:
            log.warning("[재평가] 선정 종목 없음 — 초벌 전체 폴백")
            return prelim_candidates

        # prelim의 ref_price_eod를 재평가 결과에 이어받음
        prelim_map = {c.symbol: c for c in prelim_candidates}
        candidates = self._to_candidates(results)
        for cand in candidates:
            if cand.symbol in prelim_map:
                cand.ref_price_eod = prelim_map[cand.symbol].ref_price_eod

        log.info("[재평가] 최종 %d개 선정 (초벌 %d개 중)", len(candidates), len(prelim_candidates))
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
        base = (
            f"{d.get('name', sym)}({sym})"
        )
        # 업종·시총·PER/PBR (있으면 표시)
        sector = d.get("sector", "")
        mcap = d.get("market_cap_bn", 0)
        per = d.get("per", 0)
        pbr = d.get("pbr", 0)
        if sector:
            base += f"  [{sector}]"
        if mcap:
            base += f"  시총{mcap:,}억"
        if per:
            base += f"  PER={per:.1f}"
        if pbr:
            base += f"  PBR={pbr:.2f}"
        base += (
            f"\n  현재가 {int(d.get('price', 0)):,}원  "
            f"전일대비 {d.get('chg_pct', 0):+.2f}%  "
            f"MA5={int(d.get('ma5') or 0):,}  MA20={int(d.get('ma20') or 0):,}  "
            f"ATR14={int(d.get('atr14') or 0):,}  RSI={d.get('rsi14', '-')}  "
            f"거래량={d.get('last_volume', 0):,}"
        )
        nxt_gap = d.get("nxt_gap_pct")
        nxt_amt = d.get("nxt_trade_amount_bn")
        if nxt_gap is not None:
            base += f"  [NXT 갭 {nxt_gap:+.2f}% 거래대금 {nxt_amt or 0:.1f}억]"
        lines.append(base)
    return "\n".join(lines)
