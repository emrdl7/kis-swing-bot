"""장 마감 보고 스크립트 (15:35 실행).

launchd ai.kis.swing.eod.plist 에 의해 매일 15:35 호출됨.
LLM이 오늘 매매를 분석하고 파라미터 조정을 결정하면, 실제로 config에 반영한다.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

import yaml

from src.core.config import load_config
from src.core import state_store
from src.core.models import PositionState, SwingPosition, SwingCandidate
from src.agents.llm_client import LLMClient
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("eod_report")

YAML_PATH = PROJECT_ROOT / "config" / "default.yaml"

# LLM이 조정할 수 있는 파라미터와 안전 범위
TUNABLE_PARAMS = {
    "exit.stop_loss_pct":         (1.0,  5.0),
    "exit.take_profit_pct":       (2.0, 10.0),
    "exit.trailing_activate_pct": (1.0,  4.0),
    "exit.trailing_pct":          (0.5,  3.0),
    "screening.entry_zone_slack_pct": (0.5, 3.0),
    "screening.entry_expiry_days":    (1,   7),
    "trading.position_size_pct":      (0.10, 0.50),
}


def _build_context(
    today: str,
    closed_today: list[SwingPosition],
    open_positions: list[SwingPosition],
    candidates: list[SwingCandidate],
    daily_pnl: int,
    current_params: dict,
) -> str:
    lines = [f"오늘 날짜: {today}", ""]

    lines.append("=== 오늘 청산 내역 ===")
    if closed_today:
        for p in closed_today:
            if p.close_price and p.avg_price:
                pnl = (p.close_price - p.avg_price) * p.qty
                pnl_pct = (p.close_price / p.avg_price - 1) * 100
                lines.append(
                    f"- {p.name}({p.symbol}): "
                    f"매수 {int(p.avg_price):,}원 → 매도 {int(p.close_price):,}원 "
                    f"({pnl_pct:+.2f}%, {int(pnl):+,}원) | 사유: {p.close_reason.value if p.close_reason else '?'} | "
                    f"수량: {p.qty}주"
                )
    else:
        lines.append("- 오늘 청산 없음")

    lines.append(f"\n오늘 실현 손익 합계: {daily_pnl:+,}원\n")

    lines.append("=== 현재 보유 중인 포지션 ===")
    if open_positions:
        for p in open_positions:
            hold_days = (datetime.now() - p.entry_time).days
            lines.append(
                f"- {p.name}({p.symbol}): "
                f"매수가 {int(p.avg_price):,}원, {p.qty}주, {hold_days}일 보유 | "
                f"목표 {int(p.target_price):,}원 / 손절 {int(p.stop_price):,}원 | "
                f"상태: {p.state.value}"
            )
    else:
        lines.append("- 현재 보유 없음")

    lines.append("")

    active_cands = [c for c in candidates if not c.is_expired()]
    lines.append("=== 감시 중이나 미진입 후보 ===")
    if active_cands:
        for c in active_cands:
            nxt = f" | NXT가 {int(c.nxt_close):,}원" if c.nxt_close else ""
            lines.append(
                f"- {c.name}({c.symbol}): "
                f"진입구간 {int(c.entry_low):,}~{int(c.entry_high):,}원 | "
                f"신뢰도 {c.consensus_score:.0%}{nxt}"
            )
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("=== 현재 봇 파라미터 ===")
    for k, v in current_params.items():
        lo, hi = TUNABLE_PARAMS[k]
        lines.append(f"- {k}: {v}  (조정 가능 범위: {lo} ~ {hi})")

    return "\n".join(lines)


def _generate_analysis(llm: LLMClient, context: str, today: str) -> str:
    system = """당신은 스윙 트레이딩 봇의 투자 성과 분석가입니다.
오늘 하루 봇의 매매 결과를 분석하고, 파라미터 조정이 필요하면 실제로 반영될 JSON을 출력합니다.
보고서는 한국어로 작성합니다."""

    tunable_list = "\n".join(f"  - {k}: {lo} ~ {hi}" for k, (lo, hi) in TUNABLE_PARAMS.items())

    user = f"""아래는 오늘({today}) 스윙 트레이딩 봇의 매매 데이터입니다.

{context}

다음 구조로 장 마감 보고서를 작성해주세요:

1. **오늘 투자 요약** — 실현손익, 청산 건수, 보유 현황을 1~2문장으로 간결하게

2. **잘 된 점** — 수익 청산이 있다면 어떤 판단이 맞았는지, 손절이 있다면 리스크 관리 측면에서의 의미

3. **문제점 분석** — 손실 청산의 원인, 진입 타이밍 이슈, 미진입 후보가 있다면 왜 놓쳤는지

