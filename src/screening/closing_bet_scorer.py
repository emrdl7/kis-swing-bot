"""종가배팅 V-스코어링 엔진.

거래금액, 당일 등락률, 거래량 증가율, 이동평균선 위치를 종합하여
종가배팅 적합도 점수를 산출한다.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ClosingBetScore:
    """종가배팅 종목 점수."""
    symbol: str
    name: str
    price: float
    change_pct: float          # 당일 등락률
    trade_amount: int          # 거래대금 (원)
    volume: int                # 거래량
    volume_ratio: float        # 전일 대비 거래량 비율
    ma5: Optional[float] = None
    ma20: Optional[float] = None
    score: float = 0.0         # 최종 V-스코어 (0~100)
    breakdown: dict = None     # 항목별 점수

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = {}


def compute_v_score(
    item: dict,
    indicators: dict,
    weights: dict,
    all_trade_amounts: list[int],
) -> ClosingBetScore:
    """단일 종목의 V-스코어를 계산한다.

    Args:
        item: KIS 거래량순위 API 응답 항목
        indicators: 기술지표 (ma5, ma20 등)
        weights: 가중치 딕셔너리
        all_trade_amounts: 전체 종목 거래대금 리스트 (상대 순위 계산용)
    """
    symbol = item.get("mksc_shrn_iscd", "") or item.get("stck_shrn_iscd", "")
    name = item.get("hts_kor_isnm", symbol)
    price = float(item.get("stck_prpr", 0) or 0)
    change_pct = float(item.get("prdy_ctrt", 0) or 0)
    volume = int(item.get("acml_vol", 0) or 0)
    trade_amount = int(item.get("acml_tr_pbmn", 0) or 0)

    # 전일 대비 거래량 비율 (거래량순위 API에서 제공)
    avg_vol = int(item.get("avrg_vol", 0) or 0)
    volume_ratio = (volume / avg_vol) if avg_vol > 0 else 1.0

    ma5 = indicators.get("ma5")
    ma20 = indicators.get("ma20")

    # ── 항목별 점수 (0~100) ──
    breakdown = {}

    # 1) 거래대금 점수: 상위 몇 %인지
    if all_trade_amounts:
        rank = sum(1 for a in all_trade_amounts if a > trade_amount)
        breakdown["trade_amount"] = max(0, 100 - (rank / len(all_trade_amounts) * 100))
    else:
        breakdown["trade_amount"] = 50

    # 2) 등락률 점수: 2~8% 구간이 최적, 그 이상은 과열
    if change_pct <= 0:
        breakdown["change_pct"] = 0
    elif change_pct <= 2:
        breakdown["change_pct"] = change_pct / 2 * 50
    elif change_pct <= 8:
        breakdown["change_pct"] = 50 + (change_pct - 2) / 6 * 50
    else:
        breakdown["change_pct"] = max(0, 100 - (change_pct - 8) * 10)  # 과열 감점

    # 3) 거래량 증가율 점수
    if volume_ratio <= 1.0:
        breakdown["volume_ratio"] = 20
    elif volume_ratio <= 3.0:
        breakdown["volume_ratio"] = 20 + (volume_ratio - 1) / 2 * 60
    else:
        breakdown["volume_ratio"] = min(100, 80 + (volume_ratio - 3) * 5)

    # 4) 이동평균선 위치 점수
    if ma5 and ma20 and price > 0:
        above_ma5 = price > ma5
        above_ma20 = price > ma20
        ma5_above_ma20 = ma5 > ma20
        ma_score = 0
        if above_ma5:
            ma_score += 35
        if above_ma20:
            ma_score += 35
        if ma5_above_ma20:
            ma_score += 30
        breakdown["ma_position"] = ma_score
    else:
        breakdown["ma_position"] = 50  # 데이터 없으면 중립

    # ── 가중 합산 ──
    total = 0
    for key, weight in weights.items():
        total += breakdown.get(key, 0) * weight

    return ClosingBetScore(
        symbol=symbol,
        name=name,
        price=price,
        change_pct=change_pct,
        trade_amount=trade_amount,
        volume=volume,
        volume_ratio=volume_ratio,
        ma5=ma5,
        ma20=ma20,
        score=round(total, 1),
        breakdown=breakdown,
    )
