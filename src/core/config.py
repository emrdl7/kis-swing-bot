"""앱 설정 로드 (Pydantic v2 + YAML + .env)."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Literal, Optional
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── 섹션별 서브 모델 ─────────────────────────────────────────────────────────

class KisConfig(BaseModel):
    base_url: str = "https://openapi.koreainvestment.com:9443"
    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    account_type: str = "01"          # 01=실전, 02=모의
    hts_id: str = ""                  # HTS 로그인 ID (WebSocket 체결통보 구독용)


class TradingConfig(BaseModel):
    position_size_pct: float = 0.30
    max_positions: int = 3
    max_daily_loss_pct: float = 5.0
    commission_pct: float = 0.015
    mock_budget: int = 0          # 모의투자 고정 예산 (0이면 API 조회)


class ExitConfig(BaseModel):
    take_profit_pct: float = 8.0
    stop_loss_pct: float = 7.0
    trailing_activate_pct: float = 3.0
    trailing_pct: float = 5.0
    eod_sell_hhmm: int = 1510
    eod_sell_enabled: bool = False


class ScreeningConfig(BaseModel):
    max_candidates: int = 8
    min_market_cap_bn: int = 5000
    min_volume: int = 500000
    min_trade_amount: int = 5_000_000_000
    quant_universe_enabled: bool = True
    quant_universe_top_n: int = 80
    quant_universe_min_trade_amount_bn: float = 80.0
    quant_universe_max_results: int = 40
    entry_zone_slack_pct: float = 0.5
    entry_expiry_days: int = 14
    drop_above_entry_pct: float = 5.0  # 진입구간 상단 대비 이 % 이상 위면 후보 제거
    min_entry_consensus_score: float = 0.45  # 진입 최소 신뢰도
    # 2단계 선분석 설정
    evening_prescreen_enabled: bool = True
    evening_candidate_n: int = 20        # 저녁 선분석에서 뽑을 초벌 후보 수
    entry_cooldown_until: str = "09:05"  # HH:MM. 이 시각 이전엔 매수 금지 (정보용, clock.py와 동기화)
    open_gap_abort_pct: float = 3.0      # 시초가 vs 저녁 기준가 절대 이탈이 이 % 이상이면 ABORT


class AgentsConfig(BaseModel):
    """LLM 백엔드 설정. Codex와 Gemini는 서로 fallback한다."""
    primary: Literal["codex", "gemini"] = "codex"
    codex_model: str = ""                        # 빈 값이면 codex 계정 default. ChatGPT 구독은 명시 모델명 거부함
    gemini_model: str = "gemini-2.5-pro"         # gemini 모델명
    max_tokens: int = 2000
    debate_rounds: int = 2
    num_agents: int = 3


class PivotGateConfig(BaseModel):
    """정량 피봇 게이트 설정.

    mode:
      - "pullback" (기본): LLM 진입대 + 저점 반등 + 거래량 보존 (눌림목 매수)
      - "breakout": 박스 상단 돌파 + 거래량 폭발 (모멘텀 매수)
    """
    enabled: bool = True
    mode: str = "pullback"              # pullback | breakout
    # 공통
    min_trade_amount_bn: float = 50.0   # 당일 누적 거래대금 최소 (억원)
    require_ma_uptrend: bool = True     # MA20 > MA60 추세 게이트
    # pullback 전용
    bounce_pct_min: float = 0.5         # 최근 N일 저점 대비 최소 회복율 (%)
    bounce_lookback: int = 5            # 저점 산정 윈도우 (일)
    pullback_vol_ratio_min: float = 0.8 # pullback 거래량 위축 한도
    entry_zone_slack_pct: float = 1.0   # entry_zone 양쪽 여유 (%)
    # breakout 전용
    box_lookback: int = 20              # 박스 산정 일수
    breakout_pct_min: float = 0.3       # 박스 상단 대비 최소 돌파율 (%)
    breakout_pct_max: float = 5.0       # 추격매수 차단 상한 (%)
    breakout_vol_ratio_min: float = 1.5 # breakout 거래량 폭발 임계
    # watchlist
    watchlist_max: int = 25             # watchlist 최대 종목 수
    watchlist_expiry_days: int = 14     # watchlist 항목 만료
    check_interval_sec: int = 60        # 게이트 검사 최소 간격 (monitor 사이클 내 throttle)


class PositionReviewConfig(BaseModel):
    """일일 보유 포지션 재평가 설정 (마이너스 종목 → 재료/지표 점검)."""
    enabled: bool = True
    hhmm: int = 1100                    # 실행 시각 (HHMM, 11:00 기본)
    min_holding_days: int = 1           # 최소 보유 일수 (당일 진입분 제외)
    only_negative: bool = True          # 마이너스 포지션만 평가
    sell_conviction_threshold: float = 0.7   # 이 값 이상이면 SELL 플래그 세팅
    max_sells_per_day: int = 3          # 하루 SELL 판정 최대 종목 수 (세이프티)
    news_lookback_hours: int = 48       # 재료 소멸 판단용 뉴스 수집 윈도우


class NotificationConfig(BaseModel):
    # 추후 텔레그램 연동 예정
    enabled: bool = False


class DartConfig(BaseModel):
    api_key: str = ""
    lookback_days: int = 1


class NewsConfig(BaseModel):
    max_age_hours: int = 24
    sources: list[str] = Field(default_factory=list)


# ── 메인 설정 ─────────────────────────────────────────────────────────────────

class AppConfig(BaseSettings):
    """환경변수 + YAML 병합 설정."""
    model_config = SettingsConfigDict(
        env_file="config/.env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # 환경변수 직접 맵핑 (기존 kis-auto-standalone .env 호환)
    kis_app_key: str = Field("", alias="KIS_APP_KEY", validation_alias="KIS_APP_KEY")
    kis_app_secret: str = Field("", alias="KIS_APP_SECRET", validation_alias="KIS_APP_SECRET")
    kis_account_no: str = Field("", alias="KIS_ACCOUNT_NO", validation_alias="KIS_ACCOUNT_NO")
    kis_account_type: str = Field("01", alias="KIS_ACCOUNT_TYPE", validation_alias="KIS_ACCOUNT_TYPE")
    kis_hts_id: str = Field("", alias="KIS_HTS_ID", validation_alias="KIS_HTS_ID")
    # DART: 기존 프로젝트는 OPENDART_API_KEY 사용
    dart_api_key: str = Field("", alias="OPENDART_API_KEY", validation_alias="OPENDART_API_KEY")

    # 서브 섹션 (YAML에서 오버라이드 가능)
    kis: KisConfig = Field(default_factory=KisConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    exit: ExitConfig = Field(default_factory=ExitConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    pivot_gate: PivotGateConfig = Field(default_factory=PivotGateConfig)
    position_review: PositionReviewConfig = Field(default_factory=PositionReviewConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    dart: DartConfig = Field(default_factory=DartConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)

    def merge_yaml(self, path: str | Path) -> None:
        """YAML 파일을 읽어 서브 섹션을 업데이트한다."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for section_name, section_data in data.items():
            if not isinstance(section_data, dict):
                continue
            attr = getattr(self, section_name, None)
            if attr is None:
                continue
            updated = attr.model_copy(update=section_data)
            object.__setattr__(self, section_name, updated)

    def populate_from_env(self) -> None:
        """환경변수에서 읽은 값을 서브 섹션에 주입한다."""
        if self.kis_app_key:
            self.kis = self.kis.model_copy(update={"app_key": self.kis_app_key})
        if self.kis_app_secret:
            self.kis = self.kis.model_copy(update={"app_secret": self.kis_app_secret})
        if self.kis_account_no:
            self.kis = self.kis.model_copy(update={"account_no": self.kis_account_no})
        if self.kis_account_type:
            self.kis = self.kis.model_copy(update={"account_type": self.kis_account_type})
        if self.kis_hts_id:
            self.kis = self.kis.model_copy(update={"hts_id": self.kis_hts_id})
        if self.dart_api_key:
            self.dart = self.dart.model_copy(update={"api_key": self.dart_api_key})


def load_config(yaml_path: str | Path | None = None) -> AppConfig:
    """설정 로드 진입점."""
    cfg = AppConfig()
    yaml_path = yaml_path or Path(__file__).parent.parent.parent / "config" / "default.yaml"
    if Path(yaml_path).exists():
        cfg.merge_yaml(yaml_path)
    cfg.populate_from_env()
    return cfg
