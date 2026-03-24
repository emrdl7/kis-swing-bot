"""진입 조건 판단 및 매수 실행."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from src.core.config import TradingConfig, ScreeningConfig
from src.core.models import SwingCandidate, SwingPosition, PositionState
from src.data.kis_client import KisClient

log = logging.getLogger(__name__)


class EntryExecutor:
    def __init__(
        self,
        kis: KisClient,
        trading_cfg: TradingConfig,
        screening_cfg: ScreeningConfig,
        dry_run: bool = False,
    ):
        self.kis = kis
        self.trading = trading_cfg
        self.screening = screening_cfg
        self.dry_run = dry_run

    def try_entry(
        self,
        candidate: SwingCandidate,
        current_price: float,
        cash: int,
        open_positions: list[SwingPosition],
    ) -> Optional[SwingPosition]:
        """후보 종목 진입 조건 확인 후 매수 실행.

        Returns:
            SwingPosition if entered, None otherwise.
        """
        # 이미 보유 중이면 스킵
        held_symbols = {p.symbol for p in open_positions if p.state not in (PositionState.CLOSED,)}
        if candidate.symbol in held_symbols:
            return None

        # 최대 포지션 수 확인
        active_count = len([p for p in open_positions if p.state != PositionState.CLOSED])
        if active_count >= self.trading.max_positions:
            return None

        # 가격이 진입 범위에 있는지 확인
        slack = self.screening.entry_zone_slack_pct / 100.0
        low = candidate.entry_low * (1.0 - slack)
        high = candidate.entry_high * (1.0 + slack)
        if not (low <= current_price <= high):
            return None

        # 투자금 계산
        invest_amount = int(cash * self.trading.position_size_pct)
        if invest_amount < 100_000:
            log.warning("[%s] 투자 가능 금액 부족: %d원", candidate.symbol, invest_amount)
            return None

        qty = max(1, invest_amount // int(current_price))
        if qty <= 0:
            return None

        log.info(
            "[%s] 진입 조건 충족 price=%.0f (진입대: %.0f~%.0f) qty=%d",
            candidate.symbol, current_price, low, high, qty,
        )

        # 매수 실행
        if not self.dry_run:
            try:
                self.kis.buy_market(candidate.symbol, qty)
            except Exception as e:
                log.error("[%s] 매수 주문 실패: %s", candidate.symbol, e)
                return None
        else:
            log.info("[DRY-RUN] 매수 스킵 [%s] qty=%d", candidate.symbol, qty)

        pos = SwingPosition(
            symbol=candidate.symbol,
            name=candidate.name,
            qty=qty,
            avg_price=current_price,
            entry_time=datetime.now(),
            target_price=candidate.target_price,
            stop_price=candidate.stop_price,
            state=PositionState.ENTERED,
            peak_price=current_price,
        )
        return pos
