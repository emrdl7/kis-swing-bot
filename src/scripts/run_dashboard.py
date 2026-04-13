"""웹 대시보드 서버 (FastAPI)."""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from src.core.config import load_config
from src.core import state_store
from src.core.models import PositionState, SwingPosition, SwingCandidate
from src.data.kis_client import KisClient

app = FastAPI()
_cfg = load_config()
_kis = KisClient(_cfg.kis)


def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    """종목코드 → 현재가 딕셔너리. WS 실시간 캐시 우선, 미수신 종목만 REST 보강."""
    from datetime import datetime, timedelta
    result: dict[str, float] = {}
    cache = state_store.load_realtime_prices() or {}
    cutoff = datetime.now() - timedelta(seconds=10)
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
        # WS 캐시에 없거나 오래됨 → REST 조회 (대시보드 첫 로드 대비)
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


@app.get("/", response_class=HTMLResponse)
def dashboard():
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

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="3">
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
  <h1>KIS Swing Bot</h1>
  <div class="meta">마지막 갱신: {now} &nbsp;·&nbsp; 3초 자동 새로고침 (실시간 시세 WS)</div>

  <div class="summary">
    <div class="card">
      <div class="card-label">오늘 실현손익</div>
      <div class="card-value" style="color:{pnl_color}">{daily_pnl:+,}원</div>
    </div>
    <div class="card">
      <div class="card-label">미실현손익</div>
      <div class="card-value" style="color:{unr_color}">{unrealized_pnl:+,}원</div>
    </div>
    <div class="card">
      <div class="card-label">보유 포지션</div>
      <div class="card-value">{len(active)}종목</div>
    </div>
    <div class="card">
      <div class="card-label">오늘 청산</div>
      <div class="card-value">{len(closed_today)}건</div>
    </div>
    <div class="card">
      <div class="card-label">감시 후보</div>
      <div class="card-value">{len(active_cands)}종목</div>
    </div>
    <div class="card">
      <div class="card-label">예수금 총액</div>
      <div class="card-value">{account_cash:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">주문가능액</div>
      <div class="card-value">{order_cash:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">유가평가액</div>
      <div class="card-value">{eval_amt:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">총평가금액</div>
      <div class="card-value">{total_eval:,}원</div>
    </div>
    <div class="card">
      <div class="card-label">승률</div>
      <div class="card-value" style="color:#60b8ff">{win_rate:.1f}% <small style="color:#888">({wins}/{total_trades})</small></div>
    </div>
    <div class="card">
      <div class="card-label">총 실현손익</div>
      <div class="card-value" style="color:{total_realized_color}">{total_realized:+,}원</div>
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
      <tbody>{pos_rows}</tbody>
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
      <tbody>{cand_rows}</tbody>
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
      <tbody>{closed_rows}</tbody>
    </table>
    </div>
  </section>

  <button class="fab" onclick="location.reload()">&#x21bb;</button>
  <div class="refresh">auto-refresh 3s</div>
</body>
</html>"""
    return html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
