"""일일 보유 포지션 재평가 스크립트 (기본 11:00 실행).

launchd ai.kis.swing.position_review.plist에 의해 매 영업일 정해진 시각에 호출됨.

흐름:
1. 마이너스 보유 포지션 + 보유 1일 이상 + 미청산 종목 필터
2. 시세/지표 + 최근 뉴스 컨텍스트 수집
3. PositionReviewer가 LLM 1회 호출로 일괄 판정 (HOLD/SELL + conviction)
4. SELL + conviction >= 임계값인 종목에 review_decision 플래그 세팅
   (하루 max_sells_per_day 종목 상한)
5. 다음 monitor 사이클에서 시장가 매도 처리

실거래 행위 자체는 monitor가 수행 — 본 스크립트는 판정/플래그만.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta

from src.core.config import load_config
from src.core import state_store
from src.core.clock import now_kst
from src.core.models import SwingPosition, PositionState
from src.data.kis_client import KisClient
from src.data.news_fetcher import fetch_news, format_for_llm as news_fmt
from src.data.technical import compute_indicators
from src.agents.llm_client import LLMClient
from src.engine.position_reviewer import PositionReviewer
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("position_review")


def _is_trading_day(kis: KisClient) -> bool:
    """오늘이 영업일인지 확인. 실패 시 True(보수적) 반환."""
    try:
        data = kis.get_price("005930")
        price = float(data.get("stck_prpr", 0) or 0)
        return price > 0
    except Exception:
        return True


def _make_price_fetcher(kis: KisClient):
    """포지션 재평가용 시세+지표 fetcher (현재가 기준)."""
    def fetch(symbols: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for sym in symbols:
            try:
                price_data = kis.get_price(sym)
                ohlcv = kis.get_daily_ohlcv(sym, count=130)
                ind = compute_indicators(ohlcv)

                cur_px = float(price_data.get("stck_prpr", 0) or 0)
                prdy_clpr = float(price_data.get("prdy_clpr", cur_px) or cur_px)
                chg_pct = (cur_px / prdy_clpr - 1) * 100 if prdy_clpr else 0
                name = price_data.get("hts_kor_isnm") or sym
                hts_avls = int(price_data.get("hts_avls", 0) or 0)
                per = float(price_data.get("per", 0) or 0)
                pbr = float(price_data.get("pbr", 0) or 0)
                sector = price_data.get("bstp_kor_isnm", "")

                result[sym] = {
                    "name": name,
                    "price": cur_px,
                    "chg_pct": chg_pct,
                    "ma5": ind.get("ma5"),
                    "ma20": ind.get("ma20"),
                    "ma60": ind.get("ma60"),
                    "ma120": ind.get("ma120"),
                    "atr14": ind.get("atr14"),
                    "rsi14": ind.get("rsi14"),
                    "last_volume": ind.get("last_volume", 0),
                    "volume_avg20": ind.get("volume_avg20", 0),
                    "above_ma20": ind.get("above_ma20"),
                    "trend_up": ind.get("trend_up"),
                    "prev_close": prdy_clpr,
                    "market_cap_bn": hts_avls,
                    "per": per,
                    "pbr": pbr,
                    "sector": sector,
                    "support_resistance": ind.get("support_resistance"),
                }
                log.info(
                    "  시세 [%s] %s: %s원 (%+.2f%%) MA20=%s RSI=%s",
                    sym, name, f"{int(cur_px):,}", chg_pct,
                    f"{int(ind.get('ma20') or 0):,}", ind.get("rsi14"),
                )
            except Exception as e:
                log.warning("시세 조회 실패 [%s]: %s", sym, e)
        return result
    return fetch


def _build_news_text(news_cfg, lookback_hours: int) -> str:
    """재료 소멸/악재 부상 판단용 뉴스 텍스트."""
    try:
        items = fetch_news(
            sources=news_cfg.sources or None,
            max_age_hours=lookback_hours,
        )
        return news_fmt(items, max_items=40)
    except Exception as e:
        log.warning("뉴스 수집 실패: %s", e)
        return ""


def _has_negative_targets(positions: list[SwingPosition], kis: KisClient) -> tuple[int, int]:
    """대상 후보를 미리 가늠 (마이너스 종목 수, 전체 활성 수)."""
    active = [p for p in positions if p.state != PositionState.CLOSED]
    neg = 0
    for p in active:
        try:
            d = kis.get_price(p.symbol)
            cur = float(d.get("stck_prpr", 0) or 0)
            if cur > 0 and cur < p.avg_price:
                neg += 1
        except Exception:
            continue
    return neg, len(active)


def _notify_report(report, today: str) -> None:
    """애플 노트로 판정 결과 요약 전송."""
    if not report.evaluated:
        body = f"# [{today}] 포지션 재평가\n\n{report.skipped_reason or '판정 결과 없음'}"
        apple_notes.create_note(f"[재평가] {today} 포지션 판정", body)
        return
    decided_str = report.decided_at.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# [{today}] 포지션 재평가 보고",
        "",
        f"- 판정 시각: {decided_str}",
        f"- 평가 종목: {len(report.evaluated)}개",
        f"- SELL 플래그 세팅: {len(report.sell_flagged)}개 (다음 monitor 사이클에서 시장가 매도)",
        "",
        "## 판정 결과",
    ]
    for v in sorted(report.evaluated, key=lambda x: (x.decision != "SELL", -x.conviction)):
        flag = " ⚠️SELL 적용" if v in report.sell_flagged else ""
        lines += [
            f"### {v.name} ({v.symbol})  PnL {v.pnl_pct:+.2f}%",
            f"- 판정: **{v.decision}** (conviction {v.conviction:.0%}){flag}",
            f"- 근거: {v.rationale[:200]}",
            "",
        ]
    apple_notes.create_note(f"[재평가] {today} 포지션 판정", "\n".join(lines))


def main() -> int:
    cfg = load_config()
    pr_cfg = cfg.position_review

    if not pr_cfg.enabled:
        log.info("position_review.enabled=false → 종료")
        return 0

    now = now_kst()
    today = now.strftime("%Y-%m-%d")

    kis = KisClient(cfg.kis)

    if not _is_trading_day(kis):
        log.info("비영업일로 추정 → 종료")
        return 0

    # 빠른 가늠: 마이너스 종목이 0이면 LLM 호출 자체 생략
    positions_raw = state_store.load_positions()
    positions = [SwingPosition.from_dict(d) for d in positions_raw]
    neg_count, active_count = _has_negative_targets(positions, kis)
    log.info("활성 포지션 %d개 / 마이너스 추정 %d개", active_count, neg_count)
    if active_count == 0:
        log.info("활성 포지션 없음 → 종료")
        return 0
    if pr_cfg.only_negative and neg_count == 0:
        log.info("마이너스 포지션 없음 → 재평가 생략")
        return 0

    llm = LLMClient(
        codex_model=cfg.agents.codex_model,
        gemini_model=cfg.agents.gemini_model,
        max_tokens=cfg.agents.max_tokens, primary=cfg.agents.primary,
    )
    price_fetcher = _make_price_fetcher(kis)

    def news_loader():
        return _build_news_text(cfg.news, pr_cfg.news_lookback_hours)

    reviewer = PositionReviewer(
        cfg=pr_cfg,
        llm=llm,
        price_fetcher=price_fetcher,
        news_fetcher=news_loader,
    )

    report = reviewer.run(now=now)

    log.info(
        "판정 완료: 평가 %d, SELL 플래그 %d (사유: %s)",
        len(report.evaluated), len(report.sell_flagged),
        report.skipped_reason or "정상",
    )

    try:
        _notify_report(report, today)
    except Exception as e:
        log.warning("애플 노트 보고 실패 (무시): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
