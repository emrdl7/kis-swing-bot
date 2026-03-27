"""장중 모니터링 엔진 — 30초 루프."""
from __future__ import annotations
import logging
import time
from datetime import datetime

from src.core.config import AppConfig
from src.core import state_store
from src.core.clock import is_regular_market, is_entry_allowed
from src.core.models import CloseReason, PositionState, SwingCandidate, SwingPosition
from src.data.kis_client import KisClient
from src.engine.entry_executor import EntryExecutor
from src.engine.position_manager import PositionManager
from src.engine.risk_manager import RiskManager
from src.notification import apple_notes

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
        self._daily_pnl: float = 0.0
        self._last_date: str = ""

    def run_forever(self) -> None:
        log.info("장중 모니터 시작 (interval=%ds, dry_run=%s)", POLL_INTERVAL_SEC, self.dry_run)
        try:
            while True:
                self._tick()
                # 마감 직전(15:25~15:30): 5초 간격으로 폴링 강화 (동시호가 가격 변동 대응)
                now = datetime.now()
                from datetime import time as _dt
                if _dt(15, 25) <= now.time() <= _dt(15, 30):
                    sleep_sec = 5
                else:
                    sleep_sec = POLL_INTERVAL_SEC
                time.sleep(sleep_sec)
        except KeyboardInterrupt:
            log.info("모니터 종료")
        finally:
            self.kis.close()

    # ── 메인 틱 ────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if today != self._last_date:
            self._daily_pnl = 0.0
            self._last_date = today
            log.info("날짜 변경 → 일일 PnL 초기화")

        if not is_regular_market(now):
            return

        if self.risk_mgr.is_daily_halt(self._daily_pnl):
            return

        candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
        positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
        active_positions = [p for p in positions if p.state != PositionState.CLOSED]

        changed = False
        for pos in active_positions:
            try:
                price_data = self.kis.get_price(pos.symbol)
                px = float(price_data.get("stck_prpr", 0) or 0)
                if px <= 0:
                    continue

                prev_state = pos.state
                prev_peak = pos.peak_price
                pos = self.pos_mgr.update_trailing(pos, px)

                # 트레일링 상태 변화 또는 peak 갱신 시에도 저장 (재시작 시 상태 유지)
                if pos.state != prev_state or pos.peak_price != prev_peak:
                    changed = True

                should_exit, reason = self.pos_mgr.check_exit(pos, px, now)
                if should_exit and reason:
                    self._close_position(pos, px, reason)
                    changed = True
            except Exception as e:
                log.error("[%s] 포지션 체크 오류: %s", pos.symbol, e)

        # ── 잔고 조회 1회 → 현금 + KIS 대사 동시 처리 ──
        cash = self.cfg.trading.mock_budget or 0
        try:
            bal = self.kis.get_balance()
            output2 = (bal.get("output2") or [{}])[0]
            for field in ("ord_psbl_cash", "prvs_rcdl_excc_amt"):
                v = int(output2.get(field, 0) or 0)
                if v > 0:
                    cash = v
                    break
            if cash == 0 and self.cfg.trading.mock_budget > 0:
                cash = self.cfg.trading.mock_budget
                log.debug("잔고 API 0 → mock_budget 사용: %d원", cash)

            # KIS 실제 잔고 vs positions.json 대사
            kis_holdings = {
                item.get("pdno"): int(item.get("hldg_qty", 0) or 0)
                for item in (bal.get("output1") or [])
                if int(item.get("hldg_qty", 0) or 0) > 0
            }
            for pos in active_positions:
                kis_qty = kis_holdings.get(pos.symbol, 0)
                if kis_qty == 0:
                    log.error(
                        "⚠️ 잔고 불일치 [%s] positions=%d주 / KIS=0주 — ghost position 의심, 수동 확인 필요",
                        pos.symbol, pos.qty,
                    )
                elif kis_qty != pos.qty:
                    log.warning(
                        "⚠️ 잔고 불일치 [%s] positions=%d주 / KIS=%d주",
                        pos.symbol, pos.qty, kis_qty,
                    )
            for symbol, kis_qty in kis_holdings.items():
                if not any(p.symbol == symbol for p in active_positions):
                    log.warning(
                        "⚠️ KIS 잔고 [%s] %d주 있으나 positions.json에 없음 — 수동 매수 또는 누락",
                        symbol, kis_qty,
                    )
        except Exception as e:
            log.error("잔고 조회/대사 실패: %s", e)

        # 장 시작 5분 이내 매수 금지 (호가 갭 회피)
        if not is_entry_allowed(now):
            return

        # 오늘 이미 거래된 종목 (진입 또는 당일 청산) → 재진입 금지
        today_str = today
        traded_today = {
            p.symbol for p in positions
            if p.entry_time.strftime("%Y-%m-%d") == today_str
        }

        active_now = [p for p in positions if p.state != PositionState.CLOSED]
        entered_symbols: set[str] = set()  # 이번 틱에서 진입한 종목

        remaining_candidates = list(candidates)
        for cand in candidates:
            if cand.is_expired(now):
                continue
            # 당일 이미 거래된 종목은 재진입 금지
            if cand.symbol in traded_today:
                continue
            try:
                price_data = self.kis.get_price(cand.symbol)
                px = float(price_data.get("stck_prpr", 0) or 0)
                if px <= 0:
                    continue

                new_pos = self.entry_exec.try_entry(cand, px, cash, active_now)
                if new_pos:
                    positions.append(new_pos)
                    active_now.append(new_pos)
                    entered_symbols.add(cand.symbol)
                    traded_today.add(cand.symbol)
                    changed = True
                    log.info(
                        "[%s] 매수 완료 avg=%.0f qty=%d 목표=%.0f 손절=%.0f",
                        new_pos.symbol, new_pos.avg_price, new_pos.qty,
                        new_pos.target_price, new_pos.stop_price,
                    )
                    apple_notes.report_trade(
                        "매수", new_pos.symbol, new_pos.name,
                        new_pos.avg_price, new_pos.qty,
                        f"목표가: {int(new_pos.target_price):,}원  손절가: {int(new_pos.stop_price):,}원",
                    )
            except Exception as e:
                log.error("[%s] 진입 체크 오류: %s", cand.symbol, e)

        # 진입 완료된 후보는 candidates.json에서 제거
        if entered_symbols:
            remaining_candidates = [c for c in candidates if c.symbol not in entered_symbols]
            state_store.save_candidates([c.to_dict() for c in remaining_candidates])

        if changed:
            state_store.save_positions([p.to_dict() for p in positions])

    def _close_position(
        self,
        pos: SwingPosition,
        price: float,
        reason: CloseReason,
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

            # ── 체결 검증: 잔고 수량 감소 확인 (최대 3회, 1s 간격) ──
            import time as _time
            _time.sleep(2)
            sell_verified = False

            qty_after = pos.qty  # 기본값: 변화 없음
            for attempt in range(3):
                try:
                    qty_after = self.kis.get_holding_qty(pos.symbol)
                    if qty_after == 0:
                        # 전량 매도 확인
                        sell_verified = True
                        log.info("[%s] 매도 전량 체결 확인: %d주 → 0주", pos.symbol, pos.qty)
                        break
                    elif qty_after < pos.qty:
                        # 잔고 API 지연 가능성 → 추가 대기 후 재확인
                        if attempt < 2:
                            log.warning(
                                "[%s] 잔고 %d주 잔여 (attempt %d) — API 지연 가능성, 재확인 중...",
                                pos.symbol, qty_after, attempt + 1,
                            )
                            _time.sleep(2)
                            continue
                        # 3회 모두 잔여 → 부분체결로 확정
                        sell_verified = True
                        sold_qty = pos.qty - qty_after
                        log.warning(
                            "[%s] 매도 부분 체결 확정: %d주 중 %d주 매도, %d주 잔여 — KIS 앱 확인 필요",
                            pos.symbol, pos.qty, sold_qty, qty_after,
                        )
                        pos.qty = sold_qty  # 실제 매도된 수량으로 보정
                        break
                    else:
                        log.error(
                            "[%s] 매도 체결 미확인: 잔고 %d주 그대로 (attempt %d) — ghost order 의심",
                            pos.symbol, qty_after, attempt + 1,
                        )
                except Exception as e:
                    log.warning("[%s] 매도 체결 확인 실패 (attempt %d): %s", pos.symbol, attempt + 1, e)
                    sell_verified = True  # API 오류 시 정상 처리 (재매도 방지)
                    break
                if attempt < 2:
                    _time.sleep(1)

            if not sell_verified:
                log.error(
                    "[%s] 매도 체결 최종 미확인 → CLOSED 처리 보류. "
                    "KIS 앱에서 체결 내역을 직접 확인하세요.",
                    pos.symbol,
                )
                return

            # PnL을 실제 매도 수량 기준으로 재계산
            pnl_pct = pos.pnl_pct(price)
            pnl_amount = int((price - pos.avg_price) * pos.qty)

            # ── 실제 매도 체결가 조회: 체결 내역 API (tot_ccld_amt/tot_ccld_qty) → 현재가 순 ──
            try:
                execs = self.kis.get_today_executions(pos.symbol)
                sell_execs = [e for e in execs if e.get("sll_buy_dvsn_cd") == "01"]
                if sell_execs:
                    total_qty = sum(int(e.get("tot_ccld_qty", 0) or 0) for e in sell_execs)
                    total_amt = sum(int(e.get("tot_ccld_amt", 0) or 0) for e in sell_execs)
                    p = total_amt / total_qty if total_qty > 0 else 0
                    if p > 0:
                        log.info(
                            "[%s] 매도 체결가 (체결내역 실계산): %.0f원 (트리거 %.0f원, 체결%d주×%+.0f원)",
                            pos.symbol, p, price, total_qty, p - price,
                        )
                        price = p
                        pnl_pct = pos.pnl_pct(price)
                        pnl_amount = int((price - pos.avg_price) * pos.qty)
            except Exception as e:
                log.warning("[%s] 체결 내역 조회 실패, 현재가로 fallback: %s", pos.symbol, e)
                try:
                    pd = self.kis.get_price(pos.symbol)
                    p = float(pd.get("stck_prpr", 0) or 0)
                    if p > 0:
                        log.info("[%s] 매도 체결가 (현재가 근사): %.0f원", pos.symbol, p)
                        price = p
                        pnl_pct = pos.pnl_pct(price)
                        pnl_amount = int((price - pos.avg_price) * pos.qty)
                except Exception:
                    pass

        pos.state = PositionState.CLOSED
        pos.close_reason = reason
        pos.close_price = price
        pos.close_time = datetime.now()
        self._daily_pnl += pnl_amount
        log.info("[%s] 오늘 누적 PnL: %+d원", pos.symbol, int(self._daily_pnl))

        apple_notes.report_trade(
            "매도", pos.symbol, pos.name, price, pos.qty,
            f"이유: {reason.value}  PnL: {pnl_pct:+.2f}% ({pnl_amount:+,}원)\n"
            f"오늘 누적 PnL: {int(self._daily_pnl):+,}원",
        )
