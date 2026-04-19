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

    return {
        "ma5": round(ma5, 0) if ma5 else None,
        "ma20": round(ma20, 0) if ma20 else None,
        "ma60": round(ma60, 0) if ma60 else None,
        "atr14": round(atr14, 0) if atr14 else None,
        "rsi14": round(rsi14, 1) if rsi14 else None,
        "volume_avg20": int(vol_avg20) if vol_avg20 else None,
        "last_close": int(last_close),
        "last_volume": last_volume,
        "above_ma20": above_ma20,
        "trend_up": trend_up,
        "support_resistance": sr,
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
        lines.append(f"  MA5: {int(ind['ma5']):,}  MA20: {int(ind['ma20']):,}  MA60: {int(ind.get('ma60', 0) or 0):,}")
    if ind.get("atr14"):
        lines.append(f"  ATR14: {int(ind['atr14']):,}  RSI14: {ind.get('rsi14', 'N/A')}")
    lines.append(f"  20일 평균거래량: {ind.get('volume_avg20', 'N/A'):,}  오늘거래량: {ind.get('last_volume', 0):,}")
    lines.append(f"  MA20 위: {ind.get('above_ma20')}  상승추세: {ind.get('trend_up')}")
    sr = ind.get("support_resistance", {})
    if sr.get("resistance"):
        lines.append(f"  저항선: {', '.join(f'{int(r):,}원' for r in sr['resistance'])}")
    if sr.get("support"):
        lines.append(f"  지지선: {', '.join(f'{int(s):,}원' for s in sr['support'])}")
    return "\n".join(lines)
