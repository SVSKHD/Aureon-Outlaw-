from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ConfigDict, BaseModel, Field, model_validator


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pa_risk_percent: float = Field(default=0.25, ge=0, le=5)
    fixed_pa_lot: float = Field(default=0.01, gt=0)
    scout_lot: float = Field(default=0.01, gt=0)
    min_actual_rr: float = Field(default=1.0, gt=0)
    max_spread_price: float = Field(default=0.60, gt=0)
    emergency_scout_sl_price: float = Field(default=0.0, ge=0)
    max_total_volume: float = Field(default=1.0, gt=0)
    size_basis: str = Field(default="balance", pattern="^(balance|equity)$")
    max_sl_distance_price: float = Field(default=15.0, gt=0)      # reject setups with SL further than this ($)
    daily_max_loss_percent: float = Field(default=0.0, ge=0)      # outside this demo-bot pass
    daily_profit_lock_percent: float = Field(default=0.0, ge=0)   # 0 = off; PA entries stop for the day once reached
    max_consecutive_losses: int = Field(default=0, ge=0)          # outside this demo-bot pass
    max_pa_trades_per_day: int = Field(default=4, ge=0)
    max_pa_trades_per_session: int = Field(default=2, ge=0)

    @model_validator(mode="after")
    def volume_budget_covers_configured_orders(self) -> "RiskConfig":
        required = 2 * self.scout_lot + self.fixed_pa_lot
        if self.max_total_volume + 1e-9 < required:
            raise ValueError(
                f"max_total_volume {self.max_total_volume} is below configured scout-pair + PA volume {required}"
            )
        return self


class ManagementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    breakeven_at_rr: float = Field(default=1.0, ge=0)             # move SL to entry (+offset) once open profit ≥ this RR; 0 = off
    breakeven_offset_price: float = Field(default=0.10, ge=0)
    trailing_atr: float = Field(default=0.0, ge=0)                # trail SL by this many M5 ATR behind price; 0 = off
    trailing_start_rr: float = Field(default=1.5, ge=0)
    partial_tp1_percent: float = Field(default=50, ge=0, le=100)  # % of volume closed at TP1 (0 = broker TP handles it)
    partial_tp2_percent: float = Field(default=25, ge=0, le=100)  # % of ORIGINAL volume closed at TP2; remainder runs to TP3
    close_pa_at_session_end: bool = False
    setup_cooldown_minutes: int = Field(default=30, ge=0)
    max_entries_per_setup: int = Field(default=1, ge=1)


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_scout_orders: bool = False
    allow_pa_orders: bool = False
    allow_live_account: bool = False
    require_hedging_for_scouts: bool = True
    one_pa_position_per_symbol: bool = True
    max_m1_age_seconds: int = Field(default=300, ge=60)
    retry_count: int = Field(default=1, ge=0, le=3)
    max_clock_skew_seconds: int = Field(default=600, ge=60)
    # v3.3.0: MT5 reports tick/bar times in the BROKER SERVER timezone. null = auto-detect the offset each
    # hour and quantise it to the nearest 30 min; a float pins it (e.g. 3.0 for a UTC+3 server).
    broker_utc_offset_hours: float | None = Field(default=None, ge=-24.0, le=24.0)
    # Legacy seconds form of the same pin (master PR #2/#3). Honoured only when the hours form is null;
    # 0 means "use auto-detection", so the shipped default changes nothing.
    broker_timestamp_offset_seconds: float | None = Field(default=None, ge=-86400.0, le=86400.0)
    broker_offset_remeasure_seconds: int = Field(default=3600, ge=60)

    @model_validator(mode="after")
    def _fold_legacy_offset(self) -> "SafetyConfig":
        if self.broker_utc_offset_hours is None and self.broker_timestamp_offset_seconds:
            self.broker_utc_offset_hours = float(self.broker_timestamp_offset_seconds) / 3600.0
        return self
    transition_retry_limit: int = Field(default=5, ge=0)
    repair_missing_scout_leg: bool = True
    scout_repair_max_session_fraction: float = Field(default=0.5, ge=0, le=1)
    broker_market_stale_seconds: int = Field(default=180, ge=30)
    deal_history_max_days: int = Field(default=3650, ge=90)


class MagicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scout_asia: int = 11001
    scout_london: int = 11002
    scout_new_york: int = 11003
    pa: int = 12001

    @model_validator(mode="after")
    def unique_magics(self) -> "MagicConfig":
        values = {self.scout_asia, self.scout_london, self.scout_new_york, self.pa}
        if len(values) != 4:
            raise ValueError("All scout and PA magic numbers must be unique")
        return self


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asia_timezone: str = "Asia/Tokyo"
    asia_open: str = "09:00"
    london_timezone: str = "Europe/London"
    london_open: str = "08:00"
    new_york_timezone: str = "America/New_York"
    new_york_open: str = "08:00"
    new_york_close: str = "17:00"
    # ISO dates use the New York trading calendar. Early-close values are local HH:MM.
    market_holidays: list[str] = Field(default_factory=list)
    market_early_closes: dict[str, str] = Field(default_factory=dict)


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    atr_period: int = Field(default=14, ge=5)
    swing_left: int = Field(default=2, ge=1)
    swing_right: int = Field(default=2, ge=1)
    sweep_reclaim_bars: int = Field(default=3, ge=1)
    equal_level_atr_tolerance: float = Field(default=0.10, gt=0)
    displacement_atr: float = Field(default=1.20, gt=0)
    fvg_min_atr: float = Field(default=0.10, ge=0)
    trigger_expiry_m1_bars: int = Field(default=12, ge=1)
    trigger_grace_m1_bars: int = Field(default=10, ge=0)
    sweep_max_age_bars: int = Field(default=48, ge=1)             # M5 bars a sweep stays usable for confluence / SL
    trendline_min_touches: int = Field(default=3, ge=2)
    trendline_max_slope_atr_per_bar: float = Field(default=0.15, gt=0)
    zone_proximity_atr: float = Field(default=0.20, ge=0)
    min_confluence: int = Field(default=55, ge=0, le=100)
    pattern_lookback_days: int = Field(default=30, ge=1, le=365)
    pattern_scan_retry_seconds: int = Field(default=60, ge=5, le=3600)   # back-off after a failed background scan (v1.9.0)
    min_strategy_sample_trades: int = Field(default=20, ge=0)
    max_trigger_detection_delay_seconds: int = Field(default=15, ge=0, le=60)


class ScoutAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slow_velocity_price_per_min: float = Field(default=0.03, ge=0)
    reset_pace_calibration: bool = False                                  # set true for one start to zero the 20-session counter (v1.9.0)
    fast_velocity_price_per_min: float = Field(default=0.15, gt=0)
    strength_price_step: float = Field(default=1.0, gt=0)
    min_verdict_strength: int = Field(default=5, ge=1, le=10)
    strong_contradiction_strength: int = Field(default=8, ge=1, le=10)
    hold_when_slow: bool = True
    velocity_window_minutes: int = Field(default=20, ge=5, le=120)
    session_open_grace_minutes: int = Field(default=15, ge=0, le=60)
    pace_min_observation_minutes: int = Field(default=5, ge=1, le=30)
    min_calibration_sessions: int = Field(default=20, ge=0)
    hold_before_calibrated: bool = False
    fallback_usd_per_price_per_lot_by_symbol: dict[str, float] = Field(default_factory=lambda: {"XAUUSD": 100.0})

    @model_validator(mode="after")
    def validate_thresholds(self) -> "ScoutAnalysisConfig":
        if self.fast_velocity_price_per_min <= self.slow_velocity_price_per_min:
            raise ValueError("fast scout velocity must exceed slow scout velocity")
        if self.strong_contradiction_strength < self.min_verdict_strength:
            raise ValueError("strong contradiction strength must be >= minimum verdict strength")
        return self


class IntegrationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord_webhook_env: str = "DISCORD_WEBHOOK"
    discord_min_interval_seconds: int = 300
    # v3.1.1 — status pushes: "events" = never push the status block (use !status), "changes" = only when the
    # decision/entry-state/PA side/session changes, "interval" = changes + one status every discord_min_interval_seconds.
    # v3.4.0: `hourly` pushes the decision card once an hour AND immediately on any decision change (master PR #3).
    discord_status_mode: str = Field(default="events", pattern="^(events|changes|interval|hourly|off)$")
    # v3.1.1 — event set: "trade" = lifecycle + risk events only; "all" = every eligible audit event (v3.0 behaviour).
    discord_event_level: str = Field(default="trade", pattern="^(trade|all)$")
    firebase_key_path: str = "serviceAccountKey.json"
    firestore_push_seconds: int = 300
    series_sample_seconds: int = 30
    telemetry_flush_seconds: int = 60
    discord_retry_count: int = Field(default=3, ge=0, le=6)
    discord_retry_backoff_seconds: float = Field(default=1.0, ge=0, le=30)
    firestore_series_max_points: int = Field(default=240, ge=10, le=1000)


class ReportingConfig(BaseModel):
    """v3.0.0: the bot is demo-only. The reference-lot values are NORMALISED RESEARCH VALUES used to compare sessions on a
    common scale; they are not funded-account readiness, permission or risk limits and never enter the GO decision."""
    model_config = ConfigDict(extra="forbid")
    research_reference_lot: float = Field(default=1.0, gt=0)
    research_daily_price_move: float = Field(default=5.0, gt=0)         # $5 XAUUSD move/day, research scale only
    research_daily_usd: float = Field(default=500.0, gt=0)              # ≈ P/L of that move at the reference lot (order_calc_profit)
    research_monthly_usd: float = Field(default=1500.0, gt=0)
    discord_scout_pair_min_interval_seconds: int = Field(default=60, ge=0)


class SessionTargetConfig(BaseModel):
    """v3.0.0 §7: can a $target_price_move XAUUSD move still happen this session? Price movement, not account profit."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    target_price_move: float = Field(default=10.0, gt=0)
    minimum_history_samples: int = Field(default=20, ge=1)
    achievable_probability_threshold: float = Field(default=0.60, gt=0, le=1)
    stretched_probability_threshold: float = Field(default=0.35, gt=0, le=1)
    minimum_remaining_minutes: int = Field(default=30, ge=0)
    block_when_unlikely: bool = True                                    # UNLIKELY (or structural UNLIKELY while history is short) → NO_TRADE


class OutcomesConfig(BaseModel):
    """v3.0.0 §8–10: setup-outcome tracking and pattern reliability."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    favourable_threshold: float = Field(default=5.0, gt=0)              # CONTINUATION when +5 is reached before invalidation
    adverse_threshold: float = Field(default=3.0, gt=0)                 # STOP_HUNT_THEN_CONTINUATION when MAE ≥ 3 first
    max_tracking_minutes: int = Field(default=480, ge=30)
    minimum_samples: int = Field(default=20, ge=1)                      # SUFFICIENT_SAMPLE threshold
    min_level_samples: int = Field(default=5, ge=1)                     # fallback hierarchy: first level with ≥ this many
    scope_to_fingerprint: bool = False                                  # False: all of this account/symbol history is comparable


