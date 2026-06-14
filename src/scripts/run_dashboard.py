"""웹 대시보드 서버 (FastAPI + Jinja2 + SSE)."""
from __future__ import annotations
import asyncio
import json
import re
import sys
import html
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta, timezone
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
from src.data.market_overview import get_market_overview
from src.data.technical import compute_indicators
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
            from src.core.clock import is_pre_market, is_nxt_after_hours
            data = _kis.get_nxt_price(sym) if (is_pre_market() or is_nxt_after_hours()) else _kis.get_price(sym)
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
}


def _reason_str(reason) -> str:
    if reason is None:
        return "-"
    return _REASON_KO.get(reason.value, reason.value)


def _pnl_color(pnl: float) -> str:
    """한국식 손익 색상: 플러스=빨강, 마이너스=파랑."""
    if pnl > 0:
        return "var(--red)"
    if pnl < 0:
        return "var(--blue)"
    return "var(--muted)"


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
    if strategy == "manual":
        return '<span class="badge manual-badge">수동</span>'
    return '<span class="badge sw-badge">SW</span>'


def _reason_color(reason) -> str:
    if reason is None:
        return "var(--muted)"
    m = {
        "TAKE_PROFIT": "var(--red)",       # 익절 = 빨강
        "TRAILING_STOP": "var(--amber)",    # 트레일링 = 주황 (수익성 청산)
        "STOP_LOSS": "var(--blue)",         # 손절 = 파랑
        "EOD": "var(--amber)",
        "MANUAL": "var(--muted)",
        "RECONCILE_KIS_ZERO": "var(--dim)",
    }
    return m.get(reason.value, "var(--muted)")


def _in_zone_badge(price: float, low: float, high: float) -> str:
    """현재가가 진입 구간 안에 있으면 뱃지 표시."""
    slack = _cfg.screening.entry_zone_slack_pct / 100.0
    if low * (1 - slack) <= price <= high * (1 + slack):
        return '<span style="color:var(--red);font-weight:bold">● 진입구간</span>'
    if price < low:
        gap_pct = (low - price) / price * 100
        return f'<span style="color:var(--muted)">▼ {gap_pct:.1f}% 아래</span>'
    gap_pct = (price - high) / price * 100
    return f'<span style="color:var(--amber)">▲ {gap_pct:.1f}% 위</span>'


def _daily_pnl_chart(positions: list[SwingPosition], comm: float, today_override: int | None = None) -> str:
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
    if today_override is not None:
        daily[today_d] = today_override

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
        color = "var(--red)" if v >= 0 else "var(--blue)"
        y = (mid_y - bar_h) if v >= 0 else mid_y
        if bar_h > 0.5:
            parts.append(f'<rect x="{x}" y="{y:.0f}" width="{bar_w}" height="{max(bar_h, 1):.0f}" fill="{color}" rx="2" opacity="0.85"/>')
        if i % 3 == 0 or i == 13:
            parts.append(f'<text x="{x + bar_w / 2}" y="{h - 1}" text-anchor="middle" class="chart-label" font-size="8" fill="var(--muted)">{d.strftime("%m/%d")}</text>')
        cumul += v
        cum_points.append(f"{x + bar_w / 2},{mid_y - cumul / max_abs * (mid_y - 14):.0f}")

    if any(v != 0 for v in values):
        cum_color = "var(--accent)"  # 누적 = 보라/인디고
        parts.append(f'<polyline points="{" ".join(cum_points)}" fill="none" stroke="{cum_color}" stroke-width="1.5" opacity="0.8"/>')

    svg = f'<svg viewBox="0 0 {total_w} {h}" style="width:100%;max-height:120px;display:block;overflow:visible">{"".join(parts)}</svg>'
    legend = '<div class="stat-sub" style="margin-top:6px;text-align:right;font-size:10px;color:var(--muted)">■ 일별 손익 &nbsp; <span style="color:var(--accent);font-weight:bold">─</span> 누적</div>'
    return svg + legend


_EXEC_PNL_CACHE: dict[str, tuple[int, bool, float]] = {}
_TODAY_EXEC_CACHE: dict[str, tuple[list[dict], float]] = {}
_PERIOD_PROFIT_CACHE: dict[str, tuple[dict, float]] = {}
_PERIOD_TRADE_PROFIT_CACHE: dict[str, tuple[dict, float]] = {}
_BALANCE_CACHE: dict[str, float | dict] = {"at": 0.0, "data": {}}


def _cached_balance(ttl_sec: float = 3.0) -> dict:
    """KIS 잔고 프록시 캐시. 화면 SSE가 계좌 API를 초당 호출하지 않게 한다."""
    now_ts = _time.monotonic()
    data = _BALANCE_CACHE.get("data")
    if isinstance(data, dict) and data and (now_ts - float(_BALANCE_CACHE.get("at") or 0.0)) < ttl_sec:
        return data
    data = _kis.get_balance()
    _BALANCE_CACHE["data"] = data
    _BALANCE_CACHE["at"] = now_ts
    return data


def _basis_position_for_symbol(symbol: str, positions: list[SwingPosition]) -> SwingPosition | None:
    """오늘 체결 PnL의 원가 기준으로 쓸 최신 포지션을 고른다."""
    same_symbol = [p for p in positions if p.symbol == symbol]
    if not same_symbol:
        return None
    active = [p for p in same_symbol if p.state != PositionState.CLOSED]
    if active:
        return max(active, key=lambda p: p.entry_time)
    return max(same_symbol, key=lambda p: p.close_time or p.entry_time)


def _execution_sort_key(e: dict) -> tuple[str, str, str]:
    return (
        str(e.get("ord_dt") or e.get("ord_date") or ""),
        str(e.get("ord_tmd") or e.get("ord_tmd_m") or ""),
        str(e.get("odno") or ""),
    )


def _execution_qty_amt_px(e: dict) -> tuple[int, int, float]:
    qty = _safe_int(e.get("tot_ccld_qty") or e.get("ord_qty"))
    amt = _safe_int(e.get("tot_ccld_amt"))
    avg_prvs = _safe_float(e.get("avg_prvs"))
    px = (amt / qty) if qty > 0 and amt > 0 else avg_prvs
    return qty, amt, px


def _today_all_executions(ttl_sec: float = 20.0) -> list[dict]:
    today = datetime.now().strftime("%Y%m%d")
    now_ts = _time.monotonic()
    cached = _TODAY_EXEC_CACHE.get(today)
    if cached and (now_ts - cached[1]) < ttl_sec:
        return cached[0]
    rows = _kis.get_today_executions("")
    _TODAY_EXEC_CACHE.clear()
    _TODAY_EXEC_CACHE[today] = (rows, now_ts)
    return rows


