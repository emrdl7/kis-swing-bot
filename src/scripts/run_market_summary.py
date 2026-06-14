"""LLM 시황 요약 — 영업일 08:45 실행.

뉴스 + 야간 미국시장 + 국내 지수 → codex가 1~2단락 요약.
state/market_summary.json에 저장 → 대시보드 시황 카드에 표시.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

from src.core.config import load_config
from src.core import state_store
from src.core.clock import now_kst, today_label, is_trading_day, is_regular_market
from src.data.kis_client import KisClient
from src.data.news_fetcher import fetch_news, format_for_llm as news_fmt
from src.data.overnight import fetch_overnight_delta, format_us_market
from src.data.market_overview import get_market_overview, format_market_decision_for_llm
from src.data.market_context import build_market_signal, summarize_analysis_for_legacy
from src.agents.base_agent import extract_json
from src.agents.llm_client import LLMClient
from src.utils.logging_setup import setup

log = setup("market_summary")

MIN_VOLUME_EVIDENCE_BN = 50.0


def _format_indexes(indexes: dict) -> str:
    if not indexes:
        return "지수 데이터 없음"
    lines = []
    for label, key in (("KOSPI", "kospi"), ("KOSDAQ", "kosdaq"), ("KOSPI200", "kospi200")):
        v = indexes.get(key)
        if not v:
            continue
        lines.append(f"- {label}: {v['value']:.2f} ({v['chg_pct']:+.2f}%, {v['chg_val']:+.2f})")
    return "\n".join(lines) if lines else "지수 데이터 없음"


def _derive_structured_fallback(indexes_text: str, vol_text: str, news_text: str, us_text: str) -> dict:
    gaps = []
    if "데이터 없음" in us_text:
        gaps.append("미국 시장 데이터 없음")
    if news_text.strip() in ("뉴스 데이터 없음", "뉴스 없음", "오늘 수집된 뉴스 없음", ""):
        gaps.append("뉴스 데이터 부족")
    if "데이터 없음" in vol_text:
        gaps.append("거래대금 상위 데이터 부족")
    if "유효 거래대금" in vol_text:
        gaps.append("거래대금 상위 유동성 제한")
    if "정규장 전" in vol_text:
        gaps.append("거래대금 상위 유동성 제한")
    if indexes_text and "지수 데이터 없음" not in indexes_text:
        zero_index_lines = [line for line in indexes_text.splitlines() if "(+0.00%" in line or "(-0.00%" in line]
        if len(zero_index_lines) >= 3:
            gaps.append("국내 지수 변동률 0.00% 중심")
    if vol_text and "거래대금 데이터 없음" not in vol_text:
        amounts = _extract_trade_amounts_bn(vol_text)
        if amounts and (len(amounts) < 8 or max(amounts) < 50 or any(v < 1 for v in amounts)):
            gaps.append("거래대금 상위 유동성 제한")
    return {
        "tone": "관망",
        "confidence": 35 if gaps else 50,
        "market_read": "충분한 뉴스/수급 근거가 부족해 섹터 방향성을 강하게 단정하지 않음.",
        "leading_sectors": [],
        "avoid_or_risks": [
            {"name": "무근거 테마 추격", "reason": "확인된 뉴스·수급 근거 없이 테마명을 붙인 후보는 제외"}
        ],
        "selection_bias": [
            "정량 후보군 안에서 거래대금·추세가 확인되는 종목 우선",
            "시장 주도 섹터 근거가 약하면 개별 공시/뉴스가 분명한 종목만 예외 허용",
        ],
        "watch_keywords": [],
        "evidence": [x for x in (indexes_text, vol_text[:800]) if x],
        "data_gaps": gaps,
    }


def _extract_trade_amounts_bn(text: str) -> list[float]:
    if "거래대금" not in text and "대금" not in text:
        return []
    return [float(x.replace(",", "")) for x in re.findall(r"(?:대금\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*억", text)]


def _format_volume_rank_for_summary(overview: dict, now: datetime, min_amount_bn: float) -> str:
    """LLM에 넣을 국내 거래대금 근거를 보수적으로 정제한다."""
    if not is_regular_market(now):
        return "정규장 전: 국내 거래대금 상위 데이터는 종목선정/섹터 근거로 사용하지 않음"

    items = overview.get("volume_rank") or []
    valid = [x for x in items if float(x.get("trade_amount_bn", 0) or 0) >= min_amount_bn]
    if not valid:
        return f"거래대금 데이터 없음 (유효 거래대금 {min_amount_bn:.0f}억 이상 없음)"

    lines = []
    for x in valid[:8]:
        lines.append(f"- {x['name']}({x['symbol']}) {x['chg_pct']:+.2f}% 대금 {x['trade_amount_bn']:.0f}억")
    return "\n".join(lines) if lines else f"거래대금 데이터 없음 (유효 거래대금 {min_amount_bn:.0f}억 이상 없음)"


def _normalize_analysis(data, fallback: dict) -> dict:
    if not isinstance(data, dict):
        return fallback
    result = dict(fallback)
    for key in (
        "tone", "confidence", "market_read", "leading_sectors", "avoid_or_risks",
        "selection_bias", "watch_keywords", "evidence", "data_gaps",
    ):
        if key in data:
            result[key] = data[key]
    try:
        result["confidence"] = max(0, min(100, int(float(result.get("confidence", 0)))))
    except Exception:
        result["confidence"] = fallback.get("confidence", 35)
    for list_key in ("leading_sectors", "avoid_or_risks", "selection_bias", "watch_keywords", "evidence", "data_gaps"):
        if not isinstance(result.get(list_key), list):
            result[list_key] = []
    _sanitize_low_liquidity_evidence(result, fallback)
    return result


def _sanitize_low_liquidity_evidence(analysis: dict, fallback: dict) -> None:
    gaps = list(fallback.get("data_gaps") or []) + list(analysis.get("data_gaps") or [])
    liquidity_limited = any("거래대금 상위 유동성 제한" in str(g) for g in gaps)
    if not liquidity_limited:
        return

    kept = []
    removed = []
    for sector in analysis.get("leading_sectors") or []:
        if not isinstance(sector, dict):
            continue
        evidence_text = " ".join(str(x) for x in (sector.get("evidence") or []))
        if _uses_invalid_volume_evidence(evidence_text):
            removed.append(str(sector.get("name") or "미분류"))
            continue
        kept.append(sector)
    analysis["leading_sectors"] = kept

    analysis["evidence"] = [
        x for x in (analysis.get("evidence") or [])
        if not _uses_invalid_volume_evidence(str(x))
    ]
    analysis["selection_bias"] = [
        x for x in (analysis.get("selection_bias") or [])
        if not _uses_invalid_volume_bias(str(x))
    ]
    if removed:
        analysis.setdefault("avoid_or_risks", []).append({
            "name": "소액 거래대금 테마 추격",
            "reason": f"{', '.join(removed[:3])} 섹터 근거에서 50억 미만 거래대금이 사용되어 자동선정 근거에서 제외",
        })
    bias = "거래대금 50억 미만 또는 정규장 전 거래대금은 섹터/자동매수 근거로 사용 금지"
    if bias not in analysis.setdefault("selection_bias", []):
        analysis["selection_bias"].insert(0, bias)


def _uses_invalid_volume_evidence(text: str) -> bool:
    if not text:
        return False
    if "정규장 전" in text and "거래대금" in text:
        return True
    if "거래대금" not in text and "대금" not in text:
        return False
    amounts = _extract_trade_amounts_bn(text)
    return bool(amounts) and max(amounts) < MIN_VOLUME_EVIDENCE_BN


def _uses_invalid_volume_bias(text: str) -> bool:
    if not text:
        return False
    amounts = _extract_trade_amounts_bn(text)
    if amounts and max(amounts) < MIN_VOLUME_EVIDENCE_BN:
        return True
    return "거래대금 상위" in text or "국내 거래대금" in text


def main() -> int:
    if not is_trading_day():
        log.info("비영업일 — 시황 요약 스킵")
        return 0

    cfg = load_config()
    now = now_kst()
    log.info("=== 시황 요약 생성 [%s] ===", today_label(now))

    kis = KisClient(cfg.kis)

    # 1) 야간 미국시장
    us_market_raw = None
    try:
        delta = fetch_overnight_delta(prelim_symbols=[], kis_client=kis, news_sources=cfg.news.sources or None)
        us_market_raw = delta.get("us_market")
        us_text = format_us_market(us_market_raw)
    except Exception as e:
        log.warning("야간시장 수집 실패: %s", e)
        us_text = "야간시장 데이터 없음"

    # 2) 국내 지수
    try:
        overview = get_market_overview(kis, ttl_sec=0)
        indexes_text = _format_indexes(overview.get("indexes") or {})
        min_amount_bn = max(MIN_VOLUME_EVIDENCE_BN, float(cfg.screening.min_trade_amount or 0) / 1e8)
        vol_text = _format_volume_rank_for_summary(overview, now, min_amount_bn)
        decision_text = format_market_decision_for_llm(overview)
    except Exception as e:
        log.warning("국내 지수 수집 실패: %s", e)
        indexes_text = "지수 데이터 없음"
        vol_text = "거래대금 데이터 없음"
        decision_text = "시장 판단 스냅샷 없음"
        overview = {}

    # 3) 뉴스 (조간 위주)
    try:
        items = fetch_news(sources=cfg.news.sources or None, max_age_hours=14)
        news_text = news_fmt(items, max_items=25)
    except Exception as e:
        log.warning("뉴스 수집 실패: %s", e)
        news_text = "뉴스 데이터 없음"

    kis.close()

    # 4) LLM 근거 기반 구조화 분석
    llm = LLMClient(
        codex_model=cfg.agents.codex_model,
        gemini_model=cfg.agents.gemini_model,
        max_tokens=cfg.agents.max_tokens, primary=cfg.agents.primary,
    )

    system = """당신은 한국 주식 스윙봇의 시장 레짐 분석가입니다.
