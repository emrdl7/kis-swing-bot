"""포지션 상태 관리 및 청산 조건 판단."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from src.core.config import ExitConfig
from src.core.models import CloseReason, PositionState, SwingPosition

log = logging.getLogger(__name__)


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

        Returns:
            (should_exit, reason)
        """
        now = now or datetime.now()
        pnl_pct = pos.pnl_pct(current_price)

        # 1) 손절
        if pnl_pct <= -self.cfg.stop_loss_pct:
            log.warning(
                "[%s] 손절 발동 pnl=%.2f%% <= -%.2f%%",
                pos.symbol, pnl_pct, self.cfg.stop_loss_pct,
            )
            return True, CloseReason.STOP_LOSS

        # 2) 목표가 도달
        if current_price >= pos.target_price:
            log.info(
                "[%s] 목표가 도달 %.0f >= %.0f",
                pos.symbol, current_price, pos.target_price,
            )
            return True, CloseReason.TAKE_PROFIT

        # 3) 트레일링 스탑
        if pos.state == PositionState.TRAILING and pos.trailing_stop_px:
            if current_price <= pos.trailing_stop_px:
                log.info(
                    "[%s] 트레일링 스탑 %.0f <= %.0f",
                    pos.symbol, current_price, pos.trailing_stop_px,
                )
                return True, CloseReason.TRAILING_STOP

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
        """트레일링 스탑 상태 업데이트."""
        pnl_pct = pos.pnl_pct(current_price)

        # 피크 갱신
        if current_price > pos.peak_price:
            pos.peak_price = current_price

        # 트레일링 활성화
        if (pnl_pct >= self.cfg.trailing_activate_pct
                and pos.state == PositionState.ENTERED):
            pos.state = PositionState.TRAILING
            log.info("[%s] 트레일링 스탑 활성화 pnl=%.2f%%", pos.symbol, pnl_pct)

        # 트레일링 스탑 가격 갱신
        if pos.state == PositionState.TRAILING:
            new_stop = pos.peak_price * (1.0 - self.cfg.trailing_pct / 100.0)
            if pos.trailing_stop_px is None or new_stop > pos.trailing_stop_px:
                pos.trailing_stop_px = new_stop

        return pos
