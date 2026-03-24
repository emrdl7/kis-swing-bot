"""Apple Notes 알림 — osascript로 노트 생성."""
from __future__ import annotations
import logging
import subprocess
from datetime import datetime

log = logging.getLogger(__name__)

NOTES_FOLDER = "KIS-Swing-Bot"  # Apple Notes 폴더명


def _escape(text: str) -> str:
    """AppleScript 문자열 이스케이프."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def create_note(title: str, body: str, folder: str = NOTES_FOLDER) -> bool:
    """Apple Notes에 노트 생성.

    Args:
        title: 노트 제목
        body: 노트 본문
        folder: 저장할 폴더명 (없으면 자동 생성)
    Returns:
        True if success
    """
    escaped_title = _escape(title)
    escaped_body = _escape(body)
    escaped_folder = _escape(folder)

    script = f'''
tell application "Notes"
    -- 폴더 확인/생성
    if not (exists folder "{escaped_folder}") then
        make new folder with properties {{name:"{escaped_folder}"}}
    end if
    set targetFolder to folder "{escaped_folder}"
    -- 노트 생성
    make new note at targetFolder with properties {{name:"{escaped_title}", body:"{escaped_body}"}}
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.error("Apple Notes 생성 실패: %s", result.stderr.strip())
            return False
        log.info("Apple Notes 저장: [%s] %s", folder, title)
        return True
    except Exception as e:
        log.error("Apple Notes 오류: %s", e)
        return False


def report_morning_screen(candidates: list[dict], today: str) -> bool:
    """장 전 발굴 보고."""
    lines = [f"[{today}] 스윙봇 장 전 발굴 보고\n"]
    if not candidates:
        lines.append("오늘 발굴된 후보 없음")
    else:
        for i, c in enumerate(candidates, 1):
            exp = c.get("expires_at", "")[:10] if c.get("expires_at") else "-"
            lines += [
                f"{i}. {c.get('name', '')} ({c.get('symbol', '')})",
                f"   진입: {int(c.get('entry_low', 0)):,} ~ {int(c.get('entry_high', 0)):,}원",
                f"   목표: {int(c.get('target_price', 0)):,}원  |  손절: {int(c.get('stop_price', 0)):,}원",
                f"   신뢰도: {c.get('consensus_score', 0):.0%}  |  만료: {exp}",
                f"   근거: {c.get('rationale', '')[:120]}",
                "",
            ]
    title = f"[장전] {today} 스윙봇 발굴"
    return create_note(title, "\n".join(lines))


def report_eod(positions_closed: list[dict], daily_pnl: int, today: str) -> bool:
    """장 마감 보고."""
    lines = [f"[{today}] 스윙봇 장 마감 보고\n"]
    lines.append(f"오늘 실현 손익: {daily_pnl:+,}원\n")

    if positions_closed:
        lines.append("=== 오늘 청산 내역 ===")
        for p in positions_closed:
            pnl = p.get("pnl_amount", 0)
            lines += [
                f"- {p.get('name', '')} ({p.get('symbol', '')})",
                f"  매수: {p.get('avg_price', 0):,.0f}원  매도: {p.get('close_price', 0):,.0f}원",
                f"  이유: {p.get('close_reason', '')}  PnL: {pnl:+,}원",
                "",
            ]
    else:
        lines.append("오늘 청산 없음")

    title = f"[장마감] {today} 스윙봇 보고"
    return create_note(title, "\n".join(lines))


def report_trade(action: str, symbol: str, name: str, price: float,
                 qty: int, extra: str = "") -> bool:
    """매수/매도 발생 즉시 보고."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = (
        f"시각: {now}\n"
        f"종목: {name} ({symbol})\n"
        f"가격: {int(price):,}원  수량: {qty}주\n"
    )
    if extra:
        body += f"{extra}\n"
    title = f"[{action}] {now} {name}({symbol})"
    return create_note(title, body)
