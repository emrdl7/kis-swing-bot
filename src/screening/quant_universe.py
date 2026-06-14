"""정량 1차 유니버스 생성.

LLM이 시장 전체에서 종목을 추측하지 않도록 KIS 랭킹/시세/일봉 데이터로
먼저 스윙 후보군을 좁힌 뒤, LLM은 이 후보군 안에서 해석과 랭킹을 담당한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.config import ScreeningConfig
from src.data.kis_client import KisClient
from src.data.market_context import build_market_signal
from src.data.technical import compute_indicators

log = logging.getLogger(__name__)


@dataclass
class QuantCandidate:
    symbol: str
    name: str
    price: float
    change_pct: float
    market_cap_bn: int
    trade_amount_bn: float
    volume: int
    volume_ratio: float
    per: float
    pbr: float
    sector: str
    rsi14: float | None
    ma5: float | None
    ma20: float | None
    ma60: float | None
    atr14: float | None
    market_bias_score: float
    score: float
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": self.price,
            "change_pct": self.change_pct,
            "market_cap_bn": self.market_cap_bn,
            "trade_amount_bn": self.trade_amount_bn,
            "volume": self.volume,
            "volume_ratio": self.volume_ratio,
            "per": self.per,
            "pbr": self.pbr,
            "sector": self.sector,
            "rsi14": self.rsi14,
            "ma5": self.ma5,
            "ma20": self.ma20,
            "ma60": self.ma60,
            "atr14": self.atr14,
            "market_bias_score": self.market_bias_score,
            "score": self.score,
            "reasons": self.reasons,
        }


def build_quant_universe(
    kis: KisClient,
    cfg: ScreeningConfig,
    market_signal: dict | None = None,
) -> list[QuantCandidate]:
    """거래대금/거래증가율 상위 종목에서 스윙 후보군을 만든다."""
    if not cfg.quant_universe_enabled:
        return []
    market_signal = market_signal or build_market_signal(None)

    raw_items = []
    seen = set()
    for sort_by in ("3", "1"):  # 거래금액순, 거래증가율순
        try:
            for item in kis.get_volume_rank(sort_by=sort_by, min_volume=cfg.min_volume):
                sym = _symbol_of(item)
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                raw_items.append(item)
                if len(raw_items) >= cfg.quant_universe_top_n:
                    break
        except Exception as e:
            log.warning("정량 유니버스 랭킹 조회 실패(sort=%s): %s", sort_by, e)

    candidates: list[QuantCandidate] = []
    for item in raw_items:
        sym = _symbol_of(item)
        if not sym or _is_excluded_symbol(sym, item):
            continue
        try:
                qc = _evaluate_symbol(kis, cfg, sym, item, market_signal)
        except Exception as e:
            log.debug("[정량] %s 평가 실패: %s", sym, e)
            continue
        if qc:
            candidates.append(qc)

    candidates.sort(key=lambda c: c.score, reverse=True)
    result = candidates[: cfg.quant_universe_max_results]
    log.info("정량 유니버스 생성: raw=%d pass=%d save=%d", len(raw_items), len(candidates), len(result))
    return result


def format_for_llm(candidates: list[QuantCandidate], max_items: int = 40) -> str:
    """LLM 프롬프트용 정량 후보 요약."""
    if not candidates:
        return "정량 유니버스 없음"
    lines = ["[정량 1차 후보군 - 이 목록 안에서 우선 선정]"]
    for i, c in enumerate(candidates[:max_items], 1):
        rsi = f"{c.rsi14:.1f}" if c.rsi14 is not None else "-"
        ma = "-"
        if c.ma20 and c.ma60:
            ma = "MA20>MA60" if c.ma20 > c.ma60 else "MA20<=MA60"
        reasons = ", ".join(c.reasons[:3])
        bias = f" 시장연동 +{c.market_bias_score:.1f}" if c.market_bias_score > 0 else ""
        lines.append(
            f"{i}. {c.name}({c.symbol}) score={c.score:.1f} "
            f"현재가 {int(c.price):,}원 등락 {c.change_pct:+.2f}% "
            f"거래대금 {c.trade_amount_bn:.0f}억 시총 {c.market_cap_bn:,}억 "
            f"PER {c.per:.1f} RSI {rsi} {ma} 거래량 {c.volume_ratio:.1f}x{bias} "
            f"근거: {reasons}"
        )
    return "\n".join(lines)


def _evaluate_symbol(
    kis: KisClient,
    cfg: ScreeningConfig,
    symbol: str,
    item: dict,
    market_signal: dict,
) -> QuantCandidate | None:
    price_data = kis.get_price(symbol)
    price = float(price_data.get("stck_prpr", 0) or item.get("stck_prpr", 0) or 0)
    if price <= 0:
        return None

    name = price_data.get("hts_kor_isnm") or item.get("hts_kor_isnm") or symbol
    change_pct = float(price_data.get("prdy_ctrt", 0) or item.get("prdy_ctrt", 0) or 0)
    volume = int(price_data.get("acml_vol", 0) or item.get("acml_vol", 0) or 0)
    trade_amount = int(price_data.get("acml_tr_pbmn", 0) or item.get("acml_tr_pbmn", 0) or 0)
    trade_amount_bn = trade_amount / 1e8
    market_cap_bn = int(price_data.get("hts_avls", 0) or 0)
    per = float(price_data.get("per", 0) or 0)
    pbr = float(price_data.get("pbr", 0) or 0)
    eps = float(price_data.get("eps", 0) or 0)
    sector = str(price_data.get("bstp_kor_isnm") or item.get("bstp_kor_isnm") or "")

    if market_cap_bn and market_cap_bn < cfg.min_market_cap_bn:
        return None
    if trade_amount_bn < cfg.quant_universe_min_trade_amount_bn:
        return None
    if volume < cfg.min_volume:
        return None
    if eps < 0 or per < 0:
        return None
    if change_pct < -3.0 or change_pct > 6.0:
        return None

    ohlcv = kis.get_daily_ohlcv(symbol, count=130)
    ind = compute_indicators(ohlcv)
    if not ind:
        return None

    ma5 = ind.get("ma5")
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")
    rsi14 = ind.get("rsi14")
    atr14 = ind.get("atr14")
    vol_avg20 = ind.get("volume_avg20") or 0
    volume_ratio = volume / vol_avg20 if vol_avg20 else 0.0

    if rsi14 is not None and not (40 <= rsi14 <= 72):
        return None
    if ma20 and price < ma20 * 0.97:
        return None
    if ma20 and ma60 and ma20 < ma60 * 0.98:
        return None

    reasons = []
    score = 0.0
    if trade_amount_bn >= cfg.quant_universe_min_trade_amount_bn:
        score += min(25.0, trade_amount_bn / cfg.quant_universe_min_trade_amount_bn * 10.0)
        reasons.append("거래대금 충족")
    if volume_ratio >= 1.2:
        score += min(25.0, volume_ratio * 8.0)
        reasons.append(f"거래량 {volume_ratio:.1f}x")
    if ma20 and price >= ma20:
        score += 15.0
        reasons.append("MA20 위")
    if ma20 and ma60 and ma20 >= ma60:
        score += 15.0
        reasons.append("중기 추세 양호")
    if rsi14 is not None and 45 <= rsi14 <= 65:
        score += 12.0
        reasons.append("RSI 스윙 구간")
    if -1.5 <= change_pct <= 4.0:
        score += 8.0
        reasons.append("비과열 등락")
    market_bias_score, bias_name = _market_bias_for_candidate(symbol, name, sector, market_signal)
    if market_bias_score > 0:
        score += market_bias_score
        reasons.append(f"시장연동:{bias_name}")

    return QuantCandidate(
        symbol=symbol,
        name=name,
        price=price,
        change_pct=change_pct,
        market_cap_bn=market_cap_bn,
        trade_amount_bn=trade_amount_bn,
        volume=volume,
        volume_ratio=volume_ratio,
        per=per,
        pbr=pbr,
        sector=sector,
        rsi14=rsi14,
        ma5=ma5,
        ma20=ma20,
        ma60=ma60,
        atr14=atr14,
        market_bias_score=round(market_bias_score, 1),
        score=round(score, 1),
        reasons=reasons,
    )


def _symbol_of(item: dict) -> str:
    return str(item.get("mksc_shrn_iscd") or item.get("stck_shrn_iscd") or "").strip()


def _is_excluded_symbol(symbol: str, item: dict) -> bool:
    name = str(item.get("hts_kor_isnm") or "")
    upper_name = name.upper()
    if not symbol.isdigit() or len(symbol) != 6:
        return True
    excluded_words = ("KODEX", "TIGER", "KBSTAR", "ACE", "SOL ", "HANARO", "ARIRANG", "KOSEF", "ETN", "인버스", "레버리지")
    return any(w in upper_name or w in name for w in excluded_words)


def _market_bias_for_candidate(
    symbol: str,
    name: str,
    sector: str,
    market_signal: dict,
) -> tuple[float, str]:
    """시장 섹터 신호가 국내 후보와 맞으면 정량 점수에 가산."""
    haystack = f"{symbol} {name} {sector}".lower()
    best_score = 0.0
    best_name = ""
    quality = float(market_signal.get("quality_score", 0) or 0)
    if quality < 35:
        return 0.0, ""
    for bias in market_signal.get("sector_biases") or []:
        if not isinstance(bias, dict):
            continue
        keywords = [str(x) for x in (bias.get("domestic_keywords") or [])]
        if not any(k.lower() in haystack for k in keywords if k):
            continue
        strength = float(bias.get("strength", 0) or 0)
        # 품질 낮은 날에는 가산을 작게, 품질 좋은 날에는 유의미하게 반영.
        quality_mult = 0.6 if quality < 55 else 1.0
        score = min(14.0, 14.0 * strength * quality_mult)
        if score > best_score:
            best_score = score
            best_name = str(bias.get("name") or "시장섹터")
    return best_score, best_name
