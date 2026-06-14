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
from src.data.market_context import build_market_signal

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
        self._market_signal = build_market_signal(None)

    # ── 공개 API ──────────────────────────────────────────────────────────

    def run(self, context: dict) -> tuple[list[SwingCandidate], str, list[SwingCandidate]]:
        """토론 실행 → (candidates, transcript, reserves)."""
        self._transcript = []
        self._budget_text = context.get("budget_text", "")
        self._quant_text = context.get("quant_text", "")
        self._market_context = context.get("market_context", "")
        self._market_signal = context.get("market_signal") or build_market_signal(None)
        from src.core.clock import today_label as _today_label
        today = context.get("today") or _today_label()
        self._log(f"# {today} 스윙봇 종목발굴 토론 보고서\n")
        self._log(
            "## 시장 데이터 품질 정책\n"
            f"- 품질점수: {self._market_signal.get('quality_score')} / "
            f"엄격도: {self._market_signal.get('strictness')}\n"
            f"- 최소 신뢰도 보정: +{self._market_signal.get('min_consensus_boost', 0):.2f}, "
            f"거래대금 기준 배수: x{self._market_signal.get('trade_amount_multiplier', 1):.1f}\n"
        )

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
            min_trade = self.screening_cfg.min_trade_amount * float(self._market_signal.get("trade_amount_multiplier", 1.0) or 1.0)
            filtered_out = []
            for sym in list(price_ctx.keys()):
                d = price_ctx[sym]
                mcap = d.get("market_cap_bn", 0)
                tr_amt = int(d.get("acml_tr_pbmn", 0) or 0)
                risk_reason = _disqualifying_risk_reason(d)
                if mcap and mcap < min_cap:
                    filtered_out.append(f"{d.get('name', sym)}({sym}) 시총 {mcap:,}억 < {min_cap:,}억")
                    del price_ctx[sym]
                elif tr_amt <= 0:
                    filtered_out.append(f"{d.get('name', sym)}({sym}) 거래대금 확인 불가")
                    del price_ctx[sym]
                elif tr_amt < min_trade:
                    filtered_out.append(f"{d.get('name', sym)}({sym}) 거래대금 {tr_amt/1e8:.0f}억 < {min_trade/1e8:.0f}억")
                    del price_ctx[sym]
                elif risk_reason:
                    filtered_out.append(f"{d.get('name', sym)}({sym}) 과열/붕괴 패턴: {risk_reason}")
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
                    f"- 거래량: {int(d.get('last_volume') or 0):,}  (20일평균: {int(d.get('volume_avg20') or 0):,})\n"
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
        debate_results = self._post_filter_results(debate_results, price_ctx)

        if not debate_results:
            log.info("모더레이터 0개 반환 — 품질 기준 미달, 선정 없음")
            self._log("⚠️ 모더레이터 선정 없음 — 품질 기준 미달로 후보 없음\n")

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
        cap = self._market_signal.get("max_candidates_cap")
        if isinstance(cap, int) and cap > 0:
            max_primary = min(max_primary, cap)
        all_candidates = self._to_candidates(debate_results, price_ctx, all_opinions)
        candidates = all_candidates[:max_primary]
        reserves = all_candidates[max_primary:]
        log.info("최종 후보: %d개 (예비: %d개)", len(candidates), len(reserves))

        # watchlist 보강용: 모더레이터 거부한 Round 1 통과 종목 풀까지 노출.
        # candidates+reserves 외 종목 = "후보로는 약하지만 monitor 피봇 게이트로 검증할 만한 후보"
        existing_symbols = {c.symbol for c in all_candidates}
        self.last_prelim_pool = self._build_prelim_pool(all_opinions, price_ctx, existing_symbols)
        log.info("Round 1 추가 풀: %d개 (모더레이터 미통과)", len(self.last_prelim_pool))

        return candidates, "\n".join(self._transcript), reserves

    def _build_prelim_pool(
        self,
        all_opinions: dict[str, list[AgentOpinion]],
        price_ctx: dict[str, dict],
        existing_symbols: set[str],
    ) -> list[SwingCandidate]:
        """매수 에이전트 의견 중 모더레이터에서 거부된 종목을 watchlist용 SwingCandidate로 변환.

        - risk_agent 제외
        - 같은 symbol에 여러 매수 에이전트 의견 있으면 conviction 가장 높은 것 1개
        - consensus_score = conviction × 0.7 (단독 보정 — 모더레이터 정식 통과는 못 했지만 watchlist에 유보)
        - existing_symbols에 이미 있는 종목 제외
        """
        best: dict[str, AgentOpinion] = {}
        for agent_name, ops in all_opinions.items():
            if agent_name == "risk_agent":
                continue
            for op in ops:
                if not op.symbol or op.symbol in existing_symbols:
                    continue
                if op.entry_low <= 0 or op.entry_high <= 0:
                    continue
                cur = best.get(op.symbol)
                if cur is None or op.conviction > cur.conviction:
                    best[op.symbol] = op

        # risk 강한 반대(0.75 이상)는 watchlist에서도 제외 → 모더레이터와 일관
        risk_high: dict[str, float] = {}
        for op in all_opinions.get("risk_agent", []):
            if op.symbol and op.conviction >= 0.75:
                risk_high[op.symbol] = op.conviction

        from datetime import timedelta as _td
        expiry_days = self.screening_cfg.entry_expiry_days
        now = datetime.now()
        pool: list[SwingCandidate] = []
        min_score = self.screening_cfg.min_entry_consensus_score + float(
            self._market_signal.get("min_consensus_boost", 0.0) or 0.0
        )
        min_trade_amount = self.screening_cfg.min_trade_amount * float(
            self._market_signal.get("trade_amount_multiplier", 1.0) or 1.0
        )
        for sym, op in best.items():
            if sym in risk_high:
                continue
            d = price_ctx.get(sym, {}) or {}
            score = round(min(0.7, op.conviction * 0.7), 3)  # 단독 보정 + 상한 0.7
            if score < min_score:
                continue
            trade_amt = int(d.get("acml_tr_pbmn", 0) or 0)
            if trade_amt <= 0 or trade_amt < min_trade_amount:
                continue
            if _disqualifying_risk_reason(d):
                continue
            pool.append(SwingCandidate(
                symbol=sym,
                name=d.get("name") or op.name or sym,
                entry_low=op.entry_low,
                entry_high=op.entry_high,
                target_price=op.target_price,
                stop_price=op.stop_price,
                consensus_score=score,
                rationale=op.rationale,
                tags=list(op.tags or []),
                discovered_at=now,
                expires_at=now + _td(days=expiry_days),
                prev_close=d.get("prev_close"),
            ))
        pool.sort(key=lambda c: c.consensus_score, reverse=True)
        return pool

    # ── Round 0: 종목만 추천 ────────────────────────────────────────────

    def _round0_analyze(self, agent: BaseAgent, context: dict) -> list[dict]:
        """가격 없이 종목명/코드/근거만 추천."""
        news_text = context.get("news_text", "")
        dart_text = context.get("dart_text", "")
        market_context = context.get("market_context", "")
        today = context.get("today", "")

        budget_text = context.get("budget_text", "")

        perf_text = context.get("perf_text", "")

        quant_block = f"""
=== 정량 1차 후보군 ===
{self._quant_text}
""" if self._quant_text else ""

        user_msg = f"""[{today}] 오늘 뉴스와 공시를 분석하여 스윙 트레이딩 유망 종목을 추천하십시오.
이 단계에서는 종목명과 추천 이유만 작성하고, 가격은 입력하지 마십시오.
정량 1차 후보군이 제공된 경우, 원칙적으로 그 목록 안에서만 고르십시오.
목록 밖 종목은 뉴스/공시 직접 수혜가 명확하고 유동성 조건을 만족할 때만 예외로 추천하십시오.
{budget_text}
{perf_text}

{quant_block}

=== 시장흐름 분석 ===
{market_context or "시장흐름 분석 없음"}

=== 오늘 뉴스 ===
{news_text[:1500]}

=== DART 공시 ===
{dart_text[:800]}

추천 이유에는 반드시 시장흐름 분석과의 연결 또는 시장흐름과 다른 예외 사유를 포함하십시오.

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
        market_context = getattr(self, "_market_context", "")

        # 리스크 에이전트는 다른 에이전트 추천을 평가하는 별도 프롬프트 사용
        if agent.name == "risk_agent":
            user_msg = f"""아래 실시간 주가 데이터, 정량 후보군, 다른 에이전트들의 추천 종목을 분석하여 리스크를 평가하십시오.

