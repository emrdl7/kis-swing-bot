"""도메인 모델 정의."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PositionState(str, Enum):
    WATCHING = "WATCHING"      # 관찰 중 (진입 대기)
    ENTERED = "ENTERED"        # 매수 완료
    TRAILING = "TRAILING"      # 트레일링 스탑 활성
    CLOSED = "CLOSED"          # 청산 완료


class CloseReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    EOD = "EOD"
    MANUAL = "MANUAL"
    RECONCILE_KIS_ZERO = "RECONCILE_KIS_ZERO"  # KIS 잔고 0 → ghost position 자동 정리
    CLOSING_BET_MORNING = "CLOSING_BET_MORNING"  # 종가배팅 익일 오전 매도


@dataclass
class AgentOpinion:
    """단일 에이전트의 종목 추천 의견."""
    agent_name: str                    # e.g. "news_agent"
    symbol: str                        # 종목코드
    name: str                          # 종목명
    conviction: float                  # 0.0 ~ 1.0
    rationale: str                     # 추천 이유
    entry_low: float                   # 진입 하단가
    entry_high: float                  # 진입 상단가
    target_price: float                # 목표가
    stop_price: float                  # 손절가
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class DebateResult:
    """멀티 에이전트 토론 최종 결과."""
    symbol: str
    name: str
    consensus_score: float             # 0.0 ~ 1.0 (에이전트 동의 수준)
    final_rationale: str
    entry_low: float
    entry_high: float
    target_price: float
    stop_price: float
    supporting_agents: list[str]       # 찬성 에이전트 목록
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "consensus_score": self.consensus_score,
            "final_rationale": self.final_rationale,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "supporting_agents": self.supporting_agents,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DebateResult":
        d = dict(d)
        d["created_at"] = datetime.fromisoformat(d.get("created_at", datetime.now().isoformat()))
        return cls(**d)


@dataclass
class SwingCandidate:
    """발굴된 스윙 후보 종목."""
    symbol: str
    name: str
    entry_low: float
    entry_high: float
    target_price: float
    stop_price: float
    consensus_score: float
    rationale: str
    tags: list[str] = field(default_factory=list)
    discovered_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None         # 유효 만료 시각
    nxt_close: Optional[float] = None             # NXT 현재가 (08:50 스크리닝 시)
    nxt_volume: Optional[int] = None              # NXT 누적 거래량
    nxt_gap_pct: Optional[float] = None           # NXT vs 전일종가 갭 (%)
    nxt_trade_amount_bn: Optional[float] = None   # NXT 누적 거래대금 (억원)
    prev_close: Optional[float] = None            # 전일 종가 (갭 계산용)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now()) > self.expires_at

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "consensus_score": self.consensus_score,
            "rationale": self.rationale,
            "tags": self.tags,
            "discovered_at": self.discovered_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "nxt_close": self.nxt_close,
            "nxt_volume": self.nxt_volume,
            "nxt_gap_pct": self.nxt_gap_pct,
            "nxt_trade_amount_bn": self.nxt_trade_amount_bn,
            "prev_close": self.prev_close,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SwingCandidate":
        d = dict(d)
        d["discovered_at"] = datetime.fromisoformat(d.get("discovered_at", datetime.now().isoformat()))
        if d.get("expires_at"):
            d["expires_at"] = datetime.fromisoformat(d["expires_at"])
        return cls(**d)


@dataclass
class SwingPosition:
    """보유 중인 스윙 포지션."""
    symbol: str
    name: str
    qty: int
    avg_price: float
    entry_time: datetime
    target_price: float
    stop_price: float
    state: PositionState = PositionState.ENTERED
    peak_price: float = 0.0           # 최고가 (트레일링 스탑용)
    trailing_stop_px: Optional[float] = None
    close_reason: Optional[CloseReason] = None
    close_price: Optional[float] = None
    close_time: Optional[datetime] = None
    order_id: Optional[str] = None
    strategy: str = "swing"              # "swing" or "closing_bet"

    @property
    def cost_basis(self) -> float:
        return self.avg_price * self.qty

    def pnl_pct(self, current_price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (current_price - self.avg_price) / self.avg_price * 100.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "entry_time": self.entry_time.isoformat(),
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "state": self.state.value,
            "peak_price": self.peak_price,
            "trailing_stop_px": self.trailing_stop_px,
            "close_reason": self.close_reason.value if self.close_reason else None,
            "close_price": self.close_price,
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "order_id": self.order_id,
            "strategy": self.strategy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SwingPosition":
        d = dict(d)
        d["entry_time"] = datetime.fromisoformat(d.get("entry_time", datetime.now().isoformat()))
        d["state"] = PositionState(d.get("state", PositionState.ENTERED.value))
        if d.get("close_reason"):
            d["close_reason"] = CloseReason(d["close_reason"])
        if d.get("close_time"):
            d["close_time"] = datetime.fromisoformat(d["close_time"])
        return cls(**d)
