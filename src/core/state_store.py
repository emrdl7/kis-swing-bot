"""JSON 기반 영속 상태 저장소."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).parent.parent.parent / "state"
STATE_DIR.mkdir(exist_ok=True)


def _path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def load(name: str, default: Any = None) -> Any:
    p = _path(name)
    if not p.exists():
        return default if default is not None else {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save(name: str, data: Any) -> None:
    p = _path(name)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def load_candidates() -> list[dict]:
    return load("candidates", [])


def save_candidates(candidates: list[dict]) -> None:
    save("candidates", candidates)


def load_positions() -> list[dict]:
    return load("positions", [])


def save_positions(positions: list[dict]) -> None:
    save("positions", positions)


def load_watchlist() -> list[dict]:
    return load("watchlist", [])


def save_watchlist(watchlist: list[dict]) -> None:
    save("watchlist", watchlist)


def load_token_cache() -> dict:
    return load("token_cache", {})


def save_token_cache(data: dict) -> None:
    save("token_cache", data)


def load_reserves() -> list[dict]:
    return load("reserves", [])


def save_reserves(reserves: list[dict]) -> None:
    save("reserves", reserves)


def load_daily_stats() -> dict:
    return load("daily_stats", {"date": "", "realized_pnl": 0.0, "trade_count": 0})


def save_daily_stats(data: dict) -> None:
    save("daily_stats", data)


def load_realtime_prices() -> dict:
    return load("realtime_prices", {})


def save_realtime_prices(data: dict) -> None:
    save("realtime_prices", data)


def load_pre_open_orders() -> dict:
    """동시호가 사전 손절 주문 트래킹 (symbol → {order_no, qty, stop_price, date})."""
    return load("pre_open_orders", {})


def save_pre_open_orders(data: dict) -> None:
    save("pre_open_orders", data)


def load_evening_candidates() -> dict:
    """저녁 선분석 결과 (date, generated_at, prelim_candidates, debate_log)."""
    return load("evening_candidates", {})


def save_evening_candidates(data: dict) -> None:
    save("evening_candidates", data)
