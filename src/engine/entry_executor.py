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

            # 실제 체결 여부 및 체결가 확인 (잔고 API)
            import time as _time
            _time.sleep(2)  # 체결 반영 대기
            actual_price = current_price
            verified = False
            for attempt in range(3):  # 최대 3회 확인 (체결 지연 대비)
                try:
                    bal = self.kis.get_balance()
                    for item in bal.get("output1", []):
                        if item.get("pdno") == candidate.symbol:
                            hldg_qty = int(item.get("hldg_qty", 0) or 0)
                            p = float(item.get("pchs_avg_pric", 0) or 0)
                            if hldg_qty > 0:
                                verified = True
                                if p > 0:
                                    actual_price = p
                                log.info(
                                    "[%s] 매수 체결 확인: %.0f원 x %d주 (예상가 %.0f원)",
                                    candidate.symbol, actual_price, hldg_qty, current_price,
                                )
                            break
                    if verified:
                        break
                except Exception as e:
                    log.warning("[%s] 체결 확인 실패 (attempt %d): %s", candidate.symbol, attempt + 1, e)
                if not verified and attempt < 2:
                    _time.sleep(1)

            if not verified:
                log.error(
                    "[%s] 매수 체결 미확인 → 포지션 등록 취소 (ghost order 의심). "
                    "KIS 앱에서 체결 내역을 직접 확인하세요.",
                    candidate.symbol,
                )
                return None
        else:
            log.info("[DRY-RUN] 매수 스킵 [%s] qty=%d", candidate.symbol, qty)
            actual_price = current_price

        pos = SwingPosition(
            symbol=candidate.symbol,
            name=candidate.name,
            qty=qty,
            avg_price=actual_price,
            entry_time=datetime.now(),
            target_price=candidate.target_price,
            stop_price=candidate.stop_price,
            state=PositionState.ENTERED,
            peak_price=actual_price,
        )
        return pos
