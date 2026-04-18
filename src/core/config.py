"""앱 설정 로드 (Pydantic v2 + YAML + .env)."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
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
    take_profit_pct: float = 4.0
    stop_loss_pct: float = 2.5
    trailing_activate_pct: float = 2.0
    trailing_pct: float = 1.5
    eod_sell_hhmm: int = 1510
    eod_sell_enabled: bool = False


class ScreeningConfig(BaseModel):
    max_candidates: int = 5
    min_market_cap_bn: int = 500
    min_volume: int = 500000
    min_trade_amount: int = 5_000_000_000
    entry_zone_slack_pct: float = 1.0
    entry_expiry_days: int = 3
    drop_above_entry_pct: float = 5.0  # 진입구간 상단 대비 이 % 이상 위면 후보 제거
    # 2단계 선분석 설정
    evening_prescreen_enabled: bool = True
    evening_candidate_n: int = 15        # 저녁 선분석에서 뽑을 초벌 후보 수
    entry_cooldown_until: str = "09:05"  # HH:MM. 이 시각 이전엔 매수 금지 (정보용, clock.py와 동기화)
    open_gap_abort_pct: float = 3.0      # 시초가 vs 저녁 기준가 절대 이탈이 이 % 이상이면 ABORT


class AgentsConfig(BaseModel):
    model: str = "claude-opus-4-6"
    max_tokens: int = 2000
    debate_rounds: int = 2
    num_agents: int = 3


class ClosingBetConfig(BaseModel):
    """종가배팅 전략 설정."""
    enabled: bool = False
    screening_hhmm: int = 1450           # 스크리닝 시각
    entry_from_hhmm: int = 1520          # 매수 시작
    entry_to_hhmm: int = 1525            # 매수 마감
    sell_before_hhmm: int = 1000         # 다음 날 이 시각 전 매도
    target_profit_pct: float = 3.0       # 목표 수익률
    stop_loss_pct: float = 1.5           # 손절 기준
    max_positions: int = 2               # 종가배팅 최대 포지션
    min_trade_amount_bn: int = 50        # 최소 거래대금 (억원)
    min_change_pct: float = 2.0          # 당일 최소 등락률
    top_n: int = 30                      # 순위 조회 상위 N개
    score_weights: dict = {              # V스코어 가중치
        "trade_amount": 0.30,
        "change_pct": 0.25,
        "volume_ratio": 0.25,
        "ma_position": 0.20,
    }
    # NXT 프리장(08:00~09:00) 조기 매도 옵션
    pre_market_sell_enabled: bool = True
    pre_market_from_hhmm: int = 800      # 프리장 매도 감시 시작
    pre_market_to_hhmm: int = 855        # 프리장 매도 감시 종료 (정규장 직전)
    pre_market_target_profit_pct: float = 4.0   # NXT 갭상승 익절 기준 (정규장보다 공격적)
    pre_market_stop_loss_pct: float = 3.0       # NXT 갭하락 손절 기준


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
    closing_bet: ClosingBetConfig = Field(default_factory=ClosingBetConfig)
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
