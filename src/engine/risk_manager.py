"""일일 리스크 관리."""
from __future__ import annotations
import logging

from src.core.config import TradingConfig

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg
        self._halt_notified = False

    def is_daily_halt(self, daily_pnl: float) -> bool:
        """일일 손실 한도 초과 여부."""
        # daily_pnl은 금액 기준 (원), 비율로 변환하려면 총자산이 필요하지만
        # 여기서는 absolute amount로만 판단 (향후 개선)
        # TODO: 총자산 대비 비율로 계산
        return False  # 현재는 비활성 — monitor에서 호출 시 확장
