"""Apple Notes 알림 — osascript + HTML body (줄바꿈 정상 처리)."""
from __future__ import annotations
import logging
import os
import subprocess
import tempfile
from datetime import datetime

log = logging.getLogger(__name__)

NOTES_FOLDER = "KIS-Swing-Bot"
# AppleScript body 최대 길이 (초과 시 분할)
MAX_BODY_CHARS = 30_000


def _to_html(text: str) -> str:
    """일반 텍스트 → Apple Notes HTML 변환."""
    lines = text.split("\n")
    html_lines = []
    for line in lines:
        escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if escaped.startswith("# "):
            html_lines.append(f"<h1>{escaped[2:]}</h1>")
        elif escaped.startswith("## "):
            html_lines.append(f"<h2>{escaped[3:]}</h2>")
        elif escaped.startswith("### "):
            html_lines.append(f"<h3>{escaped[4:]}</h3>")
        elif escaped.startswith("**") and escaped.endswith("**") and len(escaped) > 4:
            html_lines.append(f"<p><b>{escaped[2:-2]}</b></p>")
        elif escaped.startswith("- "):
            html_lines.append(f"<li>{escaped[2:]}</li>")
        elif escaped == "":
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p>{escaped}</p>")
    return "<html><body>" + "".join(html_lines) + "</body></html>"


def _esc_as(s: str) -> str:
    """AppleScript 문자열 이스케이프 (따옴표·역슬래시)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run_script(script: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".applescript", delete=False, encoding="utf-8"
        ) as f:
            f.write(script)
            tmp = f.name
        result = subprocess.run(
            ["osascript", tmp],
            capture_output=True, text=True, timeout=20,
        )
        os.unlink(tmp)
        if result.returncode != 0:
            log.error("Apple Notes 오류: %s", result.stderr.strip()[:200])
            return False
        return True
    except Exception as e:
        log.error("Apple Notes 예외: %s", e)
        return False


def create_note(title: str, body: str, folder: str = NOTES_FOLDER) -> bool:
    """Apple Notes에 노트 생성 (HTML body, 긴 내용은 분할)."""
    html_body = _to_html(body)

    # 길이 초과 시 분할
    if len(html_body) > MAX_BODY_CHARS:
        return _create_note_chunked(title, body, html_body, folder)

    return _create_note_single(title, html_body, folder)


def _create_note_single(title: str, html_body: str, folder: str) -> bool:
    script = f'''tell application "Notes"
    if not (exists folder "{_esc_as(folder)}") then
        make new folder with properties {{name:"{_esc_as(folder)}"}}
    end if
    make new note at folder "{_esc_as(folder)}" with properties {{name:"{_esc_as(title)}", body:"{_esc_as(html_body)}"}}
end tell'''
    ok = _run_script(script)
    if ok:
        log.info("Apple Notes 저장: [%s] %s", folder, title)
    return ok


def _create_note_chunked(title: str, body: str, html_body: str, folder: str) -> bool:
    """긴 내용을 파트로 분할하여 여러 노트 생성."""
    # 텍스트 기준으로 분할 (HTML 변환 전)
    lines = body.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > MAX_BODY_CHARS // 3 and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))

    ok = True
    for i, chunk in enumerate(chunks, 1):
        part_title = f"{title} (파트 {i}/{len(chunks)})"
        ok = _create_note_single(part_title, _to_html(chunk), folder) and ok
    return ok


def report_debate(transcript: str, today: str) -> bool:
    """토론 전 과정 및 결론 노트 저장."""
    title = f"[토론] {today} 종목발굴 상세보고"
    return create_note(title, transcript)


def report_morning_screen(candidates: list[dict], today: str) -> bool:
    """장 전 발굴 요약 보고."""
    lines = [f"# [{today}] 스윙봇 장 전 발굴 보고", ""]
    if not candidates:
        lines.append("오늘 발굴된 후보 없음")
    else:
        for i, c in enumerate(candidates, 1):
            exp = c.get("expires_at", "")[:10] if c.get("expires_at") else "-"
            entry_low = c.get("entry_low", 0)
            entry_high = c.get("entry_high", 1)
            target = c.get("target_price", 0)
            stop = c.get("stop_price", 0)
            tp_pct = (target / entry_high - 1) * 100 if entry_high else 0
            sl_pct = (1 - stop / entry_low) * 100 if entry_low else 0
            lines += [
                f"## {i}. {c.get('name', '')} ({c.get('symbol', '')})",
                f"- 진입: {int(entry_low):,} ~ {int(entry_high):,}원",
                f"- 목표: {int(target):,}원 (+{tp_pct:.1f}%)",
                f"- 손절: {int(stop):,}원 (-{sl_pct:.1f}%)",
                f"- 신뢰도: {c.get('consensus_score', 0):.0%}  |  만료: {exp}",
                f"- 근거: {c.get('rationale', '')[:200]}",
                "",
            ]
    return create_note(f"[장전] {today} 스윙봇 발굴", "\n".join(lines))


def report_eod(positions_closed: list[dict], daily_pnl: int, today: str) -> bool:
    """장 마감 보고 (raw data, 하위 호환용)."""
    lines = [f"# [{today}] 스윙봇 장 마감 보고", ""]
    lines.append(f"## 오늘 실현 손익: {daily_pnl:+,}원")
    lines.append("")

    if positions_closed:
        lines.append("## 오늘 청산 내역")
        for p in positions_closed:
            pnl = p.get("pnl_amount", 0)
            lines += [
                f"### {p.get('name', '')} ({p.get('symbol', '')})",
                f"- 매수: {p.get('avg_price', 0):,.0f}원  →  매도: {p.get('close_price', 0):,.0f}원",
                f"- 사유: {p.get('close_reason', '')}  PnL: {pnl:+,}원",
                "",
            ]
    else:
        lines.append("오늘 청산 없음")

    return create_note(f"[장마감] {today} 스윙봇 보고", "\n".join(lines))


def report_eod_analysis(analysis: str, daily_pnl: int, today: str) -> bool:
    """LLM이 작성한 장 마감 분석 보고서."""
    pnl_sign = "+" if daily_pnl >= 0 else ""
    header = f"# [{today}] 장 마감 투자 분석 보고\n\n**오늘 실현 손익: {pnl_sign}{daily_pnl:,}원**\n\n---\n\n"
    body = header + analysis
    return create_note(f"[장마감] {today} 투자 분석", body)


def report_trade(action: str, symbol: str, name: str, price: float,
                 qty: int, extra: str = "") -> bool:
    """매수/매도 발생 즉시 보고."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# [{action}] {name} ({symbol})",
        "",
        f"- 시각: {now_str}",
        f"- 가격: {int(price):,}원  수량: {qty}주",
    ]
    if extra:
        for line in extra.split("\n"):
            lines.append(f"- {line}" if not line.startswith("-") else line)
    return create_note(f"[{action}] {now_str} {name}({symbol})", "\n".join(lines))
