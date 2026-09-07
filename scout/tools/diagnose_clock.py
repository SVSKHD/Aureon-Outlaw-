"""Read-only clock evidence: run with the same Python/Windows user as the bot."""
from datetime import UTC, datetime
import json
import time

import MetaTrader5 as mt5


def main():
    if not mt5.initialize():
        raise SystemExit(f'MT5 attach failed: {mt5.last_error()}')
    try:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--symbol', default='XAUUSD')
        args = parser.parse_args()
        if not mt5.symbol_select(args.symbol, True):
            raise SystemExit(f'Symbol unavailable: {args.symbol}')
        samples = []
        for _ in range(3):
            tick = mt5.symbol_info_tick(args.symbol)
            now = datetime.now(UTC)
            if tick is None:
                raise SystemExit(f'No tick: {mt5.last_error()}')
            samples.append(dict(system_utc=now.isoformat(), raw_tick_utc=datetime.fromtimestamp(tick.time, UTC).isoformat(),
                                tick_minus_system_seconds=round(tick.time-now.timestamp(), 2)))
            time.sleep(1)
        rates = mt5.copy_rates_from_pos(args.symbol, mt5.TIMEFRAME_M1, 0, 2)
        print(json.dumps(dict(samples=samples, raw_m1_open_times=[] if rates is None else
              [datetime.fromtimestamp(int(r['time']), UTC).isoformat() for r in rates]), indent=2))
        deltas = [s['tick_minus_system_seconds'] for s in samples]
        detected = round(sum(deltas) / len(deltas) / 1800) * 0.5
        print(f'Detected broker offset would be UTC{detected:+g}h (nearest 30 min over {len(deltas)} samples); '
              f'residual {sum(deltas) / len(deltas) - detected * 3600:+.1f}s.')
        print('The bot detects this itself at startup (safety.broker_utc_offset_hours: null). Verify Windows UTC '
              'independently: a residual of more than a few seconds is the PC clock, not the broker timezone.')
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
