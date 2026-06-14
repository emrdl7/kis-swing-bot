"""운용 이벤트 JSONL 로거.

일반 텍스트 로그와 별개로 피봇 평가, 후보 승격, 진입, 청산을 구조화해서
state/operation_events.jsonl에 누적한다. 사후 성과 분석과 정책 개선 입력으로 사용한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core import state_store

log = logging.getLogger(__name__)

EVENT_LOG_PATH = Path(state_store.STATE_DIR) / "operation_events.jsonl"


def append_event(event_type: str, payload: dict[str, Any]) -> None:
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event_type": event_type,
        **payload,
    }
    try:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        log.warning("운용 이벤트 기록 실패 [%s]: %s", event_type, e)
