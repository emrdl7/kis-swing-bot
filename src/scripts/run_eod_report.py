"""장 마감 보고 스크립트 (15:35 실행).

launchd ai.kis.swing.eod.plist 에 의해 매일 15:35 호출됨.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

from src.core import state_store
from src.core.models import PositionState, SwingPosition
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("eod_report")


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 장 마감 보고 [%s] ===", today)

    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]

    # 오늘 청산된 포지션
    closed_today = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_time
        and p.close_time.strftime("%Y-%m-%d") == today
    ]

    daily_pnl = 0
    closed_dicts = []
    for p in closed_today:
        if p.close_price and p.avg_price:
            pnl_amount = int((p.close_price - p.avg_price) * p.qty)
            daily_pnl += pnl_amount
            closed_dicts.append({
                "name": p.name,
                "symbol": p.symbol,
                "avg_price": p.avg_price,
                "close_price": p.close_price,
                "close_reason": p.close_reason.value if p.close_reason else "",
                "pnl_amount": pnl_amount,
            })

    # 현재 보유 중인 포지션
    open_positions = [p for p in positions if p.state != PositionState.CLOSED]

    # Apple Notes 장 마감 보고
    apple_notes.report_eod(closed_dicts, daily_pnl, today)

    # 보유 포지션 별도 노트
    if open_positions:
        lines = [f"[{today}] 현재 보유 포지션\n"]
        for p in open_positions:
            lines += [
                f"- {p.name} ({p.symbol})",
                f"  매수가: {int(p.avg_price):,}원  수량: {p.qty}주",
                f"  목표가: {int(p.target_price):,}원  손절가: {int(p.stop_price):,}원",
                f"  상태: {p.state.value}",
                "",
            ]
        apple_notes.create_note(f"[보유] {today} 포지션 현황", "\n".join(lines))

    log.info("장 마감 보고 완료 — 오늘 PnL: %+d원, 청산: %d건, 보유: %d종목",
             daily_pnl, len(closed_dicts), len(open_positions))


if __name__ == "__main__":
    main()
