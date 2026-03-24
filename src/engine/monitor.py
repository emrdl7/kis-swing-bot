"""장중 모니터링 엔진 — 30초 루프."""
from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Optional

from src.core.config import AppConfig
from src.core import state_store
from src.core.clock import is_regular_market, minutes_to_close
from src.core.models import CloseReason, PositionState, SwingCandidate, SwingPosition
from src.data.kis_client import KisClient
from src.engine.entry_executor import EntryExecutor
from src.engine.position_manager import PositionManager
from src.engine.risk_manager import RiskManager
from src.notification.discord import DiscordNotifier

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 30


class MarketMonitor:
    def __init__(self, cfg: AppConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.kis = KisClient(cfg.kis)
        self.pos_mgr = PositionManager(cfg.exit)
        self.risk_mgr = RiskManager(cfg.trading)
        self.entry_exec = EntryExecutor(self.kis, cfg.trading, cfg.screening, dry_run=dry_run)
        self.notifier = DiscordNotifier(
            cfg.notification.discord_webhook_url,
            enabled=cfg.notification.discord_enabled,
        )
        self._daily_pnl: float = 0.0
        self._last_date: str = ""

    def run_forever(self) -> None:
        log.info("장중 모니터 시작 (interval=%ds, dry_run=%s)", POLL_INTERVAL_SEC, self.dry_run)
        try:
            while True:
                self._tick()
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("모니터 종료")
        finally:
            self.kis.close()
            self.notifier.close()

    # ── 메인 틱 ────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # 날짜 변경 시 일일 PnL 초기화
        if today != self._last_date:
            self._daily_pnl = 0.0
            self._last_date = today
            log.info("날짜 변경 → 일일 PnL 초기화")

        if not is_regular_market(now):
            return  # 정규장 외 시간은 스킵

        # 일일 손실 한도 확인
        if self.risk_mgr.is_daily_halt(self._daily_pnl):
            return

        # 상태 로드
        candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
        positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
        active_positions = [p for p in positions if p.state != PositionState.CLOSED]

        # 보유 포지션 청산 체크
        changed = False
        for pos in active_positions:
            try:
                price_data = self.kis.get_price(pos.symbol)
                px = float(price_data.get("stck_prpr", 0) or 0)
                if px <= 0:
                    continue

                # 트레일링 스탑 업데이트
                pos = self.pos_mgr.update_trailing(pos, px)

                # 청산 조건 확인
                should_exit, reason = self.pos_mgr.check_exit(pos, px, now)
                if should_exit and reason:
                    self._close_position(pos, px, reason, positions)
                    changed = True
            except Exception as e:
                log.error("[%s] 포지션 체크 오류: %s", pos.symbol, e)

        # 신규 진입 체크
        try:
            cash = self.kis.get_cash()
        except Exception as e:
            log.error("잔고 조회 실패: %s", e)
            cash = 0

        active_positions_updated = [p for p in positions if p.state != PositionState.CLOSED]
        for cand in candidates:
            if cand.is_expired(now):
                continue
            try:
                price_data = self.kis.get_price(cand.symbol)
                px = float(price_data.get("stck_prpr", 0) or 0)
                if px <= 0:
                    continue

                new_pos = self.entry_exec.try_entry(cand, px, cash, active_positions_updated)
                if new_pos:
                    positions.append(new_pos)
                    active_positions_updated.append(new_pos)
                    changed = True
                    self._notify_entry(new_pos)
            except Exception as e:
                log.error("[%s] 진입 체크 오류: %s", cand.symbol, e)

        if changed:
            state_store.save_positions([p.to_dict() for p in positions])

    def _close_position(
        self,
        pos: SwingPosition,
        price: float,
        reason: CloseReason,
        all_positions: list[SwingPosition],
    ) -> None:
        pnl_pct = pos.pnl_pct(price)
        pnl_amount = int((price - pos.avg_price) * pos.qty)

        log.info(
            "[%s] 청산 reason=%s price=%.0f pnl=%.2f%% (%+d원)",
            pos.symbol, reason.value, price, pnl_pct, pnl_amount,
        )

        if not self.dry_run:
            try:
                self.kis.sell_market(pos.symbol, pos.qty)
            except Exception as e:
                log.error("[%s] 매도 주문 실패: %s", pos.symbol, e)
                return

        pos.state = PositionState.CLOSED
        pos.close_reason = reason
        pos.close_price = price
        pos.close_time = datetime.now()

        self._daily_pnl += pnl_amount

        # 알림
        emoji = "✅" if pnl_pct >= 0 else "🔴"
        self.notifier.send(
            f"{emoji} **청산** {pos.name}({pos.symbol})\n"
            f"  이유: {reason.value}\n"
            f"  매도가: {int(price):,}원  PnL: {pnl_pct:+.2f}% ({pnl_amount:+,}원)\n"
            f"  오늘 누적 PnL: {int(self._daily_pnl):+,}원"
        )

    def _notify_entry(self, pos: SwingPosition) -> None:
        self.notifier.send(
            f"🟢 **매수** {pos.name}({pos.symbol})\n"
            f"  매수가: {int(pos.avg_price):,}원  수량: {pos.qty}주\n"
            f"  목표가: {int(pos.target_price):,}원  손절가: {int(pos.stop_price):,}원"
        )