def _group_executions_by_symbol(execs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for e in execs:
        symbol = str(e.get("pdno") or "").strip()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(e)
    return grouped


def _today_kis_period_profit(ttl_sec: float = 10.0) -> tuple[int, set[str], bool, dict]:
    """KIS 기간별손익 원장을 읽어 오늘 계좌 손익을 반환한다."""
    today = datetime.now().strftime("%Y%m%d")
    now_ts = _time.monotonic()
    cached = _PERIOD_PROFIT_CACHE.get(today)
    if cached and (now_ts - cached[1]) < ttl_sec:
        data = cached[0]
    else:
        data = _kis.get_period_profit(today, today)
        _PERIOD_PROFIT_CACHE.clear()
        _PERIOD_PROFIT_CACHE[today] = (data, now_ts)

    output2 = data.get("output2") if isinstance(data.get("output2"), dict) else {}
    rows = data.get("output1") if isinstance(data.get("output1"), list) else []
    total = _safe_int(output2.get("tot_rlzt_pfls"))
    if total == 0 and rows:
        total = sum(_safe_int(r.get("rlzt_pfls")) for r in rows)
    has_data = bool(output2 or rows)
    symbols = {
        str(r.get("pdno") or "").strip()
        for r in rows
        if str(r.get("pdno") or "").strip() and _safe_int(r.get("sll_qty1") or r.get("sll_qty")) > 0
    }
    return total, symbols, has_data, output2


def _today_kis_trade_profit_rows(ttl_sec: float = 10.0) -> list[dict]:
    today = datetime.now().strftime("%Y%m%d")
    now_ts = _time.monotonic()
    cached = _PERIOD_TRADE_PROFIT_CACHE.get(today)
    if cached and (now_ts - cached[1]) < ttl_sec:
        data = cached[0]
    else:
        data = _kis.get_period_trade_profit(today, today)
        _PERIOD_TRADE_PROFIT_CACHE.clear()
        _PERIOD_TRADE_PROFIT_CACHE[today] = (data, now_ts)
    rows = data.get("output1") if isinstance(data.get("output1"), list) else []
    return rows


def _reconstruct_start_lot_from_today(
    pos: SwingPosition,
    end_qty: int,
    end_avg: float,
    execs: list[dict],
) -> tuple[float, float]:
    """현재 잔고와 오늘 체결을 역산해 장 시작 시점의 수량/원가를 복원한다.

    오래 보유한 포지션도 오늘 매도 PnL의 원가는 전체 보유기간의 평균단가여야 한다.
    매도는 평균단가를 바꾸지 않으므로 역순 복원 시 현재 평균단가를 유지해 매도 전 수량을 되살릴 수 있다.
    """
    if end_qty <= 0 and pos.state == PositionState.CLOSED and pos.avg_price > 0:
        bought_qty = 0
        sold_qty = 0
        for e in execs:
            ex_qty, _, _ = _execution_qty_amt_px(e)
            if e.get("sll_buy_dvsn_cd") == "02":
                bought_qty += ex_qty
            elif e.get("sll_buy_dvsn_cd") == "01":
                sold_qty += ex_qty
        start_qty = float(max(sold_qty - bought_qty, 0))
        return start_qty, start_qty * pos.avg_price

    qty = float(max(end_qty, 0))
    cost = qty * max(end_avg, 0.0)
    avg_hint = max(end_avg, pos.avg_price, 0.0)

    for e in sorted(execs, key=_execution_sort_key, reverse=True):
        side = e.get("sll_buy_dvsn_cd")
        ex_qty, ex_amt, ex_px = _execution_qty_amt_px(e)
        if ex_qty <= 0:
            continue
        if side == "02":  # 오늘 매수분 제거
            cost -= ex_amt if ex_amt > 0 else ex_px * ex_qty
            qty -= ex_qty
        elif side == "01":  # 오늘 매도 전 보유분 복원
            avg = (cost / qty) if qty > 0 and cost > 0 else avg_hint
            cost += avg * ex_qty
            qty += ex_qty
            avg_hint = avg
        qty = max(qty, 0.0)
        cost = max(cost, 0.0)

    return qty, cost


def _execution_realized_for_symbol(
    pos: SwingPosition,
    comm: float,
    kis_holding: dict | None = None,
    execs: list[dict] | None = None,
    ttl_sec: float = 20.0,
) -> tuple[int, bool]:
    """KIS 오늘 매도 체결 기준 실현손익.

    반환값의 bool은 오늘 매도 체결을 실제로 확인했는지 여부다. 확인된 종목은 positions.json의
    closed_today 손익을 더하지 않아 중복 계산을 막는다.
    """
    today = datetime.now().strftime("%Y%m%d")
    end_qty = _safe_int((kis_holding or {}).get("hldg_qty"))
    end_avg = _safe_float((kis_holding or {}).get("pchs_avg_pric"))
    if end_qty <= 0 and pos.state != PositionState.CLOSED:
        end_qty = pos.qty
    if end_avg <= 0:
        end_avg = pos.avg_price

    key = f"{today}:exec-realized:v4:{pos.symbol}:{end_qty}:{round(end_avg, 4)}:{round(pos.avg_price, 4)}"
    now_ts = _time.monotonic()
    provided_execs = execs is not None
    cached = _EXEC_PNL_CACHE.get(key)
    if not provided_execs and cached and (now_ts - cached[2]) < ttl_sec:
        return cached[0], cached[1]

    realized = 0
    has_sell_execution = False
    if not provided_execs:
        execs = _kis.get_today_executions(pos.symbol)

    qty, cost = _reconstruct_start_lot_from_today(pos, end_qty, end_avg, execs)
    avg_hint = (cost / qty) if qty > 0 and cost > 0 else max(pos.avg_price, end_avg, 0.0)

    for e in sorted(execs, key=_execution_sort_key):
        side = e.get("sll_buy_dvsn_cd")
        ex_qty, ex_amt, ex_px = _execution_qty_amt_px(e)
        if ex_qty <= 0 or ex_px <= 0:
            continue
        if side == "02":
            cost += ex_amt if ex_amt > 0 else ex_px * ex_qty
            qty += ex_qty
            if qty > 0:
                avg_hint = cost / qty
        elif side == "01":
            has_sell_execution = True
            basis = (cost / qty) if qty > 0 and cost > 0 else avg_hint
            gross = (ex_px - basis) * ex_qty
            fee = (basis * ex_qty + ex_px * ex_qty) * comm
            realized += int(gross - fee)
            cost -= basis * min(ex_qty, qty)
            qty = max(qty - ex_qty, 0.0)
            if qty <= 0:
                cost = 0.0
            avg_hint = basis

    if not provided_execs:
        _EXEC_PNL_CACHE[key] = (realized, has_sell_execution, now_ts)
    return realized, has_sell_execution


def _intraday_only_realized_pnl(execs: list[dict], comm: float) -> tuple[int, bool]:
    """positions.json 기준 원가가 없는 종목의 당일 왕복매매 손익.

    전일 이전 보유분을 오늘 매도한 경우는 원가를 알 수 없으므로 계산하지 않는다.
    같은 날 매수한 수량 안에서 매도된 부분만 정확히 반영한다.
    """
    qty = 0.0
    cost = 0.0
    realized = 0
    accounted_sell = False
    for e in sorted(execs, key=_execution_sort_key):
        side = e.get("sll_buy_dvsn_cd")
        ex_qty, ex_amt, ex_px = _execution_qty_amt_px(e)
        if ex_qty <= 0 or ex_px <= 0:
            continue
        if side == "02":
            cost += ex_amt if ex_amt > 0 else ex_px * ex_qty
            qty += ex_qty
        elif side == "01" and qty > 0:
            sell_qty = min(float(ex_qty), qty)
            basis = cost / qty if qty > 0 and cost > 0 else ex_px
            gross = (ex_px - basis) * sell_qty
            fee = (basis * sell_qty + ex_px * sell_qty) * comm
            realized += int(gross - fee)
            cost -= basis * sell_qty
            qty -= sell_qty
            if qty <= 0:
                qty = 0.0
                cost = 0.0
            accounted_sell = True
    return realized, accounted_sell


def _today_execution_realized_pnl(
    positions: list[SwingPosition],
    active_positions: list[SwingPosition],
    closed_today: list[SwingPosition],
    kis_holdings_raw: list[dict],
    comm: float,
) -> tuple[int, set[str]]:
    """오늘 매도 체결이 있는 종목의 실현손익과 체결 확인 종목 집합.

    KIS 오늘 전체 체결을 기준으로 계산해 수동 매매도 포함한다.
    """
    kis_holdings = {h.get("pdno"): h for h in kis_holdings_raw if h.get("pdno")}
    execs_by_symbol = _group_executions_by_symbol(_today_all_executions())
    symbols = (
        {p.symbol for p in active_positions}
        | {p.symbol for p in closed_today}
        | {s for s, rows in execs_by_symbol.items() if any(e.get("sll_buy_dvsn_cd") == "01" for e in rows)}
    )
    total = 0
    execution_symbols: set[str] = set()
    for symbol in sorted(symbols):
        symbol_execs = execs_by_symbol.get(symbol, [])
        if not any(e.get("sll_buy_dvsn_cd") == "01" for e in symbol_execs):
            continue
        basis_pos = _basis_position_for_symbol(symbol, positions)
        if basis_pos and basis_pos.avg_price > 0:
            realized, has_sell_execution = _execution_realized_for_symbol(
                basis_pos,
                comm,
                kis_holdings.get(symbol),
                execs=symbol_execs,
            )
        else:
            realized, has_sell_execution = _intraday_only_realized_pnl(symbol_execs, comm)
        if has_sell_execution:
            total += realized
            execution_symbols.add(symbol)
    return total, execution_symbols


def _sync_positions_from_kis_holdings(
    positions: list[SwingPosition],
    kis_holdings_raw: list[dict],
    now_dt: datetime,
) -> bool:
    """KIS 보유잔고를 positions.json에 반영해 수동 보유분도 PnL에 포함한다."""
    changed = False
    active_by_symbol = {p.symbol: p for p in positions if p.state != PositionState.CLOSED}
    for h in kis_holdings_raw:
        symbol = str(h.get("pdno") or "").strip()
        qty = _safe_int(h.get("hldg_qty"))
        if not symbol or qty <= 0:
            continue
        avg = _safe_float(h.get("pchs_avg_pric"))
        name = str(h.get("prdt_name") or h.get("hts_kor_isnm") or symbol)
        pos = active_by_symbol.get(symbol)
        if pos:
            if pos.qty != qty:
                pos.qty = qty
                changed = True
            if avg > 0 and abs(pos.avg_price - avg) > 1:
                pos.avg_price = avg
                changed = True
            if name and pos.name != name:
                pos.name = name
                changed = True
            continue

        manual_pos = SwingPosition(
            symbol=symbol,
            name=name,
            qty=qty,
            avg_price=avg,
            entry_time=now_dt,
            target_price=0.0,
            stop_price=0.0,
            state=PositionState.ENTERED,
            peak_price=avg,
            strategy="manual",
            rationale="수동 보유 (KIS 잔고 기반 대시보드 자동 편입)",
        )
        positions.append(manual_pos)
        active_by_symbol[symbol] = manual_pos
        changed = True
    return changed


def _fmt_money(value: int | float, signed: bool = False) -> str:
    value_i = int(value or 0)
    return f"{value_i:+,}원" if signed else f"{value_i:,}원"


def _account_ledger_html(
    pnl_source: str,
    daily_pnl: int,
    unrealized_pnl: int,
    period_summary: dict,
    trade_rows: list[dict],
    kis_holdings_raw: list[dict],
) -> str:
    if pnl_source == "kis_period_profit":
        source_label = "KIS 기간별손익 원장"
        source_class = "ok"
        source_note = "증권사 원장값을 그대로 사용합니다."
    elif pnl_source == "last_good_kis_period_profit":
        source_label = "마지막 정상 KIS 원장"
        source_class = "warn"
        source_note = "KIS 원장 조회 실패로 오늘 마지막 정상 원장값을 유지합니다."
    else:
        source_label = "체결 역산 fallback"
        source_class = "warn"
        source_note = "원장 조회 실패 시 자체 계산값입니다."
    fee = _safe_int(period_summary.get("tot_fee"))
    tax = _safe_int(period_summary.get("tot_tltx") or period_summary.get("sll_tltx_smtl"))
    sell_qty = _safe_int(period_summary.get("sll_qty_smtl"))
    hold_count = sum(1 for h in kis_holdings_raw if _safe_int(h.get("hldg_qty")) > 0)

    row_html = ""
    sell_rows = [r for r in trade_rows if _safe_int(r.get("sll_qty")) > 0]
    for r in sell_rows[:8]:
        symbol = html.escape(str(r.get("pdno") or ""))
        name = html.escape(str(r.get("prdt_name") or symbol))
        pnl = _safe_int(r.get("rlzt_pfls"))
        pc = _pnl_color(pnl)
        row_html += f"""
        <tr>
          <td><b>{name}</b><br><small class="text-muted">{symbol}</small></td>
          <td class="num">{_safe_int(r.get("sll_qty")):,}주</td>
          <td class="num">{_safe_int(r.get("pchs_unpr")):,} → {_safe_int(r.get("sll_pric")):,}</td>
          <td class="num" style="color:{pc};font-weight:800">{_fmt_money(pnl, signed=True)}</td>
        </tr>"""
    if not row_html:
        row_html = '<tr><td colspan="4"><div class="empty-state">오늘 KIS 원장 매도 없음</div></td></tr>'

    sold_names = ", ".join(str(r.get("prdt_name") or r.get("pdno") or "") for r in sell_rows[:3]) or "매도 없음"
    return f"""
    <div class="ledger-proof">
      <div class="proof-main">
        <i class="source-pill {source_class}">{source_label}</i>
        <b>{html.escape(source_note)}</b>
        <span>원장과 체결 재계산이 다르면 원장값을 우선합니다.</span>
      </div>
      <div class="proof-meta">
        <div><span>원장 매도</span><b>{len(sell_rows)}건 · {sell_qty:,}주</b></div>
        <div><span>비용 반영</span><b>{_fmt_money(fee + tax)}</b><small>수수료 {fee:,} · 세금 {tax:,}</small></div>
        <div><span>보유평가</span><b>{hold_count}종목</b><small>KIS 평가손익 합계 사용</small></div>
      </div>
    </div>
    <div class="ledger-note">오늘 원장 매도: {html.escape(sold_names)}</div>
    <div class="table-wrap ledger-table-wrap">
      <table class="ledger-table">
        <thead><tr><th>종목</th><th>매도수량</th><th>원가 → 매도가</th><th>원장손익</th></tr></thead>
        <tbody>{row_html}</tbody>
      </table>
    </div>"""


def _strategy_stats_html(positions: list[SwingPosition], comm: float) -> str:
    """스윙 전략 누적 성과 카드 HTML."""
    from src.core.models import CloseReason
    real_closed = [
        p for p in positions
        if p.state == PositionState.CLOSED
        and p.close_reason not in (None, CloseReason.RECONCILE_KIS_ZERO)
        and p.close_price
        and (p.strategy or "swing") == "swing"
    ]
    total = len(real_closed)
    if total == 0:
        return '<div class="card"><div class="card-label">스윙 누적</div><div class="card-value text-dim">-</div></div>'
    wins = len([p for p in real_closed if p.close_price > p.avg_price])
    wr = wins / total * 100
    total_pnl = sum(
        int((p.close_price - p.avg_price) * p.qty - (p.avg_price * p.qty + p.close_price * p.qty) * comm)
        for p in real_closed
    )
    avg_ret = sum(p.pnl_pct(p.close_price) for p in real_closed) / total
    pc = _pnl_color(total_pnl)
    return (
        f'<div class="card"><div class="card-label">스윙 누적 ({total}건)</div>'
        f'<div class="card-value" style="color:{pc}">{total_pnl:+,}원</div>'
        f'<div class="stat-sub">승률 {wr:.0f}% · 평균 {avg_ret:+.1f}%</div></div>'
    )


def _candidate_state(cur_px: float, c: SwingCandidate) -> tuple[str, str, str]:
    """후보의 현재 진입 상태: label, css class, short note."""
    if not cur_px:
        return "가격대기", "state-wait", "현재가 확인 전"
    if c.entry_low <= cur_px <= c.entry_high:
        return "진입권", "state-ready", "진입 구간 안"
    if cur_px < c.entry_low:
        gap = (c.entry_low - cur_px) / c.entry_low * 100 if c.entry_low else 0
        cls = "state-near" if gap <= 2.0 else "state-wait"
        return "하단대기", cls, f"하단까지 {gap:.1f}%"
    gap = (cur_px - c.entry_high) / c.entry_high * 100 if c.entry_high else 0
    cls = "state-hot" if gap <= 3.0 else "state-blocked"
    return "상단초과", cls, f"상단 대비 +{gap:.1f}%"


def _candidate_rr(cur_px: float, c: SwingCandidate) -> tuple[float, float, float]:
    """현재가 기준 상승여력/손절위험/RR."""
    basis = cur_px or ((c.entry_low + c.entry_high) / 2)
    if basis <= 0:
        return 0.0, 0.0, 0.0
    upside = (c.target_price - basis) / basis * 100
    downside = (basis - c.stop_price) / basis * 100
    rr = upside / downside if downside > 0 else 0.0
    return upside, downside, rr


def _candidate_briefing_html(candidates: list[SwingCandidate], prices: dict[str, float]) -> str:
    """첫 화면 후보 브리핑: 지금 검토할 종목을 카드로 압축 표시."""
    if not candidates:
        return '<div class="selection-empty">활성 매수 후보 없음</div>'

    def rank_key(c: SwingCandidate) -> tuple[int, float, float]:
        cur = prices.get(c.symbol, 0)
        label, _, _ = _candidate_state(cur, c)
        state_rank = {"진입권": 3, "하단대기": 2, "상단초과": 1}.get(label, 0)
        _, _, rr = _candidate_rr(cur, c)
        return (state_rank, c.consensus_score, rr)

    top = sorted(candidates, key=rank_key, reverse=True)[:3]
    cards = []
    for c in top:
        cur = prices.get(c.symbol, 0)
        state, state_cls, state_note = _candidate_state(cur, c)
        upside, downside, rr = _candidate_rr(cur, c)
        tags = "".join(f'<span class="mini-tag">{html.escape(t)}</span>' for t in (c.tags or [])[:4])
        rationale = html.escape((c.rationale or "선정 근거 없음").strip())
        if len(rationale) > 150:
            rationale = rationale[:150] + "..."
        name_attr = html.escape(c.name, quote=True)
        cur_str = f"{int(cur):,}" if cur else "-"
        cards.append(f"""
        <article class="selection-card {state_cls}">
          <div class="selection-top">
            <div>
              <div class="selection-name">{html.escape(c.name)}</div>
              <div class="selection-symbol">{c.symbol} · 신뢰 {c.consensus_score:.0%}</div>
            </div>
            <span class="selection-state">{state}</span>
          </div>
          <div class="selection-price">
            <div><span>현재</span><b>{cur_str}</b></div>
            <div><span>진입</span><b>{int(c.entry_low):,}~{int(c.entry_high):,}</b></div>
            <div><span>RR</span><b>{rr:.1f}</b></div>
          </div>
          <div class="selection-risk">
            <span>목표 {upside:+.1f}%</span>
            <span>손절 {downside:.1f}%</span>
            <span>{html.escape(state_note)}</span>
          </div>
          <p class="selection-rationale">{rationale}</p>
          <div class="selection-foot">
            <div class="mini-tags">{tags}</div>
            <div class="selection-actions">
              <button class="btn-inline" data-action="show-analysis" data-symbol="{c.symbol}" data-name="{name_attr}">분석</button>
              <button class="btn-inline" data-action="show-chart" data-symbol="{c.symbol}" data-name="{name_attr}">차트</button>
            </div>
          </div>
        </article>""")
    return "".join(cards)


def _candidate_radar_html(candidates: list[SwingCandidate], prices: dict[str, float]) -> str:
    """매수 후보 전체를 선정/진입 판단 중심으로 표시."""
    if not candidates:
        return '<tr><td colspan="5"><div class="empty-state">매수 후보 없음</div></td></tr>'
    rows = []
    ranked = sorted(
        candidates,
        key=lambda c: (_candidate_state(prices.get(c.symbol, 0), c)[0] == "진입권", c.consensus_score),
        reverse=True,
    )
    for c in ranked:
        cur = prices.get(c.symbol, 0)
        state, state_cls, state_note = _candidate_state(cur, c)
        upside, downside, rr = _candidate_rr(cur, c)
        exp = c.expires_at.strftime("%m/%d") if c.expires_at else "-"
        tags = " ".join(f'<span class="mini-tag">{html.escape(t)}</span>' for t in (c.tags or [])[:3])
        rationale = html.escape((c.rationale or "").strip())
        if len(rationale) > 120:
            rationale = rationale[:120] + "..."
        cur_str = f"{int(cur):,}" if cur else "-"
        score_color = "var(--green)" if c.consensus_score >= 0.7 else ("var(--amber)" if c.consensus_score >= 0.5 else "var(--muted)")
        name_attr = html.escape(c.name, quote=True)
        rows.append(f"""
        <tr class="candidate-row">
          <td>
            <div class="candidate-title"><b>{html.escape(c.name)}</b>
              <button class="btn-inline" data-action="show-analysis" data-symbol="{c.symbol}" data-name="{name_attr}">분석</button>
              <button class="btn-inline" data-action="show-chart" data-symbol="{c.symbol}" data-name="{name_attr}">차트</button>
            </div>
            <div class="cell-sub">{c.symbol} · 만료 {exp}</div>
            <div class="mini-tags">{tags}</div>
          </td>
          <td>
            <span class="candidate-state {state_cls}">{state}</span>
            <small class="text-muted">{html.escape(state_note)}</small>
          </td>
          <td class="cell-num">
            <div>{cur_str}</div>
            <small class="text-muted">진입 {int(c.entry_low):,}~{int(c.entry_high):,}</small>
          </td>
          <td class="cell-meta">
            <div style="color:{score_color};font-weight:800">{c.consensus_score:.0%}</div>
            <small class="text-muted">RR {rr:.1f}<br>목표 {upside:+.1f}% · 손절 {downside:.1f}%</small>
          </td>
          <td>
            <div class="candidate-rationale">{rationale or "근거 없음"}</div>
          </td>
        </tr>""")
    return "".join(rows)


def _selection_pipeline_html(active: list[SwingPosition], candidates: list[SwingCandidate], prices: dict[str, float]) -> str:
    ready = 0
    near = 0
    high_score = 0
    for c in candidates:
        state, cls, _ = _candidate_state(prices.get(c.symbol, 0), c)
        if state == "진입권":
            ready += 1
        if cls == "state-near":
            near += 1
        if c.consensus_score >= 0.7:
            high_score += 1
    return f"""
    <div class="pipeline-strip">
      <div><span>보유</span><b>{len(active)}</b></div>
      <div><span>활성 후보</span><b>{len(candidates)}</b></div>
      <div><span>진입권</span><b>{ready}</b></div>
      <div><span>근접</span><b>{near}</b></div>
      <div><span>고신뢰</span><b>{high_score}</b></div>
    </div>"""


def _bot_status_html() -> str:
    """봇 프로세스 상태 패널 HTML."""
    import os
    services = [
        ("시세감시", "logs/market_monitor.log"),
        ("종목발굴", "logs/morning_screen.log"),
        ("저녁 선분석", "logs/evening_prescreen.log"),
    ]
    now_ts = datetime.now()
    rows = ""
    for label, log_path in services:
        full = PROJECT_ROOT / log_path
        if not full.exists():
            rows += f'<div class="bot-row"><span class="bot-dot" style="background:var(--dim)"></span> {label} <small class="text-dim">로그 없음</small></div>'
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(full))
        age = (now_ts - mtime).total_seconds()
        time_str = mtime.strftime("%H:%M:%S")
        if age < 120:
            dot, status = "var(--green)", "활성"
        elif age < 600:
            dot, status = "var(--amber)", "유휴"
        else:
            dot, status = "var(--red)", "중단"
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
        (re.compile(r"매수 완료"), "진입", "var(--amber)", 10),              # 진입 = 주황/앰버
        (re.compile(r"매도 전량 체결"), "체결", "var(--accent)", 10),          # 체결 = 보라/인디고
        (re.compile(r"청산 reason=(\w+)\s+price=(\d+)\s+pnl=([^\s]+)"), "청산", "var(--accent)", 10),
        (re.compile(r"잔고 불일치.*CLOSED"), "대사청산", "var(--amber)", 8),
        (re.compile(r"재토론.*트리거"), "재토론", "var(--blue)", 8),
        (re.compile(r"트레일링 스탑 활성화"), "트레일", "var(--amber)", 5),
        (re.compile(r"본전 보호 활성"), "본전보호", "var(--amber)", 7),
        (re.compile(r"모멘텀 소실"), "모멘텀소실", "var(--amber)", 7),
        (re.compile(r"매도 주문 실패"), "매도실패", "var(--red)", 3),       # 에러 = 빨강
        (re.compile(r"사전손절.*등록 실패"), "손절실패", "var(--red)", 3),
        (re.compile(r"일일 손실 한도"), "리스크한도", "var(--red)", 9),
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