목표는 보기 좋은 시황 코멘트가 아니라, 오늘 종목선정 프롬프트에 들어갈 '선정 바이어스'를 만드는 것입니다.

규칙:
- 입력 데이터에 없는 내용을 만들지 마십시오.
- 뉴스/미국시장 데이터가 없으면 없다고 명시하고 신뢰도를 낮추십시오.
- 뜬구름 표현 금지: "관망", "종목별 장세"라고 쓸 때도 근거와 후보선정에 미치는 영향을 적으십시오.
- 특정 섹터를 말하려면 거래대금 상위, 뉴스 헤드라인, 야간시장 중 최소 하나의 근거를 evidence에 넣으십시오.
- 정규장 전 거래대금, 거래대금 50억 미만, NXT 거래대금만으로 특정 종목/섹터를 주도주로 판단하지 마십시오.
- 상한가급 급등 이후 고점 대비 급락 중인 종목은 추격 매수 회피 대상으로 분류하십시오.
- 순수 JSON 객체만 출력하십시오."""

    user = f"""[{today_label(now)}] 장 시작 전 시황 정리

=== 야간 미국시장 ===
{us_text}

=== 국내 지수 (전일 마감 기준 또는 시간외) ===
{indexes_text}

=== 거래대금 상위 ===
{vol_text}

