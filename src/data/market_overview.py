"""시황 데이터 수집 — 국내 지수, 거래대금 상위, 외국인 순매수.

대시보드에서 가벼운 30초 캐싱으로 호출. KIS API 부담 최소화.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Optional

from src.core.clock import is_regular_market
from src.data.kis_client import KisClient

log = logging.getLogger(__name__)

# 지수 코드: KIS inquire-index-price 기준
_INDEX_CODES = {
    "kospi":     "0001",   # KOSPI
    "kosdaq":    "1001",   # KOSDAQ
    "kospi200":  "2001",   # KOSPI200
}

_CACHE: dict[str, tuple[dict, float]] = {}
_TTL_SEC = 30.0


def get_market_overview(kis: KisClient, ttl_sec: float = _TTL_SEC) -> dict:
    """시황 데이터 일괄 조회 (캐시 적용)."""
    now_ts = time.monotonic()
    cached = _CACHE.get("ov")
    if cached and (now_ts - cached[1]) < ttl_sec:
        return cached[0]
    data = _fetch_all(kis)
    _CACHE["ov"] = (data, now_ts)
    return data


def _fetch_all(kis: KisClient) -> dict:
    indexes = _fetch_indexes(kis)
    volume_rank = _fetch_volume_rank(kis, n=20)
    overview = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "indexes": indexes,
        "volume_rank": volume_rank,
        "foreign_buy": _fetch_foreign_buy(kis, n=10),
    }
    overview["decision"] = _derive_market_decision(indexes, volume_rank)
    return overview


def _fetch_indexes(kis: KisClient) -> dict[str, dict]:
    """KOSPI/KOSDAQ/KOSPI200 지수 조회."""
    result: dict[str, dict] = {}
    try:
        kis.ensure_token()
    except Exception as e:
        log.warning("토큰 확보 실패: %s", e)
        return result
    for key, code in _INDEX_CODES.items():
        try:
            url = f"{kis.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
            params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code}
            out = kis._get_with_retry(url, kis._headers("FHPUP02100000"), params).get("output", {})
            if not out:
                continue
            value = float(out.get("bstp_nmix_prpr", 0) or 0)
            chg_pct = float(out.get("bstp_nmix_prdy_ctrt", 0) or 0)
            chg_val = float(out.get("bstp_nmix_prdy_vrss", 0) or 0)
            tr_amt = int(out.get("acml_tr_pbmn", 0) or 0)
            result[key] = {
                "value": value,
                "chg_pct": chg_pct,
                "chg_val": chg_val,
                "trade_amount_bn": tr_amt / 1e8,
            }
        except Exception as e:
            log.warning("지수 [%s] 조회 실패: %s", key, e)
    return result


def _fetch_volume_rank(kis: KisClient, n: int = 10) -> list[dict]:
    """거래대금 상위 종목 (KRX 보통주 위주)."""
    try:
        kis.ensure_token()
        url = f"{kis.cfg.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",            # 전체 시장
            "FID_DIV_CLS_CODE": "1",             # 1=보통주 (ETN/ETF 제외)
            "FID_BLNG_CLS_CODE": "3",            # 3=거래대금 순
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        out = kis._get_with_retry(url, kis._headers("FHPST01710000"), params).get("output", []) or []
    except Exception as e:
        log.warning("거래대금 상위 조회 실패: %s", e)
        return []
    items = []
    for x in out[:n]:
        try:
            items.append({
                "symbol": x.get("mksc_shrn_iscd", ""),
                "name": x.get("hts_kor_isnm", ""),
                "price": int(float(x.get("stck_prpr", 0) or 0)),
                "chg_pct": float(x.get("prdy_ctrt", 0) or 0),
                "trade_amount_bn": int(x.get("acml_tr_pbmn", 0) or 0) / 1e8,
                "volume": int(x.get("acml_vol", 0) or 0),
            })
        except Exception:
            continue
    return items


def _fetch_foreign_buy(kis: KisClient, n: int = 10) -> list[dict]:
    """외국인 순매수 상위 — KIS endpoint 디버깅 필요, 현재는 빈 리스트."""
    return []


def _derive_market_decision(indexes: dict[str, dict], volume_rank: list[dict]) -> dict:
    """화면/후보선정 공통으로 쓰는 시장 판단 스냅샷.

    LLM 코멘트가 아니라 KIS 원자료에서 바로 산출하는 보수적 게이트다.
    """
    index_values = [v for v in indexes.values() if isinstance(v, dict)]
    chgs = [float(v.get("chg_pct", 0) or 0) for v in index_values]
    avg_chg = sum(chgs) / len(chgs) if chgs else 0.0
    neg_count = len([x for x in chgs if x < 0])
    pos_count = len([x for x in chgs if x > 0])
    kosdaq_chg = float((indexes.get("kosdaq") or {}).get("chg_pct", 0) or 0)
    kospi200_chg = float((indexes.get("kospi200") or {}).get("chg_pct", 0) or 0)
    spread_kosdaq_200 = kosdaq_chg - kospi200_chg

    valid_volume = [
        x for x in (volume_rank or [])
        if float(x.get("trade_amount_bn", 0) or 0) >= 50.0
    ]
    top_amount = max((float(x.get("trade_amount_bn", 0) or 0) for x in volume_rank or []), default=0.0)
    valid_count = len(valid_volume)
    rising_valid = len([x for x in valid_volume if float(x.get("chg_pct", 0) or 0) > 0])
    leadership_count = len([
        x for x in valid_volume
        if float(x.get("chg_pct", 0) or 0) >= 2.0
    ])
    overheated_count = len([
        x for x in valid_volume
        if float(x.get("chg_pct", 0) or 0) >= 8.0
    ])

    score = 50
    score += 12 if avg_chg >= 0.7 else 6 if avg_chg >= 0.15 else 0
    score -= 10 if avg_chg <= -0.7 else 5 if avg_chg <= -0.25 else 0
    score += 6 if pos_count >= 2 else 0
    score -= 8 if neg_count >= 2 else 0
    score -= 10 if kosdaq_chg <= -1.5 else 0
    score -= 6 if spread_kosdaq_200 <= -0.8 else 0
    score += 10 if valid_count >= 8 else 5 if valid_count >= 5 else -12
    score += 8 if leadership_count >= 4 else 3 if leadership_count >= 2 else 0
    score -= 8 if overheated_count >= 4 else 0
    if not is_regular_market():
        score -= 10
    score = max(0, min(100, score))

    if score >= 70:
        gate = "risk_on"
        gate_label = "확대 가능"
        stance = "정상 매수 가능"
        positioning = "정량 후보 중 거래대금·추세가 맞는 종목은 우선 검토"
    elif score >= 52:
        gate = "selective"
        gate_label = "선별 매수"
        stance = "선별 진입"
        positioning = "시장 연동 가점은 제한하고 개별 뉴스·수급·눌림 확인 후 진입"
    elif score >= 35:
        gate = "defensive"
        gate_label = "방어 선별"
        stance = "신규매수 축소"
        positioning = "후보 수를 줄이고 손절/유동성 기준을 강화"
    else:
        gate = "blocked"
        gate_label = "신규매수 보류"
        stance = "방어 우선"
        positioning = "자동 신규매수는 보류에 가깝게 보고 보유 리스크 관리 우선"

    risks = []
    opportunities = []
    if avg_chg <= -0.7:
        risks.append(f"3대 지수 평균 {avg_chg:+.2f}%")
    if kosdaq_chg <= -1.5:
        risks.append(f"KOSDAQ 약세 {kosdaq_chg:+.2f}%")
    if spread_kosdaq_200 <= -0.8:
        risks.append(f"중소형 성장주 상대약세 {spread_kosdaq_200:+.2f}%p")
    if valid_count < 5:
        risks.append(f"50억 이상 거래대금 종목 {valid_count}개")
    if overheated_count >= 4:
        risks.append(f"급등 과열 종목 {overheated_count}개")
    if not is_regular_market():
        risks.append("정규장 전/후 데이터: 거래대금 근거 제한")

    if avg_chg >= 0.15:
        opportunities.append(f"지수 평균 {avg_chg:+.2f}%")
    if valid_count >= 8:
        opportunities.append(f"유효 거래대금 {valid_count}개")
    if leadership_count >= 2:
        opportunities.append(f"강한 거래대금 상승 종목 {leadership_count}개")
    if rising_valid >= 5:
        opportunities.append(f"상승 유효대금 종목 {rising_valid}개")

    checks = [
        {
            "name": "지수 압력",
            "value": f"{avg_chg:+.2f}%",
            "state": "good" if avg_chg >= 0.15 else "risk" if avg_chg <= -0.7 else "watch",
            "impact": "시장 연동 가점" if avg_chg >= 0.15 else "후보 신뢰도 보수 적용",
        },
        {
            "name": "KOSDAQ 체력",
            "value": f"{kosdaq_chg:+.2f}%",
            "state": "risk" if kosdaq_chg <= -1.5 else "good" if kosdaq_chg > 0 else "watch",
            "impact": "성장주/테마 추격 제한" if kosdaq_chg <= -1.5 else "개별 후보 정상 검토",
        },
        {
            "name": "유효 거래대금",
            "value": f"{valid_count}개",
            "state": "good" if valid_count >= 8 else "risk" if valid_count < 5 else "watch",
            "impact": "50억 이상만 섹터 근거 허용",
        },
        {
            "name": "주도 집중",
            "value": f"{leadership_count}개",
            "state": "good" if leadership_count >= 4 else "watch" if leadership_count >= 2 else "risk",
            "impact": "주도주 후보 가점" if leadership_count >= 2 else "섹터 단정 금지",
        },
    ]

    return {
        "gate": gate,
        "gate_label": gate_label,
        "gate_score": score,
        "stance": stance,
        "positioning": positioning,
        "checks": checks,
        "risk_flags": risks[:6],
        "opportunity_flags": opportunities[:6],
        "metrics": {
            "index_avg_chg_pct": round(avg_chg, 2),
            "negative_index_count": neg_count,
            "positive_index_count": pos_count,
            "kosdaq_chg_pct": round(kosdaq_chg, 2),
            "kosdaq_vs_kospi200_pctp": round(spread_kosdaq_200, 2),
            "valid_volume_count": valid_count,
            "top_trade_amount_bn": round(top_amount, 1),
            "rising_valid_count": rising_valid,
            "leadership_count": leadership_count,
            "overheated_count": overheated_count,
            "regular_market": is_regular_market(),
        },
    }


def format_market_decision_for_llm(overview: dict) -> str:
    decision = overview.get("decision") if isinstance(overview.get("decision"), dict) else {}
    if not decision:
        return "시장 판단 스냅샷 없음"
    checks = decision.get("checks") or []
    lines = [
        "[시장 판단 스냅샷 - 후보선정 정책]",
        f"- 매수 게이트: {decision.get('gate_label')} ({decision.get('gate_score')}점) / {decision.get('stance')}",
        f"- 후보 적용: {decision.get('positioning')}",
    ]
    if checks:
        lines.append("- 핵심 체크:")
        for c in checks[:5]:
            lines.append(
                f"  * {c.get('name')}: {c.get('value')} [{c.get('state')}] - {c.get('impact')}"
            )
    risks = decision.get("risk_flags") or []
    if risks:
        lines.append("- 위험 플래그: " + "; ".join(str(x) for x in risks[:5]))
    opps = decision.get("opportunity_flags") or []
    if opps:
        lines.append("- 기회 플래그: " + "; ".join(str(x) for x in opps[:5]))
    return "\n".join(lines)
