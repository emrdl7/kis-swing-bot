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
        # 모의 API(openapivts:29443)는 SSL 호스트명 불일치 → verify=False
        self._is_mock = "openapivts" in cfg.base_url
        self._client = httpx.Client(timeout=10.0, verify=not self._is_mock)
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

    def _get_with_retry(self, url: str, headers: dict, params: dict, retries: int = 2) -> dict:
        """GET 요청, 500 오류 시 최대 retries회 재시도."""
        for attempt in range(retries + 1):
            resp = self._client.get(url, headers=headers, params=params)
            if resp.status_code == 500 and attempt < retries:
                time.sleep(1)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def get_price(self, symbol: str) -> dict:
        """현재가 조회."""
        self.ensure_token()
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        return self._get_with_retry(url, self._headers("FHKST01010100"), params).get("output", {})

    def get_daily_ohlcv(self, symbol: str, count: int = 20) -> list[dict]:
        """일봉 데이터 조회 (최근 count일)."""
        self.ensure_token()
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
        output = self._get_with_retry(url, self._headers("FHKST01010400"), params).get("output2", []) or []
        return output[:count]

    def get_nxt_price(self, symbol: str) -> dict:
        """NXT(야간) 현재가 조회."""
        self.ensure_token()
        return self.get_price(symbol)

    def is_nxt_supported(self, symbol: str) -> bool:
        """NX 마켓코드로 조회 시 실제 가격 데이터가 있으면 NXT 지원 종목."""
        try:
            self.ensure_token()
            url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
            params = {"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": symbol}
            result = self._get_with_retry(url, self._headers("FHKST01010100"), params)
            px = float((result.get("output") or {}).get("stck_prpr", 0) or 0)
            return px > 0
        except Exception:
            return False

    def get_balance(self) -> dict:
        """계좌 잔고 조회."""
        self.ensure_token()
        tr_id = "VTTC8434R" if self._is_mock else "TTTC8434R"
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
        return self._get_with_retry(url, self._headers(tr_id), params)

    def get_cash(self) -> int:
        """주문 가능 현금 조회 (원)."""
        data = self.get_balance()
        output2 = data.get("output2", [{}])
        if output2:
            s = output2[0]
            # 당일 주문가능현금만 사용 (익일/총예수금은 실제 주문가능 금액보다 큼)
            for field in ("ord_psbl_cash", "prvs_rcdl_excc_amt"):
                v = s.get(field, 0)
                if v and int(v) > 0:
                    return int(v)
        return 0

    # ── 주문 ───────────────────────────────────────────────────────────────

    def _acnt_prdt_cd(self) -> str:
        return self.cfg.account_no[8:] if len(self.cfg.account_no) > 8 else "01"

    def _post_order_with_retry(self, tr_id: str, body: dict, retries: int = 2) -> dict:
        """주문 POST — 500 에러 시 재시도."""
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                hk = self._hashkey(body)
                resp = self._client.post(url, headers=self._headers(tr_id, hashkey=hk), json=body)
                if resp.status_code == 500 and attempt < retries:
                    log.warning("주문 500 에러, 재시도 (%d/%d)", attempt + 1, retries)
                    time.sleep(1)
                    continue
                resp.raise_for_status()
                result = resp.json()
                rt_cd = result.get("rt_cd", "0")
                msg = result.get("msg1", "")
                if rt_cd != "0":
                    raise RuntimeError(f"주문 거부: {msg}")
                return result
            except RuntimeError:
                raise
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(1)
        raise last_exc or RuntimeError("주문 실패")

    def buy_market(self, symbol: str, qty: int) -> dict:
        """시장가 매수. 동시호가 구간 등 시장가 거부 시 지정가(상한+1호가)로 자동 재시도."""
        self.ensure_token()
        tr_id = "VTTC0802U" if self._is_mock else "TTTC0802U"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "PDNO": symbol,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        try:
            result = self._post_order_with_retry(tr_id, body)
            log.info("매수 주문 [%s] qty=%d → %s", symbol, qty, result.get("msg1", ""))
            return result
        except RuntimeError as e:
            # 동시호가 또는 시장가 제한 상황 → 지정가(현재가 상한)로 즉시 재시도
            emsg = str(e)
            if "주문가능금액" in emsg or "시장가" in emsg or "단가" in emsg:
                try:
                    pd = self.get_price(symbol)
                    cur_px = float(pd.get("stck_prpr", 0) or 0)
                    ref = float(pd.get("stck_mxpr", 0) or 0) or cur_px  # 상한가 선호
                    if cur_px <= 0:
                        raise
                    # 체결 우선 → 상한가에 지정가
                    body2 = dict(body)
                    body2["ORD_DVSN"] = "00"
                    body2["ORD_UNPR"] = str(int(ref))
                    result = self._post_order_with_retry(tr_id, body2)
                    log.info(
                        "매수 주문(지정가 fallback) [%s] qty=%d @%d → %s",
                        symbol, qty, int(ref), result.get("msg1", ""),
                    )
                    return result
                except Exception as e2:
                    log.error("[%s] 지정가 fallback 실패: %s", symbol, e2)
            raise

    def sell_market(self, symbol: str, qty: int) -> dict:
        """시장가 매도."""
        self.ensure_token()
        tr_id = "VTTC0801U" if self._is_mock else "TTTC0801U"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "PDNO": symbol,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        result = self._post_order_with_retry(tr_id, body)
        log.info("매도 주문 [%s] qty=%d → %s", symbol, qty, result.get("msg1", ""))
        return result

    def sell_limit(self, symbol: str, qty: int, price: float) -> dict:
        """KRX 지정가 매도. 동시호가에 미리 손절가 걸어둘 때 사용.
        반환 dict의 output.ODNO 가 주문번호 (취소·정정에 필요).
        """
        self.ensure_token()
        tr_id = "VTTC0801U" if self._is_mock else "TTTC0801U"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "PDNO": symbol,
            "ORD_DVSN": "00",          # 지정가
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(price)),
            "EXCG_ID_DVSN_CD": "KRX",
        }
        result = self._post_order_with_retry(tr_id, body)
        log.info("KRX 지정가 매도 [%s] qty=%d @%d → %s",
                 symbol, qty, int(price), result.get("msg1", ""))
        return result

    def cancel_order(self, order_no: str, branch_no: str = "") -> dict:
        """미체결 주문 전량 취소."""
        self.ensure_token()
        tr_id = "VTTC0803U" if self._is_mock else "TTTC0803U"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "KRX_FWDG_ORD_ORGNO": branch_no,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소, 01=정정
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",     # 잔량 전부 취소
        }
        hk = self._hashkey(body)
        resp = self._client.post(url, headers=self._headers(tr_id, hashkey=hk), json=body)
        resp.raise_for_status()
        data = resp.json()
        log.info("주문 취소 [order=%s] → %s", order_no, data.get("msg1", ""))
        return data

    def sell_nxt(self, symbol: str, qty: int, price: float) -> dict:
        """NXT(넥스트레이드) 지정가 매도. 프리장(08:00~09:00) 구간 대응용.

        NXT 거래소 라우팅(EXCG_ID_DVSN_CD=NXT) + 지정가 주문(ORD_DVSN=00).
        계정에 NXT 권한이 없으면 KIS가 거부 응답을 반환함.
        """
        self.ensure_token()
        tr_id = "VTTC0801U" if self._is_mock else "TTTC0801U"
        body = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "PDNO": symbol,
            "ORD_DVSN": "00",           # 지정가 (NXT 시장가 미지원)
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(price)),
            "EXCG_ID_DVSN_CD": "NXT",
        }
        result = self._post_order_with_retry(tr_id, body)
        log.info("NXT 매도 주문 [%s] qty=%d @%d → %s", symbol, qty, int(price), result.get("msg1", ""))
        return result

    def get_holding_qty(self, symbol: str) -> int:
        """특정 종목 현재 보유 수량 조회."""
        try:
            bal = self.get_balance()
            for item in bal.get("output1", []):
                if item.get("pdno") == symbol:
                    return int(item.get("hldg_qty", 0) or 0)
        except Exception:
            pass
        return 0

    def get_today_executions(self, symbol: str) -> list[dict]:
        """오늘 체결 내역 조회 (매수+매도).

        반환 필드 주요값:
          sll_buy_dvsn_cd: "01"=매도, "02"=매수
          tot_ccld_qty   : 총체결수량
          avg_prvs       : 체결평균가
          odno           : 주문번호
        """
        self.ensure_token()
        tr_id = "VTTC8001R" if self._is_mock else "TTTC8001R"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",   # 전체
            "INQR_DVSN": "00",
            "PDNO": symbol,
            "CCLD_DVSN": "01",         # 체결만 (미체결 제외)
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            return self._get_with_retry(url, self._headers(tr_id), params).get("output1", []) or []
        except Exception as e:
            log.warning("[%s] 체결 내역 조회 실패: %s", symbol, e)
            return []

    def get_positions(self) -> list[dict]:
        """현재 보유 종목 조회."""
        data = self.get_balance()
        return data.get("output1", []) or []

    def get_volume_rank(self, sort_by: str = "3", market: str = "J",
                        min_price: int = 0, max_price: int = 1000000,
                        min_volume: int = 0) -> list[dict]:
        """거래량/거래금액 순위 조회.

        Args:
            sort_by: "0"=평균거래량, "1"=거래증가율, "3"=거래금액순
            market: "J"=KRX, "NX"=NXT, "UN"=통합
            min_price: 최소 가격
            max_price: 최대 가격
            min_volume: 최소 거래량
        """
        self.ensure_token()
        tr_id = "FHPST01710000"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": sort_by,
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": str(min_price),
            "FID_INPUT_PRICE_2": str(max_price),
            "FID_VOL_CNT": str(min_volume),
            "FID_INPUT_DATE_1": "",
        }
        try:
            data = self._get_with_retry(url, self._headers(tr_id), params)
            return data.get("output", []) or []
        except Exception as e:
            log.warning("거래량순위 조회 실패: %s", e)
            return []

    def get_approval_key(self) -> str:
        """WebSocket 접속키 발급 (/oauth2/Approval).

        체결통보 WebSocket 구독에 필요한 approval_key를 발급합니다.
        access_token과 별개이며 유효기간은 24시간입니다.
        """
        url = f"{self.cfg.base_url}/oauth2/Approval"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.cfg.app_key,
            "secretkey": self.cfg.app_secret,
        }
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        key = resp.json().get("approval_key", "")
        if not key:
            raise RuntimeError("approval_key 발급 실패: 응답에 키 없음")
        log.info("WebSocket 접속키 발급 완료")
        return key

    def close(self) -> None:
        self._client.close()
