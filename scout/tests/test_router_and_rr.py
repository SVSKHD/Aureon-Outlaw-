from datetime import UTC, datetime

from xau_mt5_bot.decision_router import final_decision_router
from xau_mt5_bot.models import (
    Action,
    DecisionInput,
    EntryState,
    Freshness,
    LiquidityLevel,
    ScoutSnapshot,
    SessionName,
    Side,
    SpreadState,
    StructureResult,
    StructureState,
    TargetRealism,
    TriggerResult,
    Zone,
)
from xau_mt5_bot.zones import build_trade_plan


def valid_input(**changes):
    base = dict(
        pa_side=Side.LONG, setup_valid=True, entry_state=EntryState.CONFIRMED,
        trigger=TriggerResult(True, "M1", fresh=True),
        scout=ScoutSnapshot(SessionName.LONDON), freshness=Freshness.LIVE,
        spread_state=SpreadState.NORMAL, account_safe=True, rr=1.5, min_rr=1.0,
        target_realism=TargetRealism.REALISTIC, confluence=70, min_confluence=55,
    )
    base.update(changes)
    return DecisionInput(**base)


def test_stale_data_is_hard_no_trade():
    assert final_decision_router(valid_input(freshness=Freshness.STALE)).action == Action.NO_TRADE


def test_spread_rejection_is_hard_no_trade():
    assert final_decision_router(valid_input(spread_state=SpreadState.ABNORMAL)).action == Action.NO_TRADE


def test_actual_rr_uses_supplied_ask_for_long():
    now = datetime(2026, 7, 15, tzinfo=UTC)
    zone = Zone(99.0, 100.0, "SUPPORT", Side.LONG, now, now)
    structure = StructureResult("M5", StructureState.BULLISH)
    target = LiquidityLevel(103.0, "PDH", "D1", now, 4)
    plan = build_trade_plan(Side.LONG, 100.20, zone, structure, [], [target], 1.0, 10.0, 2.0, 0.01)
    assert plan is not None
    # Stop is 98.90, so risk is 1.30 and reward from actual ask is 2.80.
    assert round(plan.actual_rr[0], 4) == round(2.8 / 1.3, 4)

