"""종가배팅 오버나이트 리포트 (매일 10:10 실행).

어제 15:20~15:25 진입 → 오늘 오전 매도 완료된 CB 포지션을 집계해
Apple Notes로 리포트를 보낸다.

포함 내용:
- 오늘 마감된 CB 포지션별 상세 (진입가/매도가/PnL/사유/거래소)
- 오늘 CB 전용 통계 (승/패, 합산 PnL, NXT 조기매도 비중)
- 누적 CB 통계 (전체 기간 승률, 평균 PnL, 사유별 분포)
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

from src.core import state_store
from src.core.models import PositionState, SwingPosition, CloseReason
from src.notification import apple_notes
from src.utils.logging_setup import setup

log = setup("cb_report")


def _fmt_pnl(amt: int) -> str:
    return f"{amt:+,}원"


def _reason_ko(reason: CloseReason | None) -> str:
    if reason is None:
        return "-"
    return {
        CloseReason.TAKE_PROFIT: "목표가 도달",
        CloseReason.STOP_LOSS: "손절",
        CloseReason.TRAILING_STOP: "트레일링",
        CloseReason.EOD: "장 마감",
        CloseReason.MANUAL: "수동",
        CloseReason.RECONCILE_KIS_ZERO: "KIS 잔고 0",
        CloseReason.CLOSING_BET_MORNING: "시간 매도",
    }.get(reason, reason.value)


def _is_nxt_close(pos: SwingPosition) -> bool:
    """NXT 프리장에서 매도된 포지션 식별 (close_time의 시분 기준)."""
    if not pos.close_time:
        return False
    t = pos.close_time.time()
    return t < datetime.strptime("09:00", "%H:%M").time()


def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== 종가배팅 오버나이트 리포트 [%s] ===", today)

    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    cb_positions = [p for p in positions if (p.strategy or "") == "closing_bet"]

    # 오늘 청산된 CB (어제 진입, 오늘 매도)
    today_closed = [
        p for p in cb_positions
        if p.state == PositionState.CLOSED and p.close_time
        and p.close_time.strftime("%Y-%m-%d") == today
    ]

    # 오늘 보유 중 CB (15:20 이전이라면 아직 안 팔렸을 수 있음)
    still_holding = [p for p in cb_positions if p.state != PositionState.CLOSED]

    # 오늘 통계
    wins = [p for p in today_closed if p.close_price and p.close_price > p.avg_price]
    losses = [p for p in today_closed if p.close_price and p.close_price <= p.avg_price]
    total_pnl = sum(int((p.close_price - p.avg_price) * p.qty) for p in today_closed if p.close_price)
    nxt_sells = [p for p in today_closed if _is_nxt_close(p)]

    # 누적 통계 (전체 기간 CB)
    all_closed = [
        p for p in cb_positions
        if p.state == PositionState.CLOSED and p.close_price
        and p.close_reason != CloseReason.RECONCILE_KIS_ZERO
    ]
    total_count = len(all_closed)
    total_wins = len([p for p in all_closed if p.close_price > p.avg_price])
    total_win_rate = (total_wins / total_count * 100) if total_count > 0 else 0
    total_pnl_all = sum(int((p.close_price - p.avg_price) * p.qty) for p in all_closed)
    avg_pnl = (total_pnl_all / total_count) if total_count > 0 else 0

    # 사유별 분포
    reason_dist: dict[str, int] = {}
    for p in all_closed:
        key = _reason_ko(p.close_reason)
        reason_dist[key] = reason_dist.get(key, 0) + 1

    # ── 리포트 본문 작성 ──
    lines: list[str] = []
    lines.append(f"## 📊 종가배팅 오버나이트 리포트 — {today}\n")

    if not today_closed and not still_holding:
        lines.append("오늘 청산된 CB 포지션이 없습니다. (어제 진입이 없었거나 전량 이월 중)\n")
    else:
        lines.append(f"### 🔹 오늘 청산 {len(today_closed)}건\n")
        if today_closed:
            for p in today_closed:
                pnl_pct = p.pnl_pct(p.close_price) if p.close_price else 0
                pnl_amt = int((p.close_price - p.avg_price) * p.qty) if p.close_price else 0
                market = "NXT 프리장" if _is_nxt_close(p) else "KRX 정규장"
                close_t = p.close_time.strftime("%H:%M:%S") if p.close_time else "-"
                lines.append(
                    f"- **{p.name}({p.symbol})** · {market} {close_t}\n"
                    f"  - 진입 {int(p.avg_price):,}원 × {p.qty}주 → 매도 {int(p.close_price or 0):,}원\n"
                    f"  - 사유: {_reason_ko(p.close_reason)} · PnL: **{pnl_pct:+.2f}%** ({_fmt_pnl(pnl_amt)})\n"
                )
        lines.append("")
        lines.append("### 🔹 오늘 합산\n")
        lines.append(f"- 승/패: **{len(wins)}승 {len(losses)}패**")
        lines.append(f"- 실현 PnL: **{_fmt_pnl(total_pnl)}**")
        lines.append(f"- NXT 프리장 매도 비중: {len(nxt_sells)}/{len(today_closed)}건")
        if still_holding:
            lines.append(f"- 잔존 보유 (이월/미체결): {len(still_holding)}종목")
        lines.append("")

    lines.append("### 🔹 CB 전략 누적 통계")
    lines.append(f"- 총 청산 횟수: **{total_count}건**")
    lines.append(f"- 승률: **{total_win_rate:.1f}%** ({total_wins}승/{total_count - total_wins}패)")
    lines.append(f"- 누적 PnL: **{_fmt_pnl(total_pnl_all)}**")
    lines.append(f"- 평균 회당 PnL: {_fmt_pnl(int(avg_pnl))}")
    if reason_dist:
        lines.append("- 청산 사유별 분포:")
        for k, v in sorted(reason_dist.items(), key=lambda x: -x[1]):
            pct = v / total_count * 100
            lines.append(f"  - {k}: {v}건 ({pct:.0f}%)")

    body = "\n".join(lines)
    title = f"[KIS-Swing-Bot] CB 리포트 {today}"
    ok = apple_notes.create_note(title, body)
    log.info("Apple Notes 전송: %s — %s", title, "성공" if ok else "실패")
    # 콘솔에도 출력
    print(body)


if __name__ == "__main__":
    main()
