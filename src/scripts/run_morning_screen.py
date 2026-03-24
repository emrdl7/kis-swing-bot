"""장 전 종목 발굴 스크립트 (08:00 실행).

launchd ai.kis.swing.morning.plist 에 의해 매일 08:00 호출됨.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

from src.core.config import load_config
from src.core import state_store
from src.core.models import SwingCandidate
from src.data.kis_client import KisClient
from src.data.dart_client import DartClient
from src.data.news_fetcher import fetch_news, format_for_llm as news_fmt
from src.data.technical import compute_indicators
from src.agents.llm_client import LLMClient
from src.agents.news_agent import NewsAgent
from src.agents.theme_agent import ThemeAgent
from src.agents.technical_agent import TechnicalAgent
from src.agents.debate_engine import DebateEngine
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("morning_screen")


def make_price_fetcher(kis: KisClient):
    """종목코드 리스트를 받아 현재가 + 기술지표를 반환하는 함수 생성."""
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
                }
                log.info("  가격 조회 [%s] %s: %s원 (%+.2f%%)",
                         sym, name, f"{int(cur_px):,}", chg_pct)
            except Exception as e:
                log.warning("가격 조회 실패 [%s]: %s", sym, e)
        return result
    return fetch


def main() -> None:
    cfg = load_config()
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 장 전 종목 발굴 시작 [%s] ===", today)

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
                if nxt_px > 0:
                    cand.nxt_close = nxt_px
                    cand.nxt_volume = nxt_vol
            except Exception as e:
                log.warning("기존 후보 가격 조회 실패 [%s]: %s", cand.symbol, e)
        state_store.save_candidates([c.to_dict() for c in active_candidates])

    # 4) LLM 멀티에이전트 토론 (실시간 가격 연동)
    log.info("LLM 멀티에이전트 토론 시작...")
    llm = LLMClient(model=cfg.agents.model, max_tokens=cfg.agents.max_tokens)

    agents = [NewsAgent(llm), ThemeAgent(llm), TechnicalAgent(llm)]

    engine = DebateEngine(
        agents=agents,
        llm=llm,
        screening_cfg=cfg.screening,
        num_rounds=cfg.agents.debate_rounds,
        price_fetcher=make_price_fetcher(kis),
    )

    context = {
        "today": today,
        "news_text": news_text,
        "dart_text": dart_text,
        "news_summary": news_text[:500],
    }

    new_candidates, transcript = engine.run(context)
    kis.close()
    log.info("신규 발굴 후보: %d개", len(new_candidates))

    apple_notes.report_debate(transcript, today)

    # 5) 기존 후보 + 신규 후보 병합
    existing_symbols = {c.symbol for c in active_candidates}
    merged = list(active_candidates)
    for cand in new_candidates:
        if cand.symbol not in existing_symbols:
            merged.append(cand)
            existing_symbols.add(cand.symbol)

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

    apple_notes.report_morning_screen([c.to_dict() for c in merged], today)
    log.info("=== 장 전 발굴 완료 ===")


if __name__ == "__main__":
    main()
