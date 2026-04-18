"""웹 대시보드 서버 (FastAPI + Jinja2 + SSE)."""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime
import threading
import time as _time
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import StreamingResponse
import jinja2
import uvicorn

from src.core.config import load_config
from src.core import state_store
from src.core.models import PositionState, SwingPosition, SwingCandidate
from src.data.kis_client import KisClient
from src.engine import rescreen_trigger

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "src" / "static")), name="static")
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(PROJECT_ROOT / "src" / "templates")),
    autoescape=True,
)
_cfg = load_config()
_kis = KisClient(_cfg.kis)


# 종목별 NXT 지원 여부 캐시 (프로세스 생애 동안 유지 — 변동 없는 정적 속성)
_nxt_support: dict[str, bool] = {}


def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    """종목코드 → 현재가 딕셔너리. WS 실시간 캐시 우선, 미수신 종목만 REST 보강.

    캐시 파일은 monitor가 30초 주기로 저장하므로 신선도 기준을 60초로 잡아
    평시 REST fallback이 과하게 발생하지 않도록 한다.
    REST 호출 시 응답의 nxt_yn 필드로 NXT 지원 여부도 함께 캐시한다.
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
    # NXT 지원 여부: 캐시 미확인 종목만 조회 (NX 마켓코드 시도)
    unchecked = [s for s in symbols if s not in _nxt_support]
    for sym in unchecked:
        _nxt_support[sym] = _kis.is_nxt_supported(sym)
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
    """한국식 손익 색상: 플러스=빨강, 마이너스=파랑."""
    if pnl > 0:
        return "#ff5555"
    if pnl < 0:
        return "#3b82f6"
    return "#aaa"


def _elapsed_str(entry_time: datetime) -> str:
    """진입 후 경과 시간을 읽기 쉬운 문자열로 변환."""
    delta = datetime.now() - entry_time
    total_min = int(delta.total_seconds() / 60)
    if total_min < 60:
        return f"{total_min}분"
    hours = total_min // 60
    if hours < 24:
        return f"{hours}시간 {total_min % 60}분"
    days = hours // 24
    return f"{days}일 {hours % 24}시간"


def _strategy_badge(strategy: str) -> str:
    if strategy == "closing_bet":
        return '<span class="badge cb-badge">CB</span>'
    return '<span class="badge sw-badge">SW</span>'


def _reason_color(reason) -> str:
    if reason is None:
        return "#888"
    m = {
        "TAKE_PROFIT": "#ff5555",       # 익절 = 빨강
        "TRAILING_STOP": "#ff8c42",     # 트레일링 = 주황 (수익성 청산)
        "STOP_LOSS": "#3b82f6",         # 손절 = 파랑
        "CLOSING_BET_MORNING": "#ff5555",  # CB 매도 = 빨강
        "EOD": "#f9ca24",
        "MANUAL": "#888",
        "RECONCILE_KIS_ZERO": "#666",
    }
    return m.get(reason.value, "#888")


def _in_zone_badge(price: float, low: float, high: float) -> str:
    """현재가가 진입 구간 안에 있으면 뱃지 표시."""
    slack = _cfg.screening.entry_zone_slack_pct / 100.0
    if low * (1 - slack) <= price <= high * (1 + slack):
        return '<span style="color:#ff5555;font-weight:bold">● 진입구간</span>'
    if price < low:
        gap_pct = (low - price) / price * 100
        return f'<span style="color:#888">▼ {gap_pct:.1f}% 아래</span>'
    gap_pct = (price - high) / price * 100
    return f'<span style="color:#f9ca24">▲ {gap_pct:.1f}% 위</span>'


def _daily_pnl_chart(positions: list[SwingPosition], comm: float) -> str:
    """최근 14일 일별 실현손익 SVG 바 차트."""
    from collections import defaultdict
    from datetime import timedelta
    from src.core.models import CloseReason

    today_d = datetime.now().date()
    daily: dict = defaultdict(float)
    for p in positions:
        if p.state != PositionState.CLOSED or not p.close_price or not p.close_time:
            continue
        if p.close_reason and p.close_reason == CloseReason.RECONCILE_KIS_ZERO:
            continue
        d = p.close_time.date()
        if (today_d - d).days > 13:
            continue
        gross = (p.close_price - p.avg_price) * p.qty
        fee = (p.avg_price * p.qty + p.close_price * p.qty) * comm
        daily[d] += gross - fee

    dates = [today_d - timedelta(days=i) for i in range(13, -1, -1)]
    values = [int(daily.get(d, 0)) for d in dates]
    max_abs = max((abs(v) for v in values), default=0) or 1

    bar_w, gap, h = 34, 6, 110
    mid_y = h * 0.5
    total_w = len(dates) * (bar_w + gap) + gap
    parts = [f'<line x1="0" y1="{mid_y}" x2="{total_w}" y2="{mid_y}" class="chart-zero" stroke-width="1"/>']

    cumul = 0
    cum_points = []
    for i, (d, v) in enumerate(zip(dates, values)):
        x = i * (bar_w + gap) + gap
        bar_h = abs(v) / max_abs * (mid_y - 14)
        color = "#ff5555" if v >= 0 else "#60b8ff"
        y = (mid_y - bar_h) if v >= 0 else mid_y
        if bar_h > 0.5:
            parts.append(f'<rect x="{x}" y="{y:.0f}" width="{bar_w}" height="{max(bar_h, 1):.0f}" fill="{color}" rx="2" opacity="0.8"/>')
        if i % 3 == 0 or i == 13:
            parts.append(f'<text x="{x + bar_w / 2}" y="{h - 1}" text-anchor="middle" class="chart-label" font-size="8">{d.strftime("%m/%d")}</text>')
        cumul += v
        cum_points.append(f"{x + bar_w / 2},{mid_y - cumul / max_abs * (mid_y - 14):.0f}")

    if any(v != 0 for v in values):
        cum_color = "#c084fc"  # 누적 = 보라 (손익과 구분)
        parts.append(f'<polyline points="{" ".join(cum_points)}" fill="none" stroke="{cum_color}" stroke-width="1.5" opacity="0.6"/>')

    svg = f'<svg viewBox="0 0 {total_w} {h}" style="width:100%;max-height:120px;display:block">{"".join(parts)}</svg>'
    legend = '<div class="stat-sub" style="margin-top:4px;text-align:right">■ 일별 손익 &nbsp; <span style="color:#c084fc">─</span> 누적</div>'
    return svg + legend


def _strategy_stats_html(positions: list[SwingPosition], comm: float) -> str:
    """스윙 vs 종가배팅 전략별 성과 카드 HTML."""
    from src.core.models import CloseReason
    real_closed = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_reason not in (None, CloseReason.RECONCILE_KIS_ZERO)
        and p.close_price
    ]
    cards = ""
    for strat, label, badge_cls in [("swing", "스윙", "sw-badge"), ("closing_bet", "종가배팅", "cb-badge")]:
        group = [p for p in real_closed if (p.strategy or "swing") == strat]
        total = len(group)
        if total == 0:
            cards += f'<div class="card"><div class="card-label"><span class="badge {badge_cls}">{label}</span></div><div class="card-value text-dim">-</div></div>'
            continue
        wins = len([p for p in group if p.close_price > p.avg_price])
        wr = wins / total * 100
        total_pnl = sum(
            int((p.close_price - p.avg_price) * p.qty - (p.avg_price * p.qty + p.close_price * p.qty) * comm)
            for p in group
        )
        avg_ret = sum(p.pnl_pct(p.close_price) for p in group) / total
        pc = _pnl_color(total_pnl)
        cards += (
            f'<div class="card"><div class="card-label"><span class="badge {badge_cls}">{label}</span> {total}건</div>'
            f'<div class="card-value" style="color:{pc}">{total_pnl:+,}원</div>'
            f'<div class="stat-sub">승률 {wr:.0f}% · 평균 {avg_ret:+.1f}%</div></div>'
        )
    return cards


def _bot_status_html() -> str:
    """봇 프로세스 상태 패널 HTML."""
    import os
    services = [
        ("시세감시", "logs/market_monitor.log"),
        ("종목발굴", "logs/morning_screen.log"),
        ("종가배팅", "logs/closing_bet.log"),
    ]
    now_ts = datetime.now()
    rows = ""
    for label, log_path in services:
        full = PROJECT_ROOT / log_path
        if not full.exists():
            rows += f'<div class="bot-row"><span class="bot-dot" style="background:#555"></span> {label} <small class="text-dim">로그 없음</small></div>'
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(full))
        age = (now_ts - mtime).total_seconds()
        time_str = mtime.strftime("%H:%M:%S")
        if age < 120:
            dot, status = "#00c9a7", "활성"
        elif age < 600:
            dot, status = "#f9ca24", "유휴"
        else:
            dot, status = "#ff6b6b", "중단"
        rows += f'<div class="bot-row"><span class="bot-dot" style="background:{dot}"></span> {label} <small class="text-muted">{status} · {time_str}</small></div>'
    return rows


def _recent_events_html() -> str:
    """오늘 주요 이벤트 타임라인 HTML — 종목+태그별 집계."""
    import re
    log_path = PROJECT_ROOT / "logs" / "market_monitor.log"
    if not log_path.exists():
        return '<div class="empty-state">로그 없음</div>'
    today = datetime.now().strftime("%Y-%m-%d")
    # 우선순위 높은 이벤트가 먼저 (순서가 곧 우선순위)
    patterns = [
        (re.compile(r"매수 완료"), "진입", "#ff8c42", 10),              # 진입 = 주황
        (re.compile(r"매도 전량 체결"), "체결", "#c084fc", 10),          # 체결 = 보라
        (re.compile(r"청산 reason=(\w+)\s+price=(\d+)\s+pnl=([^\s]+)"), "청산", "#c084fc", 10),
        (re.compile(r"잔고 불일치.*CLOSED"), "대사청산", "#f9ca24", 8),
        (re.compile(r"재토론.*트리거"), "재토론", "#3b82f6", 8),
        (re.compile(r"트레일링 스탑 활성화"), "트레일", "#ff8c42", 5),
        (re.compile(r"본전 보호 활성"), "본전보호", "#ff8c42", 7),
        (re.compile(r"모멘텀 소실"), "모멘텀소실", "#f9ca24", 7),
        (re.compile(r"매도 주문 실패"), "매도실패", "#ff5555", 3),       # 에러 = 빨강 (손익 아님)
        (re.compile(r"사전손절.*등록 실패"), "손절실패", "#ff5555", 3),
        (re.compile(r"일일 손실 한도"), "리스크한도", "#ff5555", 9),
    ]
    symbol_re = re.compile(r"\[(\d{6})\]")
    # 종목+태그별 집계: {key: (first_time, last_time, count, tag, color, priority, msg)}
    agg: dict = {}
    order: list = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith(today):
                    continue
                for pat, tag, color, prio in patterns:
                    m = pat.search(line)
                    if not m:
                        continue
                    time_part = line[11:19]
                    # 종목코드 추출
                    sym_m = symbol_re.search(line)
                    sym = sym_m.group(1) if sym_m else ""
                    # 청산은 reason별로 구분
                    sub_tag = tag
                    if tag == "청산" and m.lastindex and m.lastindex >= 1:
                        reason = m.group(1)
                        if reason == "TAKE_PROFIT":
                            sub_tag = "익절"
                        elif reason == "STOP_LOSS":
                            sub_tag = "손절"
                        elif reason == "TRAILING_STOP":
                            sub_tag = "트레일"
                        elif reason == "CLOSING_BET_MORNING":
                            sub_tag = "CB매도"
                    key = f"{sym}:{sub_tag}"
                    tail = line[40:]
                    colon_idx = tail.find(": ")
                    msg = (tail[colon_idx + 2:] if colon_idx >= 0 else tail).strip()
                    # 심볼 접두어 제거 후 압축
                    msg_clean = re.sub(r"^\[\d{6}\]\s*", "", msg)[:60]
                    if key in agg:
                        first_t, _, cnt, t, c, p, m0 = agg[key]
                        agg[key] = (first_t, time_part, cnt + 1, t, c, p, m0)
                    else:
                        agg[key] = (time_part, time_part, 1, sub_tag, color, prio, msg_clean)
                        order.append(key)
                    break
    except Exception:
        return '<div class="empty-state">로그 읽기 실패</div>'
    if not agg:
        return '<div class="empty-state">오늘 이벤트 없음</div>'
    # 최신 이벤트가 위에 오도록 — last_time 기준 역순
    sorted_keys = sorted(order, key=lambda k: agg[k][1], reverse=True)
    rows = ""
    for key in sorted_keys[:20]:
        first_t, last_t, cnt, tag, color, prio, msg = agg[key]
        sym = key.split(":")[0]
        time_disp = last_t if cnt == 1 else f"{last_t} ×{cnt}"
        sym_prefix = f"[{sym}] " if sym else ""
        rows += (
            f'<div class="event-row">'
            f'<span class="event-time">{time_disp}</span> '
            f'<span class="event-tag" style="color:{color}">{tag}</span> '
            f'<span class="event-msg">{sym_prefix}{msg}</span>'
            f'</div>'
        )
    return rows


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

    # 수수료율
    comm = _cfg.trading.commission_pct / 100.0

    # 오늘 PnL (청산 기준, 수수료 차감)
    daily_pnl = 0
    for p in closed_today:
        if not p.close_price:
            continue
        gross = (p.close_price - p.avg_price) * p.qty
        fee = (p.avg_price * p.qty + p.close_price * p.qty) * comm
        daily_pnl += int(gross - fee)
    # 미실현 PnL 합산 (수수료 예상 차감)
    unrealized_pnl = 0
    for p in active:
        cur_px = prices.get(p.symbol, p.avg_price)
        gross = (cur_px - p.avg_price) * p.qty
        fee = (p.avg_price * p.qty + cur_px * p.qty) * comm
        unrealized_pnl += int(gross - fee)
    pnl_color = _pnl_color(daily_pnl)
    unr_color = _pnl_color(unrealized_pnl)

    # 재토론 상태
    rescreen_st = state_store.load("rescreen_state") or {}
    rescreen_last = rescreen_st.get("last_run", "")
    rescreen_count = int(rescreen_st.get("count", 0)) if rescreen_st.get("date") == today else 0

    # ── 보유 포지션 테이블 ──────────────────────────────────────────
    pos_rows = ""
    for p in active:
        cur_px = prices.get(p.symbol, 0)
        pnl_pct = p.pnl_pct(cur_px) if cur_px else 0
        pnl_amt = int((cur_px - p.avg_price) * p.qty) if cur_px else 0
        pc = _pnl_color(pnl_pct)
        trail = f"{int(p.trailing_stop_px):,}" if p.trailing_stop_px else "-"
        cur_str = f"{int(cur_px):,}" if cur_px else "-"
        elapsed = _elapsed_str(p.entry_time)
        strat_badge = _strategy_badge(p.strategy or "swing")
        # NXT 배지: 지원 종목이면 항상 표시
        nxt_badge = ' <span class="badge nxt-badge">NXT</span>' if _nxt_support.get(p.symbol) else ""
        # NXT 체결 대기 상태
        nxt_tag = ""
        if p.order_id and p.order_id.startswith("NXT:"):
            nxt_tag = ' <span class="badge nxt-pending">NXT 대기</span>'
        state_badges = f'<span class="badge {p.state.value.lower()}">{p.state.value}</span>{nxt_tag}'
        pos_rows += f"""
        <tr>
          <td>{strat_badge}{nxt_badge} <b>{p.name}</b><br><small class="text-muted">{p.symbol} · {elapsed}</small></td>
          <td>{p.qty}</td>
          <td>{round(p.avg_price):,}</td>
          <td class="cur-price">{cur_str}</td>
          <td style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%<br>
            <small style="color:{pc}">{pnl_amt:+,}원</small></td>
          <td>{state_badges}</td>
          <td class="hide-mobile editable" onclick="editPrice(this,'{p.symbol}','target_price',{int(p.target_price)})">{int(p.target_price):,}</td>
          <td class="hide-mobile editable" onclick="editPrice(this,'{p.symbol}','stop_price',{int(p.stop_price)})">{int(p.stop_price):,}</td>
          <td class="hide-mobile">{trail}</td>
          <td><button class="btn-sell" onclick="sellPosition('{p.symbol}','{p.name}',{p.qty})">매도</button></td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="10"><div class="empty-state">보유 포지션 없음</div></td></tr>'

    # ── 후보 종목 테이블 ──────────────────────────────────────────
    cand_rows = ""
    for c in active_cands:
        exp = c.expires_at.strftime("%m/%d") if c.expires_at else "-"
        score_color = "#00c9a7" if c.consensus_score >= 0.7 else ("#f9ca24" if c.consensus_score >= 0.5 else "#888")
        cur_px = prices.get(c.symbol, 0)
        cur_str = f"{int(cur_px):,}" if cur_px else "-"
        zone_badge = _in_zone_badge(cur_px, c.entry_low, c.entry_high) if cur_px else "-"
        # rationale 팝오버 (HTML 이스케이프)
        rationale_safe = (c.rationale or "").replace('"', '&quot;').replace('<', '&lt;')[:200]
        tags_str = " ".join(f'<small class="text-dim">#{t}</small>' for t in (c.tags or [])[:3])
        nxt_badge = ' <span class="badge nxt-badge">NXT</span>' if _nxt_support.get(c.symbol) else ""
        cand_rows += f"""
        <tr>
          <td class="tooltip-wrap"><b>{c.name}</b>{nxt_badge}<br><small class="text-muted">{c.symbol}</small> {tags_str}
            <div class="tooltip">{rationale_safe}</div></td>
          <td class="cur-price">{cur_str}</td>
          <td>{int(c.entry_low):,}~{int(c.entry_high):,}</td>
          <td style="color:{score_color}">{c.consensus_score:.0%}</td>
          <td>{zone_badge}</td>
          <td class="hide-mobile">{int(c.target_price):,}</td>
          <td class="hide-mobile">{int(c.stop_price):,}</td>
          <td class="hide-mobile">{exp}</td>
          <td><button class="btn-remove" onclick="removeCandidate('{c.symbol}','{c.name}')">✕</button></td>
        </tr>"""

    if not cand_rows:
        rescreen_info = ""
        if rescreen_last:
            try:
                last_dt = datetime.fromisoformat(rescreen_last)
                rescreen_info = f"마지막 토론: {last_dt.strftime('%H:%M')} · 오늘 {rescreen_count}회"
            except Exception:
                pass
        cand_rows = f'<tr><td colspan="9"><div class="empty-state">후보 종목 없음<div class="sub">{rescreen_info or "재토론 대기 중"} — 상단 재토론 버튼으로 즉시 실행 가능</div></div></td></tr>'

    # ── 오늘 청산 내역 ──────────────────────────────────────────
    closed_rows = ""
    for p in closed_today:
        if not p.close_price:
            continue
        gross = (p.close_price - p.avg_price) * p.qty
        fee = (p.avg_price * p.qty + p.close_price * p.qty) * comm
        pnl_amt = int(gross - fee)
        pnl_pct = p.pnl_pct(p.close_price)
        pc = _pnl_color(pnl_pct)
        reason = _reason_str(p.close_reason)
        rc = _reason_color(p.close_reason)
        strat_badge = _strategy_badge(p.strategy or "swing")
        closed_rows += f"""
        <tr>
          <td>{strat_badge} <b>{p.name}</b><br><small class="text-muted">{p.symbol}</small></td>
          <td>{round(p.avg_price):,}</td>
          <td>{int(p.close_price):,}</td>
          <td style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%</td>
          <td style="color:{pc}">{pnl_amt:+,}원</td>
          <td style="color:{rc}">{reason}</td>
        </tr>"""

    if not closed_rows:
        closed_rows = '<tr><td colspan="6"><div class="empty-state">오늘 청산 없음</div></td></tr>'

    # ── Phase 2 섹션 ──────────────────────────────────────────
    chart_html = _daily_pnl_chart(positions, comm)
    strat_html = _strategy_stats_html(positions, comm)
    bot_html = _bot_status_html()
    events_html = _recent_events_html()

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
        "chart_html": chart_html,
        "strategy_html": strat_html,
        "bot_html": bot_html,
        "events_html": events_html,
    }


