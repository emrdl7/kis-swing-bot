"""종가배팅 스크리닝 스크립트 (14:50 실행).

거래금액 상위 종목을 V-스코어링하여 종가배팅 후보를 선정한다.
launchd ai.kis.swing.closing_bet.plist 에 의해 매일 14:50 호출됨.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta

from src.core.config import load_config
from src.core import state_store
from src.core.models import SwingCandidate
from src.data.kis_client import KisClient
from src.data.technical import compute_indicators
from src.screening.closing_bet_scorer import compute_v_score
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("closing_bet")


def main() -> None:
    from src.core.clock import is_trading_day, is_next_trading_day
    if not is_trading_day():
        log.info("비영업일 — 종가배팅 스크리닝 스킵")
        return
    if not is_next_trading_day():
        log.info("내일 비영업일 — 종가배팅 스크리닝 스킵")
        return
    cfg = load_config()

    if not cfg.closing_bet.enabled:
        log.info("종가배팅 비활성 상태 → 종료")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    cb = cfg.closing_bet
    log.info("=== 종가배팅 스크리닝 시작 [%s] ===", today)

    kis = KisClient(cfg.kis)

    # 1) 잔고 조회 → 종목당 투자 가능 금액
    try:
        bal = kis.get_balance()
        o2 = (bal.get("output2") or [{}])[0]
        cash = 0
        for field in ("ord_psbl_cash", "prvs_rcdl_excc_amt"):
            v = int(o2.get(field, 0) or 0)
            if v > 0:
                cash = v
                break
        if cash == 0:
            cash = int(o2.get("dnca_tot_amt", 0) or 0)
    except Exception as e:
        log.warning("잔고 조회 실패: %s", e)
        cash = 0

    # 기존 스윙 포지션 제외한 가용 자금 계산
    positions = state_store.load_positions()
    swing_invested = sum(
        int(p.get("avg_price", 0) * p.get("qty", 0))
        for p in positions if p.get("state") != "CLOSED"
    )
    available_cash = max(0, cash)
    max_pos = cb.max_positions
    if available_cash < 3_000_000:
        per_stock = available_cash // max(1, max_pos)
    else:
        per_stock = int(available_cash * cfg.trading.position_size_pct)

    log.info("가용 자금: %s원, 종목당: %s원", f"{available_cash:,}", f"{per_stock:,}")

    if per_stock <= 0:
        log.warning("투자 가능 금액 부족 → 종료")
        kis.close()
        return

    # 2) 거래금액 상위 종목 조회
    log.info("거래금액순위 조회 중...")
    raw_items = kis.get_volume_rank(
        sort_by="3",  # 거래금액순
        market="J",
        min_price=0,
        max_price=int(per_stock),  # 매수 가능 가격 이하만
        min_volume=0,
    )
    log.info("조회된 종목: %d개", len(raw_items))

    if not raw_items:
        log.warning("거래금액 순위 데이터 없음 → 종료")
        kis.close()
        return

    # 3) 필터링
    filtered = []
    for item in raw_items[:cb.top_n]:
        change_pct = float(item.get("prdy_ctrt", 0) or 0)
        trade_amount = int(item.get("acml_tr_pbmn", 0) or 0)
        price = float(item.get("stck_prpr", 0) or 0)

        # ETF/ETN/인버스/레버리지 제외
        name = item.get("hts_kor_isnm", "")
        if any(kw in name for kw in ("KODEX", "TIGER", "KOSEF", "KBSTAR", "ARIRANG",
                                      "SOL", "ACE", "HANARO", "인버스", "레버리지",
                                      "2X", "곱버스", "ETN", "선물")):
            continue
        # 최소 등락률 필터
        if change_pct < cb.min_change_pct:
            continue
        # 최소 거래대금 필터 (억원)
        if trade_amount < cb.min_trade_amount_bn * 100_000_000:
            continue
        # 가격 필터: 매수 가능 금액 이하
        if price > per_stock or price <= 0:
            continue

        filtered.append(item)

    log.info("필터 통과: %d개 (등락률≥%.1f%%, 거래대금≥%d억)", len(filtered), cb.min_change_pct, cb.min_trade_amount_bn)

    if not filtered:
        log.info("조건 충족 종목 없음 → 종료")
        kis.close()
        return

    # 4) 기술지표 조회 + V-스코어링
    all_trade_amounts = [int(item.get("acml_tr_pbmn", 0) or 0) for item in filtered]
    scored = []

    for item in filtered:
        symbol = item.get("mksc_shrn_iscd", "") or item.get("stck_shrn_iscd", "")
        if not symbol:
            continue
        try:
            ohlcv = kis.get_daily_ohlcv(symbol, count=30)
            indicators = compute_indicators(ohlcv)
        except Exception:
            indicators = {}

        result = compute_v_score(
            item=item,
            indicators=indicators,
            weights=cb.score_weights,
            all_trade_amounts=all_trade_amounts,
        )
        scored.append(result)
        log.info("  [%s] %s V-스코어: %.1f (등락: %+.1f%% 거래대금: %s억)",
                 result.symbol, result.name, result.score,
                 result.change_pct, f"{result.trade_amount // 100_000_000:,}")

    # 점수순 정렬
    scored.sort(key=lambda x: x.score, reverse=True)

    # 5) 상위 종목을 종가배팅 후보로 저장
    now = datetime.now()
    cb_candidates = []
    for s in scored[:cb.max_positions]:
        if s.score < 50:  # 최소 50점 이상만
            continue
        # 진입구간: 현재가 ±0.5% (종가 근접 매수)
        slack = 0.005
        entry_low = round(s.price * (1 - slack))
        entry_high = round(s.price * (1 + slack))
        target = round(s.price * (1 + cb.target_profit_pct / 100))
        stop = round(s.price * (1 - cb.stop_loss_pct / 100))

        cand = SwingCandidate(
            symbol=s.symbol,
            name=s.name,
            entry_low=entry_low,
            entry_high=entry_high,
            target_price=target,
            stop_price=stop,
            consensus_score=s.score / 100,
            rationale=f"V-스코어 {s.score:.0f}점 | 등락 {s.change_pct:+.1f}% | 거래대금 {s.trade_amount // 100_000_000:,}억",
            tags=["closing_bet"],
            discovered_at=now,
            expires_at=now + timedelta(hours=20),  # 다음 날 오전까지
        )
        cb_candidates.append(cand)

    # 기존 스윙 후보와 별도로 closing_bet 태그가 붙은 후보만 교체
    existing = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
    non_cb = [c for c in existing if "closing_bet" not in (c.tags or [])]
    merged = non_cb + cb_candidates
    state_store.save_candidates([c.to_dict() for c in merged])

    log.info("종가배팅 후보 %d개 선정", len(cb_candidates))
    for i, c in enumerate(cb_candidates, 1):
        log.info("  [CB%d] %s(%s) V-스코어: %.0f%% 진입: %s~%s 목표: %s 손절: %s",
                 i, c.name, c.symbol, c.consensus_score * 100,
                 f"{int(c.entry_low):,}", f"{int(c.entry_high):,}",
                 f"{int(c.target_price):,}", f"{int(c.stop_price):,}")

    kis.close()
    log.info("=== 종가배팅 스크리닝 완료 ===")


if __name__ == "__main__":
    main()
