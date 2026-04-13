"""웹 대시보드 서버 (FastAPI)."""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime
import threading
import time as _time
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from src.core.config import load_config
from src.core import state_store
from src.core.models import PositionState, SwingPosition, SwingCandidate
from src.data.kis_client import KisClient
from src.engine import rescreen_trigger

app = FastAPI()
_cfg = load_config()
_kis = KisClient(_cfg.kis)


def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    """종목코드 → 현재가 딕셔너리. WS 실시간 캐시 우선, 미수신 종목만 REST 보강.

    캐시 파일은 monitor가 30초 주기로 저장하므로 신선도 기준을 60초로 잡아
    평시 REST fallback이 과하게 발생하지 않도록 한다.
    """
    from datetime import datetime, timedelta
    result: dict[str, float] = {}
    cache = state_store.load_realtime_prices() or {}
    cutoff = datetime.now() - timedelta(seconds=60)
    for sym in symbols:
        entry = cache.get(sym)
        if entry:
            try:
                ts = datetime.fromisoformat(entry.get("ts", ""))
                px = float(entry.get("price", 0) or 0)
                if px > 0 and ts >= cutoff:
                    result[sym] = px
                    continue
            except Exception:
                pass
        # WS 캐시에 없거나 오래됨 → REST 조회 (첫 로드 및 NXT 비거래 종목 대비)
        try:
            data = _kis.get_price(sym)
            px = float(data.get("stck_prpr", 0) or 0)
            if px > 0:
                result[sym] = px
        except Exception:
            pass
    return result


_REASON_KO = {
    "TAKE_PROFIT": "목표가 도달",
    "STOP_LOSS": "손절",
    "TRAILING_STOP": "트레일링 스탑",
    "EOD": "장 마감",
    "MANUAL": "수동",
    "RECONCILE_KIS_ZERO": "KIS 잔고 0",
    "CLOSING_BET_MORNING": "종가배팅 익일매도",
}


def _reason_str(reason) -> str:
    if reason is None:
        return "-"
    return _REASON_KO.get(reason.value, reason.value)


def _pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "#00c9a7"
    if pnl < 0:
        return "#ff6b6b"
    return "#aaa"


def _in_zone_badge(price: float, low: float, high: float) -> str:
    """현재가가 진입 구간 안에 있으면 뱃지 표시."""
    slack = _cfg.screening.entry_zone_slack_pct / 100.0
    if low * (1 - slack) <= price <= high * (1 + slack):
        return '<span style="color:#00c9a7;font-weight:bold">● 진입구간</span>'
    if price < low:
        gap_pct = (low - price) / price * 100
        return f'<span style="color:#888">▼ {gap_pct:.1f}% 아래</span>'
    gap_pct = (price - high) / price * 100
    return f'<span style="color:#f9ca24">▲ {gap_pct:.1f}% 위</span>'


