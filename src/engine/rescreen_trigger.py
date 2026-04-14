"""후보 재탐색(토론) 트리거.

- 자동: monitor 틱에서 후보 수 임계 미만일 때 호출
- 수동: 대시보드 버튼 → POST /api/rescreen

가드:
 * 하루 최대 MAX_PER_DAY회
 * 직전 실행 후 COOLDOWN_MIN 분 이상 경과
 * 09:00~09:10 (모닝 스크린 직후) 스킵
 * 14:30 이후 스킵 (진입 시간 부족)
 * 동시 실행 방지 (락 파일)
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
STATE_FILE = PROJECT_ROOT / "state" / "rescreen_state.json"
LOCK_FILE = PROJECT_ROOT / "state" / "rescreen.lock"
SCRIPT = PROJECT_ROOT / "src" / "scripts" / "run_morning_screen.py"
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"

MAX_PER_DAY = 2
COOLDOWN_MIN = 120
LOW_CAND_THRESHOLD = 1        # 이 수 이하면 자동 트리거
STALE_CAND_DAYS = 2           # 후보가 이만큼 묵으면 슬롯 차있어도 재토론 유도
BLACKOUT_START = time(9, 0)   # 이 시각부터
BLACKOUT_END = time(9, 10)    # 이 시각까지 스킵 (모닝 직후)
CUTOFF = time(14, 30)         # 이후는 진입 시간 부족 → 스킵


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"date": "", "count": 0, "last_run": ""}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"date": "", "count": 0, "last_run": ""}


def _save_state(data: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_locked() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        LOCK_FILE.unlink(missing_ok=True)
        return False


def should_rescreen(
    now: datetime,
    cand_count: int,
    manual: bool = False,
    slots_full: bool = False,
    oldest_cand_age_hours: float = 0.0,
) -> tuple[bool, str]:
    """재토론 가능 여부 + 사유.

    자동 트리거 조건 (manual=False):
     - 후보 수 ≤ LOW_CAND_THRESHOLD, 또는
     - 슬롯이 꽉 찼고 가장 오래된 후보 경과가 STALE_CAND_DAYS 이상
    manual=True 면 임계값 조건 무시, 나머지 가드는 적용.
    """
    if _is_locked():
        return False, "이미 실행 중"
    t = now.time()
    if BLACKOUT_START <= t <= BLACKOUT_END:
        return False, "모닝 스크린 직후 시간대"
    if t >= CUTOFF:
        return False, "장 마감 임박 (14:30 이후)"

    if not manual:
        low = cand_count <= LOW_CAND_THRESHOLD
        stale = slots_full and oldest_cand_age_hours >= STALE_CAND_DAYS * 24
        if not (low or stale):
            return False, (
                f"후보 {cand_count}개, 슬롯 full={slots_full}, "
                f"최고령 {oldest_cand_age_hours:.1f}h — 임계 미달"
            )

    today = now.strftime("%Y-%m-%d")
    st = _load_state()
    if st.get("date") == today and st.get("count", 0) >= MAX_PER_DAY:
        return False, f"오늘 {MAX_PER_DAY}회 한도 초과"
    last_run_str = st.get("last_run", "")
    if st.get("date") == today and last_run_str:
        try:
            last = datetime.fromisoformat(last_run_str)
            elapsed_min = (now - last).total_seconds() / 60
            if elapsed_min < COOLDOWN_MIN:
                return False, f"쿨다운 {int(COOLDOWN_MIN - elapsed_min)}분 남음"
        except Exception:
            pass
    return True, "OK"


def trigger_rescreen(now: datetime | None = None, manual: bool = False) -> dict:
    """스크린 스크립트를 백그라운드 실행. 결과 dict 반환."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    # 락 생성
    LOCK_FILE.parent.mkdir(exist_ok=True)
    log_path = PROJECT_ROOT / "logs" / "morning_screen.log"
    try:
        proc = subprocess.Popen(
            [str(PYTHON), str(SCRIPT)],
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1",
                 "PYTHONPATH": str(PROJECT_ROOT),
                 "KIS_RESCREEN_MODE": "intraday"},
            start_new_session=True,
        )
        LOCK_FILE.write_text(str(proc.pid))
    except Exception as e:
        log.error("rescreen 실행 실패: %s", e)
        return {"ok": False, "error": str(e)}

    # 상태 업데이트
    st = _load_state()
    if st.get("date") != today:
        st = {"date": today, "count": 0, "last_run": ""}
    st["count"] = int(st.get("count", 0)) + 1
    st["last_run"] = now.isoformat()
    st["last_manual"] = bool(manual)
    _save_state(st)
    log.info("재토론 스크린 실행 (pid=%d, manual=%s, 오늘 %d회)", proc.pid, manual, st["count"])
    return {"ok": True, "pid": proc.pid, "count_today": st["count"], "manual": manual}


def cleanup_stale_lock() -> None:
    """종료된 프로세스가 남긴 락 파일 정리 (부팅/재시작 시 호출)."""
    _is_locked()  # 자동 정리
