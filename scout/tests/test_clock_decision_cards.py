from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import xau_mt5_bot.mt5_client as adapter
from test_mt5_adapter_contract import MockMT5
from xau_mt5_bot.cards import status_card, fakeout_text, scout_card
from xau_mt5_bot.notify import Discord


def client(monkeypatch, offset=0, age=-10800):
    fake = MockMT5()
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=.01, volume_step=.01, volume_max=100)
    raw = int(datetime.now(UTC).timestamp()) - age
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(time=raw, bid=2500, ask=2500.2)
    fake.copy_rates_from_pos = lambda *args: [dict(time=raw, open=2500, high=2501, low=2499, close=2500)]
    monkeypatch.setattr(adapter, 'mt5', fake)
    return adapter.MT5Client(broker_timestamp_offset_seconds=offset)


def test_off_grid_future_tick_is_rejected_not_guessed_as_a_timezone(monkeypatch):
    """v3.3.0 changes this case deliberately.

    A difference that sits on the half-hour grid every MT5 server timezone lives on IS detected
    (that is the whole point of the auto-detection: a UTC+3 server must not block the bot for ever).
    A difference that does not — a broken PC clock — is still refused rather than absorbed into an
    invented offset, which is what this test was written to protect.
    """
    with pytest.raises(RuntimeError, match='future'):
        client(monkeypatch, age=-1500).validate_terminal()          # 25 minutes ahead: not a timezone

    detected = client(monkeypatch, age=-10800)                       # exactly 3 hours: a real UTC+3 server
    validation = detected.validate_terminal()
    assert validation['broker_utc_offset_hours'] == 3.0
    assert validation['broker_clock_source'] == 'auto'
    assert abs(validation['tick_age_seconds']) < 2


def test_verified_offset_normalizes_ticks_bars_and_startup(monkeypatch):
    c = client(monkeypatch, offset=10800)
    assert abs(c.validate_terminal()['tick_age_seconds']) < 2
    assert abs((c.get_tick('XAUUSD').time - datetime.now(UTC)).total_seconds()) < 2
    assert c.get_bars('XAUUSD', 'M1', 1).iloc[0].time == c.get_tick('XAUUSD').time


def test_offset_does_not_make_old_feed_fresh(monkeypatch):
    with pytest.raises(RuntimeError, match='stale'):
        client(monkeypatch, offset=10800, age=-7200).validate_terminal()


def test_standard_utc_remains_unchanged(monkeypatch):
    c = client(monkeypatch, age=0)
    assert abs(c.validate_terminal()['tick_age_seconds']) < 2


def snap():
    return dict(timestamp='2026-09-07T10:00:00+00:00', session='LONDON', go_status='NO-GO',
                decision=dict(action='WAIT', reason='waiting for trigger'), entry_state='WAITING', pa_side='LONG',
                trade_plan=dict(side='LONG', entry=2500, stop_loss=2495, take_profits=[2510, 2515], actual_rr=[2, 3]),
                zones=[dict(side='LONG', low=2498, high=2501, kind='FVG')], scout={})


def test_watchlist_and_missing_scouts_are_explicit():
    fields = {f['name']: f['value'] for f in status_card(snap(), 'Asia/Kolkata')['fields']}
    assert 'WATCHLIST ONLY' in fields['Manual entry / SL / targets']
    assert 'TP1 2510.00' in fields['Manual entry / SL / targets']
    assert 'TP2 2515.00' in fields['Manual entry / SL / targets']
    assert 'NO CONFIRMATION' in fields['Scouts']
    assert 'Conditional scenario' in fields['Next pattern to watch']
    assert 'UNAVAILABLE' in fields['Fakeout assessment']


def test_fakeout_sample_and_interval():
    s = snap()
    s['analysis'] = dict(historical_pattern_reliability=dict(status='SUFFICIENT_SAMPLE', samples=40,
        fakeout_rate=.25, comparison_level='PATTERN_SESSION', confidence_interval_fakeout=[.14, .40]))
    assert '25.0/100' in fakeout_text(s) and 'n=40' in fakeout_text(s) and '14.0–40.0%' in fakeout_text(s)
    s['analysis']['historical_pattern_reliability']['status'] = 'INSUFFICIENT_SAMPLE'
    assert 'UNAVAILABLE' in fakeout_text(s)


