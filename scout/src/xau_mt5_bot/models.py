from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"
    NO_TRADE = "NO TRADE"


class SessionName(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    CLOSED = "CLOSED"


class StructureState(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"
    TRANSITION = "Transition"
    RANGE = "Range"


class EntryState(str, Enum):
    ABOVE = "ABOVE ENTRY"
    BELOW = "BELOW ENTRY"
    APPROACHING = "APPROACHING ENTRY"
    INSIDE = "INSIDE ENTRY"
    CONFIRMED = "ENTRY CONFIRMED"
    MISSED = "ENTRY MISSED"
    NOT_IN_SETUP = "NOT IN SETUP"


class ScoutVerdict(str, Enum):
    CONFIRMS = "CONFIRMS"
    NEUTRAL = "NEUTRAL"
    CONTRADICTS = "CONTRADICTS"


class SpreadState(str, Enum):
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    ABNORMAL = "ABNORMAL"


class Freshness(str, Enum):
    LIVE = "LIVE"
    WARNING = "WARNING"
    STALE = "STALE"


class TargetRealism(str, Enum):
    REALISTIC = "REALISTIC"
    AGGRESSIVE = "AGGRESSIVE"
    UNLIKELY = "UNLIKELY"


@dataclass(slots=True)
class Pivot:
    timestamp: datetime
    confirmation_timestamp: datetime
    price: float
    kind: str
    index: int


@dataclass(slots=True)
class StructureResult:
    timeframe: str
    state: StructureState
    pivots: list[Pivot] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class LiquidityLevel:
    price: float
    kind: str
    timeframe: str
    valid_from: datetime
    strength: float
    taken_today: bool = False
    latest_event: dict[str, Any] | None = None
    active_event: dict[str, Any] | None = None


@dataclass(slots=True)
class SweepEvent:
    level_type: str
    direction: str
    level_price: float
    sweep_price: float
    sweep_time: datetime
    reclaim_time: datetime
    distance: float
    distance_atr: float
    age_bars: int
    active: bool = True


@dataclass(slots=True)
class Zone:
    low: float
    high: float
    kind: str
    side: Side
    created_at: datetime
    valid_from: datetime
    timeframe: str = "M5"
    score: float = 0.0
    status: str = "untouched"

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass(slots=True)
class ScoutSnapshot:
    session: SessionName
    buy_ticket: int | None = None
    sell_ticket: int | None = None
    buy_entry: float | None = None
    sell_entry: float | None = None
    buy_pnl: float = 0.0
    sell_pnl: float = 0.0
    buy_mfe: float = 0.0
    buy_mae: float = 0.0
    sell_mfe: float = 0.0
    sell_mae: float = 0.0
    displacement: float = 0.0
    velocity: float = 0.0
    velocity_direction: str = "FLAT"
    leader: str = "NONE"
    verdict: ScoutVerdict = ScoutVerdict.NEUTRAL
    strength: int = 0
    market_speed: str = "UNKNOWN"
    guidance: str = "WAITING FOR SESSION SCOUT DATA"
    pace_range: float = 0.0
    pace_window_minutes: float = 0.0
    strength_source: str = "UNAVAILABLE"
    buy_pnl_reference_lot: float = 0.0
    sell_pnl_reference_lot: float = 0.0
    calibration_sessions: int = 0
    pace_calibrated: bool = False
    emergency_sl_price_distance: float = 0.0
    estimated_pair_risk_actual: float | None = None
    estimated_pair_risk_reference_lot: float | None = None


@dataclass(slots=True)
class TriggerResult:
    confirmed: bool
    source: str
    touch_time: datetime | None = None
    sweep: bool = False
    structure_break: bool = False
    displacement: bool = False
    fresh: bool = False
    reason: str = ""
    setup_id: str | None = None
    direction: Side | None = None
    zone_kind: str | None = None
    zone_low: float | None = None
    zone_high: float | None = None
    zone_valid_from: datetime | None = None
    confirmation_timestamp: datetime | None = None
    consumed: bool = False
    trigger_bar_time: datetime | None = None          # open time of the bar that actually confirmed the trigger (v2.0.0 item 6)


@dataclass(slots=True)
class TradePlan:
    side: Side
    entry: float
    stop_loss: float
    take_profits: list[float]
    invalidation: str
    sl_reason: str
    actual_rr: list[float]
    target_realism: TargetRealism
    volume: float = 0.0
    requested_entry: float | None = None
    actual_entry: float | None = None
    initial_stop_loss: float | None = None


@dataclass(slots=True)
class DecisionInput:
    pa_side: Side | None
    setup_valid: bool
    entry_state: EntryState
    trigger: TriggerResult
    scout: ScoutSnapshot
    freshness: Freshness
    spread_state: SpreadState
    account_safe: bool
    rr: float | None
    min_rr: float
    target_realism: TargetRealism | None
    confluence: int
    min_confluence: int = 0
    setup_id: str | None = None
    scout_contradiction_threshold: int = 7
    hold_when_slow: bool = False


@dataclass(slots=True)
class Decision:
    action: Action
    reason: str
    timestamp: datetime


@dataclass(slots=True)
class AnalysisSnapshot:
    timestamp: datetime
    symbol: str
    session: SessionName
    bid: float
    ask: float
    spread: float
    freshness: Freshness
    structures: dict[str, StructureResult]
    patterns: list[dict[str, Any]]
    liquidity: list[LiquidityLevel]
    sweeps: list[SweepEvent]
    zones: list[Zone]
    pa_side: Side | None
    confluence: int
    scout: ScoutSnapshot
    entry_state: EntryState
    trigger: TriggerResult
    trade_plan: TradePlan | None
    decision: Decision
    active_positions: list[dict[str, Any]] = field(default_factory=list)
    go_status: str = "NO-GO"
    reporting: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)      # v3.0.0: mtf, treatment, session_target, pattern_reliability, live_scout_evidence, decision_summary

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
