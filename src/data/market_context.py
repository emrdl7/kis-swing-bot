"""시장 흐름 분석 결과를 종목선정 프롬프트와 검증 로직에 주입한다."""
from __future__ import annotations

import re
from typing import Any


SECTOR_BIAS_RULES = [
    {
        "name": "반도체/AI",
        "triggers": [
            "반도체", "semiconductor", "soxx", "smh", "nvda", "nvidia", "amd",
            "필라델피아 반도체", "hbm", "ai반도체",
        ],
        "domestic_keywords": [
            "반도체", "HBM", "AI반도체", "메모리", "소부장", "장비", "소재",
            "삼성전자", "SK하이닉스", "한미반도체", "HPSP", "리노공업",
            "에이치피에스피", "이오테크닉스", "원익IPS", "원익아이피에스",
            "테크윙", "피에스케이", "ISC", "하나마이크론", "주성엔지니어링", "유진테크",
        ],
    },
    {
        "name": "전기차/2차전지",
        "triggers": ["전기차", "2차전지", "배터리", "ev", "tesla", "tsla", "lit"],
        "domestic_keywords": [
            "2차전지", "배터리", "양극재", "음극재", "전해액", "셀", "전기차",
            "LG에너지솔루션", "삼성SDI", "포스코퓨처엠", "에코프로", "엘앤에프",
        ],
    },
    {
        "name": "바이오/헬스케어",
        "triggers": ["바이오", "헬스케어", "제약", "bio", "healthcare", "ibb", "xbi"],
        "domestic_keywords": [
            "바이오", "헬스케어", "제약", "신약", "CDMO", "삼성바이오로직스",
            "셀트리온", "유한양행", "알테오젠",
        ],
    },
    {
        "name": "인터넷/플랫폼",
        "triggers": ["인터넷", "플랫폼", "software", "cloud", "클라우드", "ai서비스"],
        "domestic_keywords": ["인터넷", "플랫폼", "AI", "클라우드", "NAVER", "카카오", "더존비즈온"],
    },
    {
        "name": "방산/우주",
        "triggers": ["방산", "국방", "우주", "defense", "aerospace"],
        "domestic_keywords": ["방산", "우주", "항공", "한화에어로스페이스", "LIG넥스원", "한국항공우주"],
    },
    {
        "name": "조선/해운",
        "triggers": ["조선", "해운", "lng", "shipbuilding", "shipping"],
        "domestic_keywords": ["조선", "해운", "LNG", "선박", "HD현대중공업", "한화오션", "삼성중공업"],
    },
]


