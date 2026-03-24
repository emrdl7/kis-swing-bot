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


def load_daily_stats() -> dict:
    return load("daily_stats", {"date": "", "realized_pnl": 0.0, "trade_count": 0})


def save_daily_stats(data: dict) -> None:
    save("daily_stats", data)
