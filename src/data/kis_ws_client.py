"""KIS WebSocket 실시간 체결통보 클라이언트 (H0STCNI0).

백그라운드 스레드에서 asyncio 루프를 실행하며, 체결 이벤트를 수신합니다.
monitor.py / entry_executor.py 에서 REST 폴링 대신 사용합니다.

사용 예::
    ws = KisWebSocketClient(cfg.kis, approval_key)
    ws.start()
    fill = ws.wait_for_fill("005930", "buy", since=datetime.now(), timeout=30)
    ws.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

_TR_CCNL = "H0STCNI0"   # 국내주식 실시간 체결통보
_MAX_FILLS = 200          # 보관할 최대 체결 이벤트 수


@dataclass
class FillEvent:
    """단일 체결 이벤트."""
    symbol: str
    side: str            # 'buy' | 'sell'
    qty: int             # 체결수량
    price: float         # 체결단가
    remaining: int       # 미체결잔량
    ts: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "remaining": self.remaining,
            "ts": self.ts.isoformat(),
        }


class KisWebSocketClient:
    """KIS 실시간 체결통보 WebSocket 클라이언트.

    hts_id 미설정 시 start()는 즉시 반환하고 기능이 비활성화됩니다.
    is_connected 를 확인하여 활성 여부를 판단하세요.
    """

    def __init__(self, base_url: str, app_key: str, app_secret: str,
                 hts_id: str, approval_key: str = ""):
        self._ws_url = self._build_ws_url(base_url)
        self._app_key = app_key
        self._app_secret = app_secret
        self._hts_id = hts_id
        self._approval_key = approval_key
        self._is_mock = "openapivts" in base_url

        self._fills: deque[FillEvent] = deque(maxlen=_MAX_FILLS)
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @staticmethod
    def _build_ws_url(base_url: str) -> str:
        # KIS 공식 WebSocket: ws://ops.koreainvestment.com:{port}/tryitout
        # 실전: 21000, 모의: 31000
        if "openapivts" in base_url:
            return "ws://ops.koreainvestment.com:31000/tryitout"
        return "ws://ops.koreainvestment.com:21000/tryitout"

    # ── 라이프사이클 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """백그라운드 스레드에서 WebSocket 연결 시작."""
        if not self._hts_id:
            log.info("KIS_HTS_ID 미설정 → WebSocket 체결통보 비활성")
            return
        if not self._approval_key:
            log.warning("approval_key 없음 → WebSocket 비활성. get_approval_key() 먼저 호출 필요")
            return
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="kis-ws-ccnl"
        )
        self._thread.start()

        # 최대 10초 대기
        if self._connected.wait(timeout=10):
            log.info("WebSocket 연결 완료")
        else:
            log.warning("WebSocket 연결 10초 초과 — 백그라운드에서 계속 시도")

    def stop(self) -> None:
        """WebSocket 연결 종료."""
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._connected.clear()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── asyncio 루프 ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_main())
        except Exception as e:
            log.error("WebSocket 루프 종료: %s", e)
        finally:
            self._loop.close()

    async def _ws_main(self) -> None:
        retry_delay = 5
        fail_count = 0
        max_fails = 3
        while self._running:
            try:
                await self._connect_and_run()
                retry_delay = 5
                fail_count = 0
            except Exception as e:
                self._connected.clear()
                fail_count += 1
                if fail_count >= max_fails:
                    log.warning("WebSocket %d회 연속 실패 → REST 폴링으로 전환", max_fails)
                    self._running = False
                    return
                log.warning("WebSocket 연결 끊김: %s — %ds 후 재연결 (%d/%d)", e, retry_delay, fail_count, max_fails)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def _connect_and_run(self) -> None:
        import websockets

        async with websockets.connect(
            self._ws_url,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            log.info("WebSocket 연결: %s", self._ws_url)
            await self._subscribe(ws)
            self._connected.set()

            async for message in ws:
                if not self._running:
                    break
                self._handle_message(message)

    async def _subscribe(self, ws) -> None:
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": _TR_CCNL,
                    "tr_key": self._hts_id,
                }
            },
        }
        await ws.send(json.dumps(msg))
        log.info("H0STCNI0 구독 요청 (hts_id=%s)", self._hts_id)

    # ── 메시지 파싱 ──────────────────────────────────────────────────────────

    def _handle_message(self, raw: str) -> None:
        try:
            if raw == "PINGPONG":
                return

            # JSON → 구독 응답 / 에러
            if raw.startswith("{"):
                data = json.loads(raw)
                header = data.get("header", {})
                body = data.get("body", {})
                rt_cd = header.get("tr_id", "")
                msg = body.get("msg1", "")
                if rt_cd == _TR_CCNL:
                    log.info("H0STCNI0 구독 응답: %s", msg or data)
                elif body.get("rt_cd") not in (None, "0", 0):
                    log.warning("WebSocket 오류 응답: %s", body)
                return

            # 실시간 데이터: "0|H0STCNI0|001|f0^f1^f2^..."
            parts = raw.split("|", 3)
            if len(parts) < 4 or parts[1] != _TR_CCNL:
                return

            raw_data = parts[3]
            fields = raw_data.split("^")

            # H0STCNI0 필드 순서 (KIS OpenAPI 공식 문서 기준)
            # [0]  고객 ID
            # [1]  계좌번호
            # [2]  주문번호
            # [3]  원주문번호
            # [4]  매수매도구분코드 (01=매도, 02=매수)
            # [5]  정정취소구분코드
            # [6]  주문종류코드
            # [7]  주문조건코드
            # [8]  주문주식단가
            # [9]  주문주식수량
            # [10] 주식체결수량
            # [11] 주식체결단가
            # [12] 주식미체결수량
            # [13] 주문처리결과 (2=체결, 1=접수)
            # [14] 종목코드
            # [15] 주문구분코드
            # [16] 주문잔량
            # [17] 계좌명
            # [18] 체결시각 (HHMMSS)
            # ...

            if len(fields) < 15:
                log.debug("H0STCNI0 필드 부족 (%d개): %s", len(fields), raw_data[:80])
                return

            order_result = fields[13]
            if order_result != "2":  # 2=체결만
                return

            sll_buy = fields[4]     # 01=매도, 02=매수
            side = "sell" if sll_buy == "01" else "buy"
            symbol = fields[14].strip()
            ccld_qty = int(fields[10] or 0)
            ccld_px = float(fields[11] or 0)
            remaining = int(fields[12] or 0)

            if ccld_qty <= 0 or ccld_px <= 0 or not symbol:
                return

            fill = FillEvent(
                symbol=symbol,
                side=side,
                qty=ccld_qty,
                price=ccld_px,
                remaining=remaining,
            )
            with self._lock:
                self._fills.append(fill)

            log.info(
                "[WS체결] %s %s %d주 @%.0f원 (미체결잔량 %d주)",
                "매수" if side == "buy" else "매도", symbol, ccld_qty, ccld_px, remaining,
            )

        except Exception as e:
            log.debug("WS 메시지 파싱 오류: %s | raw=%s", e, raw[:120])

    # ── 체결 이벤트 조회 ─────────────────────────────────────────────────────

    def wait_for_fill(
        self,
        symbol: str,
        side: str,
        since: Optional[datetime] = None,
        timeout: float = 30.0,
    ) -> Optional[FillEvent]:
        """체결 이벤트 대기 (블로킹).

        Args:
            symbol: 종목코드
            side: 'buy' | 'sell'
            since: 이 시각 이후 체결만 인정 (None → 지금)
            timeout: 최대 대기 시간 (초)

        Returns:
            첫 번째 매칭 FillEvent, 타임아웃 시 None
        """
        if since is None:
            since = datetime.now()

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            fill = self._find_fill(symbol, side, since)
            if fill:
                return fill
            time.sleep(0.3)

        return None

    def _find_fill(self, symbol: str, side: str, since: datetime) -> Optional[FillEvent]:
        with self._lock:
            for fill in reversed(list(self._fills)):
                if fill.ts < since:
                    break
                if fill.symbol == symbol and fill.side == side:
                    return fill
        return None

    def get_fills(
        self,
        symbol: str,
        side: str,
        since: Optional[datetime] = None,
    ) -> list[FillEvent]:
        """누적 체결 이벤트 목록 반환."""
        with self._lock:
            snapshot = list(self._fills)
        return [
            f for f in snapshot
            if f.symbol == symbol
            and f.side == side
            and (since is None or f.ts >= since)
        ]

    def total_filled_qty(
        self,
        symbol: str,
        side: str,
        since: Optional[datetime] = None,
    ) -> int:
        """since 이후 체결된 총 수량 합산."""
        return sum(f.qty for f in self.get_fills(symbol, side, since))
