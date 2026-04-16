"""포지션 상태 관리 및 청산 조건 판단."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from src.core.config import ExitConfig
from src.core.models import CloseReason, PositionState, SwingPosition

log = logging.getLogger(__name__)

# F-2: 단계별 트레일링 — 수익 구간별 trailing 폭
_TRAILING_TIERS = [
    # (pnl_pct 이상, trailing_pct)
    (7.0, 0.6),   # +7% 이상: 0.6% 폭 (수익 보호 최강)
    (5.0, 0.8),   # +5% 이상: 0.8% 폭
    (2.5, 1.2),   # +2.5% 이상: 1.2% 폭 (기본)
]


class PositionManager:
    def __init__(self, exit_cfg: ExitConfig):
        self.cfg = exit_cfg

    def check_exit(
        self,
        pos: SwingPosition,
        current_price: float,
        now: Optional[datetime] = None,
    ) -> tuple[bool, CloseReason | None]:
        """청산 조건 확인.

        우선순위: 손절 → 트레일링 스탑 → 목표가 → 모멘텀 소실 → EOD
        (F-1: 트레일링이 목표가보다 우선 — 자연스러운 추세 추종)
        """
        now = now or datetime.now()
        pnl_pct = pos.pnl_pct(current_price)

        # 1) 손절 (최우선)
        if pnl_pct <= -self.cfg.stop_loss_pct:
            log.warning(
                "[%s] 손절 발동 pnl=%.2f%% <= -%.2f%%",
                pos.symbol, pnl_pct, self.cfg.stop_loss_pct,
            )
            return True, CloseReason.STOP_LOSS

        # 2) 트레일링 스탑 (목표가보다 우선)
        if pos.state == PositionState.TRAILING and pos.trailing_stop_px:
            if current_price <= pos.trailing_stop_px:
                log.info(
                    "[%s] 트레일링 스탑 %.0f <= %.0f (peak %.0f)",
                    pos.symbol, current_price, pos.trailing_stop_px, pos.peak_price,
                )
                return True, CloseReason.TRAILING_STOP

        # 3) 목표가 도달 → G-3: 2주 이상이면 분할 매도(50% 익절 + 나머지 트레일링)
        if current_price >= pos.target_price and pos.state != PositionState.TRAILING:
            if pos.qty >= 2:
                # 분할 매도: TAKE_PROFIT_PARTIAL 시그널 (monitor에서 절반 매도 후 트레일링 전환)
                log.info(
                    "[%s] 목표가 도달 %.0f >= %.0f → 분할 매도 (%d주 중 %d주 익절)",
                    pos.symbol, current_price, pos.target_price, pos.qty, pos.qty // 2,
                )
                return True, CloseReason.TAKE_PROFIT  # monitor에서 분할 처리
            else:
                log.info(
                    "[%s] 목표가 도달 %.0f >= %.0f (1주 → 전량 익절)",
                    pos.symbol, current_price, pos.target_price,
                )
                return True, CloseReason.TAKE_PROFIT

        # F-3: 모멘텀 소실 매도 (3일 경과 + 수익률 0~1% 정체)
        if pos.entry_time:
            holding_days = (now - pos.entry_time).total_seconds() / 86400
            if holding_days >= 3 and 0 <= pnl_pct < 1.0:
                log.info(
                    "[%s] 모멘텀 소실 — 보유 %.1f일, pnl=%.2f%% (0~1%% 정체)",
                    pos.symbol, holding_days, pnl_pct,
                )
                return True, CloseReason.EOD  # EOD 사유 재사용 (모멘텀 소실)

        # 4) 장 마감 강제 청산
        if self.cfg.eod_sell_enabled:
            eod = now.replace(
                hour=self.cfg.eod_sell_hhmm // 100,
                minute=self.cfg.eod_sell_hhmm % 100,
                second=0, microsecond=0,
            )
            if now >= eod:
                log.info("[%s] EOD 강제 청산", pos.symbol)
                return True, CloseReason.EOD

        return False, None

    def update_trailing(
        self,
        pos: SwingPosition,
        current_price: float,
    ) -> SwingPosition:
        """트레일링 스탑 상태 업데이트 + 본전 보호 + 단계별 trailing."""
        pnl_pct = pos.pnl_pct(current_price)

        # 피크 갱신
        if current_price > pos.peak_price:
            pos.peak_price = current_price

        # F-4: 본전 보호 — +1% 이상이면 손절선을 매수가로 상향
        if pnl_pct >= 1.0 and pos.stop_price < pos.avg_price:
            pos.stop_price = pos.avg_price
            log.info("[%s] 본전 보호 활성: 손절선 → 매수가 %.0f", pos.symbol, pos.avg_price)

        # 트레일링 활성화
        if (pnl_pct >= self.cfg.trailing_activate_pct
                and pos.state == PositionState.ENTERED):
            pos.state = PositionState.TRAILING
            log.info("[%s] 트레일링 스탑 활성화 pnl=%.2f%%", pos.symbol, pnl_pct)

        # F-2: 단계별 트레일링 — 수익 구간에 따라 trailing 폭 동적 조정
        if pos.state == PositionState.TRAILING:
            peak_pnl = pos.pnl_pct(pos.peak_price)
            trail_pct = self.cfg.trailing_pct  # 기본값
            for threshold, tighter in _TRAILING_TIERS:
                if peak_pnl >= threshold:
                    trail_pct = tighter
                    break
            new_stop = pos.peak_price * (1.0 - trail_pct / 100.0)
            if pos.trailing_stop_px is None or new_stop > pos.trailing_stop_px:
                pos.trailing_stop_px = new_stop

        return pos
