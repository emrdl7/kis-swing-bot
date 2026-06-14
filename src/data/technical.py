"""기술적 지표 계산 (일봉 데이터 기반)."""
from __future__ import annotations
import logging
from typing import Optional

log = logging.getLogger(__name__)


def compute_support_resistance(
    ohlcv: list[dict],
    window: int = 5,
    cluster_tol: float = 0.02,
    max_levels: int = 3,
) -> dict:
    """스윙 고/저점 클러스터링으로 지지·저항 레벨 탐지.

    Returns:
        {"resistance": [가장 가까운 저항선, ...], "support": [가장 가까운 지지선, ...]}
    """
    if not ohlcv or len(ohlcv) < window * 2 + 1:
        return {"resistance": [], "support": []}

    # ohlcv[0]=최신, [-1]=과거 → 시계열 순서로 뒤집어 탐색
    data = list(reversed(ohlcv))
    highs = [float(d.get("stck_hgpr", 0) or 0) for d in data]
    lows  = [float(d.get("stck_lwpr", 0) or 0) for d in data]
    closes = [float(d.get("stck_clpr", 0) or 0) for d in data]

    current = closes[-1] if closes else 0
    if current <= 0:
        return {"resistance": [], "support": []}

    swing_highs: list[float] = []
    swing_lows:  list[float] = []
    n = len(highs)
    for i in range(window, n - window):
        h, l = highs[i], lows[i]
        if h > 0 and all(h >= highs[i - j] for j in range(1, window + 1)) \
                 and all(h >= highs[i + j] for j in range(1, window + 1)):
            swing_highs.append(h)
        if l > 0 and all(l <= lows[i - j] for j in range(1, window + 1)) \
                 and all(l <= lows[i + j] for j in range(1, window + 1)):
            swing_lows.append(l)

    # 저항: 현재가 위 스윙하이, 오름차순 클러스터링 → 가까운 순
    res_raw = sorted(h for h in swing_highs if h > current)
    resistance = _cluster_levels(res_raw, cluster_tol, max_levels)

    # 지지: 현재가 아래 스윙로우, 오름차순 클러스터링 후 내림차순(가까운 순)
    sup_raw = sorted(l for l in swing_lows if l < current)
    support = sorted(_cluster_levels(sup_raw, cluster_tol, max_levels), reverse=True)

    return {"resistance": resistance, "support": support}


def _cluster_levels(levels: list[float], tol: float, max_n: int) -> list[int]:
    if not levels:
        return []
    clusters: list[list[float]] = []
    current_group: list[float] = [levels[0]]
    for price in levels[1:]:
        ref = sum(current_group) / len(current_group)
        if ref > 0 and abs(price - ref) / ref <= tol:
            current_group.append(price)
        else:
            clusters.append(current_group)
            current_group = [price]
    clusters.append(current_group)
    return [round(sum(g) / len(g)) for g in clusters[:max_n]]


def compute_indicators(ohlcv: list[dict]) -> dict:
    """KIS 일봉 API 응답으로부터 기술적 지표 계산.

    Returns dict with keys:
        ma5, ma20, ma60, atr14, rsi14,
        volume_avg20, last_close, last_volume,
        above_ma20 (bool), trend_up (bool)
    """
    if not ohlcv or len(ohlcv) < 5:
        return {}

    closes = [float(d.get("stck_clpr", 0) or 0) for d in ohlcv]
    highs = [float(d.get("stck_hgpr", 0) or 0) for d in ohlcv]
    lows = [float(d.get("stck_lwpr", 0) or 0) for d in ohlcv]
    volumes = [int(d.get("acml_vol", 0) or 0) for d in ohlcv]

    # 이동평균
    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    ma120 = _ma(closes, 120)

    # ATR14
    atr14 = _atr(highs, lows, closes, 14)

    # RSI14
    rsi14 = _rsi(closes, 14)

    # 거래량 평균
    vol_avg20 = _ma(volumes, 20)

    last_close = closes[0] if closes else 0
    last_volume = volumes[0] if volumes else 0

    above_ma20 = (last_close > ma20) if (last_close and ma20) else False
    # 단순 상승추세: 5일선 > 20일선
    trend_up = (ma5 > ma20) if (ma5 and ma20) else False

    sr = compute_support_resistance(ohlcv)
    risk_flags = compute_risk_flags(ohlcv)

    return {
        "ma5": round(ma5, 0) if ma5 else None,
        "ma20": round(ma20, 0) if ma20 else None,
        "ma60": round(ma60, 0) if ma60 else None,
        "ma120": round(ma120, 0) if ma120 else None,
        "atr14": round(atr14, 0) if atr14 else None,
        "rsi14": round(rsi14, 1) if rsi14 else None,
        "volume_avg20": int(vol_avg20) if vol_avg20 else None,
        "last_close": int(last_close),
        "last_volume": last_volume,
        "above_ma20": above_ma20,
        "trend_up": trend_up,
        "support_resistance": sr,
        "risk_flags": risk_flags,
    }


