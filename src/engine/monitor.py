"""장중 모니터링 엔진 — 30초 루프 + WS 이벤트 드리븐 청산."""
from __future__ import annotations
import logging
import queue
import threading
import time
from datetime import datetime, timedelta

from src.core.config import AppConfig
from src.core import state_store
from src.core.clock import (
    is_regular_market, is_entry_allowed, is_closing_bet_entry,
    is_closing_bet_sell_time, is_pre_market_sell_window, now_kst,
)
from src.core.models import CloseReason, PositionState, SwingCandidate, SwingPosition
from src.data.kis_client import KisClient
from src.data.kis_ws_client import KisWebSocketClient
from src.engine.entry_executor import EntryExecutor
from src.engine.position_manager import PositionManager
from src.engine.risk_manager import RiskManager
from src.engine import rescreen_trigger
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

        # WebSocket 체결통보 클라이언트 (hts_id 설정 시 활성화)
        self.ws_client: KisWebSocketClient | None = None
        if cfg.kis.hts_id:
            try:
                approval_key = self.kis.get_approval_key()
                self.ws_client = KisWebSocketClient(
                    base_url=cfg.kis.base_url,
                    app_key=cfg.kis.app_key,
                    app_secret=cfg.kis.app_secret,
                    hts_id=cfg.kis.hts_id,
                    approval_key=approval_key,
                )
                self.ws_client.start()
                log.info("WebSocket 체결통보 활성화 (hts_id=%s)", cfg.kis.hts_id)
            except Exception as e:
                log.warning("WebSocket 초기화 실패, REST 폴링으로 운영: %s", e)
                self.ws_client = None

        self.entry_exec = EntryExecutor(
            self.kis, cfg.trading, cfg.screening,
            dry_run=dry_run, ws_client=self.ws_client,
        )
        self._reconcile_miss: dict[str, int] = {}  # 잔고 대사 연속 0 카운터
        # daily_pnl: 재시작 시에도 오늘 누적 손익 유지
        stats = state_store.load_daily_stats()
        today_str = now_kst().strftime("%Y-%m-%d")
        self._daily_pnl: float = stats.get("realized_pnl", 0.0) if stats.get("date") == today_str else 0.0
        self._last_date: str = today_str if stats.get("date") == today_str else ""

        # ── 이벤트 드리븐 청산 ──
        # WS 스레드에서 가격을 수신하면 _on_price_update 콜백이 익절/손절 판단만 빠르게 수행
        # 조건 성립 시 exit_queue에 밀어넣고, 별도 worker 스레드가 순차적으로 매도 주문 실행
        self._exit_queue: queue.Queue = queue.Queue()
        self._closing_symbols: set[str] = set()         # 진행 중인 매도 종목 (중복 방지)
        self._closing_lock = threading.Lock()
        self._worker_running = True
        self._exit_worker_thread = threading.Thread(
            target=self._exit_worker_loop, daemon=True, name="exit-worker",
        )
        self._exit_worker_thread.start()
        if self.ws_client:
            self.ws_client.set_price_callback(self._on_price_update)
            log.info("이벤트 드리븐 청산 활성화 (WS 콜백 등록)")

    def run_forever(self) -> None:
        log.info("장중 모니터 시작 (interval=%ds, dry_run=%s)", POLL_INTERVAL_SEC, self.dry_run)
        try:
            while True:
                self._tick()
                # 마감 직전(15:25~15:30): 5초 간격으로 폴링 강화 (동시호가 가격 변동 대응)
                now = now_kst()
                from datetime import time as _dt
                if _dt(15, 25) <= now.time() <= _dt(15, 30):
                    sleep_sec = 5
                else:
                    sleep_sec = POLL_INTERVAL_SEC
                time.sleep(sleep_sec)
        except KeyboardInterrupt:
            log.info("모니터 종료")
        finally:
            self._worker_running = False
            if self.ws_client:
                self.ws_client.stop()
            self.kis.close()

    # ── 메인 틱 ────────────────────────────────────────────────────────────

    def _on_price_update(self, symbol: str, price: float) -> None:
        """WS 가격 수신 콜백. 이벤트 드리븐 청산 판단 — 블로킹 금지."""
        if price <= 0:
            return
        # 진행 중이면 즉시 반환
        with self._closing_lock:
            if symbol in self._closing_symbols:
                return
        # 오직 장중 또는 프리장 매도 구간에서만 반응 (야간/마감 후 스킵)
        now = now_kst()
        cb_cfg = self.cfg.closing_bet
        in_pm = (
            cb_cfg.enabled and cb_cfg.pre_market_sell_enabled
            and is_pre_market_sell_window(now, cb_cfg.pre_market_from_hhmm, cb_cfg.pre_market_to_hhmm)
        )
        if not is_regular_market(now) and not in_pm:
            return
        try:
            positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
        except Exception:
            return
        for pos in positions:
            if pos.symbol != symbol or pos.state == PositionState.CLOSED:
                continue
            reason = None
            # 프리장: CB 포지션만 NXT 목표/손절 체크
            if in_pm:
                if pos.strategy != "closing_bet":
                    continue
                if pos.entry_time.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d"):
                    continue
                pnl_pct = pos.pnl_pct(price)
                if pnl_pct >= cb_cfg.pre_market_target_profit_pct:
                    reason = (CloseReason.TAKE_PROFIT, "nxt")
                elif pnl_pct <= -cb_cfg.pre_market_stop_loss_pct:
                    reason = (CloseReason.STOP_LOSS, "nxt")
            else:
                # 정규장: 기존 PositionManager 규칙 (트레일링 갱신 포함)
                pos2 = self.pos_mgr.update_trailing(pos, price)
                should_exit, r = self.pos_mgr.check_exit(pos2, price, now)
                if should_exit and r:
                    reason = (r, "krx")
            if reason:
                with self._closing_lock:
                    if symbol in self._closing_symbols:
                        return
                    self._closing_symbols.add(symbol)
                log.info("[이벤트청산] %s %s @%.0f (%s)", symbol, reason[0].value, price, reason[1])
                self._exit_queue.put((symbol, price, reason[0], reason[1]))
                return

    def _exit_worker_loop(self) -> None:
        """이벤트 큐에서 청산 요청을 꺼내 순차 실행 (REST 블로킹 허용)."""
        while self._worker_running:
            try:
                item = self._exit_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                symbol, price, reason, market = item
                # 최신 포지션 재로드 — _tick이 방금 닫았을 수도 있음
                positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
                target = next(
                    (p for p in positions if p.symbol == symbol and p.state != PositionState.CLOSED),
                    None,
                )
                if target is None:
                    log.debug("[이벤트청산] %s 이미 CLOSED — 스킵", symbol)
                    continue
                # 실행
                if market == "nxt":
                    self._close_position_nxt(target, price, reason)
                else:
                    self._close_position(target, price, reason)
                # 즉시 영속화 (30초 틱을 기다리지 않음)
                state_store.save_positions([p.to_dict() for p in positions])
            except Exception as e:
                log.error("[이벤트청산] 처리 실패: %s", e, exc_info=True)
            finally:
                with self._closing_lock:
                    self._closing_symbols.discard(symbol)

    def _get_px(self, symbol: str) -> float:
        """WS 실시간 체결가 우선, 없거나 오래되면 REST로 fallback."""
        if self.ws_client and self.ws_client.is_connected:
            px = self.ws_client.get_latest_price(symbol, max_age_sec=5.0)
            if px and px > 0:
                return float(px)
        pd = self.kis.get_price(symbol)
        return float(pd.get("stck_prpr", 0) or 0)

    def _sync_ws_subs(self, active_positions, candidates) -> None:
        """보유 + 후보 종목으로 WS 시세 구독 동기화 + 캐시 파일 저장."""
        if not (self.ws_client and self.ws_client.is_connected):
            return
        symbols = list({p.symbol for p in active_positions} | {c.symbol for c in candidates})
        self.ws_client.sync_price_subs(symbols)
        state_store.save_realtime_prices(self.ws_client.snapshot_prices())

    def _tick(self) -> None:
        now = now_kst()
        today = now.strftime("%Y-%m-%d")

        if today != self._last_date:
            self._daily_pnl = 0.0
            self._last_date = today
            state_store.save_daily_stats({"date": today, "realized_pnl": 0.0, "trade_count": 0})
            log.info("날짜 변경 → 일일 PnL 초기화")

        cb_cfg = self.cfg.closing_bet
        in_pre_market_sell = (
            cb_cfg.enabled and cb_cfg.pre_market_sell_enabled
            and is_pre_market_sell_window(now, cb_cfg.pre_market_from_hhmm, cb_cfg.pre_market_to_hhmm)
        )
        # 장·프리장 외 시간에도 WS 구독 동기화 + 가격 스냅샷 저장은 수행
        # (대시보드가 NXT 프리장/시간외 가격을 보려면 캐시가 살아있어야 함)
        if not is_regular_market(now) and not in_pre_market_sell:
            try:
                positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
                candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
                active_positions = [p for p in positions if p.state != PositionState.CLOSED]
                self._sync_ws_subs(active_positions, candidates)
            except Exception as e:
                log.debug("장외 WS 동기화 실패: %s", e)
            return

        if self.risk_mgr.is_daily_halt(self._daily_pnl):
            return

        candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
        positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
        active_positions = [p for p in positions if p.state != PositionState.CLOSED]

        # WS 시세 구독 동기화 + 스냅샷을 파일로 영속화 (대시보드가 참조)
        self._sync_ws_subs(active_positions, candidates)

        changed = False

        # ── 프리장(NXT) CB 조기 매도 ──
        if in_pre_market_sell:
            for pos in active_positions:
                if pos.strategy != "closing_bet":
                    continue
                if pos.entry_time.strftime("%Y-%m-%d") == today:
                    continue  # 당일 진입분은 다음날이 아님
                try:
                    px = self._get_px(pos.symbol)
                    if px <= 0:
                        continue
                    pnl_pct = pos.pnl_pct(px)
                    if pnl_pct >= cb_cfg.pre_market_target_profit_pct:
                        log.info("[NXT] %s 프리장 갭상승 익절 pnl=%.2f%%", pos.symbol, pnl_pct)
                        if self._close_position_nxt(pos, px, CloseReason.TAKE_PROFIT):
                            changed = True
                    elif pnl_pct <= -cb_cfg.pre_market_stop_loss_pct:
                        log.info("[NXT] %s 프리장 갭하락 손절 pnl=%.2f%%", pos.symbol, pnl_pct)
                        if self._close_position_nxt(pos, px, CloseReason.STOP_LOSS):
                            changed = True
                except Exception as e:
                    log.error("[NXT %s] 프리장 매도 체크 오류: %s", pos.symbol, e)
            if changed:
                state_store.save_positions([p.to_dict() for p in positions])
            return  # 프리장에서는 엔트리/reconcile 스킵

        # 정규장 로직
        # 후보 소진 시 자동 재토론 (쿨다운·한도 가드)
        active_cands = [c for c in candidates if not c.is_expired(now)]
        ok, reason = rescreen_trigger.should_rescreen(now, len(active_cands), manual=False)
        if ok:
            log.info("[재토론] 자동 트리거 — 활성 후보 %d개", len(active_cands))
            rescreen_trigger.trigger_rescreen(now, manual=False)

        # ── 종가배팅 익일 오전 매도 ──
        if cb_cfg.enabled and is_closing_bet_sell_time(now, cb_cfg.sell_before_hhmm):
            for pos in active_positions:
                if pos.strategy != "closing_bet":
                    continue
                # 어제 진입한 종가배팅 포지션만 매도
                if pos.entry_time.strftime("%Y-%m-%d") == today:
                    continue  # 오늘 진입 = 아직 오버나이트 아님
                try:
                    price_data = self.kis.get_price(pos.symbol)
                    px = float(price_data.get("stck_prpr", 0) or 0)
                    if px <= 0:
                        continue
                    pnl_pct = pos.pnl_pct(px)
                    # 목표 도달 또는 손절 또는 매도 시간 임박
                    should_sell = (
                        pnl_pct >= cb_cfg.target_profit_pct
                        or pnl_pct <= -cb_cfg.stop_loss_pct
                        or now.time() >= (datetime.combine(now.date(), datetime.min.time()) + timedelta(minutes=-5 + cb_cfg.sell_before_hhmm // 100 * 60 + cb_cfg.sell_before_hhmm % 100)).time()
                    )
                    if should_sell:
                        self._close_position(pos, px, CloseReason.CLOSING_BET_MORNING)
                        changed = True
                except Exception as e:
                    log.error("[CB %s] 익일매도 체크 오류: %s", pos.symbol, e)

        for pos in active_positions:
            if pos.state == PositionState.CLOSED:
                continue
            # 이벤트 워커가 이미 매도 진행 중이면 중복 방지
            with self._closing_lock:
                if pos.symbol in self._closing_symbols:
                    continue
            try:
                px = self._get_px(pos.symbol)
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
                item.get("pdno"): {
                    "qty": int(item.get("hldg_qty", 0) or 0),
                    "avg": float(item.get("pchs_avg_pric", 0) or 0),
                }
                for item in (bal.get("output1") or [])
                if int(item.get("hldg_qty", 0) or 0) > 0
            }
            for pos in active_positions:
                # 이번 틱에서 이미 CLOSED 처리된 포지션은 reconcile 대상에서 제외
                # (정상 매도 체결 직후 같은 틱 reconcile이 RECONCILE_KIS_ZERO로 덮어쓰는 것 방지)
                if pos.state == PositionState.CLOSED:
                    self._reconcile_miss.pop(pos.symbol, None)
                    continue
                kis = kis_holdings.get(pos.symbol)
                if kis is None:
                    miss = self._reconcile_miss.get(pos.symbol, 0) + 1
                    self._reconcile_miss[pos.symbol] = miss
                    if miss < 3:
                        log.warning(
                            "⚠️ 잔고 불일치 [%s] positions=%d주 / KIS=0주 — API 지연 가능성, 다음 틱 재확인 (miss=%d/3)",
                            pos.symbol, pos.qty, miss,
                        )
                    else:
                        log.error(
                            "⚠️ 잔고 불일치 [%s] positions=%d주 / KIS=0주 — %d틱 연속 확인, ghost position CLOSED 처리",
                            pos.symbol, pos.qty, miss,
                        )
                        pos.state = PositionState.CLOSED
                        pos.close_reason = CloseReason.RECONCILE_KIS_ZERO
                        pos.close_price = pos.avg_price
                        pos.close_time = now
                        self._reconcile_miss.pop(pos.symbol, None)
                        changed = True
                else:
                    self._reconcile_miss.pop(pos.symbol, None)
                    if kis["qty"] != pos.qty:
                        log.warning(
                            "⚠️ 잔고 불일치 [%s] positions=%d주 / KIS=%d주 — KIS 기준 수량 보정",
                            pos.symbol, pos.qty, kis["qty"],
                        )
                        pos.qty = kis["qty"]
                        changed = True
                    if kis["avg"] > 0 and abs(kis["avg"] - pos.avg_price) > 1:
                        log.warning(
                            "⚠️ 평균단가 불일치 [%s] positions=%.0f / KIS=%.0f — KIS 기준 보정",
                            pos.symbol, pos.avg_price, kis["avg"],
                        )
                        pos.avg_price = kis["avg"]
                        changed = True
            for symbol, kis in kis_holdings.items():
                if not any(p.symbol == symbol for p in active_positions):
                    log.warning(
                        "⚠️ KIS 잔고 [%s] %d주 있으나 positions.json에 없음 — 수동 매수 또는 누락",
                        symbol, kis["qty"],
                    )
        except Exception as e:
            log.error("잔고 조회/대사 실패: %s", e)

        # 청산/대사 단계에서 변경된 내용은 엔트리 단계 진입 전에 즉시 영속화
        # (엔트리 시간 전 early-return 경로에서 손절 CLOSED 상태가 소실되는 버그 방지)
        if changed:
            state_store.save_positions([p.to_dict() for p in positions])

        # 매수 허용 시간 확인 (스윙 또는 종가배팅 시간이 아니면 스킵)
        swing_ok = is_entry_allowed(now)
        cb_ok = cb_cfg.enabled and is_closing_bet_entry(now, cb_cfg.entry_from_hhmm, cb_cfg.entry_to_hhmm)
        if not swing_ok and not cb_ok:
            return

        # 오늘 이미 거래된 종목 (진입 또는 당일 청산) → 재진입 금지
        today_str = today
        traded_today = {
            p.symbol for p in positions
            if p.entry_time.strftime("%Y-%m-%d") == today_str
        }

        active_now = [p for p in positions if p.state != PositionState.CLOSED]
        entered_symbols: set[str] = set()  # 이번 틱에서 진입한 종목

        # 후보 가격 일괄 조회 + 진입 불가 후보 자동 제거
        drop_pct = self.cfg.screening.drop_above_entry_pct / 100.0
        remaining_candidates = []
        cand_prices: dict[str, float] = {}
        dropped = False
        for cand in candidates:
            if cand.is_expired(now):
                dropped = True
                continue
            try:
                cpx = self._get_px(cand.symbol)
                if cpx > 0 and cand.entry_high > 0:
                    gap = (cpx - cand.entry_high) / cand.entry_high
                    if gap > drop_pct:
                        log.info(
                            "[%s] 후보 제거: 현재가 %s원이 진입상단 %s원 대비 +%.1f%% (기준 %.1f%%)",
                            cand.symbol, f"{int(cpx):,}", f"{int(cand.entry_high):,}",
                            gap * 100, self.cfg.screening.drop_above_entry_pct,
                        )
                        dropped = True
                        continue
                cand_prices[cand.symbol] = cpx
            except Exception:
                pass
            remaining_candidates.append(cand)

        if dropped:
            # 예비후보에서 승격
            max_cands = self.cfg.screening.max_candidates
            if len(remaining_candidates) < max_cands:
                reserves = [SwingCandidate.from_dict(d) for d in state_store.load_reserves()]
                existing_symbols = {c.symbol for c in remaining_candidates}
                promoted = []
                for r in reserves:
                    if r.symbol not in existing_symbols and not r.is_expired(now):
                        remaining_candidates.append(r)
                        existing_symbols.add(r.symbol)
                        promoted.append(r)
                        log.info("[%s] 예비후보 → 정규 승격: %s (신뢰: %.0f%%)", r.symbol, r.name, r.consensus_score * 100)
                        if len(remaining_candidates) >= max_cands:
                            break
                if promoted:
                    # 승격된 후보는 reserves에서 제거
                    promoted_symbols = {p.symbol for p in promoted}
                    reserves = [r for r in reserves if r.symbol not in promoted_symbols]
                    state_store.save_reserves([r.to_dict() for r in reserves])

            state_store.save_candidates([c.to_dict() for c in remaining_candidates])
            candidates = remaining_candidates

        for cand in remaining_candidates:
            # 당일 이미 거래된 종목은 재진입 금지
            if cand.symbol in traded_today:
                continue
            px = cand_prices.get(cand.symbol, 0)
            if px <= 0:
                continue
            # 전략별 진입 시간 확인
            is_cb = "closing_bet" in (cand.tags or [])
            if is_cb and not is_closing_bet_entry(now, cb_cfg.entry_from_hhmm, cb_cfg.entry_to_hhmm):
                continue
            if not is_cb and not is_entry_allowed(now):
                continue
            try:
                strat = "closing_bet" if is_cb else "swing"
                strat_max = cb_cfg.max_positions if is_cb else self.cfg.trading.max_positions
                new_pos = self.entry_exec.try_entry(
                    cand, px, cash, active_now,
                    strategy=strat, strategy_max=strat_max,
                )
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

    def _close_position_nxt(self, pos: SwingPosition, price: float, reason: CloseReason) -> bool:
        """NXT 거래소 지정가 매도. 성공 시 True 반환."""
        pnl_amount = int((price - pos.avg_price) * pos.qty)
        pnl_pct = pos.pnl_pct(price)
        log.info(
            "[NXT %s] 매도 시도 reason=%s price=%.0f pnl=%.2f%% (%+d원)",
            pos.symbol, reason.value, price, pnl_pct, pnl_amount,
        )
        if self.dry_run:
            pos.state = PositionState.CLOSED
            pos.close_reason = reason
            pos.close_price = price
            pos.close_time = datetime.now()
            return True
        try:
            self.kis.sell_nxt(pos.symbol, pos.qty, price)
        except Exception as e:
            log.error("[NXT %s] 매도 주문 실패: %s — 정규장 재시도로 fallback", pos.symbol, e)
            return False
        import time as _t
        _t.sleep(3)
        qty_after = self.kis.get_holding_qty(pos.symbol)
        if qty_after > 0:
            log.warning("[NXT %s] 체결 미확인 (잔여 %d주), 정규장에서 재처리 대기", pos.symbol, qty_after)
            return False
        pos.state = PositionState.CLOSED
        pos.close_reason = reason
        pos.close_price = price
        pos.close_time = datetime.now()
        self._daily_pnl += pnl_amount
        log.info("[NXT %s] 체결 완료. 오늘 누적 PnL: %+d원", pos.symbol, int(self._daily_pnl))
        stats = state_store.load_daily_stats()
        stats["date"] = now_kst().strftime("%Y-%m-%d")
        stats["realized_pnl"] = self._daily_pnl
        stats["trade_count"] = stats.get("trade_count", 0) + 1
        state_store.save_daily_stats(stats)
        apple_notes.report_trade(
            "매도", pos.symbol, pos.name, price, pos.qty,
            f"이유: {reason.value} (NXT 프리장)  PnL: {pnl_pct:+.2f}% ({pnl_amount:+,}원)\n"
            f"오늘 누적 PnL: {int(self._daily_pnl):+,}원",
        )
        return True

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

            import time as _time
            order_time = now_kst()
            sell_verified = False
            qty_after = pos.qty

            # ── 체결 검증: WebSocket 우선, REST 폴링 fallback ──
            if self.ws_client and self.ws_client.is_connected:
                log.info("[%s] WS 매도 체결 대기 (최대 30초)...", pos.symbol)
                fill = self.ws_client.wait_for_fill(
                    pos.symbol, "sell", since=order_time, timeout=30.0
                )
                if fill:
                    sell_verified = True
                    log.info(
                        "[%s] WS 매도 체결 확인: %d주 @%.0f원 (미체결잔량 %d주)",
                        pos.symbol, fill.qty, fill.price, fill.remaining,
                    )
                else:
                    log.warning("[%s] WS 30초 타임아웃 → REST 잔고 확인", pos.symbol)

            if not sell_verified:
                # REST 폴링 (최대 10회, 3s 간격, 최대 30초 대기)
                _time.sleep(3)
                for attempt in range(10):
                    try:
                        qty_after = self.kis.get_holding_qty(pos.symbol)
                        if qty_after == 0:
                            sell_verified = True
                            log.info("[%s] 매도 전량 체결 확인: %d주 → 0주", pos.symbol, pos.qty)
                            break
                        else:
                            log.warning(
                                "[%s] 잔고 %d주 잔여 (attempt %d/10) — 체결 대기 중...",
                                pos.symbol, qty_after, attempt + 1,
                            )
                    except Exception as e:
                        log.warning("[%s] 매도 체결 확인 실패 (attempt %d): %s", pos.symbol, attempt + 1, e)
                        sell_verified = True  # API 오류 시 재매도 방지
                        break
                    if attempt < 9:
                        _time.sleep(3)

            if not sell_verified and qty_after < pos.qty:
                # 30초 후에도 잔고 감소는 있음 → 부분체결 가능성
                sold_qty = pos.qty - qty_after
                sell_verified = True
                log.warning(
                    "[%s] 매도 부분 체결 확정: %d주 중 %d주 매도, %d주 잔여 — KIS 앱 확인 필요",
                    pos.symbol, pos.qty, sold_qty, qty_after,
                )
                pos.qty = sold_qty

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
        # 재시작 시에도 유지되도록 영속 저장
        stats = state_store.load_daily_stats()
        stats["date"] = now_kst().strftime("%Y-%m-%d")
        stats["realized_pnl"] = self._daily_pnl
        stats["trade_count"] = stats.get("trade_count", 0) + 1
        state_store.save_daily_stats(stats)

        apple_notes.report_trade(
            "매도", pos.symbol, pos.name, price, pos.qty,
            f"이유: {reason.value}  PnL: {pnl_pct:+.2f}% ({pnl_amount:+,}원)\n"
            f"오늘 누적 PnL: {int(self._daily_pnl):+,}원",
        )