=== 실시간 주가 데이터 ===
{price_text}

=== 정량 1차 후보군 ===
{self._quant_text or "없음"}

=== 시장흐름 분석 ===
{market_context or "시장흐름 분석 없음"}

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
            user_msg = f"""아래 실시간 주가 데이터와 정량 후보군을 참고하여, 당신이 추천한 종목의 진입가/목표가/손절가를 설정하십시오.
반드시 현재가를 기준으로 현실적인 가격을 설정하고, 현재가에서 크게 벗어난 수치는 절대 사용하지 마십시오.

=== 실시간 주가 데이터 ===
{price_text}

=== 정량 1차 후보군 ===
{self._quant_text or "없음"}

=== 시장흐름 분석 ===
{market_context or "시장흐름 분석 없음"}

=== 모든 에이전트 Round0 추천 ===
{all_prelim_text}

=== 내 Round0 추천 ===
{my_text if my_text else "없음"}

진입 기준 (중기 보유 1~2주):
- 진입 구간: 현재가 ±0.5% 이내 (타이트한 진입)
- 목표가: 현재가 대비 +8~15% (ATR14 기준 + MA60/MA120 다음 저항선)
- 손절가: 현재가 대비 -7% (ATR14의 2~3배 아래, 주요 지지선 고려)
- MA60/MA120 상향 배열 또는 돌파 초기 종목 우선
- 다른 에이전트가 추천한 종목 중 동의하는 것도 포함 가능
- 시장흐름 분석의 우선 섹터/선정 바이어스와 맞는지 rationale에 명시
- 시장흐름과 맞지 않는 종목은 개별 뉴스·공시·기술적 근거가 압도적일 때만 포함

우량주 필터 (반드시 준수):
- 시가총액 5,000억 이상 대형·중견 우량주만 추천
- PER 음수(적자 기업) 종목은 추천 금지
- 현재가가 눌림목(지지선 근처, 이동평균선 위 반등) 구간인지 반드시 확인
- 추격매수(최근 급등 후 고점 부근) 종목 추천 금지

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

        system = """당신은 한국 주식 중기 스윙 트레이딩 전문 심판관(Moderator)입니다. (1~2주 보유 전략)
