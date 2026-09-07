from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from xau_mt5_bot.config import IntermarketConfig
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.intermarket import UNAVAILABLE, assess_intermarket, intermarket_patterns, rolling_correlation, smt_divergence
from xau_mt5_bot.models import Side, StructureResult, StructureState


def _frame(closes: list[float], start: datetime, step_minutes: int, wick: float = 0.2) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append({"time": start + timedelta(minutes=step_minutes * i), "open": o, "high": c + wick,
                     "low": c - wick, "close": c, "tick_volume": 100})
    return pd.DataFrame(rows)


START = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


def test_rolling_correlation_high_for_scaled_series():
    rng = np.random.default_rng(1)
    gold = 3500 + np.cumsum(rng.normal(0, 1.0, 200))
    silver = 40 + (gold - 3500) * 0.01 + rng.normal(0, 0.005, 200)
    r = rolling_correlation(_frame(list(gold), START, 5), _frame(list(silver), START, 5), 60)
    assert r is not None and r > 0.8


def test_rolling_correlation_none_when_not_enough_overlap():
    gold = _frame([3500 + i for i in range(20)], START, 5)
    silver = _frame([40 + i for i in range(20)], START + timedelta(hours=5), 5)
    assert rolling_correlation(gold, silver, 60) is None


def _pivot_series(highs: list[float], base: float, spacing: int = 5) -> list[float]:
    """Build closes that put clear swing highs at the given values with the base between them."""
    closes = []
    for h in highs:
        closes += [base, base + (h - base) * 0.5, h, base + (h - base) * 0.5, base]
    return closes


def test_bearish_smt_when_gold_higher_high_silver_lower_high():
    gold = _frame(_pivot_series([3510, 3515], 3500), START, 15, wick=0.0)
    silver = _frame(_pivot_series([41.0, 40.6], 40.0), START, 15, wick=0.0)
    out = smt_divergence(gold, silver, 2, 2, 24)
    assert out["smt"] == "BEARISH" and out["detail"]["kind"] == "HIGH"
    assert out["detail"]["xau"] == [3510.0, 3515.0] and out["detail"]["xag"] == [41.0, 40.6]


def test_bullish_smt_when_gold_lower_low_silver_higher_low():
    gold = _frame(_pivot_series([3490, 3485], 3500), START, 15, wick=0.0)
    silver = _frame(_pivot_series([39.0, 39.4], 40.0), START, 15, wick=0.0)
    out = smt_divergence(gold, silver, 2, 2, 24)
    assert out["smt"] == "BULLISH" and out["detail"]["kind"] == "LOW"


def test_no_smt_when_both_make_higher_highs():
    gold = _frame(_pivot_series([3510, 3515], 3500), START, 15, wick=0.0)
    silver = _frame(_pivot_series([41.0, 41.5], 40.0), START, 15, wick=0.0)
    assert smt_divergence(gold, silver, 2, 2, 24)["smt"] == "NONE"


def test_smt_ignored_when_pivot_too_old():
    gold = _frame(_pivot_series([3510, 3515], 3500) + [3500] * 40, START, 15, wick=0.0)
    silver = _frame(_pivot_series([41.0, 40.6], 40.0) + [40.0] * 40, START, 15, wick=0.0)
    assert smt_divergence(gold, silver, 2, 2, 24)["smt"] == "NONE"


def test_assess_points_only_when_coupled():
    cfg = IntermarketConfig()
    rng = np.random.default_rng(3)
    steps = rng.normal(0, 1.0, 120)
    gold_m5 = _frame(list(3500 + np.cumsum(steps)), START, 5)
    silver_m5 = _frame(list(40 + np.cumsum(steps) * 0.01), START, 5)
    gold_m15 = _frame(_pivot_series([3490, 3485], 3500), START, 15, wick=0.0)
    silver_m15 = _frame(_pivot_series([39.0, 39.4], 40.0), START, 15, wick=0.0)
    info = assess_intermarket({"M5": gold_m5, "M15": gold_m15}, {"M5": silver_m5, "M15": silver_m15}, cfg, now=START + timedelta(minutes=600))
    assert info["status"] == "OK" and info["regime"] == "COUPLED" and info["smt"] == "BULLISH"
    assert info["long_points"] == cfg.smt_weight and info["short_points"] == 0
    # decoupled silver removes the points but keeps the evidence
    silver_dec = _frame(list(40 + np.cumsum(rng.normal(0, 0.01, 120))), START, 5)
    info2 = assess_intermarket({"M5": gold_m5, "M15": gold_m15}, {"M5": silver_dec, "M15": silver_m15}, cfg, now=START + timedelta(minutes=600))
    assert info2["regime"] in {"WEAK", "DECOUPLED"} and info2["long_points"] == 0 and info2["smt"] == "BULLISH"


def test_assess_unavailable_when_silver_stale_or_missing():
    cfg = IntermarketConfig()
    gold = _frame([3500 + i * 0.1 for i in range(100)], START, 5)
    silver = _frame([40 + i * 0.001 for i in range(100)], START, 5)
    stale = assess_intermarket({"M5": gold}, {"M5": silver}, cfg, now=START + timedelta(days=1))
    assert stale["status"] == "UNAVAILABLE" and "old" in stale["reason"]
    assert assess_intermarket({"M5": gold}, None, cfg)["status"] == "UNAVAILABLE"
    cfg_off = IntermarketConfig(enabled=False)
    assert assess_intermarket({"M5": gold}, {"M5": silver}, cfg_off) == UNAVAILABLE


def test_patterns_named_from_info():
    names = [p["name"] for p in intermarket_patterns({"smt": "BEARISH", "regime": "DECOUPLED"}, START)]
    assert names == ["Bearish SMT XAU/XAG", "XAU/XAG decoupled"]
    assert intermarket_patterns({"smt": "NONE", "regime": "COUPLED"}, START) == []


def test_scoring_adds_capped_intermarket_family_only_when_ok():
    structures = {tf: StructureResult(tf, StructureState.NEUTRAL) for tf in ("D1", "H4", "H1", "M15", "M5")}
    base_side, base_score = TradingEngine._price_action_direction(structures, [], [], [])
    assert base_side is None
    side, score = TradingEngine._price_action_direction(structures, [], [], [], intermarket={"status": "OK", "long_points": 20, "short_points": 0})
    assert side == Side.LONG and score == 8                            # capped at the family cap
    side2, score2 = TradingEngine._price_action_direction(structures, [], [], [], intermarket={"status": "UNAVAILABLE", "long_points": 8, "short_points": 0})
    assert side2 is None and score2 == base_score


def test_fingerprint_changes_with_intermarket_settings(tmp_path):
    from xau_mt5_bot.config import load_config
    from xau_mt5_bot.fingerprint import strategy_fingerprint
    import yaml
    from pathlib import Path
    raw = yaml.safe_load(Path("config.yaml").read_text())
    a = tmp_path / "a.yaml"; a.write_text(yaml.safe_dump(raw))
    raw["intermarket"]["smt_weight"] = 2
    b = tmp_path / "b.yaml"; b.write_text(yaml.safe_dump(raw))
    assert strategy_fingerprint(load_config(a)) != strategy_fingerprint(load_config(b))