# 서버측 스냅샷 캐시 — 탭/요청 수와 무관하게 초당 1회만 실제 계산·REST 호출
_SNAP_TTL_SEC = 0.4
_snap_cache: dict = {"at": 0.0, "data": None}
_snap_lock = threading.Lock()


@app.get("/api/snapshot")
def api_snapshot():
    return _get_cached_snapshot()


def _get_cached_snapshot() -> dict:
    """스냅샷 캐시를 읽거나 갱신."""
    now_ts = _time.monotonic()
    cached = _snap_cache.get("data")
    if cached and (now_ts - _snap_cache["at"]) < _SNAP_TTL_SEC:
        return cached
    with _snap_lock:
        now_ts = _time.monotonic()
        cached = _snap_cache.get("data")
        if cached and (now_ts - _snap_cache["at"]) < _SNAP_TTL_SEC:
            return cached
        data = _compute_snapshot()
        _snap_cache["data"] = data
        _snap_cache["at"] = now_ts
        return data


@app.get("/api/stream")
async def api_stream():
    """SSE 엔드포인트 — 1초 간격으로 스냅샷 push."""
    async def event_generator():
        while True:
            try:
                data = _get_cached_snapshot()
                payload = json.dumps(data, ensure_ascii=False, default=str)
                yield f"data: {payload}\n\n"
            except Exception:
                yield "data: {}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