여러 분석가의 의견을 종합하여 최종 투자 종목을 선정하십시오.

분석가 구성:
- 뉴스/테마/기술적 에이전트: 매수 추천 관점
- 리스크 에이전트(risk_agent): 매수 반대 관점 — conviction이 높을수록 해당 종목 리스크가 큼

선정 기준 (우선순위):
1. 매수 에이전트 동의 — 다음 중 하나 충족:
   (a) 2인 이상 동의
   (b) 1인 단독이지만 해당 에이전트 conviction ≥ 0.6 + 리스크 에이전트의 명시적 강한 반대(conviction ≥ 0.6) 없음
2. 매수 에이전트 평균 conviction 0.55 이상 우선
3. 리스크 에이전트의 conviction이 0.75 이상인 종목은 선정 제외
4. consensus_score 0.35 미만 종목은 선정 금지
5. 최대 8개 종목을 신뢰도 순으로 선정 (상위 5개 정규 후보, 나머지 예비 후보)
6. 진입가/목표가/손절가는 반드시 실시간 현재가 기준으로 현실적인 값 사용
7. 동일 섹터 종목이 많으면 가장 유망한 1~2개 선택
8. 중기 관점: MA60/MA120 상향 배열, 주봉 지지선 반등, 섹터 사이클 상승 구간 종목 우선
9. 우량주 필터: 시가총액 5,000억 이상, PER 양수(흑자 기업)만 선정
10. 눌림목 진입 원칙: 현재가가 지지선·이동평균선 근처 눌림 구간이어야 함 — 고점 추격 금지
11. 시장흐름 분석 반영: 우선 섹터/선정 바이어스와 맞는 종목을 우선하고, 맞지 않는 종목은 final_rationale에 예외 근거를 명시
12. 시장흐름 분석의 data_gaps가 크면 거시/테마 단정을 낮게 반영하고 정량·개별 뉴스 근거를 더 중시
13. 데이터 품질 정책: 아래 정책의 최소 신뢰도 보정과 거래대금 배수를 실제 선정 기준에 반영

