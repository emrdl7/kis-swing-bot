"""RSS/웹 뉴스 수집기 — 당일 뉴스만 필터링."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser

log = logging.getLogger(__name__)

DEFAULT_SOURCES = [
    "https://feeds.finance.naver.com/api/rss/marketNews.nhn",
    "https://www.hankyung.com/feed/all-news",
    "https://rss.donga.com/economy.xml",
]


def fetch_news(
    sources: list[str] | None = None,
    max_age_hours: int = 24,
) -> list[dict]:
    """RSS 피드에서 뉴스 수집 후 max_age_hours 이내 기사만 반환."""
    sources = sources or DEFAULT_SOURCES
    now = datetime.now()
    cutoff = now - timedelta(hours=max_age_hours)
    items: list[dict] = []

    for url in sources:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                pub_dt = _parse_date(entry)
                if pub_dt is not None and pub_dt < cutoff:
                    continue  # 오래된 뉴스 스킵

                items.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", entry.get("description", "")),
                    "link": entry.get("link", ""),
                    "published_at": pub_dt.isoformat() if pub_dt else "",
                    "source": url,
                })
        except Exception as e:
            log.warning("뉴스 수집 실패 [%s]: %s", url, e)

    # 중복 제거 (제목 기준)
    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        key = item["title"].strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    log.info("뉴스 수집 완료: %d건 (max_age=%dh)", len(unique), max_age_hours)
    return unique


def _parse_date(entry) -> Optional[datetime]:
    """feedparser entry에서 published datetime 추출."""
    # feedparser가 파싱한 struct_time 우선
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        import calendar
        ts = calendar.timegm(entry.published_parsed)
        return datetime.utcfromtimestamp(ts)

    # 원본 문자열 fallback
    raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
    if raw:
        try:
            return parsedate_to_datetime(raw).replace(tzinfo=None)
        except Exception:
            pass
    return None


def format_for_llm(news_items: list[dict], max_items: int = 30) -> str:
    """LLM 프롬프트용 뉴스 텍스트. 최신 뉴스에 [HOT] 태그 부여."""
    if not news_items:
        return "오늘 수집된 뉴스 없음"
    now = datetime.now()
    lines = []
    for item in news_items[:max_items]:
        pub = item.get("published_at", "")[:16]  # YYYY-MM-DDTHH:MM
        # 시간 가중치 태그
        tag = ""
        try:
            pub_dt = datetime.fromisoformat(item["published_at"]) if item.get("published_at") else None
            if pub_dt:
                age_h = (now - pub_dt).total_seconds() / 3600
                if age_h <= 6:
                    tag = "[HOT] "
                elif age_h >= 18:
                    tag = "[OLD] "
        except Exception:
            pass
        lines.append(f"- {tag}[{pub}] {item['title']}")
        if item.get("summary"):
            lines.append(f"  {item['summary'][:100]}")
    return "\n".join(lines)