def _compute_snapshot() -> dict:
    """대시보드 렌더링에 필요한 모든 동적 데이터 + HTML 프래그먼트 생성."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")

    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]

    active = [p for p in positions if p.state != PositionState.CLOSED]
    from src.core.models import CloseReason
    closed_today = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_time and p.close_time.strftime("%Y-%m-%d") == today
        and p.close_reason != CloseReason.RECONCILE_KIS_ZERO  # 잔고 대사 자동처리는 제외
    ]
    active_cands = [c for c in candidates if not c.is_expired()]

    # 전체 승률 계산 (RECONCILE 제외)
    real_closed = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_reason not in (None, CloseReason.RECONCILE_KIS_ZERO)
    ]
    total_trades = len(real_closed)
    wins = len([p for p in real_closed if p.close_price and p.close_price > p.avg_price])
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    total_realized = sum(
        int((p.close_price - p.avg_price) * p.qty)
        for p in real_closed if p.close_price
    )
    total_realized_color = _pnl_color(total_realized)

    # 현재가 일괄 조회
    symbols = list({p.symbol for p in active} | {c.symbol for c in active_cands})
    prices = _fetch_prices(symbols)

    # 계좌 잔고
    try:
        bal = _kis.get_balance()
        o2 = (bal.get("output2") or [{}])[0]
        account_cash = int(o2.get("dnca_tot_amt", 0))      # 예수금 총액
        order_cash = int(o2.get("ord_psbl_cash", 0) or 0)  # 주문가능액
        if order_cash == 0:
            order_cash = int(o2.get("prvs_rcdl_excc_amt", 0) or 0)  # 모의투자 fallback
        eval_amt = int(o2.get("evlu_amt_smtl_amt", 0))      # 유가평가액
        total_eval = int(o2.get("tot_evlu_amt", 0))         # 총평가금액
    except Exception:
        account_cash = order_cash = eval_amt = total_eval = 0

    # 오늘 PnL (청산 기준)
    daily_pnl = sum(
        int((p.close_price - p.avg_price) * p.qty)
        for p in closed_today if p.close_price
    )
    # 미실현 PnL 합산
    unrealized_pnl = sum(
        int((prices.get(p.symbol, p.avg_price) - p.avg_price) * p.qty)
        for p in active
    )
    pnl_color = _pnl_color(daily_pnl)
    unr_color = _pnl_color(unrealized_pnl)

    # ── 보유 포지션 테이블 ──────────────────────────────────────────
    pos_rows = ""
    for p in active:
        cur_px = prices.get(p.symbol, 0)
        pnl_pct = p.pnl_pct(cur_px) if cur_px else 0
        pnl_amt = int((cur_px - p.avg_price) * p.qty) if cur_px else 0
        pc = _pnl_color(pnl_pct)
        trail = f"{int(p.trailing_stop_px):,}" if p.trailing_stop_px else "-"
        cur_str = f"{int(cur_px):,}" if cur_px else "-"
        pos_rows += f"""
        <tr>
          <td><b>{p.name}</b><br><small style="color:#888">{p.symbol}</small></td>
          <td>{p.qty}</td>
          <td>{round(p.avg_price):,}</td>
          <td style="color:#fff;font-weight:bold">{cur_str}</td>
          <td style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%<br>
            <small style="color:{pc}">{pnl_amt:+,}원</small></td>
          <td><span class="badge {p.state.value.lower()}">{p.state.value}</span></td>
          <td class="hide-mobile">{int(p.target_price):,}</td>
          <td class="hide-mobile">{int(p.stop_price):,}</td>
          <td class="hide-mobile">{trail}</td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="9" style="text-align:center;color:#666">보유 포지션 없음</td></tr>'

    # ── 후보 종목 테이블 ──────────────────────────────────────────
    cand_rows = ""
    for c in active_cands:
        exp = c.expires_at.strftime("%m/%d") if c.expires_at else "-"
        score_color = "#00c9a7" if c.consensus_score >= 0.7 else "#f9ca24"
        cur_px = prices.get(c.symbol, 0)
        cur_str = f"{int(cur_px):,}" if cur_px else "-"
        zone_badge = _in_zone_badge(cur_px, c.entry_low, c.entry_high) if cur_px else "-"
        cand_rows += f"""
        <tr>
          <td><b>{c.name}</b><br><small style="color:#888">{c.symbol}</small></td>
          <td style="color:#fff;font-weight:bold">{cur_str}</td>
          <td>{int(c.entry_low):,}~{int(c.entry_high):,}</td>
          <td style="color:{score_color}">{c.consensus_score:.0%}</td>
          <td>{zone_badge}</td>
          <td class="hide-mobile">{int(c.target_price):,}</td>
          <td class="hide-mobile">{int(c.stop_price):,}</td>
          <td class="hide-mobile">{exp}</td>
        </tr>"""

    if not cand_rows:
        cand_rows = '<tr><td colspan="8" style="text-align:center;color:#666">후보 종목 없음</td></tr>'

    # ── 오늘 청산 내역 ──────────────────────────────────────────
    closed_rows = ""
    for p in closed_today:
        if not p.close_price:
            continue
        pnl_amt = int((p.close_price - p.avg_price) * p.qty)
        pnl_pct = p.pnl_pct(p.close_price)
        pc = _pnl_color(pnl_pct)
        reason = _reason_str(p.close_reason)
        closed_rows += f"""
        <tr>
          <td><b>{p.name}</b><br><small style="color:#888">{p.symbol}</small></td>
          <td>{round(p.avg_price):,}</td>
          <td>{int(p.close_price):,}</td>
          <td style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%</td>
          <td style="color:{pc}">{pnl_amt:+,}원</td>
          <td>{reason}</td>
        </tr>"""

    if not closed_rows:
        closed_rows = '<tr><td colspan="6" style="text-align:center;color:#666">오늘 청산 없음</td></tr>'

    return {
        "updated_at": now,
        "summary": {
            "daily_pnl": daily_pnl, "pnl_color": pnl_color,
            "unrealized_pnl": unrealized_pnl, "unr_color": unr_color,
            "active_count": len(active),
            "closed_count": len(closed_today),
            "cand_count": len(active_cands),
            "account_cash": account_cash, "order_cash": order_cash,
            "eval_amt": eval_amt, "total_eval": total_eval,
            "win_rate": win_rate, "wins": wins, "total_trades": total_trades,
            "total_realized": total_realized,
            "total_realized_color": total_realized_color,
        },
        "positions_html": pos_rows,
        "candidates_html": cand_rows,
        "closed_html": closed_rows,
    }