def build_market_signal(summary: dict[str, Any] | None) -> dict[str, Any]:
    """시장 분석 결과를 후보 선정용 정책으로 변환한다.

    LLM이 만든 코멘트를 그대로 믿지 않고, 데이터 공백/근거량/신뢰도를 이용해
    코드 레벨의 최소 신뢰도와 유동성 기준을 조정한다.
    """
    if not summary:
        return {
            "quality_score": 20,
            "strictness": "high",
            "confidence": 0,
            "data_gaps": ["시장흐름 분석 없음"],
            "leading_sectors": [],
            "watch_keywords": [],
            "sector_biases": [],
            "min_consensus_boost": 0.12,
            "trade_amount_multiplier": 1.5,
            "max_candidates_cap": 3,
            "notes": ["시장분석 부재: 정량 후보, 거래대금, 개별 뉴스가 강한 종목만 허용"],
        }

    analysis = summary.get("analysis") if isinstance(summary.get("analysis"), dict) else {}
    decision = summary.get("market_decision") if isinstance(summary.get("market_decision"), dict) else {}
    confidence = _as_int(analysis.get("confidence"), default=0)
    gaps = _as_list(analysis.get("data_gaps"))
    evidence = _as_list(analysis.get("evidence"))
    sectors = [s for s in _as_list(analysis.get("leading_sectors")) if isinstance(s, dict)]
    keywords = _as_list(analysis.get("watch_keywords"))
    liquidity_limited = any("거래대금 상위 유동성 제한" in str(g) for g in gaps)
    if liquidity_limited:
        sectors = [s for s in sectors if not _uses_low_liquidity_evidence(" ".join(str(x) for x in _as_list(s.get("evidence"))))]
    sector_biases = [] if liquidity_limited else derive_sector_biases(summary)

    sector_evidence_count = 0
    for s in sectors:
        sector_evidence_count += len(_as_list(s.get("evidence")))

    quality = confidence
    quality += min(15, len(evidence) * 3)
    quality += min(10, sector_evidence_count * 2)
    quality -= min(35, len(gaps) * 8)
    quality = max(0, min(100, quality))

    if quality < 45 or len(gaps) >= 3:
        strictness = "high"
        boost = 0.12
        trade_mult = 1.5
        cap = 3
    elif quality < 65 or len(gaps) >= 1:
        strictness = "medium"
        boost = 0.06
        trade_mult = 1.2
        cap = 5
    else:
        strictness = "normal"
        boost = 0.0
        trade_mult = 1.0
        cap = None

    gate = str(decision.get("gate") or "")
    gate_score = _as_int(decision.get("gate_score"), default=0)
    if gate in ("blocked", "defensive"):
        strictness = "high"
        boost = max(boost, 0.12 if gate == "defensive" else 0.16)
        trade_mult = max(trade_mult, 1.5 if gate == "defensive" else 1.8)
        cap = min(cap or 99, 3 if gate == "defensive" else 2)
        quality = max(0, min(quality, gate_score + 10 if gate_score else quality))
    elif gate == "selective":
        if strictness == "normal":
            strictness = "medium"
        boost = max(boost, 0.05)
        trade_mult = max(trade_mult, 1.15)
        cap = min(cap or 99, 5)
    elif gate == "risk_on" and quality >= 60:
        trade_mult = min(trade_mult, 1.0)

    notes = []
    if gaps:
        notes.append("데이터 공백 존재: 거시/테마 단정 가중치 축소")
    if liquidity_limited:
        notes.append("50억 미만/정규장 전 거래대금 근거는 섹터 바이어스에서 제외")
    if sector_biases and quality >= 45:
        names = ", ".join(str(b.get("name")) for b in sector_biases[:3])
        notes.append(f"미국장/시황 섹터 신호를 국내 후보군에 매핑: {names}")
    if strictness != "normal":
        notes.append("후보 검증에서 최소 신뢰도와 거래대금 기준 상향")
    if decision:
        notes.append(
            f"시장 게이트 {decision.get('gate_label', gate) or gate}: "
            f"{decision.get('positioning') or '후보선정 보수 적용'}"
        )

    return {
        "quality_score": quality,
        "strictness": strictness,
        "confidence": confidence,
        "data_gaps": gaps,
        "leading_sectors": sectors,
        "watch_keywords": keywords,
        "sector_biases": sector_biases,
        "market_decision": decision,
        "min_consensus_boost": boost,
        "trade_amount_multiplier": trade_mult,
        "max_candidates_cap": cap,
        "notes": notes,
    }