@app.post("/api/sell")
def api_sell(body: dict):
    """수동 시장가 매도."""
    from src.core.models import CloseReason
    symbol = body.get("symbol", "")
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 필요"}, status_code=400)
    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    target = next((p for p in positions if p.symbol == symbol and p.state != PositionState.CLOSED), None)
    if not target:
        return JSONResponse({"ok": False, "error": f"{symbol} 보유 포지션 없음"}, status_code=404)
    try:
        _kis.sell_market(target.symbol, target.qty)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        px_data = _kis.get_price(symbol)
        close_px = float(px_data.get("stck_prpr", 0) or 0) or target.avg_price
    except Exception:
        close_px = target.avg_price
    target.state = PositionState.CLOSED
    target.close_reason = CloseReason.MANUAL
    target.close_price = close_px
    target.close_time = datetime.now()
    state_store.save_positions([p.to_dict() for p in positions])
    pnl = int((close_px - target.avg_price) * target.qty)
    return {"ok": True, "symbol": symbol, "close_price": close_px, "pnl": pnl}


@app.post("/api/update-position")
def api_update_position(body: dict):
    """보유 포지션의 목표가/손절가 수정."""
    symbol = body.get("symbol", "")
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 필요"}, status_code=400)
    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    target = next((p for p in positions if p.symbol == symbol and p.state != PositionState.CLOSED), None)
    if not target:
        return JSONResponse({"ok": False, "error": f"{symbol} 보유 포지션 없음"}, status_code=404)
    if "target_price" in body:
        val = float(body["target_price"])
        if val <= 0:
            return JSONResponse({"ok": False, "error": "목표가는 0보다 커야 함"}, status_code=400)
        target.target_price = val
    if "stop_price" in body:
        val = float(body["stop_price"])
        if val <= 0:
            return JSONResponse({"ok": False, "error": "손절가는 0보다 커야 함"}, status_code=400)
        target.stop_price = val
    state_store.save_positions([p.to_dict() for p in positions])
    return {"ok": True, "symbol": symbol, "target_price": target.target_price, "stop_price": target.stop_price}


@app.post("/api/remove-candidate")
def api_remove_candidate(body: dict):
    """감시 후보 제거."""
    symbol = body.get("symbol", "")
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 필요"}, status_code=400)
    candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
    before = len(candidates)
    candidates = [c for c in candidates if c.symbol != symbol]
    if len(candidates) == before:
        return JSONResponse({"ok": False, "error": f"{symbol} 후보 없음"}, status_code=404)
    state_store.save_candidates([c.to_dict() for c in candidates])
    return {"ok": True, "symbol": symbol, "remaining": len(candidates)}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    snap = _compute_snapshot()
    tmpl = _jinja_env.get_template("dashboard.html")
    html = tmpl.render(
        updated_at=snap["updated_at"],
        s=snap["summary"],
        positions_html=snap["positions_html"],
        candidates_html=snap["candidates_html"],
        closed_html=snap["closed_html"],
        chart_html=snap["chart_html"],
        strategy_html=snap["strategy_html"],
        bot_html=snap["bot_html"],
        events_html=snap["events_html"],
    )
    return HTMLResponse(html)




if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
