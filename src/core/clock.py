"""장 시간 판단 유틸리티."""
from __future__ import annotations
from datetime import datetime, time, date, timedelta
import pytz

# 한국 증시 휴장일 (주말 제외 공휴일)
_KRX_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 3, 1), date(2025, 5, 5), date(2025, 5, 6),
    date(2025, 6, 6), date(2025, 8, 15),
    date(2025, 10, 3), date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8), date(2025, 10, 9),
    date(2025, 12, 25), date(2025, 12, 31),
    # 2026
    date(2026, 1, 1), date(2026, 1, 27), date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),
    date(2026, 3, 1), date(2026, 3, 2),
    date(2026, 5, 5), date(2026, 5, 25),
    date(2026, 6, 6), date(2026, 8, 17),
    date(2026, 9, 24), date(2026, 9, 25), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 9),
    date(2026, 12, 25), date(2026, 12, 31),
}

KST = pytz.timezone("Asia/Seoul")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)
ENTRY_ALLOWED_FROM = time(9, 5)   # 장 시작 5분 후부터 매수 허용 (호가 갭 회피)
PRE_MARKET_OPEN = time(8, 0)    # NXT 시작
NXT_AFTER_HOURS = time(16, 0)   # NXT 장후 시작
NXT_CLOSE = time(18, 0)         # NXT 종료


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


_WEEKDAY_KR = "월화수목금토일"


def today_label(dt: datetime | None = None) -> str:
    """LLM 프롬프트용 날짜 라벨: '2026-05-04 (월요일, KST)'.

    LLM이 요일을 추측하지 않도록 명시적으로 요일·시간대 표기. 날짜 비교나
    파일명에는 쓰지 말 것 — 그 용도엔 now_kst().strftime('%Y-%m-%d') 사용.
    """
    n = dt or now_kst()
    wk = _WEEKDAY_KR[n.weekday()]
    return f"{n.strftime('%Y-%m-%d')} ({wk}요일, KST)"


def is_regular_market(dt: datetime | None = None) -> bool:
    """정규장 여부 (09:00 ~ 15:30)."""
    t = (dt or now_kst()).time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def is_entry_allowed(dt: datetime | None = None) -> bool:
    """매수 진입 허용 시간 여부 (09:05 ~ 15:20, 장 초반 호가 갭 회피)."""
    t = (dt or now_kst()).time()
    return ENTRY_ALLOWED_FROM <= t <= time(15, 20)


def is_trading_day(dt: datetime | None = None) -> bool:
    """오늘이 거래일(영업일)인지 확인."""
    d = (dt or now_kst()).date()
    return d.isoweekday() <= 5 and d not in _KRX_HOLIDAYS


def is_next_trading_day(dt: datetime | None = None) -> bool:
    """내일이 거래일(영업일)인지 확인."""
    d = (dt or now_kst()).date()
    tomorrow = d + timedelta(days=1)
    # 주말이거나 공휴일이면 False, 연속 확인 (예: 목→금→월)
    checked = tomorrow
    for _ in range(7):
        if checked.isoweekday() <= 5 and checked not in _KRX_HOLIDAYS:
            return checked == tomorrow  # 내일이 바로 거래일이면 True
        checked += timedelta(days=1)
    return False


def hhmm_to_time(hhmm: int) -> time:
    return time(hhmm // 100, hhmm % 100)


def is_pre_market(dt: datetime | None = None) -> bool:
    """장 전 (08:00 ~ 09:00)."""
    t = (dt or now_kst()).time()
    return PRE_MARKET_OPEN <= t < MARKET_OPEN


def is_open_call_auction(dt: datetime | None = None) -> bool:
    """KRX 시초 동시호가 (08:30 ~ 08:59). 사전 손절 지정가 주문 가능 시간."""
    t = (dt or now_kst()).time()
    return time(8, 30) <= t < time(9, 0)


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