=== 시장 판단 스냅샷 ===
{decision_text}

=== 조간 뉴스 헤드라인 ===
{news_text[:2500]}

반드시 아래 JSON 스키마로만 출력:
{{
  "tone": "강세|약세|혼조|관망",
  "confidence": 0~100,
  "market_read": "근거 기반 핵심 판단 1~2문장",
  "leading_sectors": [
    {{"name": "섹터/테마명", "symbols": ["관련 종목명 또는 코드"], "evidence": ["구체 근거"]}}
  ],
  "avoid_or_risks": [
    {{"name": "회피/주의 대상", "reason": "구체 이유"}}
  ],
  "selection_bias": [
    "시장 판단 스냅샷의 매수 게이트와 핵심 체크를 반영한 구체 규칙"
  ],
  "watch_keywords": ["키워드"],
  "evidence": ["판정에 사용한 핵심 데이터"],
  "data_gaps": ["부족한 데이터"]
}}"""

    log.info("LLM 호출...")
    raw = llm.chat(system=system, user=user) or ""
    fallback = _derive_structured_fallback(indexes_text, vol_text, news_text, us_text)
    analysis = _normalize_analysis(extract_json(raw), fallback)
    fallback_gaps = fallback.get("data_gaps") or []
    for gap in fallback_gaps:
        if gap not in analysis["data_gaps"]:
            analysis["data_gaps"].append(gap)
    market_signal = build_market_signal({
        "analysis": analysis,
        "us_market": us_text,
        "us_market_raw": us_market_raw,
        "market_decision": overview.get("decision") if isinstance(overview, dict) else {},
    })
    summary_text = summarize_analysis_for_legacy(analysis)

    summary = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": summary_text,
        "analysis": analysis,
        "market_signal": market_signal,
        "indexes": indexes_text,
        "us_market": us_text,
        "us_market_raw": us_market_raw,
        "volume_rank": vol_text,
        "market_decision": overview.get("decision") if isinstance(overview, dict) else {},
        "market_decision_text": decision_text,
        "news_sample": news_text[:1500],
    }
    state_store.save_market_summary(summary)
    log.info("시황 분석 저장 완료 tone=%s confidence=%s", analysis.get("tone"), analysis.get("confidence"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