# 서버측 스냅샷 캐시 — 탭/요청 수와 무관하게 초당 1회만 실제 계산·REST 호출
_SNAP_TTL_SEC = 0.4
_snap_cache: dict = {"at": 0.0, "data": None}
_snap_lock = threading.Lock()


@app.get("/api/snapshot")
def api_snapshot():
    now_ts = _time.monotonic()
    cached = _snap_cache.get("data")
    if cached and (now_ts - _snap_cache["at"]) < _SNAP_TTL_SEC:
        return cached
    with _snap_lock:
        # 락 대기 중 다른 요청이 갱신했으면 그대로 반환
        now_ts = _time.monotonic()
        cached = _snap_cache.get("data")
        if cached and (now_ts - _snap_cache["at"]) < _SNAP_TTL_SEC:
            return cached
        data = _compute_snapshot()
        _snap_cache["data"] = data
        _snap_cache["at"] = now_ts
        return data


@app.post("/api/rescreen")
def api_rescreen():
    now = datetime.now()
    cands = state_store.load_candidates() or []
    active = [c for c in cands if not SwingCandidate.from_dict(c).is_expired(now)]
    ok, reason = rescreen_trigger.should_rescreen(now, len(active), manual=True)
    if not ok:
        return JSONResponse({"ok": False, "reason": reason}, status_code=409)
    result = rescreen_trigger.trigger_rescreen(now, manual=True)
    return result


