"""진입 조건 판단 및 매수 실행."""
from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Optional

from src.core.config import TradingConfig, ScreeningConfig
from src.core.models import SwingCandidate, SwingPosition, PositionState
from src.data.kis_client import KisClient
from src.data.kis_ws_client import KisWebSocketClient

log = logging.getLogger(__name__)


class EntryExecutor:
    def __init__(
        self,
        kis: KisClient,
        trading_cfg: TradingConfig,
        screening_cfg: ScreeningConfig,
        dry_run: bool = False,
        ws_client: Optional[KisWebSocketClient] = None,
    ):
        self.kis = kis
        self.trading = trading_cfg
        self.screening = screening_cfg
        self.dry_run = dry_run
        self.ws_client = ws_client

    def _dynamic_size_pct(self, cash: int) -> float:
        """자본금 규모에 따라 종목당 투자 비중을 동적 조정.

        소액일수록 비중을 높여 1주라도 매수 가능하게 하고,
        자본금이 커지면 설정값(position_size_pct)으로 수렴.
        """
        base = self.trading.position_size_pct
        max_pos = self.trading.max_positions
        if cash <= 0:
            return base
        # 종목당 최소 투자금: 1/max_positions (균등 배분)
        equal_pct = 1.0 / max_pos
        # 소액(300만원 이하)이면 균등 배분, 이상이면 설정값 사용
        if cash < 3_000_000:
            return equal_pct
        return base

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

        # 투자금 계산 (자본금 규모에 따라 비중 동적 조정)
        pct = self._dynamic_size_pct(cash)
        invest_amount = int(cash * pct)
        if invest_amount < int(current_price):
            log.warning("[%s] 투자 가능 금액(%s원) < 주가(%s원), 매수 불가",
                        candidate.symbol, f"{invest_amount:,}", f"{int(current_price):,}")
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
            qty_before = self.kis.get_holding_qty(candidate.symbol)

            try:
                self.kis.buy_market(candidate.symbol, qty)
            except Exception as e:
                log.error("[%s] 매수 주문 실패: %s", candidate.symbol, e)
                return None

            order_time = datetime.now()
            actual_price = current_price
            verified = False

            # ── 체결 확인: WebSocket 우선, REST 폴링 fallback ──
            if self.ws_client and self.ws_client.is_connected:
                log.info("[%s] WS 체결 대기 (최대 30초)...", candidate.symbol)
                fill = self.ws_client.wait_for_fill(
                    candidate.symbol, "buy", since=order_time, timeout=30.0
                )
                if fill:
                    verified = True
                    actual_price = fill.price
                    log.info(
                        "[%s] WS 매수 체결 확인: %d주 @%.0f원 (미체결잔량 %d주)",
                        candidate.symbol, fill.qty, fill.price, fill.remaining,
                    )
                else:
                    log.warning("[%s] WS 30초 타임아웃 → REST 잔고 확인", candidate.symbol)

            if not verified:
                # REST 폴링 (최대 10회, 3s 간격, 최대 30초 대기)
                time.sleep(3)
                for attempt in range(10):
                    try:
                        qty_after = self.kis.get_holding_qty(candidate.symbol)
                        gained = qty_after - qty_before
                        if gained > 0:
                            verified = True
                            log.info(
                                "[%s] 매수 체결 확인: 보유수량 %d → %d (+%d주)",
                                candidate.symbol, qty_before, qty_after, gained,
                            )
                            break
                        log.warning(
                            "[%s] 매수 수량 미증가 (attempt %d/10): before=%d after=%d — 체결 대기 중...",
                            candidate.symbol, attempt + 1, qty_before, qty_after,
                        )
                    except Exception as e:
                        log.warning("[%s] 체결 확인 실패 (attempt %d): %s", candidate.symbol, attempt + 1, e)
                    if attempt < 9:
                        time.sleep(3)

            if not verified:
                log.error(
                    "[%s] 매수 체결 최종 미확인 → 포지션 등록 취소 (ghost order 의심). "
                    "KIS 앱에서 체결 내역을 직접 확인하세요.",
                    candidate.symbol,
                )
                return None

            # ── 실제 체결가 조회: 체결 내역 API → 잔고 평균가 순 ──
            if actual_price == current_price:
                try:
                    execs = self.kis.get_today_executions(candidate.symbol)
                    buy_execs = [e for e in execs if e.get("sll_buy_dvsn_cd") == "02"]
                    if buy_execs:
                        total_qty = sum(int(e.get("tot_ccld_qty", 0) or 0) for e in buy_execs)
                        total_amt = sum(int(e.get("tot_ccld_amt", 0) or 0) for e in buy_execs)
                        p = total_amt / total_qty if total_qty > 0 else 0
                        if p > 0:
                            actual_price = p
                            log.info("[%s] 매수 체결가 (체결내역 실계산): %.0f원", candidate.symbol, actual_price)
                except Exception:
                    pass

            if actual_price == current_price:
                # fallback: 잔고 pchs_avg_pric
                try:
                    bal = self.kis.get_balance()
                    for item in bal.get("output1", []):
                        if item.get("pdno") == candidate.symbol:
                            p = float(item.get("pchs_avg_pric", 0) or 0)
                            if p > 0:
                                actual_price = p
                                log.info("[%s] 매수 체결가 (잔고평균): %.0f원", candidate.symbol, actual_price)
                            break
                except Exception:
                    pass

            if actual_price != current_price:
                log.info(
                    "[%s] 체결가 보정: 예상 %.0f → 실제 %.0f원",
                    candidate.symbol, current_price, actual_price,
                )
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
