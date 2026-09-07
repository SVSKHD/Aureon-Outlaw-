from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import xau_mt5_bot.mt5_client as adapter


class MockMT5:
    TIMEFRAME_M1 = 1; TIMEFRAME_M5 = 5; TIMEFRAME_M15 = 15; TIMEFRAME_H1 = 60; TIMEFRAME_H4 = 240; TIMEFRAME_D1 = 1440
    ACCOUNT_TRADE_MODE_DEMO = 0; ACCOUNT_TRADE_MODE_REAL = 2; ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ORDER_TYPE_BUY = 0; ORDER_TYPE_SELL = 1; POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1; TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0; ORDER_FILLING_FOK = 0; ORDER_FILLING_IOC = 1; ORDER_FILLING_RETURN = 2   # real MT5 enum values
    SYMBOL_FILLING_FOK = 1; SYMBOL_FILLING_IOC = 2                                                  # real MT5 capability flags
    TRADE_RETCODE_DONE = 10009; TRADE_RETCODE_PLACED = 10008; TRADE_RETCODE_DONE_PARTIAL = 10010
    SYMBOL_TRADE_MODE_DISABLED = 0
    DEAL_ENTRY_OUT = 1; DEAL_REASON_SL = 4

    def __init__(self):
        self.trade_mode = self.ACCOUNT_TRADE_MODE_DEMO; self.retcode = self.TRADE_RETCODE_DONE
        self.position = SimpleNamespace(ticket=77, symbol="XAUUSD", magic=12001, volume=.10, type=0, sl=2490.0, tp=0.0)
        self.last_request = None
        self.history_window = None

    def account_info(self):
        return SimpleNamespace(login=1, server="Demo", balance=50000, equity=50000, margin_free=49000,
                               trade_allowed=True, trade_mode=self.trade_mode, margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
    def symbol_info(self, symbol):
        return SimpleNamespace(visible=True, trade_mode=4, point=.01, trade_stops_level=0, trade_freeze_level=0,
                               filling_mode=self.SYMBOL_FILLING_IOC)
    def symbol_info_tick(self, symbol): return SimpleNamespace(time=1788532800, bid=2500.0, ask=2500.2)
    def symbol_select(self, symbol, selected): return True
    def positions_get(self, symbol=None, ticket=None):
        if ticket is not None: return (self.position,) if self.position and self.position.ticket == ticket else ()
        return (self.position,) if self.position and (symbol is None or symbol == self.position.symbol) else ()
    def order_send(self, request):
        self.last_request = request
        if self.retcode not in {self.TRADE_RETCODE_DONE, self.TRADE_RETCODE_PLACED, self.TRADE_RETCODE_DONE_PARTIAL}:
            return SimpleNamespace(retcode=self.retcode, order=0, deal=0, price=0, comment="rejected")
        if request["action"] == self.TRADE_ACTION_SLTP:
            self.position.sl, self.position.tp = request["sl"], request["tp"]
        elif request.get("position") and self.position:
            self.position.volume = round(self.position.volume - request["volume"], 8)
            if self.position.volume <= 0: self.position = None
        return SimpleNamespace(retcode=self.retcode, order=77, deal=88, price=request.get("price", 0), comment="done")
    def order_check(self, request): return SimpleNamespace(retcode=0, margin_free=48000, comment="ok")
    def order_calc_profit(self, order_type, symbol, volume, opened, closed):
        return (closed - opened) * (1 if order_type == self.ORDER_TYPE_BUY else -1) * volume * 100
    def last_error(self): return (0, "ok")
    def history_deals_get(self, start, end, position=None):
        self.history_window = (start, end, position)
        return (SimpleNamespace(entry=self.DEAL_ENTRY_OUT, ticket=9, price=2480, profit=-20, commission=0, swap=0,
                                time=int(end.timestamp()) - 1, reason=self.DEAL_REASON_SL, volume=.1),)


def _client(monkeypatch):
    fake = MockMT5(); monkeypatch.setattr(adapter, "mt5", fake)
    return fake, adapter.MT5Client()


def test_account_mode_contract_detects_demo_hedging_and_live(monkeypatch):
    fake, client = _client(monkeypatch)
    state = client.account_state()
    assert state.is_demo and state.is_hedging
    fake.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL
    assert not client.account_state().is_demo


def test_order_send_retcode_and_executable_price_contract(monkeypatch):
    fake, client = _client(monkeypatch)
    result = client.send_market("XAUUSD", "LONG", .1, 12001, "PA", 2490, 2510)
    assert result.success and fake.last_request["price"] == 2500.2 and fake.last_request["type"] == fake.ORDER_TYPE_BUY
    fake.retcode = 10006
    assert not client.send_market("XAUUSD", "SHORT", .1, 12001, "PA").success


def test_partial_close_contract_uses_exact_ticket_and_bid(monkeypatch):
    fake, client = _client(monkeypatch)
    result = client.close_partial(77, .04, 12001, "TP1")
    assert result.success and fake.last_request["position"] == 77 and fake.last_request["price"] == 2500.0
    assert fake.position.volume == .06
    assert not client.close_partial(77, .01, 99999).success


def test_modify_sltp_contract_and_magic_guard(monkeypatch):
    fake, client = _client(monkeypatch)
    result = client.modify_sltp(77, 2499.0, 2510.0, 12001)
    assert result.success and fake.position.sl == 2499.0 and fake.position.tp == 2510.0
    assert not client.modify_sltp(77, 2498.0, 0.0, 99999).success


def test_deal_history_window_starts_from_position_open(monkeypatch):
    fake, client = _client(monkeypatch)
    opened = datetime.now(UTC) - timedelta(days=200)
    assert client.closed_deals(77, opened)
    assert fake.history_window[0] == opened - timedelta(days=1)