consensus_score 계산 가이드:
- 2인 이상 동의: (동의 수 / 전체 매수 에이전트 수) × 평균 conviction
- 1인 단독 동의: 해당 conviction × 0.7 (단독 보정)
- 리스크 에이전트 conviction 0.5 이상이면 위 점수에서 0.1~0.2 차감

중요: 위 기준 충족 종목이 정말로 없으면 빈 배열 []을 반환. 단, 진입 가능한 종목이 있는데 억지로 보수적으로 0개 반환하는 것도 봇의 존재 이유에 반함 — 1번 기준 (a) 또는 (b) 충족 종목은 적극 선정하라.

반드시 JSON 배열 형식으로만 응답하십시오. JSON 외 텍스트를 포함하지 마십시오."""

        user_msg = f"""[{today}] 최종 종목 선정
{self._budget_text}

=== 시장흐름 분석 ===
{self._market_context or "시장흐름 분석 없음"}

=== 데이터 품질 기반 선정 정책 ===
품질점수: {self._market_signal.get('quality_score')} / 엄격도: {self._market_signal.get('strictness')}
최소 consensus_score 보정: +{self._market_signal.get('min_consensus_boost', 0):.2f}
거래대금 기준 배수: x{self._market_signal.get('trade_amount_multiplier', 1):.1f}
주의사항: {'; '.join(self._market_signal.get('notes') or []) or '없음'}

