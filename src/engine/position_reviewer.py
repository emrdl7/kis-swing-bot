"""일일 보유 포지션 재평가 엔진.

매일 1회 (기본 11:00) 실행 — 마이너스 보유 종목만 묶어서 LLM 1회 호출로 일괄 평가.

판정 기준:
- 진입 당시 재료(rationale/tags)가 여전히 유효한가? (뉴스/테마 변화)
- 기술적 지표가 추세 훼손을 시사하는가? (MA/RSI/거래량/지지선)

판정 결과(SELL/HOLD + conviction)는 SwingPosition에 기록되며,
monitor가 다음 사이클에서 review_decision==SELL 종목을 시장가 매도한다.

세이프티: 하루 SELL 판정 max_sells_per_day(기본 3)개로 상한.
"""
from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.core import state_store
from src.core.config import PositionReviewConfig
from src.core.models import SwingPosition, PositionState
from src.agents.base_agent import extract_json
from src.agents.llm_client import LLMClient

log = logging.getLogger(__name__)


@dataclass
class ReviewVerdict:
    symbol: str
    name: str
    decision: str          # "SELL" | "HOLD"
    conviction: float      # 0.0~1.0
    rationale: str
    pnl_pct: float = 0.0


@dataclass
class ReviewReport:
    """run() 결과 — 호출자가 알림/로그에 활용."""
    decided_at: datetime
    evaluated: list[ReviewVerdict] = field(default_factory=list)
    sell_flagged: list[ReviewVerdict] = field(default_factory=list)  # 실제로 SELL 플래그 세팅된 종목
    skipped_reason: str = ""                                          # 비어있으면 정상