4. **봇 파라미터 조정 결정** — 문제점에서 도출된 파라미터 변경을 서술하고, 변경 이유를 명시.
   이 항목은 사용자에게 드리는 제언이 아니라 봇이 오늘 분석을 바탕으로 스스로 내리는 결정입니다.
   조정이 없다면 "현재 파라미터 유지" 라고 명시.

보고서 마지막에 반드시 아래 형식의 JSON 블록을 출력하세요 (조정 없으면 adjustments를 빈 배열로):

```json
{{
  "adjustments": [
    {{"param": "exit.stop_loss_pct", "to": 3.0, "reason": "손절 조기 발동 반복"}},
    {{"param": "screening.entry_zone_slack_pct", "to": 1.5, "reason": "진입 구간 미도달로 후보 미진입"}}
  ]
}}
```

조정 가능한 파라미터와 허용 범위:
{tunable_list}

범위를 벗어나는 값은 자동으로 기각됩니다."""

    return llm.chat(system=system, user=user) or ""


def _parse_adjustments(llm_output: str) -> list[dict]:
    """LLM 응답에서 JSON 블록 파싱."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", llm_output, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
        return data.get("adjustments", [])
    except Exception:
        return []


def _apply_adjustments(adjustments: list[dict]) -> list[dict]:
    """검증 후 default.yaml에 실제 반영. 적용된 항목 목록 반환."""
    if not adjustments:
        return []

    with open(YAML_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    applied = []
    for adj in adjustments:
        param = adj.get("param", "")
        to_val = adj.get("to")
        reason = adj.get("reason", "")

        if param not in TUNABLE_PARAMS or to_val is None:
            log.warning("파라미터 기각 (알 수 없는 키): %s", param)
            continue

        lo, hi = TUNABLE_PARAMS[param]
        if not (lo <= to_val <= hi):
            log.warning("파라미터 기각 (범위 초과): %s = %s (허용: %s~%s)", param, to_val, lo, hi)
            continue

        section, key = param.split(".", 1)
        if section not in config or key not in config[section]:
            log.warning("파라미터 기각 (YAML 키 없음): %s", param)
            continue

        from_val = config[section][key]
        config[section][key] = to_val
        applied.append({"param": param, "from": from_val, "to": to_val, "reason": reason})
        log.info("파라미터 조정: %s  %s → %s  (%s)", param, from_val, to_val, reason)

    if applied:
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    return applied


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 장 마감 보고 [%s] ===", today)

    cfg = load_config()
    llm = LLMClient(model=cfg.agents.model, max_tokens=cfg.agents.max_tokens)

    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]

    closed_today = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_time
        and p.close_time.strftime("%Y-%m-%d") == today
    ]
    open_positions = [p for p in positions if p.state != PositionState.CLOSED]

    daily_pnl = sum(
        int((p.close_price - p.avg_price) * p.qty)
        for p in closed_today if p.close_price and p.avg_price
    )

    # 현재 파라미터 값 수집
    current_params = {
        "exit.stop_loss_pct":         cfg.exit.stop_loss_pct,
        "exit.take_profit_pct":       cfg.exit.take_profit_pct,
        "exit.trailing_activate_pct": cfg.exit.trailing_activate_pct,
        "exit.trailing_pct":          cfg.exit.trailing_pct,
        "screening.entry_zone_slack_pct": cfg.screening.entry_zone_slack_pct,
        "screening.entry_expiry_days":    cfg.screening.entry_expiry_days,
        "trading.position_size_pct":      cfg.trading.position_size_pct,
    }

    # LLM 분석 + 조정 결정
    log.info("LLM 분석 중...")
    context = _build_context(today, closed_today, open_positions, candidates, daily_pnl, current_params)
    llm_output = _generate_analysis(llm, context, today)

    if not llm_output:
        log.error("LLM 응답 없음")
        return

    # 파라미터 실제 반영
    adjustments = _parse_adjustments(llm_output)
    applied = _apply_adjustments(adjustments)

    # 보고서에서 JSON 블록 제거 (노트엔 분석 텍스트만)
    report_text = re.sub(r"```json.*?```", "", llm_output, flags=re.DOTALL).strip()

    # 실제 반영된 조정 내역을 보고서 하단에 추가
    if applied:
        report_text += "\n\n---\n\n## 실제 반영된 파라미터 변경\n"
        for a in applied:
            report_text += f"- **{a['param']}**: {a['from']} → **{a['to']}** ({a['reason']})\n"
    else:
        report_text += "\n\n---\n\n_파라미터 변경 없음 — 현재 설정 유지_"

    apple_notes.report_eod_analysis(report_text, daily_pnl, today)

    log.info(
        "장 마감 보고 완료 — PnL: %+d원, 청산: %d건, 보유: %d종목, 파라미터 조정: %d건",
        daily_pnl, len(closed_today), len(open_positions), len(applied),
    )


if __name__ == "__main__":
    main()
