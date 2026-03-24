"""KIS Open API 클라이언트 (토큰, 시세, 주문, 잔고)."""
from __future__ import annotations
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

from src.core.config import KisConfig
from src.core import state_store

log = logging.getLogger(__name__)

_TOKEN_MARGIN_SEC = 300   # 만료 5분 전 갱신


class KisClient:
    def __init__(self, cfg: KisConfig):
        self.cfg = cfg
        self._access_token: str = ""
        self._token_expires_at: datetime = datetime.min
        self._client = httpx.Client(timeout=10.0)
        self._load_cached_token()

    # ── 토큰 관리 ──────────────────────────────────────────────────────────

    def _load_cached_token(self) -> None:
        cache = state_store.load_token_cache()
        token = cache.get("access_token", "")
        expires_str = cache.get("expires_at", "")
        if token and expires_str:
            try:
                expires_at = datetime.fromisoformat(expires_str)
                if expires_at > datetime.now() + timedelta(seconds=_TOKEN_MARGIN_SEC):
                    self._access_token = token
                    self._token_expires_at = expires_at
                    log.debug("토큰 캐시 로드 성공 (만료: %s)", expires_at.strftime("%H:%M"))
            except Exception:
                pass

    def _save_token_cache(self) -> None:
        state_store.save_token_cache({
            "access_token": self._access_token,
            "expires_at": self._token_expires_at.isoformat(),
        })

    def ensure_token(self) -> None:
        if self._access_token and datetime.now() < self._token_expires_at - timedelta(seconds=_TOKEN_MARGIN_SEC):
            return
        self._issue_token()

    def _issue_token(self) -> None:
        url = f"{self.cfg.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
        }
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)
        self._save_token_cache()
        log.info("토큰 발급 완료 (만료: %s)", self._token_expires_at.strftime("%H:%M"))

    # ── 공통 헤더 ──────────────────────────────────────────────────────────

    def _headers(self, tr_id: str, hashkey: Optional[str] = None) -> dict:
        h = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token}",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _hashkey(self, body: dict) -> str:
        url = f"{self.cfg.base_url}/uapi/hashkey"
        resp = self._client.post(url, headers={
            "Content-Type": "application/json",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
        }, json=body)
        resp.raise_for_status()
        return resp.json().get("HASH", "")

    # ── 시세 조회 ──────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> dict:
        """현재가 조회."""
        self.ensure_token()
        tr_id = "FHKST01010100"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        resp = self._client.get(url, headers=self._headers(tr_id), params=params)
        resp.raise_for_status()
        return resp.json().get("output", {})

    def get_daily_ohlcv(self, symbol: str, count: int = 20) -> list[dict]:
        """일봉 데이터 조회 (최근 count일)."""
        self.ensure_token()
        tr_id = "FHKST01010400"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
            "FID_INPUT_DATE_1": "",
            "FID_INPUT_DATE_2": today,
        }
        resp = self._client.get(url, headers=self._headers(tr_id), params=params)
        resp.raise_for_status()
        output = resp.json().get("output2", []) or []
        return output[:count]

    def get_nxt_price(self, symbol: str) -> dict:
        """NXT(야간) 현재가 조회."""
        self.ensure_token()
        tr_id = "FHKST01010100"  # 동일 TR, 시간대로 NXT 자동 반영
        return self.get_price(symbol)

    def get_balance(self) -> dict:
        """계좌 잔고 조회."""
        self.ensure_token()
        is_real = self.cfg.account_type == "01"
        tr_id = "TTTC8434R" if is_real else "VTTC8434R"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self.cfg.account_no[8:] if len(self.cfg.account_no) > 8 else "01",
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = self._client.get(url, headers=self._headers(tr_id), params=params)
        resp.raise_for_status()
        return resp.json()

    def get_cash(self) -> int:
        """주문 가능 현금 조회 (원)."""
        data = self.get_balance()
        output2 = data.get("output2", [{}])
        if output2:
            return int(output2[0].get("dnca_tot_amt", 0))
        return 0

    # ── 주문 ───────────────────────────────────────────────────────────────

    def buy_market(self, symbol: str, qty: int) -> dict:
        """시장가 매수."""
        self.ensure_token()
        is_real = self.cfg.account_type == "01"
        tr_id = "TTTC0802U" if is_real else "VTTC0802U"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self.cfg.account_no[8:] if len(self.cfg.account_no) > 8 else "01",
            "PDNO": symbol,
            "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "CTAC_TLNO": "",
            "SLL_TYPE": "01",
            "ALGO_NO": "",
        }
        hk = self._hashkey(body)
        resp = self._client.post(url, headers=self._headers(tr_id, hashkey=hk), json=body)
        resp.raise_for_status()
        result = resp.json()
        log.info("매수 주문 [%s] qty=%d → %s", symbol, qty, result.get("msg1", ""))
        return result

    def sell_market(self, symbol: str, qty: int) -> dict:
        """시장가 매도."""
        self.ensure_token()
        is_real = self.cfg.account_type == "01"
        tr_id = "TTTC0801U" if is_real else "VTTC0801U"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self.cfg.account_no[8:] if len(self.cfg.account_no) > 8 else "01",
            "PDNO": symbol,
            "ORD_DVSN": "01",   # 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "CTAC_TLNO": "",
            "SLL_TYPE": "01",
            "ALGO_NO": "",
        }
        hk = self._hashkey(body)
        resp = self._client.post(url, headers=self._headers(tr_id, hashkey=hk), json=body)
        resp.raise_for_status()
        result = resp.json()
        log.info("매도 주문 [%s] qty=%d → %s", symbol, qty, result.get("msg1", ""))
        return result

    def get_positions(self) -> list[dict]:
        """현재 보유 종목 조회."""
        data = self.get_balance()
        return data.get("output1", []) or []

    def close(self) -> None:
        self._client.close()
