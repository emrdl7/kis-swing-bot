"""장 마감 보고 스크립트 (15:35 실행).

launchd ai.kis.swing.eod.plist 에 의해 매일 15:35 호출됨.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

from src.core.config import load_config
from src.core import state_store
from src.core.models import PositionState, SwingPosition, SwingCandidate
from src.agents.llm_client import LLMClient
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("eod_report")


def _build_context(
    today: str,
    closed_today: list[SwingPosition],
    open_positions: list[SwingPosition],
    candidates: list[SwingCandidate],
    daily_pnl: int,
) -> str:
    """LLM에 전달할 오늘 하루 투자 컨텍스트 구성."""
    lines = [f"오늘 날짜: {today}", ""]

    # 오늘 청산 내역
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

    # 현재 보유 포지션
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

    # 감시 후보 (진입하지 못한 종목)
    active_cands = [c for c in candidates if not c.is_expired()]
    lines.append("=== 감시 중이나 미진입 후보 ===")
    if active_cands:
        for c in active_cands:
            lines.append(
                f"- {c.name}({c.symbol}): "
                f"진입구간 {int(c.entry_low):,}~{int(c.entry_high):,}원 | "
                f"신뢰도 {c.consensus_score:.0%} | "
                f"NXT가 {int(c.nxt_close):,}원" if c.nxt_close else
                f"- {c.name}({c.symbol}): 진입구간 {int(c.entry_low):,}~{int(c.entry_high):,}원 | 신뢰도 {c.consensus_score:.0%}"
            )
    else:
        lines.append("- 없음")

    return "\n".join(lines)


def _generate_analysis(llm: LLMClient, context: str, today: str) -> str:
    """LLM으로 오늘 투자 분석 보고서 생성."""
    system = """당신은 스윙 트레이딩 봇의 투자 성과 분석가입니다.
오늘 하루 봇의 매매 결과를 분석하여 투자자에게 보고서를 작성합니다.
보고서는 한국어로 작성하며, 통보가 아닌 '분석과 인사이트'가 담긴 보고서여야 합니다."""

    user = f"""아래는 오늘({today}) 스윙 트레이딩 봇의 매매 데이터입니다.

{context}

다음 구조로 장 마감 보고서를 작성해주세요:

1. **오늘 투자 요약** — 실현손익, 청산 건수, 보유 현황을 1~2문장으로 간결하게

2. **잘 된 점** — 수익 청산이 있다면 어떤 판단이 맞았는지, 손절이 있다면 리스크 관리 측면에서의 의미

3. **문제점 분석** — 손실 청산의 원인, 진입 타이밍 이슈, 미진입 후보가 있다면 왜 놓쳤는지

4. **봇 자체 개선 계획** — 문제점에서 도출된 봇 로직/파라미터 개선 방향을 구체적으로 서술.
   예: "손절 기준을 X%에서 Y%로 조정할 예정", "진입 구간 슬랙을 넓힐 예정", "특정 청산 사유가 반복되면 트레일링 폭 조정 등".
   이 항목은 '사용자에게 드리는 제언'이 아니라 봇이 스스로 다음에 어떻게 동작을 바꿀 것인지에 대한 계획입니다.
   개선이 필요 없는 날이라면 "현재 전략 유지" 로 명시.

청산 내역이 없는 날도 보유 포지션과 후보 상황을 바탕으로 의미 있는 분석을 작성하세요.
숫자 나열이 아닌, 투자자가 읽고 봇의 상태와 방향을 파악할 수 있는 보고서를 써주세요."""

    result = llm.chat(system=system, user=user)
    return result or "LLM 분석 생성 실패 — 원시 데이터를 확인하세요."


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

    # LLM 분석 보고서 생성
    log.info("LLM 보고서 생성 중...")
    context = _build_context(today, closed_today, open_positions, candidates, daily_pnl)
    analysis = _generate_analysis(llm, context, today)

    # Apple Notes 저장
    apple_notes.report_eod_analysis(analysis, daily_pnl, today)

    log.info(
        "장 마감 보고 완료 — 오늘 PnL: %+d원, 청산: %d건, 보유: %d종목",
        daily_pnl, len(closed_today), len(open_positions),
    )


if __name__ == "__main__":
    main()