def _sparkline_svg(
    values: list[float],
    *,
    width: int = 110,
    height: int = 36,
    css_class: str = "mini-sparkline",
    scale: tuple[float, float] | None = None,
) -> str:
    clean = [float(v) for v in values if float(v or 0) > 0]
    if len(clean) < 3:
        return f'<div class="{css_class} empty"></div>'
    pts = clean[-32:]
    if scale:
        lo = min(_safe_float(scale[0]), min(pts))
        hi = max(_safe_float(scale[1]), max(pts))
    else:
        lo = min(pts)
        hi = max(pts)
    spread = hi - lo
    if spread <= 0:
        spread = max(hi * 0.01, 1)
    step = width / max(1, len(pts) - 1)
    coords = []
    for i, value in enumerate(pts):
        x = round(i * step, 2)
        y = round(height - ((value - lo) / spread * (height - 6)) - 3, 2)
        coords.append(f"{x},{y}")
    cls = "up" if pts[-1] >= pts[0] else "down"
    last_x, last_y = coords[-1].split(",")
    return (
        f'<svg class="{css_class} {cls}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
        f'<polyline vector-effect="non-scaling-stroke" points="{" ".join(coords)}"></polyline>'
        f'<circle cx="{last_x}" cy="{last_y}" r="1.6"></circle>'
        f'</svg>'
    )


def _market_index_history(indexes: dict) -> dict[str, list[float]]:
    today = datetime.now().strftime("%Y-%m-%d")
    minute_key = datetime.now().isoformat(timespec="minutes")
    payload = state_store.load("market_index_history", {}) or {}
    if payload.get("date") != today:
        payload = {"date": today, "points": {}}
    points = payload.setdefault("points", {})
    changed = False
    series: dict[str, list[float]] = {}
    for key, value in (indexes or {}).items():
        current = _safe_float((value or {}).get("value"))
        if current <= 0:
            continue
        rows = points.setdefault(key, [])
        if not rows or rows[-1].get("t") != minute_key:
            rows.append({"t": minute_key, "value": round(current, 2)})
            points[key] = rows[-120:]
            changed = True
        elif abs(_safe_float(rows[-1].get("value")) - current) >= 0.01:
            rows[-1]["value"] = round(current, 2)
            changed = True
        series[key] = [_safe_float(row.get("value")) for row in points.get(key, [])]
    if changed:
        try:
            state_store.save("market_index_history", payload)
        except Exception:
            pass
    return series