def compute_risk_flags(ohlcv: list[dict], lookback: int = 12) -> dict:
    """최근 급등 후 붕괴 패턴을 보수적으로 표시한다.

    KIS 응답은 최신 봉이 0번이다. 상한가에 가까운 일간 수익률이 짧은 기간에
    반복된 뒤 고점 대비 크게 밀리면 스윙 자동매수 후보에서 추격하지 않는다.
    """
    if not ohlcv or len(ohlcv) < 6:
        return {"avoid_chasing": False, "reasons": []}

    rows = ohlcv[: max(6, min(len(ohlcv), lookback + 2))]
    closes = [float(d.get("stck_clpr", 0) or 0) for d in rows]
    highs = [float(d.get("stck_hgpr", 0) or 0) for d in rows]
    lows = [float(d.get("stck_lwpr", 0) or 0) for d in rows]
    if not closes or closes[0] <= 0 or not any(highs):
        return {"avoid_chasing": False, "reasons": []}

    daily_returns: list[float] = []
    for i in range(min(len(closes) - 1, lookback)):
        prev = closes[i + 1]
        daily_returns.append((closes[i] / prev - 1) * 100 if prev > 0 else 0.0)

    limit_like_days = [i for i, ret in enumerate(daily_returns) if ret >= 25.0]
    strong_jump_days = [i for i, ret in enumerate(daily_returns) if ret >= 18.0]
    recent_high = max(highs[: min(len(highs), lookback)])
    recent_low_before_high = min([v for v in lows[: min(len(lows), lookback)] if v > 0] or [0])
    drawdown_from_high = (closes[0] / recent_high - 1) * 100 if recent_high > 0 else 0.0
    runup_from_low = (recent_high / recent_low_before_high - 1) * 100 if recent_low_before_high > 0 else 0.0

    reasons: list[str] = []
    if len(limit_like_days) >= 2 and drawdown_from_high <= -10:
        reasons.append(f"최근 {lookback}일 내 상한가급 급등 {len(limit_like_days)}회 후 고점대비 {drawdown_from_high:.1f}%")
    elif len(strong_jump_days) >= 2 and runup_from_low >= 55 and drawdown_from_high <= -15:
        reasons.append(f"단기 급등률 {runup_from_low:.1f}% 후 고점대비 {drawdown_from_high:.1f}%")
    elif len(limit_like_days) >= 1 and drawdown_from_high <= -22:
        reasons.append(f"상한가급 급등 후 고점대비 {drawdown_from_high:.1f}%")

    return {
        "avoid_chasing": bool(reasons),
        "reasons": reasons,
        "recent_limit_like_days": len(limit_like_days),
        "recent_strong_jump_days": len(strong_jump_days),
        "drawdown_from_high_pct": round(drawdown_from_high, 1),
        "runup_from_low_pct": round(runup_from_low, 1),
    }


def _ma(values: list, period: int) -> Optional[float]:
    subset = [v for v in values[:period] if v > 0]
    if len(subset) < period:
        return None
    return sum(subset) / len(subset)


def _atr(highs: list, lows: list, closes: list, period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(period):
        h, l, pc = highs[i], lows[i], closes[i + 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def _rsi(closes: list, period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(period):
        diff = closes[i] - closes[i + 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def format_for_llm(symbol: str, name: str, ind: dict) -> str:
    """LLM 프롬프트용 기술적 지표 텍스트."""
    if not ind:
        return f"{name}({symbol}): 지표 계산 불가"
    lines = [f"[{name}/{symbol}] 기술적 지표"]
    lines.append(f"  현재가: {ind.get('last_close', 'N/A'):,}")
    if ind.get("ma5"):
        lines.append(
            f"  MA5: {int(ind.get('ma5') or 0):,}  "
            f"MA20: {int(ind.get('ma20') or 0):,}  "
            f"MA60: {int(ind.get('ma60') or 0):,}"
        )
    if ind.get("atr14"):
        lines.append(f"  ATR14: {int(ind['atr14']):,}  RSI14: {ind.get('rsi14', 'N/A')}")
    avg_vol = ind.get("volume_avg20")
    avg_vol_text = f"{int(avg_vol):,}" if avg_vol else "N/A"
    lines.append(f"  20일 평균거래량: {avg_vol_text}  오늘거래량: {int(ind.get('last_volume') or 0):,}")
    lines.append(f"  MA20 위: {ind.get('above_ma20')}  상승추세: {ind.get('trend_up')}")
    risk_flags = ind.get("risk_flags") or {}
    if risk_flags.get("avoid_chasing"):
        reasons = "; ".join(str(x) for x in (risk_flags.get("reasons") or [])[:2])
        lines.append(f"  추격매수 위험: {reasons or '급등 후 붕괴 패턴'}")
    sr = ind.get("support_resistance", {})
    if sr.get("resistance"):
        lines.append(f"  저항선: {', '.join(f'{int(r):,}원' for r in sr['resistance'])}")
    if sr.get("support"):
        lines.append(f"  지지선: {', '.join(f'{int(s):,}원' for s in sr['support'])}")
    return "\n".join(lines)