def format_market_context_for_llm(summary: dict[str, Any] | None) -> str:
    """state/market_summary.json을 LLM 종목선정용 근거 블록으로 변환."""
    if not summary:
        return "시장흐름 분석 없음 - 뉴스/시황 근거를 과장하지 말고 정량 후보와 개별 뉴스만 사용."

    analysis = summary.get("analysis") or {}
    if not isinstance(analysis, dict):
        text = str(summary.get("summary") or "").strip()
        return f"시장흐름 요약(구버전):\n{text or '시장흐름 분석 없음'}"

    signal = build_market_signal(summary)
    lines = ["[시장흐름 분석 - 종목선정에 반드시 반영]"]
    tone = analysis.get("tone") or "미정"
    confidence = analysis.get("confidence")
    market_read = analysis.get("market_read") or ""
    if confidence is not None:
        lines.append(f"- 시장 톤: {tone} / 신뢰도 {confidence}%")
    else:
        lines.append(f"- 시장 톤: {tone}")
    liquidity_limited = any("거래대금 상위 유동성 제한" in str(g) for g in (signal.get("data_gaps") or []))
    if liquidity_limited:
        market_read = "저장된 시황의 소액 거래대금 근거는 폐기. 50억 미만/정규장 전 거래대금은 섹터 주도주 근거로 사용하지 않음."
    if market_read:
        lines.append(f"- 핵심 판단: {market_read}")
    if liquidity_limited:
        lines.append("- 보정 판단: 50억 미만/정규장 전 거래대금 근거는 폐기하고, 섹터 주도주 단정 없이 개별 수급·일봉 리스크를 재검증.")
    lines.append(
        f"- 데이터 품질: {signal['quality_score']}점 / 검증 엄격도 {signal['strictness']} "
        f"(최소 신뢰도 +{signal['min_consensus_boost']:.2f}, 거래대금 x{signal['trade_amount_multiplier']:.1f})"
    )
    decision = signal.get("market_decision") if isinstance(signal.get("market_decision"), dict) else {}
    if decision:
        lines.append(
            f"- 매수 게이트: {decision.get('gate_label')} {decision.get('gate_score')}점 "
            f"/ {decision.get('stance')} - {decision.get('positioning')}"
        )
        checks = decision.get("checks") or []
        if checks:
            lines.append("- 시장 게이트 체크:")
            for c in checks[:4]:
                if isinstance(c, dict):
                    lines.append(
                        f"  * {c.get('name')}: {c.get('value')} [{c.get('state')}] - {c.get('impact')}"
                    )
        risks = decision.get("risk_flags") or []
        if risks:
            lines.append("- 시장 위험 플래그: " + "; ".join(str(x) for x in risks[:5]))
        opps = decision.get("opportunity_flags") or []
        if opps:
            lines.append("- 시장 기회 플래그: " + "; ".join(str(x) for x in opps[:5]))

    sectors = signal.get("leading_sectors") or []
    if sectors:
        lines.append("- 우선 검토 섹터/테마:")
        for s in sectors[:4]:
            if not isinstance(s, dict):
                continue
            name = s.get("name") or "미분류"
            symbols = ", ".join(s.get("symbols") or [])
            evidence = "; ".join(s.get("evidence") or [])
            tail = f" | 관련: {symbols}" if symbols else ""
            lines.append(f"  * {name}{tail} - 근거: {evidence or '근거 부족'}")
    else:
        lines.append("- 우선 검토 섹터/테마: 없음. 섹터 주도주라고 단정하지 말 것.")

    if signal.get("sector_biases"):
        lines.append("- 해외 신호 → 국내 후보 매핑:")
        for b in signal["sector_biases"][:4]:
            kws = ", ".join(str(x) for x in (b.get("domestic_keywords") or [])[:8])
            ev = "; ".join(str(x) for x in (b.get("evidence") or [])[:3])
            lines.append(
                f"  * {b.get('name')} strength={float(b.get('strength', 0) or 0):.2f} "
                f"| 국내 키워드: {kws} | 근거: {ev or '근거 부족'}"
            )

    risks = analysis.get("avoid_or_risks") or []
    if risks:
        lines.append("- 회피/주의:")
        for r in risks[:4]:
            if not isinstance(r, dict):
                continue
            lines.append(f"  * {r.get('name') or '주의'} - {r.get('reason') or '근거 부족'}")

    bias = analysis.get("selection_bias") or []
    if liquidity_limited:
        bias = [b for b in bias if not _uses_invalid_volume_bias(str(b))]
    if bias:
        lines.append("- 후보 선정 바이어스:")
        for b in bias[:5]:
            lines.append(f"  * {b}")

    gaps = analysis.get("data_gaps") or []
    if gaps:
        lines.append("- 데이터 공백/주의:")
        for g in gaps[:3]:
            lines.append(f"  * {g}")

    if signal.get("notes"):
        lines.append("- 코드 검증 정책:")
        for note in signal["notes"][:4]:
            lines.append(f"  * {note}")

    lines.append("선정 원칙: 위 시장흐름과 맞지 않는 종목은 개별 뉴스/기술적 근거가 강할 때만 예외로 추천.")
    return "\n".join(lines)