class PositionReviewer:
    def __init__(
        self,
        cfg: PositionReviewConfig,
        llm: LLMClient,
        price_fetcher,                  # callable(symbols) -> dict[str, dict]
        news_fetcher=None,              # callable() -> str (LLM-formatted)
    ):
        self.cfg = cfg
        self.llm = llm
        self.price_fetcher = price_fetcher
        self.news_fetcher = news_fetcher

    # ── 공개 API ───────────────────────────────────────────────────────────

    def run(self, now: Optional[datetime] = None) -> ReviewReport:
        """전체 흐름: 대상 필터 → 컨텍스트 수집 → LLM 1회 호출 → 판정 기록."""
        now = now or datetime.now()
        report = ReviewReport(decided_at=now)

        positions_raw = state_store.load_positions()
        positions = [SwingPosition.from_dict(d) for d in positions_raw]

        targets = self._filter_targets(positions, now)
        if not targets:
            report.skipped_reason = "재평가 대상 포지션 없음"
            log.info("[review] %s", report.skipped_reason)
            return report

        log.info("[review] 대상 %d개: %s",
                 len(targets), [f"{p.name}({p.symbol})" for p in targets])

        # 시세·지표 일괄 조회
        symbols = [p.symbol for p in targets]
        try:
            price_ctx = self.price_fetcher(symbols) or {}
        except Exception as e:
            report.skipped_reason = f"가격 조회 실패: {e}"
            log.error("[review] %s", report.skipped_reason)
            return report

        # 뉴스 (재료 소멸 체크용)
        news_text = ""
        if self.news_fetcher:
            try:
                news_text = self.news_fetcher() or ""
            except Exception as e:
                log.warning("[review] 뉴스 수집 실패 (무시): %s", e)

        # LLM 호출 (배치 1회)
        verdicts = self._evaluate_batch(targets, price_ctx, news_text, now)
        if not verdicts:
            report.skipped_reason = "LLM 판정 결과 없음 (파싱 실패 또는 빈 응답)"
            log.warning("[review] %s — 전체 HOLD로 폴백", report.skipped_reason)
            return report

        report.evaluated = verdicts

        # 판정을 position에 기록 + SELL 플래그 세팅 (max_sells_per_day 상한)
        sell_candidates = [
            v for v in verdicts
            if v.decision == "SELL" and v.conviction >= self.cfg.sell_conviction_threshold
        ]
        sell_candidates.sort(key=lambda v: v.conviction, reverse=True)
        sell_capped = sell_candidates[: self.cfg.max_sells_per_day]
        if len(sell_candidates) > len(sell_capped):
            log.warning(
                "[review] SELL 판정 %d개 중 상한 %d개만 적용 (세이프티)",
                len(sell_candidates), self.cfg.max_sells_per_day,
            )

        sell_set = {v.symbol for v in sell_capped}
        sym_to_verdict = {v.symbol: v for v in verdicts}
        changed = False
        for pos in positions:
            if pos.symbol not in sym_to_verdict:
                continue
            v = sym_to_verdict[pos.symbol]
            pos.review_decided_at = now
            pos.review_conviction = v.conviction
            pos.review_rationale = v.rationale
            if pos.symbol in sell_set:
                pos.review_decision = "SELL"
                report.sell_flagged.append(v)
            else:
                # 명시적으로 HOLD 기록 (SELL인데 conviction 미달인 경우 포함)
                pos.review_decision = "HOLD"
            changed = True

        if changed:
            state_store.save_positions([p.to_dict() for p in positions])

        # 판정 기록 영속 저장
        self._append_log(verdicts, sell_set, now)

        log.info(
            "[review] 평가 %d → SELL 플래그 %d (상한 %d, 임계 %.2f)",
            len(verdicts), len(report.sell_flagged),
            self.cfg.max_sells_per_day, self.cfg.sell_conviction_threshold,
        )
        return report

    # ── 내부 로직 ──────────────────────────────────────────────────────────

    def _filter_targets(self, positions: list[SwingPosition], now: datetime) -> list[SwingPosition]:
        """재평가 대상 추리기 — 마이너스 + 보유 N일 이상 + 미청산."""
        targets = []
        for p in positions:
            if p.state == PositionState.CLOSED:
                continue
            # NXT 미체결 대기 중이면 skip
            if p.order_id and str(p.order_id).startswith("NXT:"):
                continue
            # manual 종목은 재평가 대상 아님
            if (p.strategy or "swing") == "manual":
                continue
            # 보유 일수 확인
            if p.entry_time:
                holding_days = (now - p.entry_time).total_seconds() / 86400
                if holding_days < self.cfg.min_holding_days:
                    continue
            targets.append(p)
        return targets

    def _evaluate_batch(
        self,
        targets: list[SwingPosition],
        price_ctx: dict[str, dict],
        news_text: str,
        now: datetime,
    ) -> list[ReviewVerdict]:
        """배치 1회 LLM 호출 → 종목별 판정."""
        # 시세·지표가 없으면 평가 불가 종목은 자동 HOLD 처리
        targets_with_price = []
        no_price_verdicts: list[ReviewVerdict] = []
        for p in targets:
            d = price_ctx.get(p.symbol)
            if not d or not d.get("price"):
                log.warning("[review] %s 시세 없음 → 자동 HOLD", p.symbol)
                no_price_verdicts.append(ReviewVerdict(
                    symbol=p.symbol, name=p.name, decision="HOLD",
                    conviction=0.0, rationale="시세 조회 실패로 자동 HOLD",
                    pnl_pct=0.0,
                ))
                continue
            targets_with_price.append(p)

        if not targets_with_price:
            return no_price_verdicts

        positions_block = self._format_positions_block(targets_with_price, price_ctx)

        system = """당신은 한국 주식 중장기 스윙 트레이딩 전문 심판관(Position Reviewer)입니다.
이미 매수한 마이너스 보유 종목이 "여전히 보유 가치가 있는지" 재평가합니다.

판정 원칙:
1. 진입 당시 근거(재료/테마)가 여전히 유효한지 — 뉴스로 확인
   - 재료 소멸·악재 부상·실적 악화 시 SELL 우선
2. 기술적 지표가 추세 훼손을 시사하는지
   - MA20/MA60 이탈, RSI 과매도 무반등, 거래량 위축, 지지선 붕괴 → SELL 우선
3. 단순 단기 노이즈로 인한 마이너스(횡보·작은 조정)는 HOLD
4. 중장기 관점이므로 -2~-4% 수준의 일시 조정은 인내. 추세 자체가 꺾인 경우만 SELL.
5. conviction은 0.0~1.0 (1.0이 가장 강한 확신).
   - 0.7 이상: 명확한 추세 훼손/재료 소멸 — SELL 강하게 권고
   - 0.5~0.7: 우려스럽지만 결정적 신호 부족 — SELL이어도 약한 신호
   - 0.5 미만: HOLD 또는 의미 없는 차이

반드시 JSON 배열만 응답하십시오. 설명 텍스트 금지."""

        from src.core.clock import today_label as _today_label
        _label = _today_label(now)
        user_msg = f"""[{_label} {now.strftime('%H:%M')}] 마이너스 보유 종목 재평가

=== 보유 포지션 + 진입 근거 + 현재 시세/지표 ===
{positions_block}

=== 최근 시장 뉴스 (재료 소멸/악재 부상 확인용) ===
{news_text[:2500] if news_text else '뉴스 없음'}

위 종목 각각에 대해 보유 지속 여부를 판정하십시오.
출력 형식 (JSON 배열, 입력된 종목 수만큼):
[
  {{
    "symbol": "종목코드",
    "name": "종목명",
    "decision": "SELL" 또는 "HOLD",
    "conviction": 0.0~1.0,
    "rationale": "판정 근거 (1-2문장, 재료/지표 중 어떤 것이 결정적인지 명시)"
  }}
]"""

        log.info("[review] LLM 호출 (대상 %d개)", len(targets_with_price))
        raw = self.llm.chat(system, user_msg)
        data = extract_json(raw)
        if data is None:
            log.warning("[review] 파싱 실패 (1차) — 재시도")
            raw = self.llm.chat(system, user_msg + "\n\n⚠️ 반드시 JSON 배열만 출력. 다른 텍스트 금지.")
            data = extract_json(raw)
        if data is None:
            log.error("[review] 파싱 실패 (2차):\n%s", (raw or "")[:300])
            return no_price_verdicts
        if isinstance(data, dict):
            data = [data]

        # 종목 ↔ 포지션 매핑
        sym_to_pos = {p.symbol: p for p in targets_with_price}

        verdicts: list[ReviewVerdict] = list(no_price_verdicts)
        seen = set()
        for item in data:
            try:
                sym = str(item.get("symbol", "")).strip()
                if not sym or sym in seen:
                    continue
                pos = sym_to_pos.get(sym)
                if pos is None:
                    log.warning("[review] LLM이 대상 외 종목 %s 응답 — 무시", sym)
                    continue
                seen.add(sym)
                decision_raw = str(item.get("decision", "HOLD")).strip().upper()
                decision = "SELL" if decision_raw == "SELL" else "HOLD"
                conviction = float(item.get("conviction", 0.5))
                conviction = max(0.0, min(1.0, conviction))
                rationale = str(item.get("rationale", "")).strip()
                cur = price_ctx.get(sym, {}).get("price", 0)
                pnl = pos.pnl_pct(cur) if cur else 0.0
                verdicts.append(ReviewVerdict(
                    symbol=sym,
                    name=pos.name,
                    decision=decision,
                    conviction=conviction,
                    rationale=rationale,
                    pnl_pct=pnl,
                ))
            except Exception as e:
                log.warning("[review] 항목 파싱 오류: %s | %s", e, item)

        # 누락된 대상은 자동 HOLD (LLM이 빠뜨린 경우)
        for p in targets_with_price:
            if p.symbol not in seen:
                log.warning("[review] LLM 응답에서 %s 누락 → 자동 HOLD", p.symbol)
                cur = price_ctx.get(p.symbol, {}).get("price", 0)
                verdicts.append(ReviewVerdict(
                    symbol=p.symbol, name=p.name, decision="HOLD",
                    conviction=0.0, rationale="LLM 응답 누락 — 자동 HOLD",
                    pnl_pct=p.pnl_pct(cur) if cur else 0.0,
                ))

        return verdicts

    def _format_positions_block(
        self,
        targets: list[SwingPosition],
        price_ctx: dict[str, dict],
    ) -> str:
        """LLM에 입력할 포지션 + 시세 블록 포맷."""
        lines = []
        for p in targets:
            d = price_ctx.get(p.symbol, {}) or {}
            cur = float(d.get("price") or 0)
            pnl = p.pnl_pct(cur) if cur else 0.0
            tags_str = ", ".join(p.tags or []) or "-"
            sector = d.get("sector") or "-"
            mcap = d.get("market_cap_bn") or 0
            ma5 = int(d.get("ma5") or 0)
            ma20 = int(d.get("ma20") or 0)
            ma60 = int(d.get("ma60") or 0)
            rsi = d.get("rsi14")
            atr = int(d.get("atr14") or 0)
            vol = int(d.get("last_volume") or 0)
            vol20 = int(d.get("volume_avg20") or 0)
            vol_ratio = (vol / vol20) if vol20 > 0 else 0.0
            sr = d.get("support_resistance") or {}
            sup = sr.get("support") or []
            res = sr.get("resistance") or []
            holding_days = (datetime.now() - p.entry_time).total_seconds() / 86400 if p.entry_time else 0

            rationale = (p.rationale or "").strip() or "(진입 근거 없음)"
            lines.append(
                f"### {p.name}({p.symbol})  [{sector}]  시총 {mcap:,}억\n"
                f"- 매수가 {int(p.avg_price):,}원  현재가 {int(cur):,}원  PnL {pnl:+.2f}%  "
                f"보유 {holding_days:.1f}일  수량 {p.qty}주\n"
                f"- 진입 태그: {tags_str}\n"
                f"- 진입 근거: {rationale[:300]}\n"
                f"- MA5={ma5:,}  MA20={ma20:,}  MA60={ma60:,}  RSI14={rsi if rsi is not None else '-'}  "
                f"ATR14={atr:,}\n"
                f"- 거래량 {vol:,} (20일평균 {vol20:,}, 비율 {vol_ratio:.2f})\n"
                f"- 지지: {', '.join(f'{int(s):,}' for s in sup[:3]) or '-'}  "
                f"저항: {', '.join(f'{int(r):,}' for r in res[:3]) or '-'}\n"
                f"- 목표가 {int(p.target_price):,}원  손절가 {int(p.stop_price):,}원"
            )
        return "\n\n".join(lines)

    def _append_log(self, verdicts: list[ReviewVerdict], sell_set: set[str], now: datetime) -> None:
        """판정 결과를 영속 로그에 누적."""
        try:
            existing = state_store.load_position_review_log()
        except Exception:
            existing = []
        date_str = now.strftime("%Y-%m-%d")
        decided_at = now.isoformat(timespec="seconds")
        for v in verdicts:
            existing.append({
                "date": date_str,
                "decided_at": decided_at,
                "symbol": v.symbol,
                "name": v.name,
                "pnl_pct": round(v.pnl_pct, 2),
                "decision": v.decision,
                "conviction": round(v.conviction, 3),
                "applied_sell": v.symbol in sell_set,
                "rationale": v.rationale,
            })
        # 최근 200건만 유지 (무한 누적 방지)
        if len(existing) > 200:
            existing = existing[-200:]
        try:
            state_store.save_position_review_log(existing)
        except Exception as e:
            log.warning("[review] 로그 저장 실패 (무시): %s", e)
