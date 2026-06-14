"""장 전 종목 발굴 스크립트 (08:50 실행).

launchd ai.kis.swing.morning.plist 에 의해 매일 08:50 호출됨.
NXT(장전) 시세 기준으로 진입구간 판단 → 09:05 이후 실제 진입 여부 결정.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import os
from datetime import datetime, timedelta

from src.core.config import load_config
from src.core import state_store
from src.core.clock import is_pre_market, today_label
from src.core.models import SwingCandidate, SwingPosition
from src.data.kis_client import KisClient
from src.data.overnight import fetch_overnight_delta
from src.data.dart_client import DartClient
from src.data.news_fetcher import fetch_news, format_for_llm as news_fmt
from src.data.technical import compute_indicators
from src.data.market_context import build_market_signal, format_market_context_for_llm
from src.data.market_overview import get_market_overview, format_market_decision_for_llm
from src.screening.quant_universe import build_quant_universe, format_for_llm as quant_fmt
from src.agents.llm_client import LLMClient
from src.agents.news_agent import NewsAgent
from src.agents.theme_agent import ThemeAgent
from src.agents.technical_agent import TechnicalAgent
from src.agents.debate_engine import DebateEngine
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("morning_screen")


def _attach_live_market_decision(kis: KisClient, market_summary: dict) -> dict:
    """후보 발굴 직전의 KIS 시장 게이트를 시황 요약에 덧붙인다."""
    summary = dict(market_summary or {})
    try:
        overview = get_market_overview(kis, ttl_sec=0)
        decision = overview.get("decision") if isinstance(overview.get("decision"), dict) else {}
        if decision:
            summary["market_decision"] = decision
            summary["market_decision_text"] = format_market_decision_for_llm(overview)
    except Exception as e:
        log.warning("실시간 시장 판단 스냅샷 보강 실패: %s", e)
    return summary


def _build_nxt_text(candidates: list) -> str:
    """NXT 프리장 데이터 요약 (기존 후보 + 참조용).

    에이전트가 '갭상승 중 거래대금 붙은 종목'은 우선순위 ↑,
    '갭하락·거래 공백'은 경계 신호로 활용하도록 유도.
    """
    if not candidates:
        return "NXT 데이터 없음 (기존 후보 없음)"
    lines = ["[NXT 프리장 현황 — 기존 후보 기준]"]
    for c in candidates:
        gap = c.nxt_gap_pct
        amt = c.nxt_trade_amount_bn
        if gap is None:
            lines.append(f"- {c.name}({c.symbol}): NXT 미거래")
            continue
        strength = "강세" if gap >= 2 else ("과열주의" if gap >= 5 else ("약세" if gap <= -1 else "보합"))
        amt_str = f"{amt:.1f}억" if amt is not None else "-"
        lines.append(
            f"- {c.name}({c.symbol}): 갭 {gap:+.2f}% ({strength}), NXT 거래대금 {amt_str}"
        )
    lines.append("")
    lines.append("판단 가이드:")
    lines.append("* NXT 갭 +1~+3% + 거래대금 10억 이상: 당일 모멘텀 기대 → 우선순위↑")
    lines.append("* NXT 갭 +5% 초과: 과열 — 추격 금지, 진입대 상향 조정 또는 제외")
    lines.append("* NXT 갭 -1% 이하: 야간 악재 가능성 — 진입대 하향 또는 제외")
    lines.append("* 거래대금 ≈ 0 (수백만원): NXT 신호 신뢰도 낮음 — 정규장 개장가 재평가")
    return "\n".join(lines)


def make_price_fetcher(kis: KisClient):
    """종목코드 리스트를 받아 현재가 + 기술지표를 반환하는 함수 생성.

    장전(08:00~09:00) 실행 시 NXT(장전) 시세를 반환하며,
    KIS API가 시간대에 따라 자동으로 NXT 가격을 반환함.
    """
    pre_market = is_pre_market()
    price_label = "NXT(장전)" if pre_market else "현재가"

    def fetch(symbols: list[str]) -> dict[str, dict]:
        result = {}
        for sym in symbols:
            try:
                price_data = kis.get_price(sym)  # 장전 시간대에는 NXT 시세 자동 반환
                ohlcv = kis.get_daily_ohlcv(sym, count=60)
                ind = compute_indicators(ohlcv)

                cur_px = float(price_data.get("stck_prpr", 0) or 0)
                prdy_clpr = float(price_data.get("prdy_clpr", cur_px) or cur_px)
                chg_pct = (cur_px / prdy_clpr - 1) * 100 if prdy_clpr else 0
                name = price_data.get("hts_kor_isnm") or kis.get_stock_name(sym)
                # NXT 거래대금 (누적 거래대금 원)
                acml_tr_pbmn = int(price_data.get("acml_tr_pbmn", 0) or 0)
                nxt_amount_bn = acml_tr_pbmn / 1e8  # 억원 단위

                # 시총/PER/PBR/업종 (get_price에서 추출)
                hts_avls = int(price_data.get("hts_avls", 0) or 0)  # 시총(억원)
                per = float(price_data.get("per", 0) or 0)
                pbr = float(price_data.get("pbr", 0) or 0)
                eps = float(price_data.get("eps", 0) or 0)
                sector = price_data.get("bstp_kor_isnm", "")
                acml_vol = int(price_data.get("acml_vol", 0) or 0)

                result[sym] = {
                    "name": name,
                    "price": cur_px,
                    "chg_pct": chg_pct,
                    "ma5": ind.get("ma5"),
                    "ma20": ind.get("ma20"),
                    "ma60": ind.get("ma60"),
                    "atr14": ind.get("atr14"),
                    "rsi14": ind.get("rsi14"),
                    "last_volume": ind.get("last_volume", 0),
                    "volume_avg20": ind.get("volume_avg20", 0),
                    "above_ma20": ind.get("above_ma20"),
                    "trend_up": ind.get("trend_up"),
                    "prev_close": prdy_clpr,
                    "nxt_gap_pct": chg_pct if pre_market else None,
                    "nxt_trade_amount_bn": nxt_amount_bn if pre_market else None,
                    "market_cap_bn": hts_avls,
                    "per": per,
                    "pbr": pbr,
                    "eps": eps,
                    "sector": sector,
                    "acml_vol": acml_vol,
                    "acml_tr_pbmn": acml_tr_pbmn,
                    "support_resistance": ind.get("support_resistance"),
                    "risk_flags": ind.get("risk_flags"),
                }
                log.info("  %s 조회 [%s] %s: %s원 (%+.2f%%) 시총%s억 업종=%s",
                         price_label, sym, name, f"{int(cur_px):,}", chg_pct,
                         f"{hts_avls:,}", sector)
            except Exception as e:
                log.warning("가격 조회 실패 [%s]: %s", sym, e)
        return result
    return fetch


def _try_morning_update_mode(cfg, today: str) -> bool:
    """저녁 선분석 파일이 있으면 Moderator 재평가만 실행. 성공 시 True 반환."""
    # 장중 재토론(rescreen_trigger)은 풀 파이프라인 강제 (업데이트 모드 스킵)
    if os.environ.get("KIS_RESCREEN_MODE") == "intraday":
        log.info("[아침] 장중 재토론 모드 → 풀 파이프라인 강제")
        return False

    evening_data = state_store.load_evening_candidates()
    if not evening_data:
        log.info("[아침] 저녁 선분석 파일 없음 → 풀 파이프라인 모드")
        return False

    # 날짜 체크: 저녁 파일은 "전일" 날짜로 저장됨. 70시간 TTL (주말 커버)
    generated_at_str = evening_data.get("generated_at", "")
    if generated_at_str:
        try:
            generated_at = datetime.fromisoformat(generated_at_str)
            age_hours = (datetime.now() - generated_at).total_seconds() / 3600
            if age_hours > 70:
                log.info("[아침] 저녁 파일 만료 (%.1fh 경과) → 풀 파이프라인 모드", age_hours)
                return False
            log.info("[아침] 저녁 파일 유효 (%.1fh 전 생성, 전일: %s)", age_hours, evening_data.get("date"))
        except Exception:
            pass
    else:
        # generated_at 없는 구버전 파일 — date로 폴백 비교 (저녁=전일, 아침=당일이므로 불일치 정상)
        ev_date = evening_data.get("date", "")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        # 금요일 저녁 → 월요일 아침의 경우도 커버 (3일 이내)
        if ev_date and ev_date < (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d"):
            log.info("[아침] 저녁 파일 너무 오래됨 (%s) → 풀 파이프라인 모드", ev_date)
            return False

    prelim_raw = evening_data.get("prelim_candidates", [])
    if not prelim_raw:
        log.info("[아침] 저녁 초벌 후보 없음 → 풀 파이프라인 모드")
        return False

    try:
        prelim_candidates = [SwingCandidate.from_dict(d) for d in prelim_raw]
    except Exception as e:
        log.warning("[아침] 저녁 후보 파싱 실패: %s → 풀 파이프라인 모드", e)
        return False

    log.info("[아침] 저녁 선분석 파일 확인 (%d개) → 업데이트 모드", len(prelim_candidates))

    try:
        kis = KisClient(cfg.kis)
        prelim_symbols = [c.symbol for c in prelim_candidates]
        delta = fetch_overnight_delta(
            prelim_symbols=prelim_symbols,
            kis_client=kis,
            news_sources=cfg.news.sources or None,
        )
        market_summary = _attach_live_market_decision(kis, state_store.load_market_summary() or {})
        if market_summary.get("market_decision"):
            state_store.save_market_summary(market_summary)
    except Exception as e:
        log.warning("[아침] overnight delta 수집 실패: %s → 빈 delta로 계속", e)
        delta = {}

    try:
        from src.agents.llm_client import LLMClient
        from src.agents.debate_engine import DebateEngine

        llm_primary = LLMClient(
            codex_model=cfg.agents.codex_model,
            gemini_model=cfg.agents.gemini_model,
            max_tokens=cfg.agents.max_tokens, primary=cfg.agents.primary,
        )
        engine = DebateEngine(
            agents=[],
            llm=llm_primary,
            screening_cfg=cfg.screening,
            num_rounds=0,
        )
        final_candidates = engine.moderator_reevaluate(prelim_candidates, delta)
    except Exception as e:
        log.error("[아침] Moderator 재평가 실패: %s → 풀 파이프라인 폴백", e)
        kis.close()
        return False

    # 종목명이 심볼과 같으면 KIS에서 검증
    for c in final_candidates:
        if c.name == c.symbol:
            try:
                c.name = kis.get_stock_name(c.symbol)
            except Exception:
                pass
    kis.close()

    # 보유 포지션 연계 후보 보존
    held_positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    held_symbols = {p.symbol for p in held_positions if p.state.value != "CLOSED"}

    # 최근 7일 청산 종목 제외
    _week_ago = datetime.now() - timedelta(days=7)
    recently_closed_symbols = {
        p.symbol for p in held_positions
        if p.state.value == "CLOSED" and p.close_time and p.close_time >= _week_ago
    }
    if recently_closed_symbols:
        before = len(final_candidates)
        final_candidates = [c for c in final_candidates if c.symbol not in recently_closed_symbols]
        log.info("[아침] 최근청산 종목 %d개 후보 제외: %s", before - len(final_candidates), recently_closed_symbols & {c.symbol for c in prelim_candidates})

    merged = []
    existing_symbols: set[str] = set()
    for cand in final_candidates:
        if cand.symbol not in held_symbols and cand.symbol not in existing_symbols:
            merged.append(cand)
            existing_symbols.add(cand.symbol)

    if not merged:
        log.warning("[아침] 업데이트 모드 결과 0개 (전부 보유/최근청산) → 풀 파이프라인 폴백")
        return False

    merged = merged[:cfg.screening.max_candidates]
    state_store.save_candidates([c.to_dict() for c in merged])
    log.info("[아침] 업데이트 모드 완료: %d개 저장 (LLM 호출 1회)", len(merged))
    for i, c in enumerate(merged, 1):
        log.info(
            "  [%d] %s(%s) 진입:%s~%s 목표:%s 신뢰:%.0f%%",
            i, c.name, c.symbol,
            f"{int(c.entry_low):,}", f"{int(c.entry_high):,}",
            f"{int(c.target_price):,}", c.consensus_score * 100,
        )
    apple_notes.report_morning_screen([c.to_dict() for c in merged], today)

    # rescreen_state 업데이트 → monitor 자동 재토론 쿨다운 인식
    from src.engine import rescreen_trigger as _rt
    _st = _rt._load_state()
    if _st.get("date") != today:
        _st = {"date": today, "count": 0, "last_run": ""}
    _st["last_run"] = datetime.now().isoformat()
    _rt._save_state(_st)

    return True


def main() -> None:
    from src.core.clock import is_trading_day
    if not is_trading_day():
        log.info("비영업일 — 종목 발굴 스킵")
        return
    cfg = load_config()
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 장 전 종목 발굴 시작 [%s] ===", today)

    # 저녁 선분석 파일이 있으면 업데이트 모드 (Moderator 1회만)
    if cfg.screening.evening_prescreen_enabled and _try_morning_update_mode(cfg, today):
        log.info("=== 아침 발굴 완료 (업데이트 모드) ===")
        return

    log.info("[아침] 풀 파이프라인 모드 (폴백)")

    # KIS 클라이언트 (전체 과정에서 공유)
    kis = KisClient(cfg.kis)

    # 1) 뉴스 수집
    log.info("뉴스 수집 중...")
    news_items = fetch_news(
        sources=cfg.news.sources or None,
        max_age_hours=cfg.news.max_age_hours,
    )
    news_text = news_fmt(news_items, max_items=40)

    # 2) DART 공시 수집
    log.info("DART 공시 수집 중...")
    dart_client = DartClient(cfg.dart.api_key, lookback_days=cfg.dart.lookback_days)
    disclosures = dart_client.get_major_disclosures()
    dart_text = dart_client.format_for_llm(disclosures)
    dart_client.close()

    # 3) 기존 후보 NXT 가격 업데이트
    existing_candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
    active_candidates = [c for c in existing_candidates if not c.is_expired()]
    log.info("기존 미만료 후보: %d개", len(active_candidates))

    if active_candidates:
        for cand in active_candidates:
            try:
                price_data = kis.get_price(cand.symbol)
                nxt_px = float(price_data.get("stck_prpr", 0) or 0)
                nxt_vol = int(price_data.get("acml_vol", 0) or 0)
                prdy_clpr = float(price_data.get("prdy_clpr", 0) or 0)
                acml_tr_pbmn = int(price_data.get("acml_tr_pbmn", 0) or 0)
                if nxt_px > 0:
                    cand.nxt_close = nxt_px
                    cand.nxt_volume = nxt_vol
                    cand.prev_close = prdy_clpr if prdy_clpr > 0 else cand.prev_close
                    if prdy_clpr > 0:
                        cand.nxt_gap_pct = (nxt_px / prdy_clpr - 1) * 100
                    cand.nxt_trade_amount_bn = acml_tr_pbmn / 1e8
            except Exception as e:
                log.warning("기존 후보 가격 조회 실패 [%s]: %s", cand.symbol, e)
        state_store.save_candidates([c.to_dict() for c in active_candidates])

    # NXT 요약 텍스트 (LLM 컨텍스트용)
    nxt_text = _build_nxt_text(active_candidates) if is_pre_market() else "NXT 데이터 없음 (프리장 시간 외 실행)"

    # 4) 잔고 조회 → 종목당 투자 가능 금액 계산
    max_pos = cfg.trading.max_positions
    try:
        bal = kis.get_balance()
        o2 = (bal.get("output2") or [{}])[0]
        total_cash = 0
        for field in ("ord_psbl_cash", "prvs_rcdl_excc_amt"):
            v = int(o2.get(field, 0) or 0)
            if v > 0:
                total_cash = v
                break
        if total_cash == 0:
            total_cash = int(o2.get("dnca_tot_amt", 0) or 0)
    except Exception as e:
        log.warning("잔고 조회 실패: %s — 가격 제한 없이 진행", e)
        total_cash = 0

    # 소액(300만 이하)이면 균등배분, 아니면 설정 비중
    if total_cash > 0 and total_cash < 3_000_000:
        per_stock_budget = total_cash // max_pos
    elif total_cash > 0:
        per_stock_budget = int(total_cash * cfg.trading.position_size_pct)
    else:
        per_stock_budget = 0

    budget_text = ""
    if per_stock_budget > 0:
        budget_text = f"\n⚠️ 종목당 투자 가능 금액: 약 {per_stock_budget:,}원. 주당 가격이 이 금액을 초과하는 종목은 추천하지 마십시오."
        log.info("종목당 투자 가능 금액: %s원 (총 잔고 %s원)", f"{per_stock_budget:,}", f"{total_cash:,}")

    market_summary = _attach_live_market_decision(kis, state_store.load_market_summary() or {})
    market_signal = build_market_signal(market_summary)

    # 4-A) 정량 1차 유니버스
    log.info("정량 1차 후보군 생성 중...")
    quant_candidates = build_quant_universe(kis, cfg.screening, market_signal=market_signal)
    quant_text = quant_fmt(quant_candidates, max_items=cfg.screening.quant_universe_max_results)

    # 5) LLM 멀티에이전트 토론 (실시간 가격 연동)
    log.info("LLM 멀티에이전트 토론 시작...")
    from src.agents.risk_agent import RiskAgent

    # 에이전트별 LLM 배치: Gemini(저렴·빠름) ↔ primary(정밀) 교차 fallback
    llm_gemini = LLMClient(
        codex_model=cfg.agents.codex_model,
        gemini_model=cfg.agents.gemini_model,
        max_tokens=cfg.agents.max_tokens, primary="gemini",
    )
    llm_primary = LLMClient(
        codex_model=cfg.agents.codex_model,
        gemini_model=cfg.agents.gemini_model,
        max_tokens=cfg.agents.max_tokens, primary=cfg.agents.primary,
    )

    agents = [
        NewsAgent(llm_gemini),       # 뉴스 요약 → Gemini
        ThemeAgent(llm_gemini),      # 테마/공시 → Gemini
        TechnicalAgent(llm_gemini),  # 기술적 분석 → Gemini
        RiskAgent(llm_primary),      # 리스크 반론 → primary (정밀 판단)
    ]

    engine = DebateEngine(
        agents=agents,
        llm=llm_primary,  # 모더레이터 → primary
        screening_cfg=cfg.screening,
        num_rounds=cfg.agents.debate_rounds,
        price_fetcher=make_price_fetcher(kis),
    )

    # 과거 성과 피드백 (최근 7일 청산 내역)
    from src.core.models import CloseReason
    recent_closed = [
        SwingPosition.from_dict(d) for d in state_store.load_positions()
        if d.get("state") == "CLOSED"
        and d.get("close_time")
        and d.get("close_reason") not in (None, "RECONCILE_KIS_ZERO")
    ]
    week_ago = datetime.now() - timedelta(days=7)
    recent_closed = [p for p in recent_closed if p.close_time and p.close_time >= week_ago]
    perf_lines = []
    for p in recent_closed[-10:]:
        pnl_pct = p.pnl_pct(p.close_price) if p.close_price else 0
        reason = p.close_reason.value if p.close_reason else "?"
        perf_lines.append(f"  {p.name}({p.symbol}) {pnl_pct:+.1f}% [{reason}]")
    if perf_lines:
        wins = len([p for p in recent_closed if p.close_price and p.close_price > p.avg_price])
        total = len(recent_closed)
        perf_text = (
            f"\n최근 7일 실적 ({wins}승 {total - wins}패, 승률 {wins/total*100:.0f}%):\n"
            + "\n".join(perf_lines)
            + "\n→ 손절 종목과 유사한 패턴은 피하십시오."
        )
    else:
        perf_text = ""

    context = {
        "today": today_label(),  # LLM 프롬프트용 — 요일+KST 명시
        "news_text": news_text,
        "dart_text": dart_text,
        "news_summary": news_text[:500],
        "market_context": format_market_context_for_llm(market_summary),
        "market_signal": market_signal,
        "budget_text": budget_text,
        "nxt_text": nxt_text,
        "quant_text": quant_text,
        "perf_text": perf_text,
    }

    new_candidates, transcript, reserves = engine.run(context)

    # 종목명이 심볼과 같으면(LLM이 이름 대신 코드 반환) KIS에서 검증
    for c in new_candidates + list(reserves):
        if c.name == c.symbol:
            try:
                c.name = kis.get_stock_name(c.symbol)
            except Exception:
                pass

    kis.close()
    log.info("신규 발굴 후보: %d개 (예비: %d개)", len(new_candidates), len(reserves))

    apple_notes.report_debate(transcript, today)

    # 5) 매일 아침: 오늘 토론 결과로 전체 교체 (신선도 확보)
    held_positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    held_symbols = {p.symbol for p in held_positions if p.state.value != "CLOSED"}

    # 최근 7일 청산 종목 제외
    _week_ago = datetime.now() - timedelta(days=7)
    recently_closed_symbols = {
        p.symbol for p in held_positions
        if p.state.value == "CLOSED" and p.close_time and p.close_time >= _week_ago
    }
    if recently_closed_symbols:
        before = len(new_candidates)
        new_candidates = [c for c in new_candidates if c.symbol not in recently_closed_symbols]
        log.info("최근청산 종목 %d개 신규 후보에서 제외: %s", before - len(new_candidates), recently_closed_symbols)

    # 신규 후보가 0개면 LLM 실패 가능성 → 기존 후보를 폐기하지 않고 유지 (단, 보유 종목은 제거)
    if not new_candidates:
        log.warning("신규 발굴 결과 없음 — 기존 후보 유지 (보유 종목 제외)")
        merged = [c for c in active_candidates if c.symbol not in held_symbols]
    else:
        merged = []
        existing_symbols: set[str] = set()
        for cand in new_candidates:
            if cand.symbol not in held_symbols and cand.symbol not in existing_symbols:
                merged.append(cand)
                existing_symbols.add(cand.symbol)
        dropped = len(active_candidates)
        if dropped > 0:
            log.info("기존 묵은 후보 %d개 폐기 (신선도 우선)", dropped)

    merged = merged[:cfg.screening.max_candidates]
    state_store.save_candidates([c.to_dict() for c in merged])
    log.info("저장된 후보 총 %d개", len(merged))

    for i, c in enumerate(merged, 1):
        exp_str = c.expires_at.strftime("%m/%d") if c.expires_at else "-"
        log.info(
            "  [%d] %s(%s) 진입: %s~%s 목표: %s 손절: %s 신뢰: %.0f%% 만료: %s",
            i, c.name, c.symbol,
            f"{int(c.entry_low):,}", f"{int(c.entry_high):,}",
            f"{int(c.target_price):,}", f"{int(c.stop_price):,}",
            c.consensus_score * 100, exp_str,
        )

    # 예비후보 저장 (정규 후보와 중복 제거)
    merged_symbols = {c.symbol for c in merged}
    reserve_list = [r for r in reserves if r.symbol not in merged_symbols]
    state_store.save_reserves([r.to_dict() for r in reserve_list])
    if reserve_list:
        log.info("예비후보 %d개 저장", len(reserve_list))
        for i, r in enumerate(reserve_list, 1):
            log.info("  [예비%d] %s(%s) 신뢰: %.0f%%", i, r.name, r.symbol, r.consensus_score * 100)

    apple_notes.report_morning_screen([c.to_dict() for c in merged], today)

    # ── watchlist 머지: 정규+예비 + 기존 watchlist (만료/보유 제외) ──
    pg_cfg = cfg.pivot_gate
    if pg_cfg.enabled:
        try:
            now = datetime.now()
            existing_wl: list[SwingCandidate] = []
            for d in state_store.load_watchlist():
                try:
                    c = SwingCandidate.from_dict(d)
                    if not c.is_expired(now) and c.symbol not in held_symbols:
                        existing_wl.append(c)
                except Exception:
                    continue
            today_pool = [c for c in (merged + reserve_list) if c.symbol not in held_symbols]
            extra_pool = [c for c in (getattr(engine, "last_prelim_pool", []) or [])
                          if c.symbol not in held_symbols]
            today_symbols = {c.symbol for c in today_pool}
            extra_symbols = {c.symbol for c in extra_pool}
            merged_wl = (
                today_pool
                + [c for c in extra_pool if c.symbol not in today_symbols]
                + [c for c in existing_wl
                   if c.symbol not in today_symbols and c.symbol not in extra_symbols]
            )
            merged_wl.sort(key=lambda c: (c.consensus_score or 0), reverse=True)
            merged_wl = merged_wl[: pg_cfg.watchlist_max]
            state_store.save_watchlist([c.to_dict() for c in merged_wl])
            log.info(
                "watchlist 머지: 오늘 %d + 추가풀 %d + 기존 %d → 저장 %d",
                len(today_pool), len(extra_pool), len(existing_wl), len(merged_wl),
            )
        except Exception as e:
            log.warning("watchlist 머지 실패 (무시): %s", e)

    # rescreen_state 업데이트 → monitor 자동 재토론 쿨다운 인식
    from src.engine import rescreen_trigger as _rt
    _st = _rt._load_state()
    if _st.get("date") != today:
        _st = {"date": today, "count": 0, "last_run": ""}
    _st["last_run"] = datetime.now().isoformat()
    _rt._save_state(_st)

    log.info("=== 장 전 발굴 완료 ===")


if __name__ == "__main__":
    main()