def summarize_analysis_for_legacy(analysis: dict[str, Any]) -> str:
    """대시보드/구버전 호환용 짧은 텍스트 요약."""
    tone = analysis.get("tone") or "미정"
    read = analysis.get("market_read") or ""
    sectors = analysis.get("leading_sectors") or []
    sector_names = ", ".join(
        s.get("name", "") for s in sectors if isinstance(s, dict) and s.get("name")
    )
    risks = analysis.get("avoid_or_risks") or []
    risk_names = ", ".join(
        r.get("name", "") for r in risks if isinstance(r, dict) and r.get("name")
    )
    parts = [f"시장 톤: {tone}"]
    if read:
        parts.append(f"핵심 판단: {read}")
    if sector_names:
        parts.append(f"우선 검토: {sector_names}")
    if risk_names:
        parts.append(f"주의: {risk_names}")
    return "\n".join(parts)


def derive_sector_biases(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    """미국장/시황 텍스트를 국내 후보군 선호 섹터로 번역한다."""
    if not summary:
        return []
    analysis = summary.get("analysis") if isinstance(summary.get("analysis"), dict) else {}
    us_raw = summary.get("us_market_raw") if isinstance(summary.get("us_market_raw"), dict) else {}
    us_text = str(summary.get("us_market") or "")
    text_parts = [
        us_text,
        str(analysis.get("market_read") or ""),
        " ".join(str(x) for x in _as_list(analysis.get("watch_keywords"))),
        " ".join(str(x) for x in _as_list(analysis.get("evidence"))),
    ]
    for sector in _as_list(analysis.get("leading_sectors")):
        if isinstance(sector, dict):
            text_parts.append(str(sector.get("name") or ""))
            text_parts.append(" ".join(str(x) for x in _as_list(sector.get("symbols"))))
            text_parts.append(" ".join(str(x) for x in _as_list(sector.get("evidence"))))

    combined = " ".join(text_parts).lower()
    sector_moves = [
        m for m in _as_list(us_raw.get("sector_moves"))
        if isinstance(m, dict)
    ]

    biases = []
    for rule in SECTOR_BIAS_RULES:
        matched_terms = [t for t in rule["triggers"] if t.lower() in combined]
        matched_moves = [
            m for m in sector_moves
            if str(m.get("name") or "").lower() == str(rule["name"]).lower()
        ]
        if not matched_terms and not matched_moves:
            continue

        strength = 0.55
        evidence = []
        if matched_terms:
            strength += min(0.15, len(matched_terms) * 0.03)
            evidence.append("감지 키워드: " + ", ".join(matched_terms[:6]))
        for move in matched_moves:
            chg = _as_float(move.get("chg_pct"), 0.0)
            evidence.extend(str(x) for x in _as_list(move.get("evidence"))[:4])
            if chg >= 1.5:
                strength += 0.25
            elif chg >= 0.8:
                strength += 0.15
            elif chg <= -1.0:
                strength -= 0.2
                evidence.append(f"미국 섹터 약세 {chg:+.2f}%")
        strength = max(0.0, min(1.0, strength))
        if strength < 0.45:
            continue
        biases.append({
            "name": rule["name"],
            "strength": round(strength, 2),
            "trigger_terms": matched_terms[:8],
            "domestic_keywords": rule["domestic_keywords"],
            "evidence": evidence[:6],
        })
    biases.sort(key=lambda x: float(x.get("strength", 0) or 0), reverse=True)
    return biases


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except Exception:
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _uses_low_liquidity_evidence(text: str) -> bool:
    if not text:
        return False
    if "정규장 전" in text and "거래대금" in text:
        return True
    if "거래대금" not in text and "대금" not in text:
        return False
    amounts = [float(x.replace(",", "")) for x in re.findall(r"(?:대금\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*억", text)]
    return bool(amounts) and max(amounts) < 50.0


def _uses_invalid_volume_bias(text: str) -> bool:
    if not text:
        return False
    if _uses_low_liquidity_evidence(text):
        return True
    return "거래대금 상위" in text or "국내 거래대금" in text