class IntermarketConfig(BaseModel):
    """v3.1.0: XAU/XAG correlation, relative strength and SMT divergence. Evidence only — never authorises an order."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    symbol: str = "XAGUSD"
    timeframes: list[str] = Field(default_factory=lambda: ["M5", "M15"])
    bars: dict[str, int] = Field(default_factory=lambda: {"M5": 600, "M15": 300})
    correlation_bars: int = Field(default=60, ge=10, le=2000)          # M5 log-return window for Pearson r
    coupled_correlation: float = Field(default=0.50, ge=-1, le=1)
    weak_correlation: float = Field(default=0.20, ge=-1, le=1)
    relative_strength_bars: int = Field(default=12, ge=2, le=500)
    leading_threshold_pct: float = Field(default=0.15, ge=0)            # |XAG% - XAU%| over relative_strength_bars
    smt_timeframe: str = Field(default="M15", pattern="^(M5|M15)$")
    swing_left: int = Field(default=2, ge=1)
    swing_right: int = Field(default=2, ge=1)
    smt_max_age_bars: int = Field(default=24, ge=1)
    smt_weight: int = Field(default=6, ge=0, le=15)
    leading_weight: int = Field(default=3, ge=0, le=10)
    max_weight: int = Field(default=8, ge=0, le=20)                     # cap of the `intermarket` confluence family
    max_silver_age_seconds: int = Field(default=900, ge=60)

    @model_validator(mode="after")
    def thresholds(self) -> "IntermarketConfig":
        if self.weak_correlation > self.coupled_correlation:
            raise ValueError("weak_correlation must be <= coupled_correlation")
        if self.smt_timeframe not in self.timeframes or "M5" not in self.timeframes:
            raise ValueError("intermarket.timeframes must include M5 and smt_timeframe")
        for tf in self.timeframes:
            if self.bars.get(tf, 0) < 50:
                raise ValueError(f"intermarket.bars.{tf} must be >= 50")
        return self


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: int = Field(default=30, ge=1)
    jsonl_rotate_daily: bool = True
    sqlite_path: str = "data/logs/trading.sqlite3"
    jsonl_path: str = "data/logs/analysis.jsonl"


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = "XAUUSD"
    display_timezone: str = "Asia/Kolkata"
    poll_seconds: int = Field(default=3, ge=1)
    history_bars: dict[str, int]
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    management: ManagementConfig = Field(default_factory=ManagementConfig)
    analysis_window_bars: dict[str, int] = Field(default_factory=lambda: {"M1": 600, "M5": 1200, "M15": 800, "H1": 600, "H4": 400, "D1": 400})
    risk: RiskConfig = RiskConfig()
    safety: SafetyConfig = SafetyConfig()
    magic: MagicConfig = MagicConfig()
    sessions: SessionConfig = SessionConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    scout_analysis: ScoutAnalysisConfig = ScoutAnalysisConfig()
    reporting: ReportingConfig = ReportingConfig()
    session_target: SessionTargetConfig = SessionTargetConfig()
    outcomes: OutcomesConfig = OutcomesConfig()
    intermarket: IntermarketConfig = IntermarketConfig()
    logging: LoggingConfig = LoggingConfig()
    project_dir: str = Field(default=".", exclude=True)

    @model_validator(mode="after")
    def effective_pattern_window(self) -> "BotConfig":
        # Calendar-day capacity: 30 days of M5 bars = 8,640. Closed weekends merely add spare capacity.
        required_m5 = self.analysis.pattern_lookback_days * 24 * 60 // 5
        if self.history_bars.get("M5", 0) < required_m5:
            raise ValueError(
                f"history_bars.M5 must be >= {required_m5} for pattern_lookback_days={self.analysis.pattern_lookback_days}"
            )
        return self


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    cfg = BotConfig.model_validate(raw)
    base = config_path.parent
    cfg.project_dir = str(base)
    cfg.logging.sqlite_path = str((base / cfg.logging.sqlite_path).resolve())
    cfg.logging.jsonl_path = str((base / cfg.logging.jsonl_path).resolve())
    cfg.integrations.firebase_key_path = str((base / cfg.integrations.firebase_key_path).resolve())
    return cfg


def load_environment(path: str | Path = ".env", *, override: bool = True) -> dict[str, str]:
    """Load simple KEY=VALUE entries. The project .env is authoritative by default.

    Returning loaded values makes precedence testable and prevents a stale inherited Windows
    environment from silently selecting another login or webhook.
    """
    env_path = Path(path)
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"").strip("'")
        if key:
            if override or key not in os.environ:
                os.environ[key] = value
            loaded[key] = value
    return loaded


def mt5_terminal_path() -> str | None:
    """Optional path to terminal64.exe. The terminal must already be logged in to the demo account;
    the bot never logs in and never reads MT5 credentials (v3.0.0)."""
    value = os.getenv("MT5_TERMINAL_PATH", "").strip()
    return value or None
