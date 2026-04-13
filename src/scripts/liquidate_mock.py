"""모의 계좌 전체 보유주식 시장가 청산."""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import load_config
from src.data.kis_client import KisClient

def main():
    cfg = load_config()
    kis = KisClient(cfg.kis)

    print(f"계좌: {cfg.kis.account_no[:4]}**** (account_type={cfg.kis.account_type})")
    print(f"API: {cfg.kis.base_url}")

    data = kis.get_balance()
    holdings = data.get("output1", [])

    if not holdings:
        print("보유주식 없음")
        return

    print(f"\n보유주식 {len(holdings)}개 청산 시작...")
    for h in holdings:
        symbol = h.get("pdno", "")
        name = h.get("prdt_name", symbol)
        qty = int(h.get("hldg_qty", 0) or 0)
        if qty <= 0:
            continue
        print(f"  [{symbol}] {name} {qty}주 매도 중...", end=" ", flush=True)
        try:
            result = kis.sell_market(symbol, qty)
            print(f"완료 ({result.get('msg1', '')})")
        except Exception as e:
            print(f"실패: {e}")

    print("\n청산 완료. positions.json 초기화...")
    state_path = PROJECT_ROOT / "state" / "positions.json"
    state_path.write_text("[]")
    print("완료")

if __name__ == "__main__":
    main()
