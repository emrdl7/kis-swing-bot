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
        """GET 요청, 500 오류 시 최대 retries회 재시도. 토큰 만료 시 강제 재발급."""
        token_refreshed = False
        for attempt in range(retries + 1):
            resp = self._client.get(url, headers=headers, params=params)
            # 토큰 서버 측 무효화 자동 복구
            if (resp.status_code in (401, 500)
                    and not token_refreshed
                    and self._is_token_expired_response(resp)):
                log.warning("GET 응답에 토큰 만료(EGW00123) → 강제 재발급 후 재시도")
                self._access_token = ""
                self._token_expires_at = datetime.min
                self._issue_token()
                token_refreshed = True
                # 헤더 갱신 후 재시도 (호출자가 만든 headers의 authorization 재구성 필요)
                # tr_id를 headers에서 추출해 재구성
                tr_id = headers.get("tr_id", "")
                headers = self._headers(tr_id)
                continue
            if resp.status_code == 500 and attempt < retries:
                time.sleep(1)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def get_stock_name(self, symbol: str) -> str:
        """종목 한글명 조회 (search-stock-info)."""
        self.ensure_token()
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/search-stock-info"
        out = self._get_with_retry(url, self._headers("CTPF1604R"),
                                   {"PRDT_TYPE_CD": "300", "PDNO": symbol}).get("output", {})
        return out.get("prdt_abrv_name") or out.get("prdt_name") or symbol

    def get_price(self, symbol: str) -> dict:
        """현재가 조회."""
        self.ensure_token()
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        return self._get_with_retry(url, self._headers("FHKST01010100"), params).get("output", {})

    def get_index_change_pct(self, index_code: str = "0001") -> float:
        """지수 등락률 조회. 0001=KOSPI, 1001=KOSDAQ. 실패 시 0.0 반환."""
        try:
            self.ensure_token()
            url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
            params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": index_code}
            out = self._get_with_retry(url, self._headers("FHKUP03500100"), params).get("output", {})
            return float(out.get("bstp_nmix_prdy_ctrt", 0) or 0)
        except Exception as e:
            log.warning("지수 등락률 조회 실패 [%s]: %s", index_code, e)
            return 0.0

    def get_daily_ohlcv(self, symbol: str, count: int = 20, period: str = "D") -> list[dict]:
        """일봉 데이터 조회 (최근 count일).

        inquire-daily-itemchartprice (FHKST03010100) — 1회 호출 최대 100일.
        100일을 초과하는 count 요청 시 시작일을 (count*1.6)일 전으로 잡아
        영업일 기준으로 충분히 확보. 응답은 최신 → 과거 순.

        과거 KIS 변경 이력: inquire-daily-price 응답이 output 단일 키로 변경되어
        output2를 찾던 구버전 코드가 빈 리스트만 반환하던 버그가 있었음 — 본 메서드로 교체.
        """
        self.ensure_token()
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        period = period.upper()
        if period not in ("D", "W", "M"):
            period = "D"
        today = datetime.now().strftime("%Y%m%d")
        # 영업일 기준 60% 비율로 캘린더 윈도우 산정 (주말/공휴일 마진 포함)
        factor = 8 if period == "W" else (35 if period == "M" else 1.6)
        lookback_days = max(int(count * factor) + 10, 40)
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": today,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
        }
        resp = self._get_with_retry(url, self._headers("FHKST03010100"), params)
        output = resp.get("output2") or []
        return output[:count]

    def get_intraday_candles(self, symbol: str, from_time: str = "090000") -> list[dict]:
        """당일 분봉 데이터 조회. from_time(HHMMSS) 이후 체결 캔들 반환 (오래된순).

        KIS 분봉 API는 FID_INPUT_HOUR_1 시각 이전의 제한된 개수만 반환하므로,
        장중 전체 흐름을 보려면 기준 시각을 뒤로 옮겨가며 여러 번 조회해야 한다.
        """
        self.ensure_token()
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"

        def prev_second(hhmmss: str) -> str:
            try:
                dt = datetime.strptime(hhmmss, "%H%M%S") - timedelta(seconds=1)
                return dt.strftime("%H%M%S")
            except Exception:
                return from_time

        end_time = min(datetime.now().strftime("%H%M%S"), "153000")
        if end_time < from_time:
            end_time = "153000"

        merged: dict[tuple[str, str], dict] = {}
        target_date = ""
        for _ in range(16):
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": end_time,
                "FID_ETC_CLS_CODE": "0",
                "FID_PW_DATA_INCU_YN": "Y",
            }
            output = self._get_with_retry(url, self._headers("FHKST03010200"), params).get("output2", []) or []
            if not output:
                break
            if not target_date:
                dates = [str(row.get("stck_bsop_date", "")) for row in output if row.get("stck_bsop_date")]
                target_date = max(dates) if dates else ""
            for row in output:
                if target_date and str(row.get("stck_bsop_date", "")) != target_date:
                    continue
                tm = str(row.get("stck_cntg_hour", ""))
                if tm >= from_time:
                    key = (str(row.get("stck_bsop_date", "")), tm)
                    merged[key] = row
            times = sorted(str(row.get("stck_cntg_hour", "")) for row in output if row.get("stck_cntg_hour"))
            if not times or times[0] <= from_time:
                break
            next_end = prev_second(times[0])
            if next_end >= end_time:
                break
            end_time = next_end

        return [merged[k] for k in sorted(merged)]

    def get_nxt_price(self, symbol: str) -> dict:
        """NXT(야간) 현재가 조회. NX 마켓코드로 조회 후 실패 시 KRX fallback."""
        self.ensure_token()
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": symbol}
        result = self._get_with_retry(url, self._headers("FHKST01010100"), params).get("output", {})
        if float(result.get("stck_prpr", 0) or 0) > 0:
            return result
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

    def _is_token_expired_response(self, resp) -> bool:
        """KIS 응답이 토큰 만료(EGW00123)인지 판별. 500/401 본문에 EGW00123."""
        try:
            data = resp.json()
            if data.get("msg_cd") == "EGW00123":
                return True
            if "토큰" in (data.get("msg1") or "") and "만료" in (data.get("msg1") or ""):
                return True
        except Exception:
            pass
        return False

    def _post_order_with_retry(self, tr_id: str, body: dict, retries: int = 2) -> dict:
        """주문 POST — 500 에러 시 재시도. 토큰 만료(EGW00123) 응답이면 강제 재발급 후 1회 재시도."""
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        last_exc: Exception | None = None
        token_refreshed = False
        for attempt in range(retries + 1):
            try:
                hk = self._hashkey(body)
                resp = self._client.post(url, headers=self._headers(tr_id, hashkey=hk), json=body)
                # 토큰 서버 측 무효화 자동 복구 (500/401 + EGW00123)
                if (resp.status_code in (401, 500)
                        and not token_refreshed
                        and self._is_token_expired_response(resp)):
                    log.warning("주문 응답에 토큰 만료(EGW00123) → 강제 재발급 후 재시도")
                    self._access_token = ""
                    self._token_expires_at = datetime.min
                    self._issue_token()
                    token_refreshed = True
                    continue
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

    def get_period_profit(self, start_date: str, end_date: str, symbol: str = "") -> dict:
        """기간별손익일별합산조회.

        KIS HTS [0856] 기간별 매매손익의 "일별" 화면에 해당한다. 대시보드의
        오늘 실현손익은 자체 재계산보다 이 증권사 원장값을 우선 사용한다.
        """
        self.ensure_token()
        tr_id = "VTTC8708R" if self._is_mock else "TTTC8708R"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/inquire-period-profit"
        params = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "SORT_DVSN": "00",
            "INQR_DVSN": "00",
            "CBLC_DVSN": "00",
            "PDNO": symbol or "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            return self._get_with_retry(url, self._headers(tr_id), params)
        except Exception as e:
            log.warning("기간별손익 조회 실패 [%s~%s %s]: %s", start_date, end_date, symbol or "ALL", e)
            return {}

    def get_period_trade_profit(self, start_date: str, end_date: str, symbol: str = "") -> dict:
        """기간별매매손익현황조회.

        KIS HTS [0856] 기간별 매매손익의 "종목별" 화면에 해당한다.
        """
        self.ensure_token()
        tr_id = "VTTC8715R" if self._is_mock else "TTTC8715R"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/inquire-period-trade-profit"
        params = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "SORT_DVSN": "00",
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "CBLC_DVSN": "00",
            "PDNO": symbol or "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            return self._get_with_retry(url, self._headers(tr_id), params)
        except Exception as e:
            log.warning("기간별매매손익 조회 실패 [%s~%s %s]: %s", start_date, end_date, symbol or "ALL", e)
            return {}

    def get_today_executions(self, symbol: str = "") -> list[dict]:
        """오늘 체결 내역 조회 (매수+매도).

        symbol을 비우면 계좌의 오늘 전체 체결을 조회한다.

        반환 필드 주요값:
          pdno           : 종목코드
          sll_buy_dvsn_cd: "01"=매도, "02"=매수
          tot_ccld_qty   : 총체결수량
          avg_prvs       : 체결평균가
          odno           : 주문번호
        """
        self.ensure_token()
        tr_id = "VTTC8001R" if self._is_mock else "TTTC8001R"
        url = f"{self.cfg.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        today = datetime.now().strftime("%Y%m%d")
        base_params = {
            "CANO": self.cfg.account_no[:8],
            "ACNT_PRDT_CD": self._acnt_prdt_cd(),
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",   # 전체
            "INQR_DVSN": "00",
            "PDNO": symbol or "",
            "CCLD_DVSN": "01",         # 체결만 (미체결 제외)
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
        }
        try:
            rows: list[dict] = []
            fk = ""
            nk = ""
            seen_pages: set[tuple[str, str]] = set()
            for _ in range(10):
                params = dict(base_params)
                params["CTX_AREA_FK100"] = fk
                params["CTX_AREA_NK100"] = nk
                data = self._get_with_retry(url, self._headers(tr_id), params)
                rows.extend(data.get("output1", []) or [])
                next_fk = str(data.get("ctx_area_fk100") or "").strip()
                next_nk = str(data.get("ctx_area_nk100") or "").strip()
                page_key = (next_fk, next_nk)
                if not next_fk and not next_nk:
                    break
                if page_key in seen_pages:
                    break
                seen_pages.add(page_key)
                fk, nk = next_fk, next_nk
            deduped: list[dict] = []
            seen_rows: set[tuple[str, str, str, str, str]] = set()
            for row in rows:
                row_key = (
                    str(row.get("ord_dt") or ""),
                    str(row.get("ord_tmd") or ""),
                    str(row.get("odno") or ""),
                    str(row.get("pdno") or ""),
                    str(row.get("sll_buy_dvsn_cd") or ""),
                )
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                deduped.append(row)
            return deduped
        except Exception as e:
            log.warning("[%s] 체결 내역 조회 실패: %s", symbol or "ALL", e)
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