@app.get("/", response_class=HTMLResponse)
def dashboard():
    # 초기 로드 시 한 번만 스냅샷 계산 (이후 JS가 주기 갱신)
    snap = _compute_snapshot()
    now = snap["updated_at"]
    s = snap["summary"]
    daily_pnl = s["daily_pnl"]; pnl_color = s["pnl_color"]
    unrealized_pnl = s["unrealized_pnl"]; unr_color = s["unr_color"]
    account_cash = s["account_cash"]; order_cash = s["order_cash"]
    eval_amt = s["eval_amt"]; total_eval = s["total_eval"]
    win_rate = s["win_rate"]; wins = s["wins"]; total_trades = s["total_trades"]
    total_realized = s["total_realized"]; total_realized_color = s["total_realized_color"]
    active_len = s["active_count"]; closed_len = s["closed_count"]; cand_len = s["cand_count"]
    pos_rows = snap["positions_html"]
    cand_rows = snap["candidates_html"]
    closed_rows = snap["closed_html"]

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <title>KIS Swing Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0f1117; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 16px; }}
    h1 {{ font-size: 18px; color: #fff; margin-bottom: 4px; }}
    .meta {{ color: #666; font-size: 12px; margin-bottom: 20px; }}

    /* 카드 그리드 — 모바일 2열, 태블릿 3열, 데스크탑 자동 */
    .summary {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 24px; }}
    @media (min-width: 480px) {{ .summary {{ grid-template-columns: repeat(3, 1fr); }} }}
    @media (min-width: 768px) {{ .summary {{ grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }} }}
    .card {{ background: #1a1d27; border-radius: 10px; padding: 12px 14px; }}
    .card-label {{ font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
    .card-value {{ font-size: 18px; font-weight: 700; margin-top: 4px; word-break: keep-all; }}
    @media (min-width: 768px) {{ .card {{ padding: 16px 22px; }} .card-value {{ font-size: 22px; }} }}

    section {{ margin-bottom: 24px; }}
    h2 {{ font-size: 12px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; border-bottom: 1px solid #2a2d3a; padding-bottom: 6px; }}

    /* 테이블 — 모바일에서 가로 스크롤 */
    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 500px; }}
    @media (min-width: 768px) {{ table {{ font-size: 13px; }} }}
    th {{ text-align: left; color: #666; font-weight: 500; padding: 6px 8px; border-bottom: 1px solid #2a2d3a; white-space: nowrap; }}
    td {{ padding: 8px 8px; border-bottom: 1px solid #1e2130; vertical-align: middle; white-space: nowrap; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #1e2130; }}

    /* 모바일에서 덜 중요한 컬럼 숨김 */
    .hide-mobile {{ display: none; }}
    @media (min-width: 640px) {{ .hide-mobile {{ display: table-cell; }} }}

    .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
    .entered {{ background: #1a3a5c; color: #60b8ff; }}
    .trailing {{ background: #1a3a2a; color: #00c9a7; }}
    .watching {{ background: #2a2a1a; color: #f9ca24; }}
    .refresh {{ color: #555; font-size: 11px; margin-top: 16px; text-align: right; }}
    .fab {{ position: fixed; bottom: 20px; right: 20px; width: 48px; height: 48px;
            border-radius: 50%; background: #2a3a5c; color: #60b8ff; border: none;
            font-size: 22px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
            display: flex; align-items: center; justify-content: center; z-index: 100; }}
    .fab:active {{ background: #3a4a6c; }}
  </style>
</head>
<body>
  <h1>KIS Swing Bot
    <button id="rescreen-btn" onclick="triggerRescreen()"
      style="float:right;background:#2a3a5c;color:#60b8ff;border:none;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer">
      🔍 지금 재토론
    </button>
  </h1>
  <div class="meta">마지막 갱신: <span id="updated-at">{now}</span> &nbsp;·&nbsp; <span id="live-status" style="color:#00c9a7">● LIVE</span> (AJAX 1초 갱신) &nbsp;·&nbsp; <span id="rescreen-msg" style="color:#888"></span></div>

  <div class="summary">
    <div class="card">
      <div class="card-label">오늘 실현손익</div>
      <div class="card-value" id="daily-pnl" style="color:{pnl_color}">{daily_pnl:+,}원</div>
    </div>
    <div class="card">
      <div class="card-label">미실현손익</div>
      <div class="card-value" id="unrealized-pnl" style="color:{unr_color}">{unrealized_pnl:+,}원</div>
    </div>
    <div class="card">
      <div class="card-label">보유 포지션</div>
      <div class="card-value" id="active-count">{active_len}종목</div>
    </div>
    <div class="card">
      <div class="card-label">오늘 청산</div>
      <div class="card-value" id="closed-count">{closed_len}건</div>
    </div>
    <div class="card">
      <div class="card-label">감시 후보</div>
      <div class="card-value" id="cand-count">{cand_len}종목</div>
    </div>
    <div class="card">
      <div class="card-label">예수금 총액</div>
      <div class="card-value" id="account-cash">{account_cash:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">주문가능액</div>
      <div class="card-value" id="order-cash">{order_cash:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">유가평가액</div>
      <div class="card-value" id="eval-amt">{eval_amt:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">총평가금액</div>
      <div class="card-value" id="total-eval">{total_eval:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">승률</div>
      <div class="card-value" id="win-rate" style="color:#60b8ff">{win_rate:.1f}% <small style="color:#888">({wins}/{total_trades})</small></div>
    </div>
    <div class="card">
      <div class="card-label">총 실현손익</div>
      <div class="card-value" id="total-realized" style="color:{total_realized_color}">{total_realized:+,}원</div>
    </div>
  </div>

  <section>
    <h2>보유 포지션</h2>
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>종목</th><th>수량</th><th>매수가</th><th>현재가</th><th>수익률 / 손익</th><th>상태</th>
        <th class="hide-mobile">목표가</th><th class="hide-mobile">손절가</th><th class="hide-mobile">트레일링</th>
      </tr></thead>
      <tbody id="positions-tbody">{pos_rows}</tbody>
    </table>
    </div>
  </section>

  <section>
    <h2>감시 후보</h2>
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>종목</th><th>현재가</th><th>진입구간</th><th>신뢰도</th><th>진입여부</th>
        <th class="hide-mobile">목표가</th><th class="hide-mobile">손절가</th><th class="hide-mobile">만료</th>
      </tr></thead>
      <tbody id="candidates-tbody">{cand_rows}</tbody>
    </table>
    </div>
  </section>

  <section>
    <h2>오늘 청산 내역</h2>
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>종목</th><th>매수가</th><th>매도가</th><th>수익률</th><th>손익</th><th>사유</th>
      </tr></thead>
      <tbody id="closed-tbody">{closed_rows}</tbody>
    </table>
    </div>
  </section>

  <button class="fab" onclick="refreshNow()">&#x21bb;</button>
  <div class="refresh">AJAX 1s polling · <span id="fetch-count">0</span> updates</div>

  <script>
    const POLL_MS = 500;
    let fetchCount = 0;
    let failCount = 0;

    function setText(id, text) {{
      const el = document.getElementById(id);
      if (el && el.textContent !== text) el.textContent = text;
    }}
    function setHTML(id, html) {{
      const el = document.getElementById(id);
      if (el && el.innerHTML !== html) el.innerHTML = html;
    }}
    function setColor(id, color) {{
      const el = document.getElementById(id);
      if (el) el.style.color = color;
    }}
    function fmtSigned(n) {{
      return (n >= 0 ? '+' : '') + n.toLocaleString('ko-KR') + '원';
    }}
    function fmtInt(n) {{ return n.toLocaleString('ko-KR') + '원'; }}

    async function refreshNow() {{
      try {{
        const res = await fetch('/api/snapshot', {{cache: 'no-store'}});
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const snap = await res.json();
        const s = snap.summary;
        setText('updated-at', snap.updated_at);
        setText('daily-pnl', fmtSigned(s.daily_pnl));    setColor('daily-pnl', s.pnl_color);
        setText('unrealized-pnl', fmtSigned(s.unrealized_pnl)); setColor('unrealized-pnl', s.unr_color);
        setText('active-count', s.active_count + '종목');
        setText('closed-count', s.closed_count + '건');
        setText('cand-count', s.cand_count + '종목');
        setText('account-cash', fmtInt(s.account_cash));
        setText('order-cash', fmtInt(s.order_cash));
        setText('eval-amt', fmtInt(s.eval_amt));
        setText('total-eval', fmtInt(s.total_eval));
        setHTML('win-rate', s.win_rate.toFixed(1) + '% <small style="color:#888">(' + s.wins + '/' + s.total_trades + ')</small>');
        setText('total-realized', fmtSigned(s.total_realized)); setColor('total-realized', s.total_realized_color);
        setHTML('positions-tbody', snap.positions_html);
        setHTML('candidates-tbody', snap.candidates_html);
        setHTML('closed-tbody', snap.closed_html);
        fetchCount++;
        setText('fetch-count', fetchCount);
        setHTML('live-status', '<span style="color:#00c9a7">● LIVE</span>');
        failCount = 0;
      }} catch (e) {{
        failCount++;
        setHTML('live-status', '<span style="color:#ff6b6b">● OFFLINE ('+failCount+')</span>');
      }}
    }}
    setInterval(refreshNow, POLL_MS);

    async function triggerRescreen() {{
      const btn = document.getElementById('rescreen-btn');
      const msg = document.getElementById('rescreen-msg');
      if (!confirm('지금 재토론을 실행하시겠어요? LLM 비용이 발생합니다.')) return;
      btn.disabled = true; btn.textContent = '⏳ 실행 중...';
      msg.textContent = '';
      try {{
        const res = await fetch('/api/rescreen', {{method: 'POST'}});
        const data = await res.json();
        if (res.ok && data.ok) {{
          msg.textContent = '✓ 재토론 시작 (오늘 ' + data.count_today + '회째, pid=' + data.pid + ')';
          msg.style.color = '#00c9a7';
        }} else {{
          msg.textContent = '✗ 실행 불가: ' + (data.reason || data.error || 'unknown');
          msg.style.color = '#ff6b6b';
        }}
      }} catch (e) {{
        msg.textContent = '✗ 네트워크 오류: ' + e.message;
        msg.style.color = '#ff6b6b';
      }} finally {{
        setTimeout(() => {{ btn.disabled = false; btn.textContent = '🔍 지금 재토론'; }}, 3000);
      }}
    }}
  </script>
</body>
</html>"""
    return html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
