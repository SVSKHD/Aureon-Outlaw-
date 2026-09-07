from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conftest import FakeClient
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.execution import risk_based_volume
from xau_mt5_bot.liquidity import round_number_levels
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.sessions import SessionEngine


def _cfg(config):
    config.safety.allow_scout_orders = True
    return config


def test_restart_adopts_current_session_pair_and_closes_stale_pair(config):
    cfg = _cfg(config); client = FakeClient()
    client.send_market("XAUUSD", "BUY", 0.01, cfg.magic.scout_new_york, "x"); client.send_market("XAUUSD", "SELL", 0.01, cfg.magic.scout_new_york, "x")
    client.send_market("XAUUSD", "BUY", 0.01, cfg.magic.scout_london, "old"); client.send_market("XAUUSD", "SELL", 0.01, cfg.magic.scout_london, "old")
    scouts = ScoutManager(client, cfg)
    scouts.adopt_existing(SessionName.NEW_YORK, datetime(2026, 9, 4, 15, tzinfo=UTC))
    assert scouts.current_session == SessionName.NEW_YORK
    assert len(client.positions("XAUUSD", cfg.magic.scout_new_york)) == 2
    assert client.positions("XAUUSD", cfg.magic.scout_london) == []


def test_missing_leg_is_repaired_early_and_closed_late(config):
    cfg = _cfg(config); client = FakeClient(); scouts = ScoutManager(client, cfg)
    now = datetime(2026, 9, 4, 13, tzinfo=UTC)
    scouts.open_session(SessionName.NEW_YORK, now)
    sell = [p for p in client.positions() if p.type == 1][0]; client._positions.remove(sell)
    assert scouts.repair_leg(now + timedelta(minutes=30)).success and len(client.positions()) == 2
    sell = [p for p in client.positions() if p.type == 1][0]; client._positions.remove(sell)
    assert "orphan" in scouts.repair_leg(now + timedelta(hours=7)).message.lower() and client.positions() == []


def test_round_numbers_are_unique_per_price():
    levels = round_number_levels(3421.7, datetime.now(UTC))
    assert len({level.price for level in levels}) == len(levels)
    assert any(level.kind == "ROUND_100" and level.price == 3400 for level in levels)


def test_risk_based_volume_uses_percent(config):
    client = FakeClient()
    client.symbol_info = lambda s: SimpleNamespace(volume_min=0.01, volume_max=100, volume_step=0.01, trade_contract_size=100, trade_tick_size=0.01, trade_tick_value=1.0)
    volume, _ = risk_based_volume(client, config, 5.0)     # 0.25% of 50000 = 125 over $5 → 0.25 lots
    assert volume == 0.25


def test_weekend_is_closed():
    assert not SessionEngine.market_open(datetime(2026, 9, 5, 12, tzinfo=UTC))
    assert SessionEngine.market_open(datetime(2026, 9, 6, 22, 30, tzinfo=UTC))
    assert SessionEngine(__import__("xau_mt5_bot.config", fromlist=["SessionConfig"]).SessionConfig()).session_at(datetime(2026, 9, 5, 3, tzinfo=UTC)) == SessionName.CLOSED
