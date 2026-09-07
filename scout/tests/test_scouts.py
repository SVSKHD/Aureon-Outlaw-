from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import FakeClient
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.sessions import SessionBoundary


def test_scouts_disabled_on_netting_account(config):
    manager = ScoutManager(FakeClient(hedging=False), config)
    result = manager.open_session(SessionName.ASIA, datetime.now(UTC))
    assert not result.success
    assert "NETTING" in result.message


def test_session_transition_closes_old_pair_before_new_pair(config):
    client = FakeClient()
    manager = ScoutManager(client, config)
    assert manager.open_session(SessionName.ASIA, datetime.now(UTC)).success
    asia_tickets = {p.ticket for p in client.positions("XAUUSD", config.magic.scout_asia)}
    result = manager.handle_boundary(SessionBoundary(datetime.now(UTC), "START", SessionName.LONDON))
    assert result.success
    assert client.positions("XAUUSD", config.magic.scout_asia) == []
    assert len(client.positions("XAUUSD", config.magic.scout_london)) == 2
    assert {ticket for ticket, _ in client.closed} == asia_tickets


def test_closing_scouts_never_closes_pa_position(config):
    client = FakeClient()
    manager = ScoutManager(client, config)
    manager.open_session(SessionName.ASIA, datetime.now(UTC))
    pa = SimpleNamespace(
        ticket=999, symbol="XAUUSD", magic=config.magic.pa, volume=0.01,
        type=0, price_open=2500.0, profit=0.0,
    )
    client._positions.append(pa)
    assert manager.close_session(SessionName.ASIA).success
    assert [p.ticket for p in client.positions("XAUUSD", config.magic.pa)] == [999]


def test_all_magics_are_separate(config):
    values = {config.magic.scout_asia, config.magic.scout_london, config.magic.scout_new_york, config.magic.pa}
    assert len(values) == 4