def _update_position_mini_history(symbols: list[str], prices: dict[str, float]) -> dict[str, list[float]]:
    """보유현황 미니 차트 전용 가격 히스토리.

    큰 차트 API를 호출하지 않고, 이미 스냅샷에서 확보한 현재가만 누적한다.
    같은 분 안에서는 마지막 값을 교체해서 실시간 가격 변화가 화면에 바로 반영되게 한다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    minute_key = datetime.now().isoformat(timespec="minutes")
    payload = state_store.load("mini_price_history", {}) or {}
    if payload.get("date") != today:
        payload = {"date": today, "points": {}}
    points = payload.setdefault("points", {})
    wanted = {str(s) for s in symbols if s}
    changed = False
    series: dict[str, list[float]] = {}

    for symbol in wanted:
        current = _safe_float(prices.get(symbol))
        rows = points.setdefault(symbol, [])
        if current > 0:
            rounded = round(current, 2)
            if not rows or rows[-1].get("t") != minute_key:
                rows.append({"t": minute_key, "value": rounded})
                points[symbol] = rows[-180:]
                changed = True
            elif abs(_safe_float(rows[-1].get("value")) - rounded) >= 0.01:
                rows[-1]["value"] = rounded
                changed = True
        series[symbol] = [_safe_float(row.get("value")) for row in points.get(symbol, [])]

    stale_symbols = [symbol for symbol in points.keys() if symbol not in wanted]
    for symbol in stale_symbols:
        points.pop(symbol, None)
        changed = True

    if changed:
        try:
            state_store.save("mini_price_history", payload)
        except Exception:
            pass
    return series


def _index_sparkline_html(key: str, current_value: float, chg_pct: float, history: dict[str, list[float]]) -> str:
    values = list(history.get(key) or [])
    scale = None
    if current_value > 0:
        base = current_value / (1 + chg_pct / 100.0) if abs(chg_pct) < 50 else current_value
        if base > 0:
            scale_pct = max(3.0, abs(chg_pct) * 1.8)
            scale = (base * (1 - scale_pct / 100.0), base * (1 + scale_pct / 100.0))
            if len(values) < 3:
                values = [base + (current_value - base) * (i / 11) for i in range(12)]
    return _sparkline_svg(values, width=132, height=46, css_class="index-sparkline", scale=scale)


def _position_sparkline_html(symbol: str, latest_price: float, history: dict[str, list[float]]) -> str:
    """보유현황용 미니 차트. 큰 차트 캐시 + 실시간 현재가 히스토리를 합쳐 그린다."""
    live_values = list(history.get(symbol) or [])
    try:
        cached = _read_disk_chart_cache(f"{symbol}:intraday")
        data = cached[0] if cached else None
        candles = data.get("candles") if isinstance(data, dict) else None
        closes = [_safe_float(c.get("close")) for c in (candles or []) if isinstance(c, dict)]
        values = closes[-24:] + live_values[-32:]
        if latest_price > 0 and (not values or abs(values[-1] - latest_price) >= 0.01):
            values.append(latest_price)
        return _sparkline_svg(values)
    except Exception:
        values = live_values[-32:]
        if latest_price > 0 and (not values or abs(values[-1] - latest_price) >= 0.01):
            values.append(latest_price)
        return _sparkline_svg(values) if len(values) >= 3 else '<div class="mini-sparkline empty"></div>'


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

    # 계좌 잔고
    kis_holdings_raw: list[dict] = []
    try:
        bal = _cached_balance()
        o2 = (bal.get("output2") or [{}])[0]
        account_cash = int(o2.get("dnca_tot_amt", 0))      # 예수금 총액
        order_cash = int(o2.get("ord_psbl_cash", 0) or 0)  # 주문가능액
        if order_cash == 0:
            order_cash = int(o2.get("prvs_rcdl_excc_amt", 0) or 0)  # 모의투자 fallback
        eval_amt = int(o2.get("evlu_amt_smtl_amt", 0))      # 유가평가액
        total_eval = int(o2.get("tot_evlu_amt", 0))         # 총평가금액
        kis_holdings_raw = [h for h in (bal.get("output1") or [])
                            if int(h.get("hldg_qty", 0) or 0) > 0]
    except Exception:
        account_cash = order_cash = eval_amt = total_eval = 0

    sync_changed = _sync_positions_from_kis_holdings(positions, kis_holdings_raw, datetime.now())
    if sync_changed:
        state_store.save_positions([p.to_dict() for p in positions])
    active = [p for p in positions if p.state != PositionState.CLOSED]

    # 현재가 일괄 조회
    symbols = list({p.symbol for p in active} | {c.symbol for c in active_cands})
    prices = _fetch_prices(symbols)
    for h in kis_holdings_raw:
        sym = str(h.get("pdno") or "").strip()
        px = _safe_float(h.get("prpr"))
        if sym and px > 0:
            prices[sym] = px
    position_mini_history = _update_position_mini_history([p.symbol for p in active], prices)

    # 수수료율
    comm = _cfg.trading.commission_pct / 100.0

    # 오늘 PnL: KIS 기간별손익 원장값을 최우선 사용한다. 실패 시에만 체결 역산으로 fallback.
    kis_period_pnl, kis_period_symbols, kis_period_ok, kis_period_summary = _today_kis_period_profit()
    if kis_period_ok:
        execution_pnl = kis_period_pnl
        execution_symbols = kis_period_symbols | {p.symbol for p in closed_today}
        daily_pnl = kis_period_pnl
        pnl_source = "kis_period_profit"
    else:
        last_account = state_store.load("account_snapshot") or {}
        if last_account.get("date") == today and last_account.get("pnl_source") == "kis_period_profit":
            daily_pnl = _safe_int(last_account.get("daily_pnl"))
            execution_pnl = daily_pnl
            execution_symbols = set(last_account.get("execution_symbols") or [])
            kis_period_summary = last_account.get("kis_period_profit") or {}
            pnl_source = "last_good_kis_period_profit"
        else:
            execution_pnl, execution_symbols = _today_execution_realized_pnl(
                positions,
                active,
                closed_today,
                kis_holdings_raw,
                comm,
            )
            daily_pnl = execution_pnl
            for p in closed_today:
                if p.symbol in execution_symbols:
                    continue
                if not p.close_price:
                    continue
                gross = (p.close_price - p.avg_price) * p.qty
                fee = (p.avg_price * p.qty + p.close_price * p.qty) * comm
                daily_pnl += int(gross - fee)
            pnl_source = "execution_rebuild"

    stats = state_store.load_daily_stats()
    if stats.get("date") != today or int(stats.get("realized_pnl", 0) or 0) != int(daily_pnl):
        stats["date"] = today
        stats["realized_pnl"] = int(daily_pnl)
        stats["source"] = pnl_source
        stats["execution_symbols"] = sorted(execution_symbols)
        state_store.save_daily_stats(stats)

    kis_trade_rows = _today_kis_trade_profit_rows() if pnl_source == "kis_period_profit" else []

    # 미실현 PnL 합산. KIS 평가손익이 있으면 수동 보유분까지 포함된 계좌 기준값을 우선 사용한다.
    if kis_holdings_raw:
        unrealized_pnl = sum(_safe_int(h.get("evlu_pfls_amt")) for h in kis_holdings_raw)
    else:
        unrealized_pnl = 0
        for p in active:
            cur_px = prices.get(p.symbol, p.avg_price)
            gross = (cur_px - p.avg_price) * p.qty
            fee = (p.avg_price * p.qty + cur_px * p.qty) * comm
            unrealized_pnl += int(gross - fee)
    if pnl_source in ("kis_period_profit", "last_good_kis_period_profit"):
        state_store.save("account_snapshot", {
            "date": today,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "daily_pnl": int(daily_pnl),
            "unrealized_pnl": int(unrealized_pnl),
            "pnl_source": "kis_period_profit" if pnl_source == "kis_period_profit" else pnl_source,
            "execution_symbols": sorted(execution_symbols),
            "kis_period_profit": kis_period_summary,
            "holdings_count": len(kis_holdings_raw),
        })
    pnl_cost = _safe_int(kis_period_summary.get("tot_fee")) + _safe_int(
        kis_period_summary.get("tot_tltx") or kis_period_summary.get("sll_tltx_smtl")
    )
    if pnl_source == "kis_period_profit":
        pnl_source_label = f"KIS 원장 · 비용 {pnl_cost:,}원"
    elif pnl_source == "last_good_kis_period_profit":
        pnl_source_label = f"마지막 정상 원장 · 비용 {pnl_cost:,}원"
    else:
        pnl_source_label = "체결 역산 fallback"
    unrealized_source_label = f"KIS 보유평가 · {len(kis_holdings_raw)}종목" if kis_holdings_raw else "현재가 추정"
    pnl_color = _pnl_color(daily_pnl)
    unr_color = _pnl_color(unrealized_pnl)

    # 재토론 상태
    rescreen_st = state_store.load("rescreen_state") or {}
    rescreen_last = rescreen_st.get("last_run", "")
    rescreen_count = int(rescreen_st.get("count", 0)) if rescreen_st.get("date") == today else 0
    selection_briefing_html = _candidate_briefing_html(active_cands, prices)
    candidate_radar_html = _candidate_radar_html(active_cands, prices)
    selection_pipeline_html = _selection_pipeline_html(active, active_cands, prices)

    # ── 보유 포지션 테이블 ──────────────────────────────────────────
    blacklist_set = {e.get("symbol") for e in (state_store.load_sell_blacklist() or [])}
    pos_rows = ""
    for p in active:
        cur_px = prices.get(p.symbol, 0)
        pnl_pct = p.pnl_pct(cur_px) if cur_px else 0
        pnl_amt = int((cur_px - p.avg_price) * p.qty) if cur_px else 0
        eval_amount = int(cur_px * p.qty) if cur_px else 0
        pc = _pnl_color(pnl_pct)
        trail = f"{int(p.trailing_stop_px):,}" if p.trailing_stop_px else "-"
        cur_str = f"{int(cur_px):,}" if cur_px else "-"
        elapsed = _elapsed_str(p.entry_time)
        is_manual = (p.strategy or "swing") == "manual"
        is_blocked = p.symbol in blacklist_set
        strat_badge = _strategy_badge(p.strategy or "swing")
        # NXT 배지: 지원 종목이면 항상 표시 (manual 제외)
        nxt_badge = ' <span class="badge nxt-badge">NXT</span>' if _nxt_support.get(p.symbol) and not is_manual else ""
        lock_badge = ' <span class="badge locked-badge">🔒</span>' if is_blocked else ""
        # NXT 체결 대기 상태
        nxt_tag = ""
        if p.order_id and p.order_id.startswith("NXT:"):
            nxt_tag = ' <span class="badge nxt-pending">NXT 대기</span>'
        state_label = "미관리" if is_manual else p.state.value
        state_class = "manual-badge" if is_manual else p.state.value.lower()
        state_badges = f'<span class="badge {state_class}">{state_label}</span>{nxt_tag}'
        if is_blocked:
            sell_btn = '<span style="color:#888;font-size:11px">금지</span>'
            cur_cell = f'<td class="cur-price">{cur_str}</td>'
        else:
            sell_btn = f"<button class=\"btn-sell\" onclick=\"sellPosition('{p.symbol}','{p.name}',{p.qty})\">매도</button>"
            cur_cell = f"<td class=\"cur-price clickable-price\" onclick=\"sellAtPrice('{p.symbol}','{p.name}',{p.qty},{int(cur_px or 0)})\" title=\"클릭 시 이 가격으로 지정가 매도\">{cur_str}</td>"
        target_cell = (
            f'<td class="hide-mobile">-</td>' if is_manual
            else f'<td class="hide-mobile editable" onclick="editPrice(this,\'{p.symbol}\',\'target_price\',{int(p.target_price)})">{int(p.target_price):,}</td>'
        )
        stop_cell = (
            f'<td class="hide-mobile">-</td>' if is_manual
            else f'<td class="hide-mobile editable" onclick="editPrice(this,\'{p.symbol}\',\'stop_price\',{int(p.stop_price)})">{int(p.stop_price):,}</td>'
        )
        # 4컬럼 압축: 종목 / 보유·매수 / 현재·손익 / 목표·손절·트레일 + 매도
        if is_manual:
            target_line = '<small class="text-muted">목표 -</small>'
            stop_line = '<small class="text-muted">손절 -</small>'
            trail_line = '<small class="text-muted">트레일 -</small>'
        else:
            target_line = f'<small class="text-muted editable" onclick="editPrice(this,\'{p.symbol}\',\'target_price\',{int(p.target_price)})" title="클릭 편집">목표 {int(p.target_price):,}</small>'
            stop_line = f'<small class="text-muted editable" onclick="editPrice(this,\'{p.symbol}\',\'stop_price\',{int(p.stop_price)})" title="클릭 편집">손절 {int(p.stop_price):,}</small>'
            trail_line = f'<small class="text-muted">트레일 {trail}</small>'
        if is_blocked:
            sell_action = '<span class="btn-blocked">🔒 금지</span>'
            cur_click = f'<div class="cur-price">{cur_str}</div>'
        else:
            sell_action = f"<button class=\"btn-sell\" onclick=\"sellPosition('{p.symbol}','{p.name}',{p.qty})\">매도</button>"
            cur_click = f"<div class=\"cur-price clickable-price\" onclick=\"sellAtPrice('{p.symbol}','{p.name}',{p.qty},{int(cur_px or 0)})\" title=\"클릭 시 이 가격으로 지정가 매도\">{cur_str}</div>"
        name_attr = html.escape(p.name, quote=True)
        analysis_btn = "" if is_manual else f'<button class="btn-inline" data-action="show-analysis" data-symbol="{p.symbol}" data-name="{name_attr}">분석</button>'
        chart_btn = f'<button class="btn-inline" data-action="show-chart" data-symbol="{p.symbol}" data-name="{name_attr}">차트</button>'
        sparkline_html = _position_sparkline_html(p.symbol, cur_px, position_mini_history)
        pos_rows += f"""
        <tr class="row-compact" data-eval-amount="{eval_amount}" data-pnl-pct="{pnl_pct:.6f}">
          <td>
            <div class="cell-badges">{strat_badge}{nxt_badge}{lock_badge}{state_badges}</div>
            <div class="cell-title"><b>{p.name}</b> {analysis_btn}{chart_btn}</div>
            <div class="cell-sub">{p.symbol} · {elapsed}</div>
          </td>
          <td class="cell-num">
            <div>{p.qty}주</div>
            <small class="text-muted">@{round(p.avg_price):,}</small>
          </td>
          <td class="cell-num">
            {cur_click}
            <div style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%</div>
            <small style="color:{pc}">{pnl_amt:+,}</small>
            {sparkline_html}
          </td>
          <td class="cell-meta">
            {target_line}<br>
            {stop_line}<br>
            {trail_line}
          </td>
          <td class="cell-action">{sell_action}</td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="5"><div class="empty-state">보유 포지션 없음</div></td></tr>'

    # ── 후보 종목 테이블 ──────────────────────────────────────────
    cand_rows = ""
    for c in active_cands:
        exp = c.expires_at.strftime("%m/%d") if c.expires_at else "-"
        score_color = "var(--green)" if c.consensus_score >= 0.7 else ("var(--amber)" if c.consensus_score >= 0.5 else "var(--muted)")
        cur_px = prices.get(c.symbol, 0)
        cur_str = f"{int(cur_px):,}" if cur_px else "-"
        zone_badge = _in_zone_badge(cur_px, c.entry_low, c.entry_high) if cur_px else "-"
        # rationale 팝오버 (HTML 이스케이프)
        rationale_safe = (c.rationale or "").replace('"', '&quot;').replace('<', '&lt;')[:200]
        tags_str = " ".join(f'<small class="text-dim">#{t}</small>' for t in (c.tags or [])[:3])
        nxt_badge = ' <span class="badge nxt-badge">NXT</span>' if _nxt_support.get(c.symbol) else ""
        name_attr = html.escape(c.name, quote=True)
        cand_rows += f"""
        <tr class="row-compact">
          <td>
            <div class="cell-badges">{nxt_badge}{zone_badge}</div>
            <div class="cell-title"><b>{c.name}</b>
              <button class="btn-inline" data-action="show-analysis" data-symbol="{c.symbol}" data-name="{name_attr}">분석</button>
              <button class="btn-inline" data-action="show-chart" data-symbol="{c.symbol}" data-name="{name_attr}">차트</button></div>
            <div class="cell-sub">{c.symbol} {tags_str}</div>
          </td>
          <td class="cell-num">
            <div class="cur-price">{cur_str}</div>
            <small class="text-muted">진입 {int(c.entry_low):,}~{int(c.entry_high):,}</small>
          </td>
          <td class="cell-meta">
            <div style="color:{score_color};font-weight:700">{c.consensus_score:.0%}</div>
            <small class="text-muted">목표 {int(c.target_price):,}<br>손절 {int(c.stop_price):,}<br>만료 {exp}</small>
          </td>
          <td class="cell-action">
            <button class="btn-remove" onclick="removeCandidate('{c.symbol}','{c.name}')" title="삭제">✕</button>
          </td>
        </tr>"""

    if not cand_rows:
        rescreen_info = ""
        if rescreen_last:
            try:
                last_dt = datetime.fromisoformat(rescreen_last)
                rescreen_info = f"마지막 토론: {last_dt.strftime('%H:%M')} · 오늘 {rescreen_count}회"
            except Exception:
                pass
        cand_rows = f'<tr><td colspan="4"><div class="empty-state">후보 종목 없음<div class="sub">{rescreen_info or "재토론 대기 중"}</div></div></td></tr>'

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
        <tr class="row-compact">
          <td>
            <div class="cell-badges">{strat_badge}</div>
            <div class="cell-title"><b>{p.name}</b></div>
            <div class="cell-sub">{p.symbol}</div>
          </td>
          <td class="cell-num">
            <small class="text-muted">{round(p.avg_price):,} → {int(p.close_price):,}</small>
            <div style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%</div>
            <small style="color:{pc}">{pnl_amt:+,}</small>
          </td>
          <td class="cell-meta">
            <span style="color:{rc};font-size:11px">{reason}</span>
          </td>
        </tr>"""

    displayed_closed_symbols = {p.symbol for p in closed_today}
    if pnl_source == "kis_period_profit":
        for r in kis_trade_rows:
            symbol = str(r.get("pdno") or "").strip()
            sell_qty = _safe_int(r.get("sll_qty"))
            if not symbol or sell_qty <= 0 or symbol in displayed_closed_symbols:
                continue
            name = html.escape(str(r.get("prdt_name") or symbol))
            buy_px = _safe_int(r.get("pchs_unpr"))
            sell_px = _safe_int(r.get("sll_pric"))
            pnl_amt = _safe_int(r.get("rlzt_pfls"))
            pnl_pct = _safe_float(r.get("pfls_rt"))
            pc = _pnl_color(pnl_amt)
            closed_rows += f"""
        <tr class="row-compact">
          <td>
            <div class="cell-badges"><span class="badge manual-badge">KIS</span></div>
            <div class="cell-title"><b>{name}</b></div>
            <div class="cell-sub">{html.escape(symbol)} · 원장 매도 {sell_qty}주</div>
          </td>
          <td class="cell-num">
            <small class="text-muted">{buy_px:,} → {sell_px:,}</small>
            <div style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%</div>
            <small style="color:{pc}">{pnl_amt:+,}</small>
          </td>
          <td class="cell-meta">
            <span style="color:#c084fc;font-size:11px">KIS 원장</span>
          </td>
        </tr>"""

    if not closed_rows:
        closed_rows = '<tr><td colspan="3"><div class="empty-state">오늘 청산 없음</div></td></tr>'

    # ── Phase 2 섹션 ──────────────────────────────────────────
    chart_html = _daily_pnl_chart(positions, comm, daily_pnl)
    strat_html = _strategy_stats_html(positions, comm)
    bot_html = _bot_status_html()
    events_html = _recent_events_html()
    account_ledger_html = _account_ledger_html(
        pnl_source,
        daily_pnl,
        unrealized_pnl,
        kis_period_summary,
        kis_trade_rows,
        kis_holdings_raw,
    )

    watchlist_html = _watchlist_html(active, active_cands)
    sell_blacklist_html = _sell_blacklist_html()
    market_overview_html = _market_overview_html()
    position_analysis_html = _position_analysis_html(active, prices)
    bot_state_data = state_store.load_bot_state() or {}
    entry_paused = bool(bot_state_data.get("entry_paused", False))

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
            "execution_pnl": execution_pnl,
            "pnl_source": pnl_source,
            "pnl_source_label": pnl_source_label,
            "unrealized_source_label": unrealized_source_label,
        },
        "positions_html": pos_rows,
        "candidates_html": cand_rows,
        "selection_briefing_html": selection_briefing_html,
        "candidate_radar_html": candidate_radar_html,
        "selection_pipeline_html": selection_pipeline_html,
        "account_ledger_html": account_ledger_html,
        "watchlist_html": watchlist_html,
        "sell_blacklist_html": sell_blacklist_html,
        "market_overview_html": market_overview_html,
        "position_analysis_html": position_analysis_html,
        "entry_paused": entry_paused,
        "closed_html": closed_rows,
        "chart_html": chart_html,
        "strategy_html": strat_html,
        "bot_html": bot_html,
        "events_html": events_html,
        "debug": {
            "execution_pnl": execution_pnl,
            "execution_symbols": sorted(execution_symbols),
            "pnl_source": pnl_source,
            "kis_period_profit": kis_period_summary,
        },
    }


def _watchlist_html(active_positions, active_candidates) -> str:
    """관찰 후보 + 피봇 게이트 진단 표시.

    candidates와 보유 종목은 제외 (이미 별도 섹션에 있음).
    pivot_diag.json (monitor가 작성)이 있으면 마지막 게이트 결과를 표시.
    """
    try:
        watchlist_raw = state_store.load_watchlist() or []
    except Exception:
        return '<tr><td colspan="8"><div class="empty-state">watchlist 로드 실패</div></td></tr>'

    if not watchlist_raw:
        return '<tr><td colspan="8"><div class="empty-state">관찰 후보 없음 (저녁/아침 발굴 후 표시)</div></td></tr>'

    held = {p.symbol for p in active_positions}
    cand_syms = {c.symbol for c in active_candidates}
    diag = state_store.load("pivot_diag", {}) or {}

    # 가격 일괄 조회
    syms = [d["symbol"] for d in watchlist_raw]
    px_map = _fetch_prices(syms)

    rows = []
    now = datetime.now()
    for d in watchlist_raw:
        sym = d.get("symbol", "")
        name = d.get("name", sym)
        if sym in held or sym in cand_syms:
            continue
        try:
            c = SwingCandidate.from_dict(d)
            if c.is_expired(now):
                continue
        except Exception:
            continue
        cur_px = px_map.get(sym, 0)
        info = diag.get(sym) or {}
        passed = info.get("passed")
        mode = info.get("mode", "?")
        # 모드별 키 컬럼: pullback → 저점반등% / breakout → 박스돌파%
        if mode == "pullback":
            key_label = info.get("trough_low") or 0
            key_pct = info.get("bounce_pct")
        else:
            key_label = info.get("box_high") or 0
            key_pct = info.get("breakout_pct")
        vol_ratio = info.get("vol_ratio")
        trade_amt = info.get("trade_amount_bn")
        ma_up = info.get("ma_trend_up")
        reason = info.get("reason", "(미검사)")

        if passed is True:
            gate_html = f'<span style="color:var(--green);font-weight:bold" title="{reason}">✅ 통과</span>'
        elif passed is False:
            gate_html = f'<span style="color:var(--muted)" title="{reason}">❌ 대기</span>'
        else:
            gate_html = '<span style="color:var(--dim)">— 미검사</span>'
        key_pct_html = f"{key_pct:+.2f}%" if isinstance(key_pct, (int, float)) else "-"
        vol_html = f"{vol_ratio:.2f}x" if isinstance(vol_ratio, (int, float)) else "-"
        amt_html = f"{trade_amt:.0f}억" if isinstance(trade_amt, (int, float)) else "-"
        trend_html = "↑" if ma_up else ("↓" if ma_up is False else "?")
        mode_label = "저점" if mode == "pullback" else "박스"
        name_attr = html.escape(name, quote=True)
        rows.append(f"""
        <tr class="row-compact">
          <td>
            <div class="cell-title"><b>{name}</b>
              <button class="btn-inline" data-action="show-chart" data-symbol="{sym}" data-name="{name_attr}">차트</button></div>
            <div class="cell-sub">{sym}</div>
          </td>
          <td class="cell-num">
            <div class="cur-price">{int(cur_px):,}</div>
            <small class="text-muted">{mode_label} {int(key_label):,}<br>{key_pct_html}</small>
          </td>
          <td class="cell-meta">
            <small class="text-muted">vol {vol_html}<br>{amt_html}<br>추세 {trend_html}</small>
          </td>
          <td class="cell-action">{gate_html}</td>
        </tr>""")

    if not rows:
        return '<tr><td colspan="4"><div class="empty-state">관찰 후보 비어 있음</div></td></tr>'
    return "".join(rows)


def _market_overview_html() -> str:
    """첫 화면용 시황 상황판 — 시장 톤, 유효 근거, 회피 조건, 거래대금."""
    try:
        ov = get_market_overview(_kis)
    except Exception as e:
        return f'<div class="empty-state">시황 조회 실패: {html.escape(str(e))}</div>'

    summary = state_store.load_market_summary() or {}
    analysis = summary.get("analysis") if isinstance(summary.get("analysis"), dict) else None
    signal = summary.get("market_signal") if isinstance(summary.get("market_signal"), dict) else {}
    if not analysis and summary.get("summary"):
        analysis = {
            "tone": "구버전",
            "confidence": None,
            "market_read": str(summary.get("summary") or ""),
            "leading_sectors": [],
            "selection_bias": ["구조화 분석 생성 전 요약입니다. 다음 시황 생성 후 선정 바이어스가 자동 반영됩니다."],
            "avoid_or_risks": [{"name": "근거 부족", "reason": "구버전 요약은 섹터별 근거와 회피 조건을 분리 저장하지 않았습니다."}],
            "data_gaps": ["구조화 시장흐름 분석 미생성"],
        }
    if not analysis:
        analysis = {
            "tone": "대기",
            "confidence": None,
            "market_read": "시황 요약 생성 대기 중입니다.",
            "leading_sectors": [],
            "selection_bias": ["정량 후보군과 개별 뉴스 근거 중심"],
            "avoid_or_risks": [{"name": "시황 미생성", "reason": "08:45 시황 요약 이후 시장 판단이 보강됩니다."}],
            "data_gaps": ["시황 요약 미생성"],
        }

    gaps = [str(g) for g in (analysis.get("data_gaps") or [])]
    liquidity_limited = any("거래대금 상위 유동성 제한" in g for g in gaps)
    tone_text = str(analysis.get("tone") or "미정")
    tone_class = "watch"
    if any(x in tone_text for x in ("강세", "우호", "상승")):
        tone_class = "up"
    elif any(x in tone_text for x in ("약세", "위험", "하락")):
        tone_class = "down"
    confidence_raw = analysis.get("confidence")
    confidence_value = max(0, min(100, _safe_int(confidence_raw))) if confidence_raw is not None else 0
    confidence = f"{confidence_value}%" if confidence_raw is not None else "제한"
    quality = signal.get("quality_score")
    quality_value = max(0, min(100, _safe_int(quality))) if quality is not None else 0
    quality_text = f"{quality_value}" if quality is not None else "대기"
    strictness = str(signal.get("strictness") or ("high" if liquidity_limited else "normal"))
    strictness_label = {"high": "엄격", "medium": "선별", "normal": "보통", "low": "완화"}.get(strictness, strictness)
    tone_badge = {"up": "RISK-ON", "down": "RISK-OFF", "watch": "WATCH"}.get(tone_class, "WATCH")
    gen = html.escape((summary.get("generated_at") or ov.get("fetched_at") or "")[:16])
    decision = (
        ov.get("decision") if isinstance(ov.get("decision"), dict)
        else summary.get("market_decision") if isinstance(summary.get("market_decision"), dict)
        else {}
    )

    read_text = str(analysis.get("market_read") or "")
    if liquidity_limited:
        read_text = "소액/장전 거래대금 근거는 폐기했습니다. 시장 판단은 지수, 야간 미국 섹터, 정규장 유효 거래대금, 개별 뉴스가 맞을 때만 후보 선정에 반영합니다."
    read = html.escape(read_text)

    index_history = _market_index_history(ov.get("indexes") or {})
    idx_cards = []
    for key, label in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ"), ("kospi200", "KOSPI200")):
        v = (ov.get("indexes") or {}).get(key)
        if not v:
            continue
        chg = _safe_float(v.get("chg_pct"))
        chg_val = _safe_float(v.get("chg_val"))
        color = _pnl_color(chg)
        meter_cls = "up" if chg >= 0 else "down"
        meter_width = min(100, max(8, abs(chg) / 3.0 * 100))
        sparkline_html = _index_sparkline_html(key, _safe_float(v.get("value")), chg, index_history)
        idx_cards.append(f"""
        <div class="market-index-tile {meter_cls}">
          <span>{label}</span>
          <b>{_safe_float(v.get("value")):.2f}</b>
          <small style="color:{color}">{chg:+.2f}% ({chg_val:+.2f})</small>
          {sparkline_html}
          <div class="index-meter"><i style="width:{meter_width:.0f}%"></i></div>
        </div>""")
    if not idx_cards:
        idx_cards.append('<div class="market-index-tile empty">지수 데이터 대기</div>')

    gate = str(decision.get("gate") or "selective")
    gate_class = {
        "risk_on": "good",
        "selective": "watch",
        "defensive": "caution",
        "blocked": "risk",
    }.get(gate, "watch")
    decision_checks = []
    if isinstance(decision.get("checks"), list):
        decision_checks = [c for c in decision.get("checks") if isinstance(c, dict)]
    state_label = {"good": "우호", "watch": "확인", "caution": "주의", "risk": "위험"}.get(gate_class, "확인")
    decision_cards = f"""
      <div class="decision-card state-{gate_class} primary">
        <span>매수 게이트</span>
        <b>{html.escape(str(decision.get("gate_label") or "선별 매수"))}</b>
        <small>{_safe_int(decision.get("gate_score"))}점 · {html.escape(str(decision.get("stance") or "선별 진입"))}</small>
      </div>
      <div class="decision-card state-{gate_class}">
        <span>후보선정 영향</span>
        <b>{state_label}</b>
        <small>{html.escape(str(decision.get("positioning") or "개별 뉴스·수급 확인 후 진입"))}</small>
      </div>
    """
    for c in decision_checks[:4]:
        state = str(c.get("state") or "watch")
        state_cls = {"good": "good", "watch": "watch", "risk": "risk"}.get(state, "watch")
        decision_cards += f"""
      <div class="decision-card state-{state_cls}">
        <span>{html.escape(str(c.get("name") or "체크"))}</span>
        <b>{html.escape(str(c.get("value") or "-"))}</b>
        <small>{html.escape(str(c.get("impact") or ""))}</small>
      </div>
        """

    risk_flag_text = " · ".join(str(x) for x in (decision.get("risk_flags") or [])[:3])
    opp_flag_text = " · ".join(str(x) for x in (decision.get("opportunity_flags") or [])[:3])
    decision_flags_html = ""
    if risk_flag_text or opp_flag_text:
        decision_flags_html = f"""
        <div class="decision-flags">
          <span class="risk">{html.escape(risk_flag_text) if risk_flag_text else "위험 플래그 없음"}</span>
          <span class="good">{html.escape(opp_flag_text) if opp_flag_text else "기회 플래그 제한"}</span>
        </div>"""

    sector_rows = ""
    for sector in (analysis.get("leading_sectors") or [])[:5]:
        if not isinstance(sector, dict) or _uses_low_liquidity_sector_evidence(sector):
            continue
        name = html.escape(str(sector.get("name") or "미분류"))
        symbols = ", ".join(str(x) for x in (sector.get("symbols") or [])[:5])
        evidence = " · ".join(str(x) for x in (sector.get("evidence") or [])[:3])
        sector_rows += f"""
        <div class="sector-row">
          <div>
            <b>{name}</b>
            <span>{html.escape(symbols) if symbols else "관련 종목 확인 필요"}</span>
          </div>
          <small>{html.escape(evidence) if evidence else "근거 부족"}</small>
        </div>"""
    if not sector_rows:
        sector_rows = '<div class="market-empty-line">주도 섹터 단정 없음</div>'

    raw_bias = [str(x) for x in (analysis.get("selection_bias") or [])]
    if liquidity_limited:
        raw_bias = [x for x in raw_bias if not _uses_low_liquidity_bias(x)]
        raw_bias.insert(0, "50억 미만/정규장 전 거래대금은 자동선정 근거에서 제외")
    bias_items = "".join(
        f"<li>{html.escape(x)}</li>" for x in raw_bias[:4]
    ) or "<li>정량 후보군과 개별 뉴스 근거 중심</li>"

    risk_items = ""
    for r in (analysis.get("avoid_or_risks") or [])[:4]:
        if not isinstance(r, dict):
            continue
        risk_items += (
            f"<li><b>{html.escape(str(r.get('name') or '주의'))}</b>"
            f"<span>{html.escape(str(r.get('reason') or ''))}</span></li>"
        )
    if not risk_items:
        risk_items = "<li><b>회피 조건 없음</b><span>개별 종목 변동성은 별도 확인</span></li>"

    volume_rows = ""
    for x in (ov.get("volume_rank") or [])[:8]:
        amount = _safe_float(x.get("trade_amount_bn"))
        chg = _safe_float(x.get("chg_pct"))
        color = _pnl_color(chg)
        guard = "유효" if amount >= 50 else "제외"
        guard_cls = "ok" if amount >= 50 else "warn"
        volume_rows += f"""
        <tr class="{guard_cls}">
          <td><b>{html.escape(str(x.get('name') or ''))}</b><small>{html.escape(str(x.get('symbol') or ''))}</small></td>
          <td>{_safe_int(x.get('price')):,}</td>
          <td style="color:{color}">{chg:+.2f}%</td>
          <td>{amount:.0f}억</td>
          <td><span class="liquidity-pill {guard_cls}">{guard}</span></td>
        </tr>"""
    if not volume_rows:
        volume_rows = '<tr><td colspan="5"><div class="empty-state">거래대금 데이터 없음</div></td></tr>'

    gap_text = " · ".join(gaps[:4])
    guard_text = "저유동성/장전 거래대금 차단" if liquidity_limited else "정규장 유효 대금 기준 적용"
    us_text = html.escape(str(summary.get("us_market") or "야간 미국시장 데이터 대기"))

    return f"""
    <div class="market-board">
      <div class="market-pulse tone-{tone_class}" style="--confidence:{confidence_value}%">
        <div class="pulse-status-row">
          <span class="market-overline">시장 톤</span>
          <span class="tone-chip">{html.escape(tone_badge)}</span>
        </div>
        <div class="pulse-title-row">
          <strong>{html.escape(tone_text)}</strong>
          <span class="confidence-dial"><b>{confidence}</b><small>신뢰</small></span>
        </div>
        <div class="pulse-meta-grid">
          <span><b>{html.escape(quality_text)}</b>품질</span>
          <span><b>{html.escape(strictness_label)}</b>엄격도</span>
        </div>
        <p>{read}</p>
        <div class="confidence-track"><span></span></div>
        <div class="market-guard"><span>LIQUIDITY GUARD</span><b>{html.escape(guard_text)}</b></div>
      </div>

      <div class="market-index-strip">
        {''.join(idx_cards)}
      </div>

      <div class="market-decision-strip">
        {decision_cards}
        {decision_flags_html}
      </div>

      <div class="market-us-line">
        <span>야간 미국</span>
        <b>{us_text}</b>
      </div>

      <div class="market-panel market-sectors">
        <div class="market-panel-title">주도 섹터 후보</div>
        {sector_rows}
      </div>

      <div class="market-panel market-decisions">
        <div class="market-panel-title">선정 기준</div>
        <ul>{bias_items}</ul>
      </div>

      <div class="market-panel market-risks">
        <div class="market-panel-title">회피/주의</div>
        <ul>{risk_items}</ul>
      </div>

      <div class="market-panel market-volume">
        <div class="market-panel-head">
          <div>
            <div class="market-panel-title">거래대금 상위</div>
            <small>50억 미만은 후보 근거 제외</small>
          </div>
          <span>{gen}</span>
        </div>
        <div class="table-wrap">
          <table class="market-volume-table">
            <thead><tr><th>종목</th><th>현재</th><th>등락</th><th>대금</th><th>판정</th></tr></thead>
            <tbody>{volume_rows}</tbody>
          </table>
        </div>
      </div>

      <div class="market-gap-line">{html.escape(gap_text) if gap_text else "데이터 공백 없음"}</div>
    </div>"""


def _uses_low_liquidity_sector_evidence(sector: dict) -> bool:
    evidence = " ".join(str(x) for x in (sector.get("evidence") or []))
    if "정규장 전" in evidence and "거래대금" in evidence:
        return True
    if "거래대금" not in evidence and "대금" not in evidence:
        return False
    amounts = [float(x.replace(",", "")) for x in re.findall(r"(?:대금\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*억", evidence)]
    return bool(amounts) and max(amounts) < 50.0


def _uses_low_liquidity_bias(text: str) -> bool:
    if not text:
        return False
    if "거래대금 상위" in text or "국내 거래대금" in text:
        return True
    if "거래대금" not in text and "대금" not in text:
        return False
    amounts = [float(x.replace(",", "")) for x in re.findall(r"(?:대금\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*억", text)]
    return bool(amounts) and max(amounts) < 50.0


def _position_analysis_html(active_positions, prices) -> str:
    """보유 종목별 기술 분석 카드."""
    if not active_positions:
        return '<div class="empty-state">보유 종목 없음 — 진입 후 분석 데이터 표시</div>'

    cards = []
    for p in active_positions:
        if (p.strategy or "swing") == "manual":
            continue
        sym = p.symbol
        cur_px = prices.get(sym, 0)
        pnl_pct = p.pnl_pct(cur_px) if cur_px else 0
        pc = _pnl_color(pnl_pct)

        # OHLCV 캐싱 (5분 TTL — 장중 일봉은 변하지만 5분이면 충분)
        ind = _cached_indicators(sym)
        ma5 = int(ind.get("ma5") or 0)
        ma20 = int(ind.get("ma20") or 0)
        ma60 = int(ind.get("ma60") or 0)
        ma120 = int(ind.get("ma120") or 0)
        rsi = ind.get("rsi14")
        atr = int(ind.get("atr14") or 0)
        sr = ind.get("support_resistance") or {}
        sup = sr.get("support") or []
        res = sr.get("resistance") or []

        # 추세 판단
        trend_msg = "?"
        if ma20 and ma60:
            trend_msg = "상승추세 (MA20>MA60)" if ma20 > ma60 else "하락추세 (MA20≤MA60)"
        rsi_msg = "-"
        if rsi is not None:
            if rsi >= 70:
                rsi_msg = f'<span style="color:var(--red)">{rsi:.0f} (과매수)</span>'
            elif rsi <= 30:
                rsi_msg = f'<span style="color:var(--green)">{rsi:.0f} (과매도)</span>'
            else:
                rsi_msg = f"{rsi:.0f}"

        # 에이전트 의견 요약
        ag_html = ""
        for op in (p.agent_opinions or [])[:4]:
            label = op.get("label", op.get("agent_name", ""))
            conv = op.get("conviction", 0)
            rat = (op.get("rationale", "") or "")[:120]
            role = op.get("role", "")
            role_color = "var(--red)" if role == "risk" else "var(--blue)"
            ag_html += f"""
            <div class="agent-op">
              <div><b style="color:{role_color}">{label}</b> <small>conv {conv:.0%}</small></div>
              <div class="agent-rat">{rat}</div>
            </div>"""
        if not ag_html:
            ag_html = '<div class="text-muted" style="font-size:11px">진입 시 에이전트 의견 없음</div>'

        rationale = (p.rationale or "").strip()
        rationale_html = (
            f'<div class="analysis-rationale"><b>진입 근거:</b> {rationale[:300]}</div>'
            if rationale else ""
        )

        cards.append(f"""
        <div class="analysis-card">
          <div class="analysis-header">
            <div>
              <b>{p.name}</b> <small class="text-muted">{sym}</small>
            </div>
            <div style="color:{pc};font-weight:bold">{pnl_pct:+.2f}%</div>
          </div>
          <div class="analysis-grid">
            <div><span class="text-muted">현재가</span> <b>{int(cur_px):,}</b></div>
            <div><span class="text-muted">매수가</span> <b>{int(p.avg_price):,}</b></div>
            <div><span class="text-muted">목표가</span> <b>{int(p.target_price):,}</b></div>
            <div><span class="text-muted">손절가</span> <b>{int(p.stop_price):,}</b></div>
            <div><span class="text-muted">MA20</span> {ma20:,}</div>
            <div><span class="text-muted">MA60</span> {ma60:,}</div>
            <div><span class="text-muted">MA120</span> {ma120:,}</div>
            <div><span class="text-muted">RSI14</span> {rsi_msg}</div>
            <div><span class="text-muted">ATR14</span> {atr:,}</div>
            <div><span class="text-muted">추세</span> {trend_msg}</div>
          </div>
          <div class="analysis-sr">
            <div><span class="text-muted">저항선:</span> {', '.join(f"{int(r):,}" for r in res[:3]) or '-'}</div>
            <div><span class="text-muted">지지선:</span> {', '.join(f"{int(s):,}" for s in sup[:3]) or '-'}</div>
          </div>
          {rationale_html}
          <div class="agent-opinions">{ag_html}</div>
        </div>""")

    if not cards:
        return '<div class="empty-state">분석 대상 종목 없음 (수동 매수 종목 제외)</div>'
    return "".join(cards)


# 일봉 + 지표 캐시 — 5분 TTL
_IND_CACHE: dict[str, tuple[dict, float]] = {}

def _cached_indicators(symbol: str, ttl_sec: float = 300.0) -> dict:
    now_ts = _time.monotonic()
    cached = _IND_CACHE.get(symbol)
    if cached and (now_ts - cached[1]) < ttl_sec:
        return cached[0]
    try:
        ohlcv = _kis.get_daily_ohlcv(symbol, count=130)
        ind = compute_indicators(ohlcv) or {}
    except Exception:
        ind = {}
    _IND_CACHE[symbol] = (ind, now_ts)
    return ind


# 차트 캐시 — API 한도 보호용. 분봉은 짧게, 일/주봉은 5분 캐시한다.
_CHART_CACHE: dict[str, tuple[dict, float, float]] = {}
_CHART_CACHE_DIR = PROJECT_ROOT / "state" / "chart_cache"
_CHART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CHART_REFRESHING: set[str] = set()
_CHART_REFRESH_LOCK = threading.Lock()


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _safe_int(value) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _ma_points(candles: list[dict], period: int) -> list[dict]:
    points: list[dict] = []
    closes: list[float] = []
    for candle in candles:
        close = _safe_float(candle.get("close"))
        closes.append(close)
        if len(closes) >= period and all(v > 0 for v in closes[-period:]):
            points.append({
                "time": candle["time"],
                "value": round(sum(closes[-period:]) / period, 2),
            })
    return points


def _chart_interval(raw_interval: str | None) -> str:
    value = (raw_interval or "intraday").strip().lower()
    aliases = {
        "m": "intraday", "min": "intraday", "minute": "intraday", "intraday": "intraday",
        "d": "daily", "day": "daily", "daily": "daily",
        "w": "weekly", "week": "weekly", "weekly": "weekly",
    }
    return aliases.get(value, "intraday")


def _interval_label(interval: str) -> str:
    return {"intraday": "분봉", "daily": "일봉", "weekly": "주봉"}.get(interval, "분봉")


def _chart_cache_ttls(interval: str) -> tuple[float, float]:
    if interval == "intraday":
        return 30.0, 600.0
    return 300.0, 86_400.0


def _chart_cache_file(cache_key: str) -> Path:
    safe = "".join(ch for ch in cache_key if ch.isalnum() or ch in ("_", "-"))
    return _CHART_CACHE_DIR / f"{safe}.json"


def _with_chart_cache_info(data: dict, source: str, cached_at: float, stale: bool = False) -> dict:
    out = dict(data)
    out["cache"] = {
        "source": source,
        "stale": stale,
        "cached_at": datetime.fromtimestamp(cached_at).isoformat(timespec="seconds"),
        "age_sec": max(0, int(_time.time() - cached_at)),
    }
    return out


def _read_disk_chart_cache(cache_key: str) -> tuple[dict, float] | None:
    path = _chart_cache_file(cache_key)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        data = payload.get("data")
        cached_at = float(payload.get("cached_at", 0) or 0)
        if isinstance(data, dict) and cached_at > 0:
            return data, cached_at
    except Exception:
        return None
    return None


def _write_disk_chart_cache(cache_key: str, data: dict) -> None:
    path = _chart_cache_file(cache_key)
    tmp = path.with_suffix(".json.tmp")
    payload = {"cached_at": _time.time(), "data": data}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _find_chart_context(symbol: str) -> dict:
    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    pos = next((p for p in positions if p.symbol == symbol and p.state != PositionState.CLOSED), None)
    if pos:
        levels = [
            {"title": "매수가", "price": pos.avg_price, "color": "#f59e0b"},
            {"title": "목표가", "price": pos.target_price, "color": "#ef4444"},
            {"title": "손절가", "price": pos.stop_price, "color": "#3b82f6"},
        ]
        if pos.trailing_stop_px:
            levels.append({"title": "트레일", "price": pos.trailing_stop_px, "color": "#a78bfa"})
        return {"kind": "position", "name": pos.name, "levels": levels}

    candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
    cand = next((c for c in candidates if c.symbol == symbol and not c.is_expired()), None)
    if cand:
        return {
            "kind": "candidate",
            "name": cand.name,
            "levels": [
                {"title": "진입 하단", "price": cand.entry_low, "color": "#22c55e"},
                {"title": "진입 상단", "price": cand.entry_high, "color": "#22c55e"},
                {"title": "목표가", "price": cand.target_price, "color": "#ef4444"},
                {"title": "손절가", "price": cand.stop_price, "color": "#3b82f6"},
            ],
        }

    for item in state_store.load_watchlist() or []:
        if item.get("symbol") == symbol:
            try:
                cand = SwingCandidate.from_dict(item)
                return {
                    "kind": "watchlist",
                    "name": cand.name,
                    "levels": [
                        {"title": "진입 하단", "price": cand.entry_low, "color": "#22c55e"},
                        {"title": "진입 상단", "price": cand.entry_high, "color": "#22c55e"},
                        {"title": "목표가", "price": cand.target_price, "color": "#ef4444"},
                        {"title": "손절가", "price": cand.stop_price, "color": "#3b82f6"},
                    ],
                }
            except Exception:
                return {"kind": "watchlist", "name": item.get("name") or symbol, "levels": []}

    try:
        name = _kis.get_stock_name(symbol) or symbol
    except Exception:
        name = symbol
    return {"kind": "symbol", "name": name, "levels": []}


def _daily_weekly_chart_candles(symbol: str, interval: str) -> tuple[list[dict], list[dict]]:
    period = "W" if interval == "weekly" else "D"
    count = 80 if interval == "weekly" else 100
    raw = _kis.get_daily_ohlcv(symbol, count=count, period=period)
    candles: list[dict] = []
    for row in reversed(raw):
        date = str(row.get("stck_bsop_date") or "")
        if len(date) != 8:
            continue
        open_px = _safe_float(row.get("stck_oprc"))
        high_px = _safe_float(row.get("stck_hgpr"))
        low_px = _safe_float(row.get("stck_lwpr"))
        close_px = _safe_float(row.get("stck_clpr") or row.get("stck_prpr"))
        if min(open_px, high_px, low_px, close_px) <= 0:
            continue
        candles.append({
            "time": f"{date[:4]}-{date[4:6]}-{date[6:8]}",
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": _safe_int(row.get("acml_vol") or row.get("cntg_vol")),
        })
    return candles, raw


def _intraday_chart_candles(symbol: str) -> tuple[list[dict], list[dict]]:
    raw = _kis.get_intraday_candles(symbol, from_time="090000")
    candles: list[dict] = []
    kst = timezone(timedelta(hours=9))
    today = datetime.now().strftime("%Y%m%d")
    for row in raw:
        date = str(row.get("stck_bsop_date") or row.get("bsop_date") or today)
        tm = str(row.get("stck_cntg_hour") or row.get("cntg_hour") or "")
        if len(date) != 8 or len(tm) < 6:
            continue
        open_px = _safe_float(row.get("stck_oprc") or row.get("oprc"))
        high_px = _safe_float(row.get("stck_hgpr") or row.get("hgpr"))
        low_px = _safe_float(row.get("stck_lwpr") or row.get("lwpr"))
        close_px = _safe_float(row.get("stck_prpr") or row.get("stck_clpr") or row.get("prpr"))
        if min(open_px, high_px, low_px, close_px) <= 0:
            continue
        ts = int(datetime(
            int(date[:4]), int(date[4:6]), int(date[6:8]),
            int(tm[:2]), int(tm[2:4]), int(tm[4:6]),
            tzinfo=kst,
        ).timestamp())
        candles.append({
            "time": ts,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": _safe_int(row.get("cntg_vol") or row.get("acml_vol")),
        })
    return candles, raw


def _build_chart_payload(symbol: str, interval: str = "intraday") -> dict:
    symbol = symbol.strip()
    if not symbol:
        raise ValueError("symbol 필요")
    interval = _chart_interval(interval)

    if interval == "intraday":
        candles, raw = _intraday_chart_candles(symbol)
    else:
        candles, raw = _daily_weekly_chart_candles(symbol, interval)

    if not candles:
        raise ValueError("차트 데이터 없음")

    volume = [
        {
            "time": c["time"],
            "value": c["volume"],
            "color": "rgba(239, 68, 68, 0.45)" if c["close"] >= c["open"] else "rgba(59, 130, 246, 0.45)",
        }
        for c in candles
    ]
    ctx = _find_chart_context(symbol)
    ind = compute_indicators(raw) or {}
    return {
        "symbol": symbol,
        "name": ctx["name"],
        "kind": ctx["kind"],
        "interval": interval,
        "interval_label": _interval_label(interval),
        "candles": [{k: v for k, v in c.items() if k != "volume"} for c in candles],
        "volume": volume,
        "ma": {
            "ma5": _ma_points(candles, 5),
            "ma20": _ma_points(candles, 20),
            "ma60": _ma_points(candles, 60),
        },
        "levels": [x for x in ctx["levels"] if _safe_float(x.get("price")) > 0],
        "indicators": {
            "rsi14": ind.get("rsi14"),
            "atr14": ind.get("atr14"),
            "ma20": ind.get("ma20"),
            "ma60": ind.get("ma60"),
            "ma120": ind.get("ma120"),
        },
        "last": candles[-1],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _refresh_chart_cache(cache_key: str, symbol: str, interval: str) -> None:
    with _CHART_REFRESH_LOCK:
        if cache_key in _CHART_REFRESHING:
            return
        _CHART_REFRESHING.add(cache_key)
    try:
        data = _build_chart_payload(symbol, interval)
        now_mono = _time.monotonic()
        now_epoch = _time.time()
        _CHART_CACHE[cache_key] = (data, now_mono, now_epoch)
        _write_disk_chart_cache(cache_key, data)
    except Exception:
        pass
    finally:
        with _CHART_REFRESH_LOCK:
            _CHART_REFRESHING.discard(cache_key)


def _refresh_chart_cache_soon(cache_key: str, symbol: str, interval: str) -> None:
    with _CHART_REFRESH_LOCK:
        if cache_key in _CHART_REFRESHING:
            return
    t = threading.Thread(
        target=_refresh_chart_cache,
        args=(cache_key, symbol, interval),
        daemon=True,
    )
    t.start()


def _get_cached_chart(symbol: str, interval: str = "intraday", force_refresh: bool = False) -> dict:
    symbol = symbol.strip()
    interval = _chart_interval(interval)
    fresh_ttl, stale_ttl = _chart_cache_ttls(interval)
    cache_key = f"{symbol}:{interval}"
    now_ts = _time.monotonic()
    if not force_refresh:
        cached = _CHART_CACHE.get(cache_key)
        if cached:
            data, mono_at, epoch_at = cached
            age = now_ts - mono_at
            if age < fresh_ttl:
                return _with_chart_cache_info(data, "memory", epoch_at)
            if age < stale_ttl:
                return _with_chart_cache_info(data, "memory", epoch_at, stale=True)

        disk_cached = _read_disk_chart_cache(cache_key)
        if disk_cached:
            data, epoch_at = disk_cached
            age = _time.time() - epoch_at
            _CHART_CACHE[cache_key] = (data, now_ts - min(age, stale_ttl + 1), epoch_at)
            if age < fresh_ttl:
                return _with_chart_cache_info(data, "disk", epoch_at)
            if age < stale_ttl:
                return _with_chart_cache_info(data, "disk", epoch_at, stale=True)

    data = _build_chart_payload(symbol, interval)
    now_epoch = _time.time()
    _CHART_CACHE[cache_key] = (data, now_ts, now_epoch)
    _write_disk_chart_cache(cache_key, data)
    return _with_chart_cache_info(data, "live", now_epoch)


def _sell_blacklist_html() -> str:
    """매도 금지 종목 목록 HTML."""
    try:
        entries = state_store.load_sell_blacklist() or []
    except Exception:
        entries = []
    if not entries:
        return '<tr><td colspan="3"><div class="empty-state">매도 금지 종목 없음</div></td></tr>'
    rows = []
    for e in entries:
        sym = e.get("symbol", "")
        name = e.get("name", sym)
        added = (e.get("added_at") or "")[:16]
        rows.append(f"""
        <tr>
          <td><b>{name}</b> <small class="text-muted">{sym}</small></td>
          <td><small class="text-muted">{added}</small></td>
          <td><button class="btn-sell" onclick="removeSellBlacklist('{sym}','{name}')">해제</button></td>
        </tr>""")
    return "".join(rows)


# 서버측 스냅샷 캐시 — 탭/요청 수와 무관하게 초당 1회만 실제 계산·REST 호출
_SNAP_TTL_SEC = 2.0
_snap_cache: dict = {"at": 0.0, "data": None}
_snap_lock = threading.Lock()


@app.get("/api/snapshot")
def api_snapshot():
    return _get_cached_snapshot()


@app.get("/api/chart/{symbol}")
def api_chart(symbol: str, interval: str = "intraday", refresh: int = 0):
    """종목 차트 데이터. interval=intraday|daily|weekly."""
    try:
        return _get_cached_chart(symbol, interval, force_refresh=bool(refresh))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


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
                data = await asyncio.to_thread(_get_cached_snapshot)
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


@app.post("/api/entry-pause")
def api_entry_pause(body: dict):
    """매수 정지/재개 토글. body: {paused: bool, reason?: str}"""
    paused = bool(body.get("paused", False))
    reason = (body.get("reason") or "").strip()
    state_store.save_bot_state({
        "entry_paused": paused,
        "paused_at": datetime.now().isoformat(timespec="seconds") if paused else "",
        "reason": reason,
    })
    return {"ok": True, "entry_paused": paused}


@app.get("/api/entry-pause")
def api_entry_pause_status():
    return state_store.load_bot_state() or {"entry_paused": False}


@app.get("/api/sell-blacklist")
def api_get_sell_blacklist():
    """매도 금지 종목 목록 조회."""
    return {"entries": state_store.load_sell_blacklist()}


@app.post("/api/sell-blacklist/add")
def api_add_sell_blacklist(body: dict):
    """매도 금지 종목 추가. body: {symbol, name?}"""
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 필요"}, status_code=400)
    name = (body.get("name") or "").strip()
    if not name:
        # 이름 자동 조회
        try:
            name = _kis.get_stock_name(symbol) or symbol
        except Exception:
            name = symbol
    entries = state_store.load_sell_blacklist()
    if any(e.get("symbol") == symbol for e in entries):
        return {"ok": True, "symbol": symbol, "already": True}
    entries.append({
        "symbol": symbol, "name": name,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    })
    state_store.save_sell_blacklist(entries)
    return {"ok": True, "symbol": symbol, "name": name}


@app.post("/api/sell-blacklist/remove")
def api_remove_sell_blacklist(body: dict):
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 필요"}, status_code=400)
    entries = [e for e in state_store.load_sell_blacklist() if e.get("symbol") != symbol]
    state_store.save_sell_blacklist(entries)
    return {"ok": True, "symbol": symbol}


@app.post("/api/sell")
def api_sell(body: dict):
    """수동 매도.

    body:
      - symbol: 종목코드 (필수)
      - price: 지정가 매도 가격 (선택). 주면 지정가, 없으면 시장가/NXT 자동 분기.
    """
    from src.core.models import CloseReason
    symbol = body.get("symbol", "")
    price_arg = body.get("price")
    if not symbol:
        return JSONResponse({"ok": False, "error": "symbol 필요"}, status_code=400)
    if state_store.is_sell_blocked(symbol):
        return JSONResponse(
            {"ok": False, "error": "매도 금지 종목 — 금지 해제 후 다시 시도하세요"},
            status_code=403,
        )
    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    target = next((p for p in positions if p.symbol == symbol and p.state != PositionState.CLOSED), None)
    if not target:
        return JSONResponse({"ok": False, "error": f"{symbol} 보유 포지션 없음"}, status_code=404)
    try:
        from src.core.clock import is_pre_market, is_nxt_after_hours, is_regular_market

        if price_arg is not None and float(price_arg) > 0:
            # 지정가 매도 — KRX 정규장 또는 NXT 시간대 자동 분기
            limit_px = float(price_arg)
            if is_pre_market() or is_nxt_after_hours():
                _kis.sell_nxt(target.symbol, target.qty, limit_px)
            else:
                _kis.sell_limit(target.symbol, target.qty, limit_px)
            close_px = limit_px
        elif is_pre_market() or is_nxt_after_hours():
            # NXT 시간대 시장가 대용 — NXT 현재가로 지정가
            px_data = _kis.get_nxt_price(symbol)
            close_px = float(px_data.get("stck_prpr", 0) or 0) or target.avg_price
            _kis.sell_nxt(target.symbol, target.qty, close_px)
        else:
            # 정규장 시장가
            _kis.sell_market(target.symbol, target.qty)
            px_data = _kis.get_price(symbol)
            close_px = float(px_data.get("stck_prpr", 0) or 0) or target.avg_price
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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


@app.get("/api/candidate-detail/{symbol}")
def api_candidate_detail(symbol: str):
    """후보 또는 보유 포지션의 에이전트 분석 상세 조회."""
    # 후보 먼저 확인
    candidates = [SwingCandidate.from_dict(d) for d in state_store.load_candidates()]
    cand = next((c for c in candidates if c.symbol == symbol), None)
    if cand:
        return {
            "symbol": cand.symbol,
            "name": cand.name,
            "consensus_score": cand.consensus_score,
            "rationale": cand.rationale,
            "agent_opinions": cand.agent_opinions or [],
            "tags": cand.tags or [],
        }
    # 후보에 없으면 포지션 확인
    positions = [SwingPosition.from_dict(d) for d in state_store.load_positions()]
    pos = next((p for p in positions if p.symbol == symbol and p.state != PositionState.CLOSED), None)
    if pos:
        return {
            "symbol": pos.symbol,
            "name": pos.name,
            "consensus_score": None,
            "rationale": pos.rationale,
            "agent_opinions": pos.agent_opinions or [],
            "tags": [],
        }
    return JSONResponse({"error": "not found"}, status_code=404)


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
        account_ledger_html=snap.get("account_ledger_html", ""),
        selection_briefing_html=snap.get("selection_briefing_html", ""),
        candidate_radar_html=snap.get("candidate_radar_html", ""),
        selection_pipeline_html=snap.get("selection_pipeline_html", ""),
        watchlist_html=snap.get("watchlist_html", ""),
        sell_blacklist_html=snap.get("sell_blacklist_html", ""),
        market_overview_html=snap.get("market_overview_html", ""),
        position_analysis_html=snap.get("position_analysis_html", ""),
        entry_paused=snap.get("entry_paused", False),
        closed_html=snap["closed_html"],
        chart_html=snap["chart_html"],
        strategy_html=snap["strategy_html"],
        bot_html=snap["bot_html"],
        events_html=snap["events_html"],
    )
    return HTMLResponse(html)




if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
