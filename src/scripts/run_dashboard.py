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
    """종목코드 → 현재가 딕셔너리 반환. 실패 또는 0이면 제외."""
    result = {}
    for sym in symbols:
        try:
            data = _kis.get_price(sym)
            px = float(data.get("stck_prpr", 0) or 0)
            if px > 0:
                result[sym] = px
        except Exception:
            pass
    return result


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
    closed_today = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_time and p.close_time.strftime("%Y-%m-%d") == today
    ]
    active_cands = [c for c in candidates if not c.is_expired()]

    # 현재가 일괄 조회
    symbols = list({p.symbol for p in active} | {c.symbol for c in active_cands})
    prices = _fetch_prices(symbols)

    # 계좌 잔고
    try:
        bal = _kis.get_balance()
        o2 = (bal.get("output2") or [{}])[0]
        account_cash = int(o2.get("dnca_tot_amt", 0))      # 예수금 총액
        order_cash = int(o2.get("ord_psbl_cash", 0))        # 주문가능액
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
          <td>{int(p.avg_price):,}</td>
          <td style="color:#fff;font-weight:bold">{cur_str}</td>
          <td>{int(p.target_price):,}</td>
          <td>{int(p.stop_price):,}</td>
          <td>{trail}</td>
          <td style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%<br>
            <small style="color:{pc}">{pnl_amt:+,}원</small></td>
          <td><span class="badge {p.state.value.lower()}">{p.state.value}</span></td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="8" style="text-align:center;color:#666">보유 포지션 없음</td></tr>'

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
          <td>{int(c.entry_low):,} ~ {int(c.entry_high):,}</td>
          <td>{int(c.target_price):,}</td>
          <td>{int(c.stop_price):,}</td>
          <td>{zone_badge}</td>
          <td style="color:{score_color}">{c.consensus_score:.0%}</td>
          <td>{exp}</td>
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
        reason = p.close_reason.value if p.close_reason else "-"
        closed_rows += f"""
        <tr>
          <td><b>{p.name}</b><br><small style="color:#888">{p.symbol}</small></td>
          <td>{int(p.avg_price):,}</td>
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
  <meta http-equiv="refresh" content="30">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KIS Swing Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0f1117; color: #e0e0e0; font-family: -apple-system, sans-serif; padding: 20px; }}
    h1 {{ font-size: 18px; color: #fff; margin-bottom: 4px; }}
    .meta {{ color: #666; font-size: 12px; margin-bottom: 24px; }}
    .summary {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
    .card {{ background: #1a1d27; border-radius: 10px; padding: 16px 22px; min-width: 140px; }}
    .card-label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
    .card-value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    section {{ margin-bottom: 28px; }}
    h2 {{ font-size: 13px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; border-bottom: 1px solid #2a2d3a; padding-bottom: 6px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ text-align: left; color: #666; font-weight: 500; padding: 6px 10px; border-bottom: 1px solid #2a2d3a; }}
    td {{ padding: 8px 10px; border-bottom: 1px solid #1e2130; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #1e2130; }}
    .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
    .entered {{ background: #1a3a5c; color: #60b8ff; }}
    .trailing {{ background: #1a3a2a; color: #00c9a7; }}
    .watching {{ background: #2a2a1a; color: #f9ca24; }}
    .refresh {{ color: #555; font-size: 11px; margin-top: 20px; text-align: right; }}
  </style>
</head>
<body>
  <h1>KIS Swing Bot</h1>
  <div class="meta">마지막 갱신: {now} &nbsp;·&nbsp; 30초마다 자동 새로고침</div>

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
  </div>

  <section>
    <h2>보유 포지션</h2>
    <table>
      <thead><tr>
        <th>종목</th><th>매수가</th><th>현재가</th><th>목표가</th><th>손절가</th><th>트레일링</th><th>수익률 / 손익</th><th>상태</th>
      </tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>감시 후보</h2>
    <table>
      <thead><tr>
        <th>종목</th><th>현재가</th><th>진입 구간</th><th>목표가</th><th>손절가</th><th>진입여부</th><th>신뢰도</th><th>만료</th>
      </tr></thead>
      <tbody>{cand_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>오늘 청산 내역</h2>
    <table>
      <thead><tr>
        <th>종목</th><th>매수가</th><th>매도가</th><th>수익률</th><th>손익</th><th>사유</th>
      </tr></thead>
      <tbody>{closed_rows}</tbody>
    </table>
  </section>

  <div class="refresh">auto-refresh 30s</div>
</body>
</html>"""
    return html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