=== 정량 1차 후보군 ===
{self._quant_text or "없음"}

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

    def _post_filter_results(
        self,
        results: list[DebateResult],
        price_ctx: dict[str, dict],
    ) -> list[DebateResult]:
        """LLM 모더레이터 결과를 코드 레벨에서 한 번 더 검증한다."""
        filtered: list[DebateResult] = []
        min_score = self.screening_cfg.min_entry_consensus_score + float(
            self._market_signal.get("min_consensus_boost", 0.0) or 0.0
        )
        min_trade_amount = self.screening_cfg.min_trade_amount * float(
            self._market_signal.get("trade_amount_multiplier", 1.0) or 1.0
        )
        sector_gate_active = (
            int(self._market_signal.get("quality_score", 0) or 0) >= 55
            and (
                bool(self._market_signal.get("leading_sectors"))
                or bool(self._market_signal.get("sector_biases"))
            )
        )
        for r in results:
            d = price_ctx.get(r.symbol, {}) or {}
            cur = float(d.get("price", 0) or 0)
            if cur <= 0:
                log.warning("[검증제외] %s 현재가 없음", r.symbol)
                continue

            if r.consensus_score < min_score:
                log.warning(
                    "[검증제외] %s 신뢰도 부족 %.2f < %.2f (시장 데이터 엄격도=%s)",
                    r.symbol, r.consensus_score, min_score, self._market_signal.get("strictness"),
                )
                continue

            mcap = int(d.get("market_cap_bn", 0) or 0)
            if mcap and mcap < self.screening_cfg.min_market_cap_bn:
                log.warning("[검증제외] %s 시총 부족 %s억", r.symbol, f"{mcap:,}")
                continue

            trade_amt = int(d.get("acml_tr_pbmn", 0) or 0)
            if trade_amt <= 0:
                log.warning("[검증제외] %s 거래대금 확인 불가", r.symbol)
                continue
            if trade_amt < min_trade_amount:
                log.warning(
                    "[검증제외] %s 거래대금 부족 %.0f억 < %.0f억 (시장 데이터 엄격도=%s)",
                    r.symbol, trade_amt / 1e8, min_trade_amount / 1e8,
                    self._market_signal.get("strictness"),
                )
                continue

            risk_reason = _disqualifying_risk_reason(d)
            if risk_reason:
                log.warning("[검증제외] %s 과열/붕괴 패턴: %s", r.symbol, risk_reason)
                continue

            if sector_gate_active and not self._is_market_aligned(r, d):
                required = min_score + 0.10
                if r.consensus_score < required:
                    log.warning(
                        "[검증제외] %s 시장 주도 근거 미약 score %.2f < 예외 %.2f",
                        r.symbol, r.consensus_score, required,
                    )
                    continue

            per = float(d.get("per", 0) or 0)
            eps = float(d.get("eps", 0) or 0)
            if per < 0 or eps < 0:
                log.warning("[검증제외] %s 적자/PER 음수 per=%.1f eps=%.0f", r.symbol, per, eps)
                continue

            rsi = d.get("rsi14")
            if rsi is not None and float(rsi) > 75:
                log.warning("[검증제외] %s RSI 과열 %.1f", r.symbol, float(rsi))
                continue

            ma20 = d.get("ma20")
            ma60 = d.get("ma60")
            if ma20 and ma60 and float(ma20) < float(ma60) * 0.97:
                log.warning("[검증제외] %s 중기 추세 약함 MA20 %.0f < MA60 %.0f", r.symbol, ma20, ma60)
                continue

            if r.entry_low <= 0 or r.entry_high <= 0 or r.entry_low > r.entry_high:
                log.warning("[검증제외] %s 진입가 비정상 %.0f~%.0f", r.symbol, r.entry_low, r.entry_high)
                continue
            if r.entry_high > cur * 1.08 or r.entry_low < cur * 0.90:
                log.warning(
                    "[검증제외] %s 진입대가 현재가와 과도하게 이탈 현재 %.0f 진입 %.0f~%.0f",
                    r.symbol, cur, r.entry_low, r.entry_high,
                )
                continue
            if r.target_price <= r.entry_high * 1.04:
                log.warning("[검증제외] %s 기대수익 부족 target %.0f entry_high %.0f", r.symbol, r.target_price, r.entry_high)
                continue
            if r.stop_price >= r.entry_low:
                log.warning("[검증제외] %s 손절가 비정상 stop %.0f entry_low %.0f", r.symbol, r.stop_price, r.entry_low)
                continue

            risk = r.entry_high - r.stop_price
            reward = r.target_price - r.entry_high
            if risk > 0 and reward / risk < 1.0:
                log.warning("[검증제외] %s 손익비 부족 R/R %.2f", r.symbol, reward / risk)
                continue

            filtered.append(r)
        return filtered

    def _is_market_aligned(self, result: DebateResult, price_data: dict) -> bool:
        """시장흐름 분석의 섹터/키워드와 후보 설명이 맞물리는지 보수적으로 확인."""
        haystack = " ".join(
            str(x or "")
            for x in (
                result.symbol,
                result.name,
                result.final_rationale,
                " ".join(result.tags or []),
                price_data.get("sector"),
            )
        ).lower()
        tokens: list[str] = []
        for sector in self._market_signal.get("leading_sectors") or []:
            if not isinstance(sector, dict):
                continue
            tokens.append(str(sector.get("name") or ""))
            tokens.extend(str(x) for x in (sector.get("symbols") or []))
        for bias in self._market_signal.get("sector_biases") or []:
            if not isinstance(bias, dict):
                continue
            tokens.append(str(bias.get("name") or ""))
            tokens.extend(str(x) for x in (bias.get("domestic_keywords") or []))
        tokens.extend(str(x) for x in (self._market_signal.get("watch_keywords") or []))
        clean_tokens = [t.lower() for t in tokens if len(t.strip()) >= 2]
        return any(t in haystack for t in clean_tokens)

    # ── 변환 ────────────────────────────────────────────────────────────

    def _to_candidates(
        self,
        results: list[DebateResult],
        price_ctx: dict[str, dict] | None = None,
        all_opinions: dict[str, list[AgentOpinion]] | None = None,
    ) -> list[SwingCandidate]:
        expiry_days = self.screening_cfg.entry_expiry_days
        now = datetime.now()
        price_ctx = price_ctx or {}
        candidates = []
        for r in results:
            d = price_ctx.get(r.symbol, {})
            # 종목별 에이전트 의견 수집
            opinions_for_symbol = []
            if all_opinions:
                for agent_name, ops in all_opinions.items():
                    for op in ops:
                        if op.symbol == r.symbol:
                            opinions_for_symbol.append({
                                "agent_name": agent_name,
                                "label": _AGENT_LABEL.get(agent_name, agent_name),
                                "conviction": op.conviction,
                                "rationale": op.rationale,
                                "role": "risk" if agent_name == "risk_agent" else "buy",
                            })
            verified_name = d.get("name") or r.name
            candidates.append(SwingCandidate(
                symbol=r.symbol,
                name=verified_name,
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
                agent_opinions=opinions_for_symbol or None,
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
        from src.data.market_context import build_market_signal, format_market_context_for_llm
        from src.core import state_store
        from src.core.clock import today_label

        today = today_label()
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
        market_summary = state_store.load_market_summary() or {}
        market_context = format_market_context_for_llm(market_summary)
        self._market_signal = build_market_signal(market_summary)

        system = """당신은 한국 주식 스윙 트레이딩 전문 심판관(Moderator)입니다.
전날 저녁에 선정한 초벌 후보 종목들을 밤사이 변화를 반영하여 재평가하십시오.

판정 기준:
1. 초벌 후보 중 악재·갭다운으로 당일 진입 부적합한 종목은 제외
2. 과도한 갭업(+5% 이상)은 consensus_score 감점 또는 제외
3. 초벌에 없던 종목을 새로 추가하지 말 것 (유니버스 확장 금지)
4. 미국 시장 하락(-1.5% 이상)이면 전반적 리스크 가중
5. 시장흐름 분석의 우선 섹터/회피 조건을 반영하고, 맞지 않는 후보는 재평가 사유에 예외 근거를 명시
6. 데이터 품질 정책의 최소 신뢰도 보정과 후보 수 제한을 반영
7. 선정된 종목은 기존 스키마와 동일한 형식으로 출력

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

[시장흐름 분석]
{market_context}

[데이터 품질 기반 선정 정책]
품질점수: {self._market_signal.get('quality_score')} / 엄격도: {self._market_signal.get('strictness')}
최소 consensus_score 보정: +{self._market_signal.get('min_consensus_boost', 0):.2f}
후보 수 상한: {self._market_signal.get('max_candidates_cap') or '기본값'}
주의사항: {'; '.join(self._market_signal.get('notes') or []) or '없음'}

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

        min_score = self.screening_cfg.min_entry_consensus_score + float(
            self._market_signal.get("min_consensus_boost", 0.0) or 0.0
        )
        before_filter = len(results)
        results = [r for r in results if r.consensus_score >= min_score]
        if before_filter != len(results):
            log.info("[재평가] 시장 데이터 엄격도 반영: 신뢰도 미달 %d개 제외", before_filter - len(results))
        cap = self._market_signal.get("max_candidates_cap")
        if isinstance(cap, int) and cap > 0:
            results = sorted(results, key=lambda r: r.consensus_score, reverse=True)[:cap]
        if not results:
            log.warning("[재평가] 시장 데이터 품질 기준 통과 종목 없음")
            return []

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
            f"거래량={int(d.get('last_volume') or 0):,}"
        )
        nxt_gap = d.get("nxt_gap_pct")
        nxt_amt = d.get("nxt_trade_amount_bn")
        if nxt_gap is not None:
            base += f"  [NXT 갭 {nxt_gap:+.2f}% 거래대금 {nxt_amt or 0:.1f}억]"
        risk_reason = _disqualifying_risk_reason(d)
        if risk_reason:
            base += f"\n  과열/붕괴 주의: {risk_reason}"
        sr = d.get("support_resistance") or {}
        if sr.get("resistance"):
            base += f"\n  저항선: {', '.join(f'{int(r):,}원' for r in sr['resistance'][:3])}"
        if sr.get("support"):
            base += f"\n  지지선: {', '.join(f'{int(s):,}원' for s in sr['support'][:3])}"
        lines.append(base)
    return "\n".join(lines)


def _disqualifying_risk_reason(price_data: dict) -> str:
    flags = price_data.get("risk_flags") or {}
    if not isinstance(flags, dict) or not flags.get("avoid_chasing"):
        return ""
    reasons = flags.get("reasons") or []
    return "; ".join(str(x) for x in reasons[:2]) if reasons else "급등 후 붕괴 패턴"
