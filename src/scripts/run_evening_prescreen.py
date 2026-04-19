"""전일 15:20 실행 — 2단계 선분석 Phase A (초벌 후보 선정).

launchd ai.kis.swing.evening_prescreen.plist에 의해 매 영업일 15:25 호출됨.
결과는 state/evening_candidates.json에 저장되며,
다음날 아침 run_morning_screen.py에서 로드해 Moderator 재평가에 활용된다.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta

from src.core.config import load_config
from src.core import state_store
from src.core.models import SwingCandidate, SwingPosition
from src.data.kis_client import KisClient
from src.data.dart_client import DartClient
from src.data.news_fetcher import fetch_news, format_for_llm as news_fmt
from src.data.technical import compute_indicators
from src.agents.llm_client import LLMClient
from src.agents.news_agent import NewsAgent
from src.agents.theme_agent import ThemeAgent
from src.agents.technical_agent import TechnicalAgent
from src.agents.risk_agent import RiskAgent
from src.agents.debate_engine import DebateEngine
from src.utils.logging_setup import setup

log = setup("evening_prescreen")

# 락 파일 — 중복 실행 방지
LOCK_FILE = PROJECT_ROOT / "state" / "evening_prescreen.lock"


def _is_trading_day(kis: KisClient) -> bool:
    """오늘이 영업일인지 확인. 실패 시 True(보수적) 반환."""
    try:
        data = kis.get_price("005930")  # 삼성전자로 시장 확인
        price = float(data.get("stck_prpr", 0) or 0)
        return price > 0
    except Exception:
        return True  # 조회 실패 시 보수적으로 영업일 가정


def make_price_fetcher(kis: KisClient, label: str = "EOD"):
    """종목 현재가 + 기술지표를 반환하는 fetcher (저녁 기준가 포함)."""
    def fetch(symbols: list[str]) -> dict[str, dict]:
        result = {}
        for sym in symbols:
            try:
                price_data = kis.get_price(sym)
                ohlcv = kis.get_daily_ohlcv(sym, count=60)
                from src.data.technical import compute_indicators
                ind = compute_indicators(ohlcv)

                cur_px = float(price_data.get("stck_prpr", 0) or 0)
                prdy_clpr = float(price_data.get("prdy_clpr", cur_px) or cur_px)
                chg_pct = (cur_px / prdy_clpr - 1) * 100 if prdy_clpr else 0
                name = price_data.get("hts_kor_isnm", sym)
                acml_tr_pbmn = int(price_data.get("acml_tr_pbmn", 0) or 0)
                hts_avls = int(price_data.get("hts_avls", 0) or 0)
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
                    "nxt_gap_pct": None,
                    "nxt_trade_amount_bn": None,
                    "market_cap_bn": hts_avls,
                    "per": per,
                    "pbr": pbr,
                    "eps": eps,
                    "sector": sector,
                    "acml_vol": acml_vol,
                    "acml_tr_pbmn": acml_tr_pbmn,
                    "support_resistance": ind.get("support_resistance"),
                    # 저녁 기준가 — 아침 갭 게이트에서 재사용
                    "ref_price_eod": cur_px,
                }
                log.info("  [%s] %s (%s): %s원 (%+.2f%%) 업종=%s",
                         label, sym, name, f"{int(cur_px):,}", chg_pct, sector)
            except Exception as e:
                log.warning("가격 조회 실패 [%s]: %s", sym, e)
        return result
    return fetch


def main() -> None:
    cfg = load_config()

    if not cfg.screening.evening_prescreen_enabled:
        log.info("evening_prescreen_enabled=False → 스킵")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 저녁 선분석 Phase A 시작 [%s] ===", today)

    # 락 파일 체크
    if LOCK_FILE.exists():
        log.warning("락 파일 존재 → 이미 실행 중이거나 비정상 종료. 삭제 후 재실행하거나 대기.")
        return
    LOCK_FILE.write_text(datetime.now().isoformat())

    try:
        _run(cfg, today)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def _run(cfg, today: str) -> None:
    kis = KisClient(cfg.kis)

    if not _is_trading_day(kis):
        log.info("휴장일 또는 시장 미개장 → 스킵")
        kis.close()
        return

    # 1) 뉴스 수집 (전체 당일)
    log.info("뉴스 수집 중...")
    news_items = fetch_news(
        sources=cfg.news.sources or None,
        max_age_hours=cfg.news.max_age_hours,
    )
    news_text = news_fmt(news_items, max_items=40)

    # 2) DART 공시
    log.info("DART 공시 수집 중...")
    dart_client = DartClient(cfg.dart.api_key, lookback_days=cfg.dart.lookback_days)
    disclosures = dart_client.get_major_disclosures()
    dart_text = dart_client.format_for_llm(disclosures)
    dart_client.close()

    # 3) 잔고 → 종목당 예산
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
        log.warning("잔고 조회 실패: %s", e)
        total_cash = 0

    if total_cash > 0 and total_cash < 3_000_000:
        per_stock_budget = total_cash // max_pos
    elif total_cash > 0:
        per_stock_budget = int(total_cash * cfg.trading.position_size_pct)
    else:
        per_stock_budget = 0

    budget_text = ""
    if per_stock_budget > 0:
        budget_text = f"\n⚠️ 종목당 투자 가능 금액: 약 {per_stock_budget:,}원."

    # 4) 최근 실적
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
            f"\n최근 7일 실적 ({wins}승 {total - wins}패):\n"
            + "\n".join(perf_lines)
        )
    else:
        perf_text = ""

    # 5) LLM 멀티에이전트 토론
    log.info("LLM 멀티에이전트 토론 시작 (저녁 선분석 Phase A)...")
    llm_gemini = LLMClient(model=cfg.agents.model, max_tokens=cfg.agents.max_tokens, primary="gemini")
    llm_claude = LLMClient(model=cfg.agents.model, max_tokens=cfg.agents.max_tokens, primary="claude")

    agents = [
        NewsAgent(llm_gemini),
        ThemeAgent(llm_gemini),
        TechnicalAgent(llm_gemini),
        RiskAgent(llm_claude),
    ]

    # 저녁 선분석은 더 많은 후보 수 (evening_candidate_n)
    from src.core.config import ScreeningConfig
    evening_screening_cfg = cfg.screening.model_copy(
        update={"max_candidates": cfg.screening.evening_candidate_n}
    )

    engine = DebateEngine(
        agents=agents,
        llm=llm_claude,
        screening_cfg=evening_screening_cfg,
        num_rounds=cfg.agents.debate_rounds,
        price_fetcher=make_price_fetcher(kis, label="EOD"),
    )

    context = {
        "today": today,
        "news_text": news_text,
        "dart_text": dart_text,
        "news_summary": news_text[:500],
        "budget_text": budget_text,
        "nxt_text": "NXT 데이터 없음 (저녁 선분석 — 장마감 후 실행)",
        "perf_text": perf_text,
    }

    new_candidates, transcript, reserves = engine.run(context)
    kis.close()

    all_prelim = new_candidates + reserves
    log.info("초벌 후보: %d개 (정규 %d + 예비 %d)", len(all_prelim), len(new_candidates), len(reserves))

    # ref_price_eod 주입 — 장마감 가격은 이미 price_fetcher에서 ref_price_eod로 저장됨
    # (debate_engine._to_candidates는 nxt_close만 쓰므로 별도 주입 필요)
    # price_fetcher가 반환한 price_ctx를 저장하려면 engine에서 꺼내야 하는데,
    # 현재 DebateEngine.run()은 price_ctx를 외부에 노출하지 않음.
    # 대신 현재가를 다시 조회하지 않고, candidates에 담긴 entry_high 기준으로 근사.
    # 실제 기준가는 저녁 실행 시 entry_high ≈ 현재가이므로 entry_high를 ref로 사용.
    for cand in all_prelim:
        if cand.ref_price_eod is None and cand.entry_high > 0:
            # entry_high는 현재가 +2~3% 이내로 설정되므로 (prev_close or entry_low)를 사용
            cand.ref_price_eod = cand.prev_close if cand.prev_close and cand.prev_close > 0 else cand.entry_low

    # 저녁 후보 저장
    evening_data = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "prelim_candidates": [c.to_dict() for c in all_prelim],
        "debate_log": transcript[:2000],  # 로그 일부 저장 (용량 절약)
    }
    state_store.save_evening_candidates(evening_data)
    log.info("state/evening_candidates.json 저장 완료 (%d개)", len(all_prelim))

    for i, c in enumerate(all_prelim, 1):
        log.info(
            "  [%d] %s(%s) 진입:%s~%s 목표:%s 신뢰:%.0f%% ref_eod:%s",
            i, c.name, c.symbol,
            f"{int(c.entry_low):,}", f"{int(c.entry_high):,}",
            f"{int(c.target_price):,}",
            c.consensus_score * 100,
            f"{int(c.ref_price_eod):,}" if c.ref_price_eod else "N/A",
        )

    log.info("=== 저녁 선분석 완료 ===")


if __name__ == "__main__":
    main()
