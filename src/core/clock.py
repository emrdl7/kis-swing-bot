"""장 시간 판단 유틸리티."""
from __future__ import annotations
from datetime import datetime, time
import pytz

KST = pytz.timezone("Asia/Seoul")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)
ENTRY_ALLOWED_FROM = time(9, 5)   # 장 시작 5분 후부터 매수 허용 (호가 갭 회피)
PRE_MARKET_OPEN = time(8, 0)    # NXT 시작
NXT_AFTER_HOURS = time(16, 0)   # NXT 장후 시작
NXT_CLOSE = time(18, 0)         # NXT 종료


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def is_regular_market(dt: datetime | None = None) -> bool:
    """정규장 여부 (09:00 ~ 15:30)."""
    t = (dt or now_kst()).time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def is_entry_allowed(dt: datetime | None = None) -> bool:
    """매수 진입 허용 시간 여부 (09:05 ~ 15:20, 장 초반 호가 갭 회피)."""
    t = (dt or now_kst()).time()
    return ENTRY_ALLOWED_FROM <= t <= time(15, 20)


def is_pre_market(dt: datetime | None = None) -> bool:
    """장 전 (08:00 ~ 09:00)."""
    t = (dt or now_kst()).time()
    return PRE_MARKET_OPEN <= t < MARKET_OPEN


def is_nxt_after_hours(dt: datetime | None = None) -> bool:
    """NXT 장후 (16:00 ~ 18:00)."""
    t = (dt or now_kst()).time()
    return NXT_AFTER_HOURS <= t < NXT_CLOSE


def minutes_to_close(dt: datetime | None = None) -> int:
    """장 마감까지 남은 분 수 (정규장 외에는 0)."""
    dt = dt or now_kst()
    if not is_regular_market(dt):
        return 0
    close_dt = dt.replace(hour=15, minute=30, second=0, microsecond=0)
    return max(0, int((close_dt - dt).total_seconds() / 60))


def hhmm_to_time(hhmm: int) -> time:
    return time(hhmm // 100, hhmm % 100)
