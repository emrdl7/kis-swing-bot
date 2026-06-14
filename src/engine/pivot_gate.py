"""정량 피봇 게이트 — 두 가지 모드.

LLM이 종목 발굴(정성 판단), 본 모듈이 진입 시점(정량 판단)을 분리 담당.

[1] evaluate_pullback (눌림목 모드, 기본):
    한국 우량주 박스권 패턴에 적합. LLM이 정한 entry_zone 안에서 회복 시작 시 진입.
    조건:
      - 현재가가 entry_low ~ entry_high 안
      - 최근 N일 저점 대비 일정% 이상 회복 (반등 시작)
      - 거래량 위축이 너무 깊지 않음 (≥ 20일 평균 × 0.8)
      - MA20 > MA60 (장기 추세 살아있음)
      - 거래대금 임계 (작전주 필터)

[2] evaluate_breakout (돌파 모드, 옵션):
    박스 상단 돌파 + 거래량 폭발 모멘텀 매수.
    조건:
      - 박스 상단 +0.3~5% 돌파
      - 거래량 ≥ 20일 평균 × 1.5
      - 거래대금 임계
      - MA20 > MA60
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class PivotResult:
    passed: bool
    reason: str                  # 통과 또는 실패 사유 (사람 읽기 좋은 짧은 문장)
    mode: str = "pullback"       # "pullback" | "breakout"
    # 공통
    vol_ratio: float = 0.0       # 현재 거래량 / 20일 평균
    trade_amount_bn: float = 0.0 # 현재 거래대금 (억원)
    ma_trend_up: bool = False    # MA20 > MA60
    # breakout 전용
    box_high: float = 0.0        # 박스 상단
    breakout_pct: float = 0.0    # (현재가 - 박스 상단) / 박스 상단 * 100
    # pullback 전용
    in_entry_zone: bool = False  # 현재가가 entry_low~high 안에 있는지
    trough_low: float = 0.0      # 최근 N일 저점
    bounce_pct: float = 0.0      # (현재가 - 저점) / 저점 * 100

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "mode": self.mode,
            "vol_ratio": round(self.vol_ratio, 2),
            "trade_amount_bn": round(self.trade_amount_bn, 1),
            "ma_trend_up": self.ma_trend_up,
            "box_high": round(self.box_high, 0) if self.box_high else 0,
            "breakout_pct": round(self.breakout_pct, 2),
            "in_entry_zone": self.in_entry_zone,
            "trough_low": round(self.trough_low, 0) if self.trough_low else 0,
            "bounce_pct": round(self.bounce_pct, 2),
        }


def evaluate_pullback(
    ohlcv: list[dict],
    current_price: float,
    current_volume: int,
    entry_low: float,
    entry_high: float,
    current_trade_amount: int = 0,
    *,
    bounce_pct_min: float = 0.5,        # 최근 저점 대비 최소 회복율
    bounce_lookback: int = 5,           # 저점 산정 윈도우 (일)
    vol_ratio_min: float = 0.8,         # 거래량 위축 한도 (1.0 미만 OK, 0.5 미만은 dry-up 너무 깊음)
    min_trade_amount_bn: float = 50.0,
    require_ma_uptrend: bool = True,
    entry_zone_slack_pct: float = 1.0,  # entry_zone 양쪽 여유 (%)
) -> PivotResult:
    """눌림목 매수용 게이트 — LLM 진입대 + 저점 반등 + 거래량 보존 검증."""
    if not ohlcv or len(ohlcv) < max(bounce_lookback, 60) + 3:
        return PivotResult(False, f"OHLCV 부족 ({len(ohlcv)})", mode="pullback")

    closes = [float(d.get("stck_clpr", 0) or 0) for d in ohlcv]
    lows = [float(d.get("stck_lwpr", 0) or 0) for d in ohlcv]
    today_clpr = closes[0]
    same_day = today_clpr > 0 and abs(current_price - today_clpr) / today_clpr <= 0.005
    box_offset = 1 if same_day else 0

    # 1) entry_zone 체크 (slack 적용)
    slack = entry_zone_slack_pct / 100.0
    zone_low = entry_low * (1.0 - slack)
    zone_high = entry_high * (1.0 + slack)
    in_zone = bool(entry_low > 0 and entry_high > 0 and zone_low <= current_price <= zone_high)

    # 2) 최근 저점 산정 (오늘 봉 포함 — 장중 저점 반영)
    low_slice = lows[: bounce_lookback]
    low_slice = [l for l in low_slice if l > 0]
    trough = min(low_slice) if low_slice else 0
    bounce_pct = ((current_price - trough) / trough * 100) if trough > 0 else 0

    # 3) 거래량 (20일 평균 — 오늘 봉 제외)
    volumes = [int(d.get("acml_vol", 0) or 0) for d in ohlcv]
    vol_slice = volumes[box_offset : box_offset + 20]
    vol_avg20 = sum(vol_slice) / len(vol_slice) if vol_slice else 0
    vol_ratio = current_volume / vol_avg20 if vol_avg20 > 0 else 0.0

    # 4) MA 추세
    ma20 = _safe_ma(closes, 20, box_offset)
    ma60 = _safe_ma(closes, 60, box_offset)
    ma_trend_up = bool(ma20 and ma60 and ma20 > ma60)

    trade_amount_bn = current_trade_amount / 1e8

    result = PivotResult(
        passed=False, reason="", mode="pullback",
        vol_ratio=vol_ratio, trade_amount_bn=trade_amount_bn, ma_trend_up=ma_trend_up,
        in_entry_zone=in_zone, trough_low=trough, bounce_pct=bounce_pct,
    )

    if not in_zone:
        result.reason = (
            f"entry_zone 밖 (현재 {int(current_price):,} / 진입대 "
            f"{int(zone_low):,}~{int(zone_high):,})"
        )
        return result
    if bounce_pct < bounce_pct_min:
        result.reason = (
            f"저점 반등 부족 (저점 {int(trough):,}원 대비 {bounce_pct:+.2f}% "
            f"< 필요 +{bounce_pct_min:.1f}%)"
        )
        return result
    if vol_ratio < vol_ratio_min:
        result.reason = f"거래량 위축 심함 ({vol_ratio:.2f}x < {vol_ratio_min:.2f}x)"
        return result
    if trade_amount_bn < min_trade_amount_bn:
        result.reason = f"거래대금 부족 ({trade_amount_bn:.1f}억 < {min_trade_amount_bn:.0f}억)"
        return result
    if require_ma_uptrend and not ma_trend_up:
        result.reason = "하락 추세 (MA20≤MA60)"
        return result

    result.passed = True
    result.reason = (
        f"통과: 진입대 안 + 저점 +{bounce_pct:.2f}% 반등 + 거래량 {vol_ratio:.2f}x "
        f"대금 {trade_amount_bn:.0f}억 추세{'↑' if ma_trend_up else '?'}"
    )
    return result


def evaluate_breakout(
    ohlcv: list[dict],
    current_price: float,
    current_volume: int,
    current_trade_amount: int = 0,
    *,
    box_lookback: int = 20,
    breakout_pct_min: float = 0.3,
    breakout_pct_max: float = 5.0,
    vol_ratio_min: float = 1.5,
    min_trade_amount_bn: float = 50.0,
    require_ma_uptrend: bool = True,
) -> PivotResult:
    """피봇 게이트 평가.

    Args:
        ohlcv: KIS 일봉 응답. ohlcv[0]=최신(어제 종가 기준 일봉 = 오늘 직전).
               단, 장중 호출 시 ohlcv[0]은 "오늘 일봉"일 수도 있고 "어제 일봉"일 수도 있어
               함수 내부에서 자동 판별 (현재가가 ohlcv[0] 종가와 ±0.5% 이내면 오늘 봉으로 간주).
        current_price: 현재가
        current_volume: 당일 누적 거래량
        current_trade_amount: 당일 누적 거래대금 (원)
    """
    if not ohlcv or len(ohlcv) < box_lookback + 5:
        return PivotResult(False, f"OHLCV 부족 ({len(ohlcv)}/{box_lookback + 5})", mode="breakout")

    # 1) 오늘 봉 포함 여부 판별
    closes = [float(d.get("stck_clpr", 0) or 0) for d in ohlcv]
    highs = [float(d.get("stck_hgpr", 0) or 0) for d in ohlcv]
    today_clpr = closes[0]
    # 현재가와 ohlcv[0] 종가가 ±0.5% 이내면 ohlcv[0]을 "오늘 봉"으로 간주 → skip
    same_day = today_clpr > 0 and abs(current_price - today_clpr) / today_clpr <= 0.005
    box_offset = 1 if same_day else 0
    box_slice = highs[box_offset : box_offset + box_lookback]
    if len(box_slice) < box_lookback:
        return PivotResult(False, "박스 산정 데이터 부족", mode="breakout")
    box_high = max(box_slice) if box_slice else 0
    if box_high <= 0:
        return PivotResult(False, "박스 상단 계산 실패", mode="breakout")

    breakout_pct = (current_price - box_high) / box_high * 100

    # 2) 추세 게이트: MA20 > MA60 (상승 추세 위에서의 돌파만)
    ma20 = _safe_ma(closes, 20, box_offset)
    ma60 = _safe_ma(closes, 60, box_offset)
    ma_trend_up = bool(ma20 and ma60 and ma20 > ma60)

    # 3) 거래량 비율
    volumes = [int(d.get("acml_vol", 0) or 0) for d in ohlcv]
    vol_slice = volumes[box_offset : box_offset + box_lookback]
    vol_avg20 = sum(vol_slice) / len(vol_slice) if vol_slice else 0
    vol_ratio = current_volume / vol_avg20 if vol_avg20 > 0 else 0.0

    trade_amount_bn = current_trade_amount / 1e8  # 억원

    result = PivotResult(
        passed=False,
        reason="",
        mode="breakout",
        box_high=box_high,
        breakout_pct=breakout_pct,
        vol_ratio=vol_ratio,
        trade_amount_bn=trade_amount_bn,
        ma_trend_up=ma_trend_up,
    )

    # 게이트 평가 — 실패 사유는 가장 결정적인 한 가지만
    if breakout_pct < breakout_pct_min:
        result.reason = f"박스 미돌파 (현재 {breakout_pct:+.2f}%, 필요 ≥{breakout_pct_min:+.1f}%)"
        return result
    if breakout_pct > breakout_pct_max:
        result.reason = f"갭 추격 위험 (현재 {breakout_pct:+.2f}% > 상한 {breakout_pct_max:+.1f}%)"
        return result
    if vol_ratio < vol_ratio_min:
        result.reason = f"거래량 부족 ({vol_ratio:.2f}x < {vol_ratio_min:.2f}x)"
        return result
    if trade_amount_bn < min_trade_amount_bn:
        result.reason = f"거래대금 부족 ({trade_amount_bn:.1f}억 < {min_trade_amount_bn:.0f}억)"
        return result
    if require_ma_uptrend and not ma_trend_up:
        result.reason = f"하락 추세 (MA20≤MA60)"
        return result

    result.passed = True
    result.reason = (
        f"통과: 박스 +{breakout_pct:.2f}% 거래량 {vol_ratio:.2f}x "
        f"대금 {trade_amount_bn:.0f}억 추세{'↑' if ma_trend_up else '?'}"
    )
    return result


def _safe_ma(values: list[float], period: int, offset: int = 0) -> Optional[float]:
    """offset부터 period개 이동평균. 데이터 부족 시 None."""
    subset = values[offset : offset + period]
    subset = [v for v in subset if v > 0]
    if len(subset) < period:
        return None
    return sum(subset) / len(subset)
