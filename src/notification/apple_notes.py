"""Apple Notes 알림 — osascript로 노트 생성."""
from __future__ import annotations
import logging
import os
import subprocess
import tempfile
from datetime import datetime

log = logging.getLogger(__name__)

NOTES_FOLDER = "KIS-Swing-Bot"


def create_note(title: str, body: str, folder: str = NOTES_FOLDER) -> bool:
    """Apple Notes에 노트 생성.

    임시 AppleScript 파일 방식으로 줄바꿈 정상 처리.
    """
    # AppleScript 내부 문자열 이스케이프 (따옴표·역슬래시만)
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    # 본문의 줄바꿈을 AppleScript return 문자로 변환
    body_lines = body.split("\n")
    body_as = " & return & ".join(f'"{_esc(line)}"' for line in body_lines)

    script = f'''tell application "Notes"
    if not (exists folder "{_esc(folder)}") then
        make new folder with properties {{name:"{_esc(folder)}"}}
    end if
    set targetFolder to folder "{_esc(folder)}"
    make new note at targetFolder with properties {{name:"{_esc(title)}", body:{body_as}}}
end tell'''

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".applescript", delete=False, encoding="utf-8"
        ) as f:
            f.write(script)
            tmp_path = f.name

        result = subprocess.run(
            ["osascript", tmp_path],
            capture_output=True, text=True, timeout=15,
        )
        os.unlink(tmp_path)

        if result.returncode != 0:
            log.error("Apple Notes 생성 실패: %s", result.stderr.strip())
            return False
        log.info("Apple Notes 저장: [%s] %s", folder, title)
        return True
    except Exception as e:
        log.error("Apple Notes 오류: %s", e)
        return False


def report_debate(transcript: str, today: str) -> bool:
    """토론 전 과정 및 결론을 노트로 저장."""
    title = f"[토론] {today} 종목발굴 상세보고"
    return create_note(title, transcript)


def report_morning_screen(candidates: list[dict], today: str) -> bool:
    """장 전 발굴 요약 보고."""
    lines = [f"[{today}] 스윙봇 장 전 발굴 보고", ""]
    if not candidates:
        lines.append("오늘 발굴된 후보 없음")
    else:
        for i, c in enumerate(candidates, 1):
            exp = c.get("expires_at", "")[:10] if c.get("expires_at") else "-"
            tp_pct = (c.get("target_price", 0) / c.get("entry_high", 1) - 1) * 100
            sl_pct = (1 - c.get("stop_price", 0) / c.get("entry_low", 1)) * 100
            lines += [
                f"{i}. {c.get('name', '')} ({c.get('symbol', '')})",
                f"   진입: {int(c.get('entry_low', 0)):,} ~ {int(c.get('entry_high', 0)):,}원",
                f"   목표: {int(c.get('target_price', 0)):,}원 (+{tp_pct:.1f}%)  |  손절: {int(c.get('stop_price', 0)):,}원 (-{sl_pct:.1f}%)",
                f"   신뢰도: {c.get('consensus_score', 0):.0%}  |  만료: {exp}",
                f"   근거: {c.get('rationale', '')[:150]}",
                "",
            ]
    title = f"[장전] {today} 스윙봇 발굴"
    return create_note(title, "\n".join(lines))


def report_eod(positions_closed: list[dict], daily_pnl: int, today: str) -> bool:
    """장 마감 보고."""
    lines = [f"[{today}] 스윙봇 장 마감 보고", ""]
    lines.append(f"오늘 실현 손익: {daily_pnl:+,}원")
    lines.append("")

    if positions_closed:
        lines.append("=== 오늘 청산 내역 ===")
        for p in positions_closed:
            pnl = p.get("pnl_amount", 0)
            lines += [
                f"- {p.get('name', '')} ({p.get('symbol', '')})",
                f"  매수: {p.get('avg_price', 0):,.0f}원  →  매도: {p.get('close_price', 0):,.0f}원",
                f"  사유: {p.get('close_reason', '')}  PnL: {pnl:+,}원",
                "",
            ]
    else:
        lines.append("오늘 청산 없음")

    title = f"[장마감] {today} 스윙봇 보고"
    return create_note(title, "\n".join(lines))


def report_trade(action: str, symbol: str, name: str, price: float,
                 qty: int, extra: str = "") -> bool:
    """매수/매도 발생 즉시 보고."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"시각: {now_str}",
        f"종목: {name} ({symbol})",
        f"가격: {int(price):,}원  수량: {qty}주",
    ]
    if extra:
        lines.append(extra)
    title = f"[{action}] {now_str} {name}({symbol})"
    return create_note(title, "\n".join(lines))
