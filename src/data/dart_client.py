"""DART 전자공시 API 클라이언트."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

log = logging.getLogger(__name__)

DART_BASE = "https://opendart.fss.or.kr/api"


class DartClient:
    def __init__(self, api_key: str, lookback_days: int = 1):
        self.api_key = api_key
        self.lookback_days = lookback_days
        self._client = httpx.Client(timeout=15.0)

    def _date_range(self) -> tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    def get_recent_disclosures(self, pblntf_ty: str = "A") -> list[dict]:
        """최근 공시 목록 조회.

        pblntf_ty:
            A=정기공시, B=주요사항보고, C=발행공시, D=지분공시,
            E=기타공시, F=외부감사, G=펀드공시, H=자산유동화, I=거래소공시
        """
        start_dt, end_dt = self._date_range()
        params = {
            "crtfc_key": self.api_key,
            "bgn_de": start_dt,
            "end_de": end_dt,
            "pblntf_ty": pblntf_ty,
            "page_no": 1,
            "page_count": 100,
        }
        try:
            resp = self._client.get(f"{DART_BASE}/list.json", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "000":
                log.warning("DART 공시 조회 오류: %s", data.get("message", ""))
                return []
            return data.get("list", [])
        except Exception as e:
            log.error("DART 공시 조회 실패: %s", e)
            return []

    def get_major_disclosures(self) -> list[dict]:
        """주요사항보고 (B) + 기타공시 (E) 합산."""
        result = []
        for ty in ["B", "E"]:
            result.extend(self.get_recent_disclosures(pblntf_ty=ty))
        return result

    def get_company_info(self, corp_code: str) -> Optional[dict]:
        """기업 기본 정보 조회."""
        params = {"crtfc_key": self.api_key, "corp_code": corp_code}
        try:
            resp = self._client.get(f"{DART_BASE}/company.json", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "000":
                return data
        except Exception as e:
            log.error("DART 기업정보 조회 실패: %s", e)
        return None

    def format_for_llm(self, disclosures: list[dict]) -> str:
        """LLM 프롬프트용 공시 텍스트 변환."""
        if not disclosures:
            return "오늘 주요 공시 없음"
        lines = []
        for d in disclosures[:30]:  # 최대 30건
            corp = d.get("corp_name", "")
            title = d.get("report_nm", "")
            rcept_dt = d.get("rcept_dt", "")
            lines.append(f"- [{rcept_dt}] {corp}: {title}")
        return "\n".join(lines)

    def close(self) -> None:
        self._client.close()
