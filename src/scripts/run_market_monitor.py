"""장중 모니터 실행 스크립트 (launchd KeepAlive).

launchd ai.kis.swing.monitor.plist 에 의해 항상 실행 유지됨.
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import logging

from src.core.config import load_config
from src.engine.monitor import MarketMonitor
from src.utils.logging_setup import setup

log = setup("market_monitor")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="주문 없이 시뮬레이션")
    args = parser.parse_args()

    cfg = load_config()

    if not cfg.kis.app_key:
        log.error("KIS_APP_KEY 설정 없음. config/.env 확인")
        sys.exit(1)

    if not cfg.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY 없음 — LLM 기능 비활성")

    monitor = MarketMonitor(cfg, dry_run=args.dry_run)
    monitor.run_forever()


if __name__ == "__main__":
    main()
