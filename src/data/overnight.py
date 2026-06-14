"""밤사이 변화 수집 — 미국장 마감, 조간 뉴스, NXT 프리장 가격.

fetch_overnight_delta()는 절대 raise하지 않음.
데이터 수집 실패 시 해당 필드를 None으로 채워 반환.
"""
from __future__ import annotations
import logging
from datetime import datetime, time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.kis_client import KisClient

log = logging.getLogger(__name__)


def fetch_overnight_delta(
    prelim_symbols: list[str],
    kis_client: "KisClient",
    news_sources: list[str] | None = None,
) -> dict:
    """밤사이 변화 데이터 수집.

    Returns:
        {
            "us_market": {
                "sp500_chg_pct": float | None,
                "nasdaq_chg_pct": float | None,
                "sector_moves": [{"name": str, "chg_pct": float, "evidence": [str]}],
            },
            "fresh_news": [{"title": str, "summary": str, ...}],  # 06:00 이후 뉴스
            "nxt_prices": {symbol: {"price": float, "gap_pct": float, "amount_bn": float}},
            "collected_at": ISO datetime str,
        }
    """
    result: dict = {
        "us_market": None,
        "fresh_news": [],
        "nxt_prices": {},
        "collected_at": datetime.now().isoformat(),
    }

    # ── 미국 시장 마감 (yfinance) ──────────────────────────────────────────
    try:
        import yfinance as yf
        sp500_chg = _ticker_change_pct(yf, "^GSPC")
        nasdaq_chg = _ticker_change_pct(yf, "^IXIC")
        sector_moves = _fetch_us_sector_moves(yf)
        result["us_market"] = {
            "sp500_chg_pct": round(float(sp500_chg), 2) if sp500_chg is not None else None,
            "nasdaq_chg_pct": round(float(nasdaq_chg), 2) if nasdaq_chg is not None else None,
            "sector_moves": sector_moves,
        }
        log.info(
            "미국장: S&P500 %s%%, NASDAQ %s%%, 섹터신호 %d개",
            f"{sp500_chg:+.2f}" if sp500_chg is not None else "N/A",
            f"{nasdaq_chg:+.2f}" if nasdaq_chg is not None else "N/A",
            len(sector_moves),
        )
    except Exception as e:
        log.warning("미국장 데이터 수집 실패 (yfinance): %s", e)

    # ── 조간 뉴스 (06:00 이후) ─────────────────────────────────────────────
    try:
        from src.data.news_fetcher import fetch_news
        today = datetime.now().replace(hour=6, minute=0, second=0, microsecond=0)
        fresh = fetch_news(sources=news_sources, since_dt=today)
        result["fresh_news"] = fresh
        log.info("조간 뉴스: %d건 (06:00 이후)", len(fresh))
    except Exception as e:
        log.warning("조간 뉴스 수집 실패: %s", e)

    # ── NXT 프리장 가격 (초벌 후보 종목만) ────────────────────────────────
    if prelim_symbols and kis_client:
        for sym in prelim_symbols:
            try:
                pd = kis_client.get_price(sym)
                cur_px = float(pd.get("stck_prpr", 0) or 0)
                prdy_clpr = float(pd.get("prdy_clpr", cur_px) or cur_px)
                acml_tr_pbmn = int(pd.get("acml_tr_pbmn", 0) or 0)
                gap_pct = (cur_px / prdy_clpr - 1) * 100 if prdy_clpr > 0 else 0
                result["nxt_prices"][sym] = {
                    "price": cur_px,
                    "gap_pct": round(gap_pct, 2),
                    "amount_bn": round(acml_tr_pbmn / 1e8, 2),
                    "prev_close": prdy_clpr,
                }
                log.info("NXT [%s]: %s원 (갭 %+.2f%%, 거래대금 %.1f억)", sym, f"{int(cur_px):,}", gap_pct, acml_tr_pbmn / 1e8)
            except Exception as e:
                log.warning("NXT 가격 조회 실패 [%s]: %s", sym, e)

    return result


def _ticker_change_pct(yf, ticker: str) -> float | None:
    hist = yf.Ticker(ticker).history(period="2d", interval="1d")
    if len(hist) < 2:
        return None
    return float((hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100)


def _fetch_us_sector_moves(yf) -> list[dict]:
    """미국장 섹터 프록시를 국내 후보 선정용 신호로 요약."""
    groups = {
        "반도체/AI": ["SOXX", "SMH", "NVDA", "AMD"],
        "전기차/2차전지": ["TSLA", "LIT"],
        "바이오/헬스케어": ["IBB", "XBI"],
    }
    moves: list[dict] = []
    for name, tickers in groups.items():
        evidence = []
        vals = []
        for ticker in tickers:
            try:
                chg = _ticker_change_pct(yf, ticker)
            except Exception as e:
                log.debug("미국 섹터 프록시 조회 실패 [%s]: %s", ticker, e)
                continue
            if chg is None:
                continue
            vals.append(chg)
            evidence.append(f"{ticker} {chg:+.2f}%")
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        if abs(avg) >= 0.6 or any(abs(v) >= 1.0 for v in vals):
            moves.append({
                "name": name,
                "chg_pct": round(float(avg), 2),
                "evidence": evidence,
            })
    moves.sort(key=lambda x: abs(float(x.get("chg_pct", 0) or 0)), reverse=True)
    return moves


def format_us_market(us_market: dict | None) -> str:
    """미국장 요약 텍스트 (Moderator 프롬프트용)."""
    if not us_market:
        return "미국 시장 데이터 없음"
    sp = us_market.get("sp500_chg_pct")
    nq = us_market.get("nasdaq_chg_pct")
    parts = []
    if sp is not None:
        parts.append(f"S&P500 {sp:+.2f}%")
    if nq is not None:
        parts.append(f"NASDAQ {nq:+.2f}%")
    for move in us_market.get("sector_moves") or []:
        try:
            chg = float(move.get("chg_pct", 0) or 0)
        except Exception:
            continue
        ev = ", ".join(str(x) for x in (move.get("evidence") or [])[:4])
        parts.append(f"{move.get('name')}: {chg:+.2f}% ({ev})")
    return ", ".join(parts) if parts else "미국 시장 데이터 없음"


def format_nxt_prices(nxt_prices: dict, prelim_candidates: list) -> str:
    """NXT 프리장 가격 요약 텍스트 (Moderator 프롬프트용)."""
    if not nxt_prices:
        return "NXT 프리장 데이터 없음"
    lines = []
    for cand in prelim_candidates:
        sym = cand.symbol if hasattr(cand, "symbol") else cand.get("symbol", "")
        name = cand.name if hasattr(cand, "name") else cand.get("name", sym)
        info = nxt_prices.get(sym)
        if info:
            gap = info.get("gap_pct", 0)
            amt = info.get("amount_bn", 0)
            lines.append(f"- {name}({sym}): 갭 {gap:+.2f}% / 거래대금 {amt:.1f}억")
        else:
            lines.append(f"- {name}({sym}): NXT 데이터 없음")
    return "\n".join(lines) if lines else "NXT 프리장 데이터 없음"
