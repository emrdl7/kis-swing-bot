"""일일 리스크 관리."""
from __future__ import annotations
import logging

from src.core.config import TradingConfig

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg
        self._halt_notified = False
        self._total_capital: float = 0.0  # 당일 기준 총자산 (첫 조회 시 설정)

    def set_capital(self, total_capital: float) -> None:
        """당일 총자산 기준값 설정 (첫 틱에서 호출)."""
        if self._total_capital <= 0 and total_capital > 0:
            self._total_capital = total_capital
            log.info("일일 리스크 기준 자산: %s원", f"{int(total_capital):,}")

    def is_daily_halt(self, daily_pnl: float) -> bool:
        """일일 손실 한도 초과 여부. 초과 시 신규 진입만 차단 (보유 포지션은 유지)."""
        if self._total_capital <= 0:
            return False
        loss_pct = abs(daily_pnl) / self._total_capital * 100 if daily_pnl < 0 else 0
        if loss_pct >= self.cfg.max_daily_loss_pct:
            if not self._halt_notified:
                log.error(
                    "⛔ 일일 손실 한도 초과: %.1f%% (%.0f원 / 기준 %.0f원) — 신규 진입 차단",
                    loss_pct, daily_pnl, self._total_capital,
                )
                self._halt_notified = True
            return True
        self._halt_notified = False
        return False