def ns(s):
    # snapshot_dict is separately exercised by card tests; expose only the notification attributes here.
    return SimpleNamespace(decision=SimpleNamespace(action=SimpleNamespace(value=s['decision']['action'])),
        entry_state=SimpleNamespace(value=s['entry_state']), pa_side=s['pa_side'],
        session=SimpleNamespace(value=s['session']), go_status=s['go_status'])


def test_hourly_delivery_and_failed_send_retry(monkeypatch):
    import xau_mt5_bot.notify as notify
    s=snap(); monkeypatch.setattr(notify, 'snapshot_dict', lambda _: s)
    d=Discord('UNSET', status_mode='hourly'); sent=[]
    d.send=lambda **kw: sent.append(kw) or True
    d.on_snapshot(ns(s), 'Asia/Kolkata'); d.on_snapshot(ns(s), 'Asia/Kolkata')
    assert len(sent)==1
    s['timestamp']='2026-09-07T10:30:00+00:00'  # new IST hour, despite same UTC hour
    d.send=lambda **kw: False
    d.on_snapshot(ns(s), 'Asia/Kolkata')
    d.send=lambda **kw: sent.append(kw) or True
    d.on_snapshot(ns(s), 'Asia/Kolkata'); d.on_snapshot(ns(s), 'Asia/Kolkata')
    assert len(sent)==2
    s['decision']['action']='LONG'; s['go_status']='GO'
    d.on_snapshot(ns(s), 'Asia/Kolkata')
    assert len(sent)==3


def test_off_sends_nothing(monkeypatch):
    d=Discord('UNSET', status_mode='off')
    d.send=lambda **kw: pytest.fail('off must not send')
    d.on_snapshot(ns(snap()), 'UTC')


def test_clock_failure_has_actionable_hint():
    c=scout_card('scout_open_failed', dict(message='broker clock skew 10799s > 600s'))
    assert 'Windows' in c['fields'][1]['value']
    assert 'backoff' in c['fields'][2]['value']


def test_scout_pair_recovers_when_clock_is_corrected(config, tmp_path):
    from conftest import FakeClient
    from xau_mt5_bot.engine import TradingEngine
    from xau_mt5_bot.logger import AuditLogger
    from xau_mt5_bot.models import SessionName
    config.project_dir=str(tmp_path)
    c=FakeClient(); now=c.tick.time
    log=AuditLogger(str(tmp_path/'test.sqlite3'), str(tmp_path/'test.jsonl'))
    engine=TradingEngine(c, config, log)
    engine.cycle_now=now
    c.tick.time=now+timedelta(hours=3)
    engine._validate_clock(now)
    result=engine.scouts.open_session(SessionName.LONDON, now)
    assert not result.success and result.retryable and not c.sent
    c.tick.time=now
    engine._validate_clock(now)
    assert engine.scouts.open_session(SessionName.LONDON, now).success
    assert len(c.positions(config.symbol, config.magic.scout_london))==2
    assert not engine.scouts.open_session(SessionName.LONDON, now).success
    assert len(c.sent)==2


def test_manual_ready_requires_confirming_pair():
    s=snap(); s['go_status']='GO'; s['decision']['action']='LONG'
    assert 'WAIT / NO MANUAL ENTRY' in status_card(s, 'UTC')['title']
    s['scout']=dict(buy_ticket=1, sell_ticket=2, verdict='CONFIRMS')
    assert 'BUY READY' in status_card(s, 'UTC')['title']
    s['scout']['verdict']='CONTRADICTS'
    assert 'WAIT / NO MANUAL ENTRY' in status_card(s, 'UTC')['title']


def test_offset_normalizes_position_and_deal_times_without_mutating_broker(monkeypatch):
    c=client(monkeypatch, offset=10800)
    fake=adapter.mt5
    fake.position.time=10801
    fake.position.time_msc=10801000
    assert c.positions()[0].time==1 and c.positions()[0].time_msc==1000
    assert fake.position.time==10801
    fake.history_deals_get=lambda *args, **kwargs: [SimpleNamespace(entry=1, ticket=5, price=2500,
        profit=1, commission=0, swap=0, time=10801, reason=4, volume=.01)]
    assert c.closed_deals(77)[0]['time']==datetime.fromtimestamp(1, UTC)
