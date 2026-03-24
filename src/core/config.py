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


class TradingConfig(BaseModel):
    position_size_pct: float = 0.30
    max_positions: int = 3
    max_daily_loss_pct: float = 5.0
    commission_pct: float = 0.015


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


class AgentsConfig(BaseModel):
    model: str = "claude-opus-4-6"
    max_tokens: int = 2000
    debate_rounds: int = 2
    num_agents: int = 3


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
    # DART: 기존 프로젝트는 OPENDART_API_KEY 사용
    dart_api_key: str = Field("", alias="OPENDART_API_KEY", validation_alias="OPENDART_API_KEY")

    # 서브 섹션 (YAML에서 오버라이드 가능)
    kis: KisConfig = Field(default_factory=KisConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    exit: ExitConfig = Field(default_factory=ExitConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
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
