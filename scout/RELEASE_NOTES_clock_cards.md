# Scout clock diagnostics and hourly manual decision cards

The screenshot's +10,799 second tick/system difference blocks scout orders. It does not establish whether the Windows clock is incorrect or a feed supplies non-standard timestamps. Standard MT5 tick/bar epochs are UTC: https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py

## Changes

- Startup now rejects future ticks (previously it accepted any negative tick age). Runtime rejects ticks more than five seconds ahead, including a fresh recheck before orders.
- Explicit `safety.broker_timestamp_offset_seconds` defaults to **0**. Only for a independently verified non-standard feed, this value is subtracted from ticks, bars, position times and closed-deal times. For confirmed UTC+3-encoded epochs use 10800; never set it just because the broker chart displays UTC+3. Review it on broker/DST changes. Restart after changing it so cached history is reloaded. Existing saved analyses remain historical and may have incorrect timestamps from the old configuration; do not mix them into reliability assessment without review.
- Clock-block cards give specific diagnostic guidance and describe the actual bounded retry backoff. A corrected clock allows the existing scout transition retry mechanism to recover; demo/hedging and freshness requirements remain enforced.
- `discord_status_mode: hourly` sends a current decision on startup, the first successful snapshot in each display-timezone hour, and decision changes. Failed deliveries remain eligible for retry. `!status` uses the same card. `events`, `changes`, `interval`, and `off` remain selectable.
- Manual BUY/SELL READY additionally requires a complete scout pair with CONFIRMS verdict. The underlying router signal is shown separately; scout confirmation changes trigger a card update.
- Cards show entry, structural SL reason, TP1/TP2/TP3 with RR when available, scout-pair completeness, conditional next-pattern scenario, and historical fakeout score with sample count/comparison group/95% interval. Insufficient history shows UNAVAILABLE. Scores are historical frequencies, not calibrated predictions of the next trade.
- Supplied configs enable demo scouts and disable automatic PA orders for manual entry. Manual mode avoids generating repeated disabled-order events. Existing user configs must be updated explicitly.
- Root installation now points to the actual `scout/src` package; both root and `scout/` installations work.

## Apply on Windows

From the repository root, install with `py -m pip install -e ".[mt5,dev]"`. Use the config beside the launcher you run. Preserve your environment and existing logs.

First synchronize Windows Date & time, then run:

```powershell
py scout/tools/diagnose_clock.py --symbol XAUUSD
```

This read-only utility attaches with `mt5.initialize()` and prints three raw tick/system comparisons plus recent M1 open times. It never logs in or places orders. If the discrepancy persists after independently checking Windows UTC, verify the broker's timestamp convention before changing the offset. Never widen freshness limits to hide the discrepancy.

Set these in the active config:

```yaml
safety:
  allow_scout_orders: true
  allow_pa_orders: false
  broker_timestamp_offset_seconds: 0
integrations:
  discord_status_mode: hourly
```

These are individual fields to merge into the existing sections, not a replacement config. Restart the bot. Expect two demo scout tickets or a specific blocker; run `!status` for the decision card. Ready-at-snapshot is not a standing order instruction: recheck current executable price, validity and risk before manual entry. This release does not connect to cTrader or manage manually opened positions automatically.

## Validation limits

Tests use fake broker responses; actual broker order acceptance and Discord delivery require a Windows demo run. No orders or Discord messages were sent during development.
